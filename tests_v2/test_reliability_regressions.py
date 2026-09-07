"""The false-success surface, pinned before it is fixed.

Every test here was written against the audited revision
`7815cb7aa316c7ee073a1b46aafc183c7b724a89` and failed on it. They exist because
each of the defects below reports *success* -- to an operator, to a dashboard,
to the next scheduled run -- while having collected nothing, or the wrong
thing, or having overwritten a truthful signal with an optimistic one.

The organising rule is the one that decides what is worth testing at all: an
explicit failure is an acceptable outcome, and a false success is not. So each
test asserts an observable consequence -- what the cursor says, what the job
event says, what the reader returns, what the operator's health row says --
rather than the shape of the code that produces it.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncCursor
from tests_v2.test_migrations import upgrade

ROOT = Path(__file__).parents[1]

SIGENERGY_ENV = (
    ("NEMSEI_V2_SIGENERGY_REF_APP_KEY", "k"),
    ("NEMSEI_V2_SIGENERGY_REF_APP_SECRET", "s"),
    ("NEMSEI_V2_SIGENERGY_REF_BASE_URL", "https://example.invalid"),
    ("NEMSEI_V2_SIGENERGY_REF_AUTH_ENDPOINT", "/a"),
    ("NEMSEI_V2_SIGENERGY_REF_SYSTEMS_ENDPOINT", "/s"),
    ("NEMSEI_V2_SIGENERGY_REF_ENERGY_FLOW_ENDPOINT", "/e"),
    ("NEMSEI_V2_SIGENERGY_REF_REGION", "eu"),
    ("NEMSEI_V2_SIGENERGY_REF_PRODUCTION_TIMEZONE", "Europe/Lisbon"),
    ("NEMSEI_V2_SIGENERGY_REF_PRODUCTION_UNIT", "kWh"),
    ("NEMSEI_V2_PROVIDER_READS", "true"),
)

COMPLETE_DAY = {
    "unit": "kWh",
    "powerGenerationKwh": 120.5,
    "powerUseKwh": 80.0,
    "powerOneselfKwh": 60.0,
    "powerToGridKwh": 60.5,
    "powerFromGridKwh": 20.0,
}


class RecordingClient:
    """Answers each requested day from a canned map; records what was asked.

    Keyed by day so one stub can serve a window in which some days are
    complete and others are not -- which is the case every one of these
    regressions is about.
    """

    def __init__(self, by_day: dict[date, dict] | None = None, default: dict | None = None) -> None:
        self.by_day = by_day or {}
        self.default = default if default is not None else COMPLETE_DAY
        self.requested: list[tuple[str, date]] = []

    def authenticate(self) -> None:
        return None

    def get_system_history(self, system_id: str, *, target_date, level: str = "Day") -> dict:
        self.requested.append((system_id, target_date))
        return self.by_day.get(target_date, self.default)


def sigenergy_fixture(settings, monkeypatch, *, valid_from: date = date(2026, 1, 1)):
    """One Sigenergy connection, one asset, one active mapping, one policy."""
    upgrade(settings, monkeypatch)
    for name, value in SIGENERGY_ENV:
        monkeypatch.setenv(name, value)
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        asset = create_asset(session, canonical_name="Sigen Regression", timezone="Europe/Lisbon")
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.flush()
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id,
            external_id="SYS1", valid_from=valid_from,
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=mapping.id,
            source_use="production", priority=1, valid_from=valid_from,
        )
        session.commit()
        return factory, asset.id, connection.id, mapping.id


def cursor_day(factory, connection_id: int) -> str | None:
    with factory() as session:
        cursor = session.scalar(
            select(SyncCursor).where(SyncCursor.provider_connection_id == connection_id)
        )
        return (cursor.checkpoint_json or {}).get("last_completed_day") if cursor else None


# --- F01: an empty payload is not a collected day -----------------------------


def test_an_empty_sigenergy_payload_never_reports_a_successful_collection(settings, monkeypatch) -> None:
    """`{}` parses to `missing`. Missing is the absence of data, and the run
    that found it collected nothing -- so it cannot be a success, and it
    certainly cannot move coverage past the day it failed to read."""
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient(default={"unit": "kWh"})
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )

    result = service.sync_daily_production(connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))

    assert result.status != "success"
    assert result.days_accepted == 0
    assert cursor_day(factory, connection_id) is None


def test_a_partly_read_sigenergy_day_is_partial_and_holds_the_cursor(settings, monkeypatch) -> None:
    """Four of five metrics is not the day. It stays retryable, and the cursor
    stays where it was so the next run reads the day again."""
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    incomplete = {key: value for key, value in COMPLETE_DAY.items() if key != "powerFromGridKwh"}
    stub = RecordingClient(default=incomplete)
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )

    result = service.sync_daily_production(connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))

    assert result.status == "partial"
    assert cursor_day(factory, connection_id) is None


def test_one_unread_day_holds_back_coverage_for_the_whole_window(settings, monkeypatch) -> None:
    """The second day of three comes back empty. The first day's facts are
    kept -- they were really collected -- but coverage must not claim the
    third, because the window has a hole in the middle of it."""
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient(by_day={date(2026, 8, 19): {"unit": "kWh"}})
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )

    result = service.sync_daily_production(connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 20))

    assert result.status != "success"
    assert cursor_day(factory, connection_id) is None
    with factory() as session:
        kept = session.scalars(
            select(ProductionFact).where(
                ProductionFact.asset_id == asset_id,
                ProductionFact.metric_kind == "production_energy",
                ProductionFact.value.is_not(None),
            )
        ).all()
    # Two real days landed and stay landed; only the missing one is owed.
    assert {fact.period_start.date() for fact in kept} == {date(2026, 8, 18), date(2026, 8, 20)}


def test_a_genuine_zero_is_a_reading_and_not_an_absence(settings, monkeypatch) -> None:
    """The counterpart the empty-payload rule must not break: a plant that
    made nothing reports 0.0, and 0.0 is data."""
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient(default={key: (0.0 if key != "unit" else "kWh") for key in COMPLETE_DAY})
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )

    result = service.sync_daily_production(connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))

    assert result.status == "success"
    assert cursor_day(factory, connection_id) == "2026-08-18"


# --- F02: the day still in progress is not a daily total ----------------------


def test_sigenergy_never_closes_the_day_that_is_still_running(settings, monkeypatch) -> None:
    """A cumulative counter read at midday is not that day's production. V1
    refused `target_date >= today` for this reason; the window here is resolved
    in the source's own timezone, so "today" means the provider's today."""
    from zoneinfo import ZoneInfo

    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient()
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )

    service.sync_incremental(connection_id)

    source_today = datetime.now(tz=ZoneInfo("Europe/Lisbon")).date()
    requested = {day for _, day in stub.requested}
    assert source_today not in requested, "the open day was requested as if it were finished"
    assert cursor_day(factory, connection_id) != source_today.isoformat()


def test_an_explicit_window_cannot_be_used_to_close_the_open_day(settings, monkeypatch) -> None:
    """The bound belongs to the service, not to whoever calls it: a caller
    asking for today must not be able to buy a final total for a day that has
    not happened yet."""
    from zoneinfo import ZoneInfo

    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    stub = RecordingClient()
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )
    source_today = datetime.now(tz=ZoneInfo("Europe/Lisbon")).date()

    service.sync_daily_production(
        connection_id, start_date=source_today - timedelta(days=1), end_date=source_today
    )

    assert source_today not in {day for _, day in stub.requested}


def test_a_window_bound_of_zero_days_is_refused_rather_than_inverted(settings, monkeypatch) -> None:
    """`max_days=0` computes `end = start - 1`, an inverted window. It has to
    be rejected as the configuration error it is."""
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    factory, asset_id, connection_id, _ = sigenergy_fixture(settings, monkeypatch)
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: RecordingClient(),
    )

    with pytest.raises(ValueError):
        service.sync_incremental(connection_id, max_days=0)


# --- F03/F04: a job outcome says what happened --------------------------------


def test_a_handler_that_failed_ends_the_job_failed_with_its_own_reason(settings, monkeypatch) -> None:
    """`availability.history_sync` returns `failed` when no connection is
    configured. On the audited revision `finish` rejected that status, the
    worker's generic handler turned it into a `ValueError` about the finish
    contract, and the operator's event log recorded a bug in the queue instead
    of a misconfigured integration."""
    from nemsei.jobs.repository import JobRepository
    from nemsei.jobs.worker import Worker

    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(
        job_type="availability.history_sync", payload={}, actor_source="system", max_attempts=1
    )

    assert Worker(settings, worker_id="test-failed-outcome").run_once()

    events = repo.events_for(job.id)
    assert events[-1].to_status == "failed"
    recorded = " ".join(str(event.metadata_json) for event in events)
    assert "ValueError" not in recorded
    assert "no_connection_configured" in recorded


def test_a_monitoring_read_that_failed_is_not_a_successful_job(settings, monkeypatch) -> None:
    """A plant-state read against a connection that is not configured collects
    nothing. Reporting the job `success` makes the automation health page count
    it among the runs that worked."""
    from nemsei.jobs.repository import JobRepository
    from nemsei.jobs.worker import Worker

    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    with factory() as session:
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="broken", display_name="Sigen",
            credential_reference="ref", enabled=False, configuration_status="disabled",
        )
        session.commit()
        connection_id = connection.id

    repo = JobRepository(engine, factory)
    job, _ = repo.enqueue(
        job_type="monitoring.current", payload={"connection_id": connection_id},
        actor_source="system", max_attempts=1,
    )
    assert Worker(settings, worker_id="test-monitoring-outcome").run_once()

    events = repo.events_for(job.id)
    assert events[-1].to_status != "success"
    assert "configuration" in " ".join(str(event.metadata_json) for event in events)


def test_a_partial_sigenergy_production_run_is_not_a_successful_job(settings, monkeypatch) -> None:
    """The handler mapped `partial` onto `success` explicitly. A run that read
    four of five metrics has not finished the work the schedule asked for."""
    from nemsei.integrations.sigenergy import production as production_module
    from nemsei.integrations.sigenergy.production import SigenergyProductionResult
    from nemsei.jobs.repository import JobRepository
    from nemsei.jobs.worker import Worker

    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    with factory() as session:
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.commit()
        connection_id = connection.id

    def partial_sync(self, connection_id, **kwargs):
        return SigenergyProductionResult("partial", 1, 1, 4, 1, None)

    monkeypatch.setattr(production_module.SigenergyProductionService, "sync_incremental", partial_sync)

    repo = JobRepository(engine, factory)
    job, _ = repo.enqueue(
        job_type="production.incremental", payload={"connection_id": connection_id},
        actor_source="system", max_attempts=1,
    )
    assert Worker(settings, worker_id="test-partial-outcome").run_once()

    events = repo.events_for(job.id)
    assert events[-1].to_status != "success"


# --- F09: the counters an outcome is made of survive being written ------------


def test_a_job_result_keeps_the_counters_that_explain_the_outcome() -> None:
    """`safe_metadata` dropped `expected`/`accepted`/`rejected`/`error_code`
    /`facts_written`, so `jobs.result_json` said `partial` without ever saying
    partial *of what*. These are integers and short codes -- there is nothing
    in them to leak."""
    from nemsei.jobs.repository import safe_metadata

    kept = safe_metadata(
        {
            "result_status": "partial",
            "expected": 266,
            "received": 265,
            "accepted": 260,
            "rejected": 5,
            "facts_written": 1300,
            "error_code": "rate_limited",
        }
    )

    assert kept["expected"] == "266"
    assert kept["accepted"] == "260"
    assert kept["rejected"] == "5"
    assert kept["received"] == "265"
    assert kept["facts_written"] == "1300"
    assert kept["error_code"] == "rate_limited"


def test_a_job_result_still_refuses_anything_it_was_not_asked_to_keep() -> None:
    """The allowlist is the point of the function; widening it for counters
    must not turn it into a passthrough."""
    from nemsei.jobs.repository import safe_metadata

    kept = safe_metadata({"password": "hunter2", "raw_payload": {"token": "abc"}, "expected": 3})

    assert set(kept) == {"expected"}


def test_a_device_status_job_persists_its_counters(settings, monkeypatch) -> None:
    """The end of the same contract: what a handler counted reaches the row an
    operator reads, not just the log line nobody kept."""
    from nemsei.integrations.fusionsolar import device_status as device_status_module
    from nemsei.jobs.models import Job
    from nemsei.jobs.repository import JobRepository
    from nemsei.jobs.worker import Worker

    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    with factory() as session:
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="live", display_name="FS",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.commit()
        connection_id = connection.id

    class Result:
        status, expected, received, accepted, rejected = "success", 12, 12, 11, 1
        sync_run_id, error_code = 1, None

    monkeypatch.setattr(
        device_status_module.FusionSolarDeviceStatusService,
        "sync_device_status",
        lambda self, connection_id: Result(),
    )

    repo = JobRepository(engine, factory)
    job, _ = repo.enqueue(
        job_type="device_status.poll", payload={"connection_id": connection_id}, actor_source="system"
    )
    assert Worker(settings, worker_id="test-counters").run_once()

    with factory() as session:
        result = session.get(Job, job.id).result_json
    assert result["expected"] == "12"
    assert result["accepted"] == "11"
    assert result["rejected"] == "1"


# --- F28: starting work is not succeeding at it -------------------------------


def test_starting_a_sync_run_does_not_create_a_record_of_success(settings, monkeypatch) -> None:
    """`start_sync_run` recorded health with no error, and "no error" set
    `last_success_at = now`. On this deployment on 2026-09-07 that made
    connection 5 report a success at 15:40 when its last real successful sync
    had been at 10:40."""
    from nemsei.providers.registry import ProviderCapability
    from nemsei.sync.service import health_for, start_sync_run

    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.flush()
        start_sync_run(
            session,
            provider_connection_id=connection.id,
            capability=ProviderCapability.PRODUCTION_HISTORY.value,
        )
        session.flush()
        health = health_for(session, connection.id)

        assert health.last_attempt_at is not None, "the attempt itself must still be recorded"
        assert health.last_success_at is None
        assert health.last_successful_sync_at is None


def test_a_failed_run_leaves_the_previous_success_time_alone(settings, monkeypatch) -> None:
    """The other half: a failure must not erase the memory of the last real
    success, or "how long since this worked" becomes unanswerable."""
    from nemsei.providers.errors import ProviderError, ProviderErrorCode
    from nemsei.providers.registry import ProviderCapability
    from nemsei.sync.service import finish_sync_run, health_for, start_sync_run

    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
            credential_reference="ref", enabled=True, configuration_status="configured",
        )
        session.flush()
        good = start_sync_run(session, provider_connection_id=connection.id, capability=ProviderCapability.PRODUCTION_HISTORY.value)
        session.flush()
        finish_sync_run(session, run=good, status="success", completeness="complete")
        session.flush()
        succeeded_at = health_for(session, connection.id).last_success_at
        assert succeeded_at is not None

        bad = start_sync_run(session, provider_connection_id=connection.id, capability=ProviderCapability.PRODUCTION_HISTORY.value)
        session.flush()
        finish_sync_run(
            session, run=bad, status="failed", completeness="none",
            error=ProviderError(ProviderErrorCode.TIMEOUT, "provider timed out"),
        )
        session.flush()
        health = health_for(session, connection.id)

    assert health.last_success_at == succeeded_at
    assert health.last_failure_at is not None


# --- F14/F15: one asset, two sources, one canonical answer --------------------


def two_source_asset(settings, monkeypatch, *, day: date):
    """An asset read by a primary and a fallback mapping, both holding a fact
    for the same day. This is not hypothetical: 132 of the 267 assets on this
    deployment have two active plant mappings."""
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        asset = create_asset(session, canonical_name="Two Sources", timezone="Europe/Lisbon")
        primary_connection = create_connection(
            session, provider_code="fusionsolar", connection_key="primary", display_name="FS",
            credential_reference="fs", enabled=True, configuration_status="configured",
        )
        fallback_connection = create_connection(
            session, provider_code="huawei_scada", connection_key="scada", display_name="SCADA",
            credential_reference="scada", enabled=True, configuration_status="configured",
        )
        session.flush()
        primary = create_mapping(
            session, asset_id=asset.id, provider_connection_id=primary_connection.id,
            external_id="PLANT-1", valid_from=date(2026, 1, 1),
        )
        fallback = create_mapping(
            session, asset_id=asset.id, provider_connection_id=fallback_connection.id,
            external_id="SCADA-1", valid_from=date(2026, 1, 1),
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=primary.id,
            source_use="production", priority=1, valid_from=date(2026, 1, 1),
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=fallback.id,
            source_use="production", priority=2, valid_from=date(2026, 1, 1), is_fallback=True,
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=primary.id,
            source_use="monitoring", priority=1, valid_from=date(2026, 1, 1),
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=fallback.id,
            source_use="monitoring", priority=2, valid_from=date(2026, 1, 1), is_fallback=True,
        )
        session.commit()
        return factory, asset.id, primary.id, fallback.id


def write_day(session, *, asset_id: int, mapping_id: int, day: date, value: float) -> None:
    from nemsei.monitoring.service import record_production_fact

    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    record_production_fact(
        session, asset_id=asset_id, provider_mapping_id=mapping_id,
        source_fact_key=f"day:{day.isoformat()}", metric_kind="production_energy",
        period_start=start, period_end=start + timedelta(days=1), granularity="day",
        value=Decimal(str(value)), unit="kWh", quality="complete", completeness="complete",
    )


def test_two_sources_for_one_day_are_not_added_together(settings, monkeypatch) -> None:
    """Asset 180 on 2026-07-24 holds 59.55 kWh from one mapping and 59.56 from
    another. It made about 59.56 that day; the fleet reader returned 119.11."""
    from nemsei.web.series import fleet_metric_totals

    day = date(2026, 7, 24)
    factory, asset_id, primary_id, fallback_id = two_source_asset(settings, monkeypatch, day=day)
    with factory() as session, session.begin():
        write_day(session, asset_id=asset_id, mapping_id=primary_id, day=day, value=59.55)
        write_day(session, asset_id=asset_id, mapping_id=fallback_id, day=day, value=59.56)

    with factory() as session:
        totals = fleet_metric_totals(session, start=day, end=day + timedelta(days=1))

    assert totals[asset_id] == pytest.approx(59.55), "the primary's day, not the sum of both sources"


def test_the_installation_chart_reads_the_same_single_source(settings, monkeypatch) -> None:
    """A chart and a fleet total that disagree are two wrong answers, not one
    right one -- both go through the canonical reader or neither does."""
    from nemsei.monitoring.repository import CanonicalFactRepository

    day = date(2026, 7, 24)
    factory, asset_id, primary_id, fallback_id = two_source_asset(settings, monkeypatch, day=day)
    with factory() as session, session.begin():
        write_day(session, asset_id=asset_id, mapping_id=primary_id, day=day, value=59.55)
        write_day(session, asset_id=asset_id, mapping_id=fallback_id, day=day, value=59.56)

    with factory() as session:
        facts = CanonicalFactRepository(session).current_production_facts_for_asset(
            asset_id=asset_id,
            period_start=datetime.combine(day, time.min, tzinfo=timezone.utc),
            period_end=datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc),
        )

    assert [float(fact.value) for fact in facts] == [pytest.approx(59.55)]


def test_the_portfolio_chart_reads_the_same_single_source(settings, monkeypatch) -> None:
    """The third reader of the same facts. A monthly bar built by adding both
    sources is the same defect drawn larger."""
    from nemsei.web.series import portfolio_monthly_series

    day = datetime.now(tz=timezone.utc).date().replace(day=15)
    factory, asset_id, primary_id, fallback_id = two_source_asset(settings, monkeypatch, day=day)
    with factory() as session, session.begin():
        write_day(session, asset_id=asset_id, mapping_id=primary_id, day=day, value=1000.0)
        write_day(session, asset_id=asset_id, mapping_id=fallback_id, day=day, value=1000.0)

    with factory() as session:
        series = portfolio_monthly_series(session, months=1)

    bar = series["chart"].bars[-1]
    assert bar.point.value == pytest.approx(1.0), "1000 kWh once, not 2000 kWh twice"


def test_a_fallback_only_day_still_reports_its_value(settings, monkeypatch) -> None:
    """Choosing one source must not mean losing the day the primary never had.
    A fallback exists precisely for the days the primary cannot answer."""
    from nemsei.web.series import fleet_metric_totals

    day = date(2026, 7, 24)
    factory, asset_id, primary_id, fallback_id = two_source_asset(settings, monkeypatch, day=day)
    with factory() as session, session.begin():
        write_day(session, asset_id=asset_id, mapping_id=fallback_id, day=day, value=42.0)

    with factory() as session:
        totals = fleet_metric_totals(session, start=day, end=day + timedelta(days=1))

    assert totals[asset_id] == pytest.approx(42.0)


def test_a_fresh_read_of_one_source_does_not_refresh_another(settings, monkeypatch) -> None:
    """The state query took the newest observation of any mapping and the
    newest confirmation of any mapping, independently. A SCADA mapping polled a
    minute ago therefore made a two-day-old FusionSolar observation look
    current, and the plant read "operational" on evidence nobody had
    re-confirmed."""
    from nemsei.monitoring.models import MonitoringCurrentState
    from nemsei.monitoring.installation_state import current_installation_state
    from nemsei.monitoring.service import record_observation

    day = date(2026, 7, 24)
    factory, asset_id, primary_id, fallback_id = two_source_asset(settings, monkeypatch, day=day)
    now = datetime.now(tz=timezone.utc)
    stale_moment = now - timedelta(days=3)
    with factory() as session, session.begin():
        observation, _ = record_observation(
            session, asset_id=asset_id, provider_mapping_id=primary_id,
            source_observation_key="plant:1", observed_at=stale_moment, condition="operational",
        )
        session.flush()
        session.add(
            MonitoringCurrentState(
                provider_mapping_id=primary_id, latest_observation_id=observation.id,
                last_confirmed_at=stale_moment, updated_at=stale_moment,
            )
        )
        # The other source was read a minute ago and said nothing about this.
        session.add(
            MonitoringCurrentState(
                provider_mapping_id=fallback_id, last_confirmed_at=now, updated_at=now,
            )
        )

    with factory() as session:
        state = current_installation_state(session, asset_id=asset_id, now=now)

    assert state.state == "stale"


# --- F21: an unconfigured channel has not delivered anything ------------------


def test_a_runtime_without_a_telegram_token_cannot_report_a_delivery(monkeypatch) -> None:
    """`default_client_factory` fell back to the mock, whose `send_message`
    returns `delivered=True`. An alert nobody could have received was recorded
    as sent -- the single worst outcome for a notification system, because it
    is indistinguishable from working."""
    from nemsei.notifications.telegram_client import default_client_factory

    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TESTING", raising=False)

    client = default_client_factory(object())
    result = client.send_message(chat_id="123", text="hello")

    assert result.delivered is False
    assert result.error is not None


def test_an_explicitly_configured_test_run_may_still_use_the_mock(monkeypatch) -> None:
    """The mock is not the problem; reaching it by accident in production is.
    A run that says it is testing keeps it."""
    from nemsei.notifications.telegram_client import MockTelegramClient, default_client_factory

    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    monkeypatch.setenv("NEMSEI_V2_TESTING", "true")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", raising=False)

    assert isinstance(default_client_factory(object()), MockTelegramClient)


def test_an_undeliverable_digest_is_not_recorded_as_delivered(settings, monkeypatch) -> None:
    """The consequence that matters, through a real delivery path: with the
    capability on, a channel enabled and no token anywhere, the digest must
    end `failed` with a reason -- never `delivered`."""
    from nemsei.notifications.digests import deliver_digest
    from nemsei.notifications.models import DigestRun, NotificationChannel

    upgrade(settings, monkeypatch)
    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TESTING", raising=False)

    moment = datetime.now(timezone.utc)
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        channel = NotificationChannel(
            name="Ops", kind="telegram", enabled=True, target_chat_id="123",
            created_at=moment, updated_at=moment,
        )
        session.add(channel)
        session.flush()
        digest = DigestRun(
            kind="diagnostics", window_start=moment - timedelta(hours=1), window_end=moment,
            generated_at=moment, summary_json={}, rendered_text="resumo",
            channel_id=channel.id, created_at=moment, updated_at=moment,
        )
        session.add(digest)
        session.flush()
        digest_id = digest.id

    outcome = deliver_digest(factory, digest_run_id=digest_id, notifications_enabled=True)

    assert outcome.delivered is False
    with factory() as session:
        stored = session.get(DigestRun, digest_id)
        assert stored.delivery_status != "delivered"
        assert stored.last_error is not None


# --- F23: an interrupted dump is not a backup ---------------------------------


def backup_script() -> str:
    return (ROOT / "scripts/v2_postgres_backup.sh").read_text(encoding="utf-8")


def retention_module():
    path = ROOT / "scripts/v2_backup_retention.py"
    spec = importlib.util.spec_from_file_location("v2_backup_retention_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dump_is_written_under_a_name_retention_cannot_mistake_for_a_backup() -> None:
    """`pg_dump ... > final.dump` makes the archive eligible the instant the
    first byte lands. A dump killed halfway is then a file of the right name,
    the right age and the wrong contents, and retention counts it among the
    seven dailies."""
    script = backup_script()
    assert ".partial" in script
    partial_write = [line for line in script.splitlines() if "pg_dump" in line]
    assert partial_write and all(".partial" in line for line in partial_write), (
        "pg_dump must write to the partial name, never straight to the final one"
    )


def test_the_archive_is_verified_before_it_is_renamed_into_place() -> None:
    """Non-zero length is not integrity. `pg_restore --list` reads the custom
    format's table of contents, which a truncated dump does not have."""
    script = backup_script()
    assert "pg_restore" in script and "--list" in script
    assert script.index("pg_restore") < script.index("mv "), "verify, then rename"


def test_retention_never_counts_or_deletes_a_partial_dump() -> None:
    """Both halves: a partial is not one of the seven kept, and it is not
    something this rule is allowed to remove either -- cleaning them up is a
    separate policy with a separate reason."""
    module = retention_module()
    partial = "nemsei-v2-20260901T030000Z.dump.partial"
    names = [f"nemsei-v2-202608{day:02d}T030000Z.dump" for day in range(25, 32)] + [partial]

    assert partial not in module.retained(names)
    assert partial not in module.expired(names)
