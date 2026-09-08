"""A collection run is fulfilled by evidence, never by a handler's opinion.

The predicate tests below are pure; the rest need the real database because
the point of half of them is that the *schema* refuses what the service
refuses, so a second write path added later cannot get around it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job
from nemsei.jobs.ownership import OwnershipFence, OwnershipLost
from nemsei.providers.service import create_connection
from nemsei.shared.clock import utc_now
from nemsei.sync.collection_models import (
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_LOST_OWNERSHIP,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    STATUS_SUPERSEDED,
    CollectionRun,
)
from nemsei.sync.collection_service import (
    CollectionEvidence,
    CollectionRunStateError,
    can_fulfill_collection_run,
    finalize_collection_run,
    mark_collection_run_lost_ownership,
    start_collection_run,
)
from nemsei.sync.scope_lock import SCOPE_KIND_CONNECTION
from tests_v2.test_migrations import upgrade


PERIOD_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 2, tzinfo=timezone.utc)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _connection(session, *, key="conta"):
    return create_connection(
        session, provider_code="sigenergy", connection_key=key, display_name=f"Conta {key}",
        credential_reference="primary", enabled=True, configuration_status="configured",
    )


def _running_job(session, *, generation=10, expires_in_seconds=300):
    now = utc_now()
    job = Job(
        job_type="production.incremental",
        status="running",
        payload_json={},
        priority=100,
        available_at=now,
        attempt_count=1,
        max_attempts=3,
        lease_owner="worker-a",
        lease_token="token-a",
        lease_generation=generation,
        claimed_at=now,
        lease_expires_at=now + timedelta(seconds=expires_in_seconds),
        created_at=now,
        updated_at=now,
        started_at=now,
    )
    session.add(job)
    session.flush()
    return job, OwnershipFence(job_id=job.id, lease_token="token-a", lease_generation=generation)


def _start(session, connection, job, generation=10):
    return start_collection_run(
        session,
        provider_connection_id=connection.id,
        capability="production_history",
        scope_kind=SCOPE_KIND_CONNECTION,
        scope_key=str(connection.id),
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        job_id=job.id,
        lease_generation=generation,
    )


# ---------------------------------------------------------------------------
# O predicado, sem base de dados.
# ---------------------------------------------------------------------------


def test_complete_evidence_fulfills():
    evidence = CollectionEvidence(facts_written=4, scopes_required=2, scopes_written=2, cursor_advanced=True)
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is True


def test_an_unknown_required_count_never_fulfills():
    """`None` is not zero. An unknown denominator is not a full one."""
    evidence = CollectionEvidence(facts_written=4, scopes_required=None, scopes_written=2, cursor_advanced=True)
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is False


def test_a_missing_scope_never_fulfills():
    evidence = CollectionEvidence(facts_written=2, scopes_required=2, scopes_written=1, cursor_advanced=True)
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is False


def test_an_open_period_never_fulfills():
    """Closed-day semantics outrank the counters."""
    evidence = CollectionEvidence(
        facts_written=2, scopes_required=2, scopes_written=2, cursor_advanced=True, period_closed=False
    )
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is False


def test_a_recorded_error_never_fulfills():
    evidence = CollectionEvidence(
        facts_written=2, scopes_required=2, scopes_written=2, cursor_advanced=True, error_code="provider_refused"
    )
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is False


def test_a_cursor_capability_needs_its_cursor():
    evidence = CollectionEvidence(facts_written=2, scopes_required=2, scopes_written=2, cursor_advanced=False)
    assert can_fulfill_collection_run(evidence, requires_cursor=True) is False
    # The same evidence is enough where no cursor is involved.
    assert can_fulfill_collection_run(evidence, requires_cursor=False) is True


def test_negative_counters_are_refused():
    with pytest.raises(ValueError):
        CollectionEvidence(facts_written=-1)


# ---------------------------------------------------------------------------
# Contra a base de dados real.
# ---------------------------------------------------------------------------


def test_a_handler_that_succeeded_with_a_missing_scope_is_only_partial(settings, monkeypatch):
    """The rule, end to end: execution success is not collection fulfilment."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        run = _start(session, connection, job)

        # The handler raised nothing at all -- no error code is recorded.
        finalize_collection_run(
            session,
            run,
            fence=fence,
            evidence=CollectionEvidence(facts_written=1, scopes_required=2, scopes_written=1, cursor_advanced=True),
            requires_cursor=True,
        )
        session.commit()

        assert run.status == STATUS_PARTIAL
        assert run.fulfilled is False


