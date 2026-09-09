"""Two writers, one scope, a real database.

Nothing here is mocked away. Advisory locks, `FOR UPDATE`, unique indexes and
lease expiry are all database behaviour, and a test that stubs them proves
only that the stub agrees with itself. Each test below opens genuine
connections -- in threads where the interleaving matters -- against the
PostgreSQL the rest of the suite uses.

The failures these guard against are the quiet kind. None of them raise in
the code that causes them; they show up later as a cursor that skipped a day,
a scope marked collected twice, or a worker that kept writing after its lease
was gone.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job
from nemsei.jobs.ownership import OwnershipFence, OwnershipLost, assert_ownership
from nemsei.jobs.repository import JobRepository
from nemsei.monitoring.models import ProductionFact
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.assets.service import create_asset
from nemsei.shared.clock import utc_now
from nemsei.sync.collection_models import STATUS_FULFILLED, CollectionRun
from nemsei.sync.collection_service import (
    CollectionEvidence,
    finalize_collection_run,
    start_collection_run,
)
from nemsei.sync.models import SyncCursor
from nemsei.sync.scope_lock import SCOPE_KIND_CONNECTION, acquire_collection_scope_lock
from tests_v2.test_migrations import upgrade


PERIOD_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 2, tzinfo=timezone.utc)
CAPABILITY = "production_history"


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _fixture(factory):
    """One connection, one asset, one mapping -- enough to write a fact."""
    with factory() as session:
        asset = create_asset(session, canonical_name="Concurrency", timezone="Europe/Lisbon")
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.flush()
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id,
            external_id="SYS1", valid_from=date(2026, 1, 1),
        )
        session.commit()
        return connection.id, asset.id, mapping.id


def _queued_job(factory) -> int:
    with factory() as session:
        now = utc_now()
        job = Job(
            job_type="production.incremental", status="queued", payload_json={}, priority=100,
            available_at=now, attempt_count=0, max_attempts=3, created_at=now, updated_at=now,
        )
        session.add(job)
        session.commit()
        return job.id


def _expire_lease(factory, job_id: int) -> None:
    """Push the lease into the past without touching anything else.

    Deliberately not via `recover_expired`: this is the case where *nobody*
    reclaims the job, and the worker is alone with an expired lease.
    """
    with factory() as session:
        session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": job_id},
        )
        session.commit()


def _scope(connection_id: int) -> dict:
    return {
        "connection_id": connection_id,
        "capability": CAPABILITY,
        "scope_kind": SCOPE_KIND_CONNECTION,
        "scope_key": str(connection_id),
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
    }


def _write_fact(session, *, asset_id, mapping_id, day: date, value: str):
    record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"sigenergy:production_energy:{day.isoformat()}",
        metric_kind="production_energy",
        period_start=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
        period_end=datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        granularity="day",
        value=value,
        unit="kWh",
        quality="complete",
        completeness="complete",
    )


# ---------------------------------------------------------------------------
# A -- an expired lease revokes on its own, with nobody else involved.
# ---------------------------------------------------------------------------


def test_an_expired_lease_rejects_writes_with_no_reclaim(settings, monkeypatch):
    """The case that needs no second worker, and is the easiest to get wrong.

    The generation is still the newest in the table and the token still
    matches. Only the clock has moved. If ownership were checked by comparing
    against a *newer* owner, there would be nothing to compare against and
    this write would go through.
    """
    factory = factory_for(settings, monkeypatch)
    repository = JobRepository(build_engine(settings), factory)
    _queued_job(factory)
    claimed = repository.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None and claimed.lease_generation is not None

    _expire_lease(factory, claimed.id)

    with factory() as session:
        with pytest.raises(OwnershipLost) as caught:
            assert_ownership(session, claimed.fence)
        assert caught.value.reason == "lease_expired"
        session.rollback()


def test_a_job_claimed_before_fencing_existed_fails_closed(settings, monkeypatch):
    """`NULL` generation matches no fence, so an old row is allowed nothing."""
    factory = factory_for(settings, monkeypatch)
    job_id = _queued_job(factory)
    with factory() as session:
        session.execute(
            text(
                "UPDATE jobs SET status='running', lease_token='t', lease_generation=NULL,"
                " lease_expires_at=now() + interval '5 minutes' WHERE id=:id"
            ),
            {"id": job_id},
        )
        session.commit()

    with factory() as session:
        with pytest.raises(OwnershipLost) as caught:
            assert_ownership(session, OwnershipFence(job_id=job_id, lease_token="t", lease_generation=1))
        assert caught.value.reason == "lease_generation_absent"


# ---------------------------------------------------------------------------
# B -- reclaim: the older generation loses.
# ---------------------------------------------------------------------------


def test_the_reclaimed_generation_wins_and_the_old_one_is_rejected(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    engine = build_engine(settings)
    repository = JobRepository(engine, factory)
    _queued_job(factory)

    first = repository.claim_next(worker_id="worker-a", lease_seconds=300)
    assert first is not None
    _expire_lease(factory, first.id)
    repository.recover_expired()
    repository.activate_due_waiting()
    second = repository.claim_next(worker_id="worker-b", lease_seconds=300)

    assert second is not None
    assert second.lease_generation > first.lease_generation, "generations must be monotonic"

    with factory() as session:
        # B is the owner and may proceed.
        assert_ownership(session, second.fence)
    with factory() as session:
        # A, arriving late with a live handler, may not.
        with pytest.raises(OwnershipLost) as caught:
            assert_ownership(session, first.fence)
        assert caught.value.reason in {"lease_token_mismatch", "lease_generation_mismatch"}


def test_recovering_a_job_does_not_consume_a_generation(settings, monkeypatch):
    """One allocator. Recovery revokes; only a claim allocates."""
    factory = factory_for(settings, monkeypatch)
    engine = build_engine(settings)
    repository = JobRepository(engine, factory)
    _queued_job(factory)

    first = repository.claim_next(worker_id="worker-a", lease_seconds=300)
    _expire_lease(factory, first.id)
    repository.recover_expired()

    with factory() as session:
        row = session.execute(
            text("SELECT lease_generation, lease_token, status FROM jobs WHERE id=:id"), {"id": first.id}
        ).mappings().one()
    assert row["lease_generation"] is None
    assert row["lease_token"] is None
    assert row["status"] == "waiting"

    repository.activate_due_waiting()
    second = repository.claim_next(worker_id="worker-b", lease_seconds=300)
    # Exactly one step: recovery burned nothing in between.
    assert second.lease_generation == first.lease_generation + 1


# ---------------------------------------------------------------------------
# C -- the cursor cannot go backwards, whoever gets there second.
# ---------------------------------------------------------------------------


def test_the_scope_lock_serialises_two_cursor_writers(settings, monkeypatch):
    """A wants D-2, B wants D-1, run concurrently. D-2 must never be final.

    Both threads take the same advisory key, so one waits for the other. The
    loser then sees the winner's value and `advance_cursor`'s monotonic guard
    refuses to move coverage back.
    """
    factory = factory_for(settings, monkeypatch)
    connection_id, _asset_id, _mapping_id = _fixture(factory)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def writer(day: date, covered: datetime):
        try:
            barrier.wait(timeout=10)
            with factory() as session:
                acquire_collection_scope_lock(session, **_scope(connection_id))
                cursor = session.scalar(
                    select(SyncCursor)
                    .where(SyncCursor.provider_connection_id == connection_id)
                    .with_for_update()
                )
                if cursor is None:
                    session.add(
                        SyncCursor(
                            provider_connection_id=connection_id,
                            capability=CAPABILITY,
                            cursor_key="sigenergy-daily-production",
                            checkpoint_json={"last_completed_day": day.isoformat()},
                            covered_through=covered,
                            updated_at=utc_now(),
                        )
                    )
                else:
                    existing = date.fromisoformat(cursor.checkpoint_json["last_completed_day"])
                    if day > existing:
                        cursor.checkpoint_json = {"last_completed_day": day.isoformat()}
                        cursor.covered_through = covered
                        cursor.updated_at = utc_now()
                session.commit()
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    older = date(2026, 9, 5)
    newer = date(2026, 9, 6)
    threads = [
        threading.Thread(target=writer, args=(older, datetime(2026, 9, 6, tzinfo=timezone.utc))),
        threading.Thread(target=writer, args=(newer, datetime(2026, 9, 7, tzinfo=timezone.utc))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    with factory() as session:
        cursor = session.scalar(select(SyncCursor).where(SyncCursor.provider_connection_id == connection_id))
        final = date.fromisoformat(cursor.checkpoint_json["last_completed_day"])
    assert final == newer, "the cursor regressed to the older day"


def test_the_advisory_lock_actually_blocks_a_second_holder(settings, monkeypatch):
    """Without this, every other concurrency test could pass by luck."""
    factory = factory_for(settings, monkeypatch)
    connection_id, _asset_id, _mapping_id = _fixture(factory)
    holder_ready = threading.Event()
    release = threading.Event()
    observations: list[bool] = []

    def holder():
        with factory() as session:
            acquire_collection_scope_lock(session, **_scope(connection_id))
            holder_ready.set()
            release.wait(timeout=10)
            session.commit()

    thread = threading.Thread(target=holder)
    thread.start()
    assert holder_ready.wait(timeout=10)

    with factory() as session:
        # `try` variant: returns immediately instead of waiting, so the test
        # can observe contention without deadlocking itself.
        from nemsei.sync.scope_lock import collection_scope_lock_key

        key = collection_scope_lock_key(**_scope(connection_id))
        got = session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar_one()
        observations.append(bool(got))
        session.rollback()

    release.set()
    thread.join(timeout=10)
    assert observations == [False], "a second holder acquired a lock that was already held"


# ---------------------------------------------------------------------------
# D -- two writers on one fact key.
# ---------------------------------------------------------------------------


def test_two_writers_on_one_fact_key_leave_a_coherent_chain(settings, monkeypatch):
    """Serialised by the scope lock, so revisions are 1 then 2 -- never 1 and 1.

    The unique constraint would catch a collision anyway; the lock is what
    keeps it from being an exception in normal operation.
    """
    factory = factory_for(settings, monkeypatch)
    connection_id, asset_id, mapping_id = _fixture(factory)
    day = date(2026, 9, 1)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def writer(value: str):
        try:
            barrier.wait(timeout=10)
            with factory() as session:
                acquire_collection_scope_lock(session, **_scope(connection_id))
                _write_fact(session, asset_id=asset_id, mapping_id=mapping_id, day=day, value=value)
                session.commit()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(v,)) for v in ("10.0", "20.0")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    with factory() as session:
        revisions = sorted(
            session.scalars(
                select(ProductionFact.source_revision).where(ProductionFact.provider_mapping_id == mapping_id)
            ).all()
        )
    assert revisions == [1, 2], f"incoherent revision chain: {revisions}"


# ---------------------------------------------------------------------------
# E -- one scope, one fulfilment.
# ---------------------------------------------------------------------------


def test_only_one_worker_can_fulfil_a_scope(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    engine = build_engine(settings)
    repository = JobRepository(engine, factory)
    connection_id, _asset_id, _mapping_id = _fixture(factory)
    _queued_job(factory)
    _queued_job(factory)
    first = repository.claim_next(worker_id="a", lease_seconds=300)
    second = repository.claim_next(worker_id="b", lease_seconds=300)
    assert first is not None and second is not None

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def fulfiller(claimed):
        try:
            barrier.wait(timeout=10)
            with factory() as session:
                acquire_collection_scope_lock(session, **_scope(connection_id))
                run = start_collection_run(
                    session,
                    provider_connection_id=connection_id,
                    capability=CAPABILITY,
                    scope_kind=SCOPE_KIND_CONNECTION,
                    scope_key=str(connection_id),
                    period_start=PERIOD_START,
                    period_end=PERIOD_END,
                    job_id=claimed.id,
                    lease_generation=claimed.lease_generation,
                )
                finalize_collection_run(
                    session,
                    run,
                    fence=claimed.fence,
                    evidence=CollectionEvidence(
                        facts_written=1, scopes_required=1, scopes_written=1, cursor_advanced=True
                    ),
                    requires_cursor=True,
                )
                status = run.status
                session.commit()
                outcomes.append(status)
        except IntegrityError:
            # The index, if the lock ever failed to serialise them.
            outcomes.append("refused")
        except Exception as exc:  # pragma: no cover
            outcomes.append(f"error:{exc}")

    threads = [threading.Thread(target=fulfiller, args=(claim,)) for claim in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # One collected the scope; the other found it already collected and said
    # so, rather than crashing or claiming it too.
    assert sorted(outcomes) == ["fulfilled", "superseded"], outcomes
    with factory() as session:
        fulfilled = session.scalars(
            select(CollectionRun).where(CollectionRun.status == STATUS_FULFILLED)
        ).all()
    assert len(fulfilled) == 1
