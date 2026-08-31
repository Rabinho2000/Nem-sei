from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from nemsei.integrations.fusionsolar.client import FusionSolarClient
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.monitoring.models import ProductionFact
from nemsei.monitoring.production_coverage import production_coverage
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_mapping
from nemsei.sources.models import AssetSourcePolicy
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncCursor
from tests_v2.test_fusionsolar_production import FakeTransport, LOGIN_OK, daily, factory_for, response, row, selected_connection, configured_environment


def service(factory, settings, transport, **settings_changes):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True}, **settings_changes)
    return FusionSolarProductionService(factory, configured, client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport))


def test_reconciliation_uses_provider_local_d1_across_dst_and_never_moves_cursor(settings, monkeypatch):
    configured_environment(monkeypatch)
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "Europe/Lisbon")
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    baseline = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "10")])]))
    baseline.sync_incremental(connection_id, start_date=date(2026, 3, 28), end_date=date(2026, 3, 28))
    result = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "12")])])).sync_reconciliation(
        connection_id, as_of=datetime(2026, 3, 30, 0, 0, tzinfo=timezone.utc)
    )
    assert result.status == "success" and result.mode == "reconciliation" and not result.cursor_advanced
    assert result.requested_from == result.requested_until == date(2026, 3, 29)
    with factory() as session:
        fact = session.scalar(select(ProductionFact).where(ProductionFact.metadata_json["source_period_date"].as_string() == "2026-03-29"))
        cursor = session.scalar(select(SyncCursor))
        assert fact is not None and fact.period_start == datetime(2026, 3, 29, tzinfo=timezone.utc)
        assert fact.period_end == datetime(2026, 3, 29, 23, tzinfo=timezone.utc)
        assert cursor is not None and cursor.checkpoint_json["last_completed_day"] == "2026-03-28"
        assert fact.provider_mapping_id == mappings[0].id


def test_reconciliation_is_idempotent_corrects_and_missing_does_not_replace_valid_fact(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    day = date(2026, 1, 15)
    initial = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "120")])]))
    initial.sync_incremental(connection_id, start_date=day, end_date=day)
    same = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "120")])])).sync_reconciliation(connection_id, as_of=datetime(2026, 1, 16, tzinfo=timezone.utc))
    corrected = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "121")])])).sync_reconciliation(connection_id, as_of=datetime(2026, 1, 16, tzinfo=timezone.utc))
    missing = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", None)])])).sync_reconciliation(connection_id, as_of=datetime(2026, 1, 16, tzinfo=timezone.utc))
    assert same.status == corrected.status == "success"
    assert missing.status == "partial"
    with factory() as session:
        facts = list(session.scalars(select(ProductionFact).where(ProductionFact.provider_mapping_id == mappings[0].id).order_by(ProductionFact.source_revision)))
        assert [(fact.source_revision, fact.value, fact.quality, fact.completeness) for fact in facts] == [
            (1, Decimal("120"), "complete", "complete"),
            (2, Decimal("121"), "complete", "complete"),
            (3, None, "missing", "partial"),
        ]
        assert facts[-1].sync_run_id == missing.sync_run_id


def test_canonical_gap_detection_keeps_zero_complete_and_never_calls_provider(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    with factory() as session:
        for source_day, value, quality, completeness in [
            (date(2026, 1, 1), Decimal("0"), "complete", "complete"),
            (date(2026, 1, 2), None, "missing", "partial"),
        ]:
            record_production_fact(
                session, asset_id=mappings[0].asset_id, provider_mapping_id=mappings[0].id,
                source_fact_key=f"canonical:{source_day}", period_start=datetime.combine(source_day, datetime.min.time(), tzinfo=timezone.utc),
                period_end=datetime.combine(source_day, datetime.min.time(), tzinfo=timezone.utc), granularity="day", value=value,
                unit="kWh", quality=quality, completeness=completeness,
                metadata={"source_period_timezone": "UTC", "source_period_date": source_day.isoformat()},
            )
        session.commit()
        coverage = production_coverage(session, provider_mapping_id=mappings[0].id, source_timezone="UTC", start_date=date(2026, 1, 1), end_date=date(2026, 1, 3))
    assert [(item.source_day, item.status) for item in coverage] == [(date(2026, 1, 1), "complete"), (date(2026, 1, 2), "missing"), (date(2026, 1, 3), "missing")]
    assert connection_id > 0


def test_bounded_backfill_chunks_chronologically_is_resumable_and_only_extends_contiguously(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    settings_changes = {"production_backfill_max_source_days": 5, "production_backfill_chunk_days": 2}
    first = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "1")]), daily([row("FS-001", "2")]),
    ]), **settings_changes).sync_bounded_backfill(connection_id, start_date=date(2026, 1, 1), end_date=date(2026, 1, 4))
    assert first.status == "success" and first.next_source_day == date(2026, 1, 3)
    resumed = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "3")]), daily([row("FS-001", "4")]),
    ]), **settings_changes).sync_bounded_backfill(connection_id, start_date=date(2026, 1, 1), end_date=date(2026, 1, 4), resume_from=first.next_source_day)
    assert resumed.status == "success" and resumed.next_source_day is None and resumed.cursor_advanced
    with factory() as session:
        cursor = session.scalar(select(SyncCursor))
        assert cursor is not None and cursor.checkpoint_json["last_completed_day"] == "2026-01-04"