def test_complete_evidence_reaches_fulfilled(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        run = _start(session, connection, job)
        finalize_collection_run(
            session,
            run,
            fence=fence,
            evidence=CollectionEvidence(facts_written=2, scopes_required=2, scopes_written=2, cursor_advanced=True),
            requires_cursor=True,
        )
        session.commit()

        assert run.status == STATUS_FULFILLED
        assert run.lease_generation == 10
        assert run.finished_at is not None


def test_an_expired_lease_blocks_finalization(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session, expires_in_seconds=-1)
        run = _start(session, connection, job)
        with pytest.raises(OwnershipLost):
            finalize_collection_run(
                session,
                run,
                fence=fence,
                evidence=CollectionEvidence(
                    facts_written=2, scopes_required=2, scopes_written=2, cursor_advanced=True
                ),
                requires_cursor=True,
            )
        session.rollback()

    with factory() as session:
        # Nothing was written: the run is still open, not fulfilled.
        assert session.query(CollectionRun).count() == 0


def test_a_retry_is_a_new_row_at_the_next_attempt(settings, monkeypatch):
    """History survives. A repair never overwrites the failure it repaired."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        first = _start(session, connection, job)
        finalize_collection_run(
            session,
            first,
            fence=fence,
            evidence=CollectionEvidence(scopes_required=2, scopes_written=0, error_code="provider_refused"),
            requires_cursor=True,
        )
        session.commit()
        assert first.status == STATUS_FAILED
        assert first.attempt == 1

        second = _start(session, connection, job)
        session.commit()
        assert second.attempt == 2
        assert second.id != first.id

    with factory() as session:
        assert session.query(CollectionRun).count() == 2


def test_a_failed_run_can_never_be_promoted_to_fulfilled(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        run = _start(session, connection, job)
        finalize_collection_run(
            session,
            run,
            fence=fence,
            evidence=CollectionEvidence(scopes_required=1, scopes_written=0, error_code="boom"),
            requires_cursor=False,
        )
        session.commit()
        assert run.status == STATUS_FAILED

        with pytest.raises(CollectionRunStateError):
            finalize_collection_run(
                session,
                run,
                fence=fence,
                evidence=CollectionEvidence(scopes_required=1, scopes_written=1),
                requires_cursor=False,
            )


def test_the_database_refuses_fulfilled_without_evidence(settings, monkeypatch):
    """The service is where the message comes from; this is what still holds.

    Written as raw SQL on purpose: it proves the constraint, not the service.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        session.commit()
        connection_id = connection.id

    with factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO collection_runs ("
                    " provider_connection_id, capability, scope_kind, scope_key,"
                    " period_start, period_end, attempt, status, started_at,"
                    " facts_written, scopes_required, scopes_written, cursor_advanced,"
                    " created_at, updated_at)"
                    " VALUES (:c, 'production_history', 'connection', :k,"
                    " :s, :e, 1, 'fulfilled', now(), 0, NULL, 0, false, now(), now())"
                ),
                {"c": connection_id, "k": str(connection_id), "s": PERIOD_START, "e": PERIOD_END},
            )
        session.rollback()


def test_a_second_run_for_a_collected_scope_is_superseded_not_fulfilled(settings, monkeypatch):
    """A replay is not a failure, and it is not a second fulfilment either.

    Detected under the scope lock, so the legitimate case resolves cleanly
    instead of surfacing as an IntegrityError from the queue.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        first = _start(session, connection, job)
        finalize_collection_run(
            session,
            first,
            fence=fence,
            evidence=CollectionEvidence(scopes_required=1, scopes_written=1, cursor_advanced=True),
            requires_cursor=True,
        )
        session.commit()

        second = _start(session, connection, job)
        finalize_collection_run(
            session,
            second,
            fence=fence,
            evidence=CollectionEvidence(scopes_required=1, scopes_written=1, cursor_advanced=True),
            requires_cursor=True,
        )
        session.commit()
        assert second.status == STATUS_SUPERSEDED

    with factory() as session:
        fulfilled = session.query(CollectionRun).filter(CollectionRun.status == STATUS_FULFILLED).count()
        assert fulfilled == 1


def test_the_index_still_refuses_two_fulfilled_rows_behind_the_service(settings, monkeypatch):
    """The service now resolves the replay, so the index is never reached in
    normal operation. It still has to hold: it is what protects the invariant
    from a future write path that does not go through the service at all."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, fence = _running_job(session)
        run = _start(session, connection, job)
        finalize_collection_run(
            session,
            run,
            fence=fence,
            evidence=CollectionEvidence(scopes_required=1, scopes_written=1, cursor_advanced=True),
            requires_cursor=True,
        )
        session.commit()
        connection_id = connection.id

    with factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO collection_runs ("
                    " provider_connection_id, capability, scope_kind, scope_key,"
                    " period_start, period_end, attempt, status, started_at, finished_at,"
                    " facts_written, scopes_required, scopes_written, cursor_advanced,"
                    " lease_generation, created_at, updated_at)"
                    " VALUES (:c, 'production_history', 'connection', :k,"
                    " :s, :e, 2, 'fulfilled', now(), now(), 0, 1, 1, true, 10, now(), now())"
                ),
                {"c": connection_id, "k": str(connection_id), "s": PERIOD_START, "e": PERIOD_END},
            )
        session.rollback()


def test_lost_ownership_is_recorded_in_its_own_transaction(settings, monkeypatch):
    """The record of the loss must not live in the discarded transaction."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, _fence = _running_job(session)
        run = _start(session, connection, job)
        session.commit()
        run_id = run.id

    with factory() as session:
        recorded = mark_collection_run_lost_ownership(session, run_id, reason="lease_expired")
        session.commit()
        assert recorded is not None
        assert recorded.status == STATUS_LOST_OWNERSHIP
        assert recorded.error_code == "lease_expired"


def test_recording_a_loss_twice_is_harmless(settings, monkeypatch):
    """Best effort by construction: a failure to record must never resurrect."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, _fence = _running_job(session)
        run = _start(session, connection, job)
        session.commit()
        run_id = run.id

    with factory() as session:
        mark_collection_run_lost_ownership(session, run_id, reason="lease_expired")
        session.commit()
    with factory() as session:
        assert mark_collection_run_lost_ownership(session, run_id, reason="lease_expired") is None
        assert mark_collection_run_lost_ownership(session, 10_000_000, reason="lease_expired") is None


def test_the_run_starts_running_and_carries_its_job(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session)
        job, _fence = _running_job(session)
        run = _start(session, connection, job)
        session.commit()
        assert run.status == STATUS_RUNNING
        assert run.job_id == job.id
        assert run.attempt == 1
        assert run.finished_at is None
