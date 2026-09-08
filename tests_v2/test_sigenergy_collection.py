"""Sigenergy, converted: facts, cursor and run land together or not at all.

Before this, `_persist` opened its own session and committed once per
mapping-day, and `_advance_cursor` opened another and committed after them.
A run's writes were spread across as many transactions as it had mapping-days
plus one, and there was no moment at which they were consistent with each
other. The tests here are about that boundary -- what survives a failure
partway through, and what a lost lease is allowed to leave behind.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from nemsei.config import Settings
from nemsei.db import build_engine
from nemsei.jobs.models import Job
from nemsei.jobs.repository import JobRepository
from nemsei.monitoring.models import ProductionFact
from nemsei.shared.clock import utc_now
from nemsei.sync.collection_models import (
    STATUS_FULFILLED,
    STATUS_RUNNING,
    STATUS_LOST_OWNERSHIP,
    STATUS_PARTIAL,
    CollectionRun,
)
from tests_v2.test_reliability_regressions import (
    COMPLETE_DAY,
    RecordingClient,
    cursor_day,
    sigenergy_fixture,
)


def _claimed(settings, factory):
    """A genuinely claimed job, so the fence carries a real generation."""
    engine = build_engine(settings)
    repository = JobRepository(engine, factory)
    with factory() as session:
        now = utc_now()
        session.add(
            Job(
                job_type="production.incremental", status="queued", payload_json={}, priority=100,
                available_at=now, attempt_count=0, max_attempts=3, created_at=now, updated_at=now,
            )
        )
        session.commit()
    claimed = repository.claim_next(worker_id="worker-a", lease_seconds=300)
    assert claimed is not None
    return claimed


def _service(factory, settings, stub):
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    return SigenergyProductionService(
        factory,
        Settings.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )


def _runs(factory):
    with factory() as session:
        return session.scalars(select(CollectionRun).order_by(CollectionRun.id)).all()


def _facts(factory, asset_id):
    with factory() as session:
        return session.scalars(
            select(ProductionFact).where(ProductionFact.asset_id == asset_id)
        ).all()


# ---------------------------------------------------------------------------
# O caminho feliz, e o que ele regista.
# ---------------------------------------------------------------------------


def test_a_complete_day_fulfils_its_collection_run(settings, monkeypatch):
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient()

    result = _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=claimed.fence
    )

    assert result.status == "success"
    runs = _runs(factory)
    assert len(runs) == 1
    assert runs[0].status == STATUS_FULFILLED
    assert runs[0].scopes_required == 1
    assert runs[0].scopes_written == 1
    assert runs[0].cursor_advanced is True
    assert runs[0].lease_generation == claimed.lease_generation
    assert runs[0].job_id == claimed.id
    assert cursor_day(factory, connection_id) == "2026-08-18"


def test_a_partly_read_day_is_a_partial_run_and_moves_no_cursor(settings, monkeypatch):
    """Execution did not raise. Collection is still not fulfilled."""
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    incomplete = {key: value for key, value in COMPLETE_DAY.items() if key != "powerFromGridKwh"}
    stub = RecordingClient(default=incomplete)

    result = _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=claimed.fence
    )

    assert result.status == "partial"
    runs = _runs(factory)
    assert len(runs) == 1
    assert runs[0].status == STATUS_PARTIAL
    assert runs[0].fulfilled is False
    assert runs[0].cursor_advanced is False
    assert cursor_day(factory, connection_id) is None
    # The evidence that was collected is still kept -- partial is not empty.
    assert runs[0].facts_written > 0


def test_a_window_with_a_hole_never_fulfils(settings, monkeypatch):
    """Two good days and one empty one is not a collected window."""
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient(by_day={date(2026, 8, 19): {"unit": "kWh"}})

    _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 20), fence=claimed.fence
    )

    runs = _runs(factory)
    assert runs[0].status != STATUS_FULFILLED
    assert runs[0].scopes_written < runs[0].scopes_required
    assert cursor_day(factory, connection_id) is None


# ---------------------------------------------------------------------------
# Closed-day: o bug dos 145 factos, na fronteira nova.
# ---------------------------------------------------------------------------


def test_the_day_in_progress_is_never_collected(settings, monkeypatch):
    """A reading taken at 10:40 during D cannot close D.

    The window is clamped to the last day the *provider's* calendar has
    finished, not the server's. This is the shape of the defect that wrote
    145 facts holding a running counter as if it were a day's total.
    """
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient()
    lisbon = ZoneInfo("Europe/Lisbon")
    today = utc_now().astimezone(lisbon).date()

    result = _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=today, end_date=today, fence=claimed.fence
    )

    # Nothing was due, so nothing was asked of the provider.
    assert stub.requested == []
    assert result.days_accepted == 0
    # And no run claims to have collected today.
    fulfilled_today = [
        run for run in _runs(factory)
        if run.status == STATUS_FULFILLED and run.period_start.astimezone(lisbon).date() == today
    ]
    assert fulfilled_today == []
    assert cursor_day(factory, connection_id) is None


# ---------------------------------------------------------------------------
# Injeção de falhas: o que sobrevive a um erro a meio.
# ---------------------------------------------------------------------------


def test_a_failure_before_the_cursor_rolls_back_the_facts_too(settings, monkeypatch):
    """Facts and cursor share a transaction, so neither survives alone."""
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient()
    service = _service(factory, settings, stub)

    import nemsei.integrations.sigenergy.production as module

    real_advance = module.advance_cursor

    def explode(*args, **kwargs):
        raise RuntimeError("injected failure between facts and cursor")

    monkeypatch.setattr(module, "advance_cursor", explode)
    with pytest.raises(RuntimeError):
        service.sync_daily_production(
            connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=claimed.fence
        )
    monkeypatch.setattr(module, "advance_cursor", real_advance)

    assert _facts(factory, asset_id) == []
    assert cursor_day(factory, connection_id) is None
    # The attempt row survives as `running`: it was committed before the
    # authoritative transaction precisely so a crash leaves a trace. What it
    # must not be is fulfilled, and no facts or cursor may stand behind it.
    statuses = [run.status for run in _runs(factory)]
    assert statuses == [STATUS_RUNNING]
    assert STATUS_FULFILLED not in statuses


def test_a_failure_before_finalisation_rolls_back_the_cursor_too(settings, monkeypatch):
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient()
    service = _service(factory, settings, stub)

    import nemsei.integrations.sigenergy.production as module

    def explode(*args, **kwargs):
        raise RuntimeError("injected failure before finalisation")

    monkeypatch.setattr(module, "finalize_collection_run", explode)
    with pytest.raises(RuntimeError):
        service.sync_daily_production(
            connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=claimed.fence
        )

    assert _facts(factory, asset_id) == []
    assert cursor_day(factory, connection_id) is None
    with factory() as session:
        assert session.query(CollectionRun).filter(CollectionRun.status == STATUS_FULFILLED).count() == 0


def test_a_lease_lost_during_the_transaction_leaves_nothing_behind(settings, monkeypatch):
    """The second ownership assertion is what this test exists for.

    Ownership is real when the writes begin and gone by the time they finish.
    Without the check after the writes, the run would close and the cursor
    would stand.
    """
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    stub = RecordingClient()
    service = _service(factory, settings, stub)

    import nemsei.integrations.sigenergy.production as module

    real_advance = module.advance_cursor

    def expire_then_advance(session, **kwargs):
        # The lease runs out mid-transaction, exactly as a slow provider read
        # followed by a slow write would cause.
        outcome = real_advance(session, **kwargs)
        session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": claimed.id},
        )
        return outcome

    monkeypatch.setattr(module, "advance_cursor", expire_then_advance)
    result = service.sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=claimed.fence
    )
    monkeypatch.setattr(module, "advance_cursor", real_advance)

    assert result.status != "success"
    assert _facts(factory, asset_id) == []
    assert cursor_day(factory, connection_id) is None
    runs = _runs(factory)
    # The loss is recorded in its own transaction, after the rollback.
    assert [run.status for run in runs] == [STATUS_LOST_OWNERSHIP]
    assert runs[0].error_code == "lease_expired"


# ---------------------------------------------------------------------------
# Idempotência.
# ---------------------------------------------------------------------------


def test_replaying_the_same_day_writes_no_duplicate_facts(settings, monkeypatch):
    """Same payload, same scope, twice. The second run adds no revisions."""
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient()

    first_claim = _claimed(settings, factory)
    _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=first_claim.fence
    )
    after_first = {(fact.source_fact_key, fact.source_revision) for fact in _facts(factory, asset_id)}
    cursor_after_first = cursor_day(factory, connection_id)

    second_claim = _claimed(settings, factory)
    _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=second_claim.fence
    )
    after_second = {(fact.source_fact_key, fact.source_revision) for fact in _facts(factory, asset_id)}

    assert after_second == after_first, "a replay created new revisions"
    # The cursor did not regress, and did not jump either.
    assert cursor_day(factory, connection_id) == cursor_after_first


def test_a_replay_cannot_produce_a_second_fulfilment(settings, monkeypatch):
    """The partial unique index is the last line; this proves it is reached."""
    factory, asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient()

    first_claim = _claimed(settings, factory)
    _service(factory, settings, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=first_claim.fence
    )

    second_claim = _claimed(settings, factory)
    # The same logical scope, a second time. Whatever the outcome, the fleet
    # must not end up with two rows claiming to have collected it.
    try:
        _service(factory, settings, stub).sync_daily_production(
            connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18), fence=second_claim.fence
        )
    except Exception:
        pass

    with factory() as session:
        fulfilled = session.scalars(
            select(CollectionRun).where(CollectionRun.status == STATUS_FULFILLED)
        ).all()
    assert len(fulfilled) == 1


# ---------------------------------------------------------------------------
# Sem histórico retroativo.
# ---------------------------------------------------------------------------


def test_existing_facts_alone_never_create_a_collection_run(settings, monkeypatch):
    """A fact is evidence that something was written, not that it was owed.

    The 145 Sigenergy facts already in production must gain no marker from
    this feature existing.
    """
    factory, asset_id, connection_id, mapping_id = sigenergy_fixture(settings, monkeypatch)
    with factory() as session:
        from nemsei.monitoring.service import record_production_fact

        day = date(2026, 7, 1)
        record_production_fact(
            session,
            asset_id=asset_id,
            provider_mapping_id=mapping_id,
            source_fact_key=f"sigenergy:production_energy:{day.isoformat()}",
            metric_kind="production_energy",
            period_start=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
            period_end=datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            granularity="day",
            value="99.0",
            unit="kWh",
            quality="complete",
            completeness="complete",
        )
        session.commit()

    assert _runs(factory) == []


# ---------------------------------------------------------------------------
# A ligação do handler: o fence tem de sair mesmo do claim.
# ---------------------------------------------------------------------------


def test_the_handler_hands_the_service_its_claim_fence(settings, monkeypatch):
    """The wiring, asserted rather than assumed.

    `ClaimedJob.fence` is only useful if the handler actually passes it. A
    service that quietly received `None` would still be atomic and would prove
    nothing about ownership -- and every test in this file that builds the
    fence by hand would still pass.
    """
    from nemsei.jobs import handlers

    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    claimed = _claimed(settings, factory)
    seen: dict = {}

    class Spy:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def sync_incremental(self, cid, *, fence=None, **kwargs):
            seen["connection_id"] = cid
            seen["fence"] = fence
            from nemsei.integrations.sigenergy.production import SigenergyProductionResult

            return SigenergyProductionResult("success", 1, 1, 5, 1, None, 0, 1)

    monkeypatch.setattr(handlers, "SigenergyProductionService", Spy)
    outcome = handlers._execute_sigenergy_production(
        claimed, connection_id, settings=Settings.from_environment(), session_factory=factory
    )

    assert outcome.status == "success"
    assert seen["connection_id"] == connection_id
    assert seen["fence"] is not None
    assert seen["fence"].job_id == claimed.id
    assert seen["fence"].lease_token == claimed.lease_token
    assert seen["fence"].lease_generation == claimed.lease_generation