def test_backfill_bounds_rate_limit_and_gap_window_are_safe(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    invalid = service(factory, settings, FakeTransport([])).sync_bounded_backfill(connection_id, start_date=None, end_date=date(2026, 1, 2))
    assert invalid.status == "failed"
    future = service(factory, settings, FakeTransport([])).sync_bounded_backfill(connection_id, start_date=date(2099, 1, 1), end_date=date(2099, 1, 1))
    assert future.status == "failed"
    limited = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        response({"success": False, "failCode": 407, "message": "rate"}),
    ])).sync_bounded_backfill(connection_id, start_date=date(2026, 1, 10), end_date=date(2026, 1, 10))
    assert limited.status == "rate_limited"
    with factory() as session:
        cursor = session.scalar(select(SyncCursor))
        assert cursor is None


def test_backfill_resolves_historical_source_policy_per_source_day(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    with factory() as session:
        first_policy = session.scalar(select(AssetSourcePolicy).where(AssetSourcePolicy.provider_mapping_id == mappings[0].id))
        assert first_policy is not None
        first_policy.valid_to = date(2026, 1, 1)
        second = create_mapping(session, asset_id=mappings[0].asset_id, provider_connection_id=connection_id, external_id="FS-002", valid_from=date(2026, 1, 2))
        create_source_policy(session, asset_id=mappings[0].asset_id, provider_mapping_id=second.id, source_use="production", priority=1, valid_from=date(2026, 1, 2))
        session.commit()
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "1")]), daily([row("FS-002", "2")]),
    ])
    result = service(factory, settings, transport).sync_bounded_backfill(connection_id, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    assert result.status == "success"
    assert [call[1].get("stationCodes") for call in transport.calls[1:]] == ["FS-001", "FS-002"]


def test_a_chunked_job_gets_its_retry_budget_back_for_each_chunk_it_finishes(settings, monkeypatch) -> None:
    """Otherwise a job cannot outlive `max_attempts` chunks, however well it goes.

    `claim_next` counts every claim as an attempt, and a resumed chunk is a
    claim. The FusionSolar catch-up from a month-wide gap needs five chunks
    against a three-attempt job: without this, the fourth is claimed as attempt
    4 and a single failure there is terminal with nothing left to retry.

    The reset is paid for by committed progress, so it cannot become an
    unbounded retry loop: the caller only reaches `reschedule` after a chunk
    whose cursor advanced.
    """
    from nemsei.db import build_engine, build_session_factory
    from nemsei.jobs.models import Job
    from nemsei.jobs.repository import JobRepository
    from nemsei.shared.clock import utc_now
    from tests_v2.test_migrations import upgrade

    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    repository = JobRepository(engine, session_factory)
    now = utc_now()
    with session_factory() as session:
        job = Job(
            job_type="production.incremental", status="queued", payload_json={"connection_id": 3},
            dedupe_key="production.incremental:3:slot", priority=100, available_at=now,
            attempt_count=0, max_attempts=3, created_at=now, updated_at=now,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    for expected_attempt in (1, 2, 3, 4, 5):
        claimed = repository.claim_next(worker_id="chunker", lease_seconds=60)
        assert claimed is not None and claimed.id == job_id
        assert claimed.attempt == 1, "each finished chunk starts its successor with a full budget"
        assert repository.reschedule(claimed, payload={"connection_id": 3, "next_source_day": f"2026-08-0{expected_attempt}"})
        assert repository.activate_due_waiting() == 1

    with session_factory() as session:
        assert session.get(Job, job_id).status == "queued"
