"""A provisional month becomes final on its own, without rewriting itself.

August is generated on the 31st from a month that is neither over nor complete.
These pin what happens next: nothing, until the days arrive and the month ends,
and then a *new* snapshot beside the old one rather than in place of it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.assembler import assemble_asset_report
from nemsei.reporting.close import close_reporting_months, provisional_periods
from nemsei.reporting.datasets import snapshot_dataset
from nemsei.reporting.models import ReportSnapshot
from nemsei.reporting.periods import monthly_period


def utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


@pytest.fixture
def prepared(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central de Fecho")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1"
        )
        ids = (asset.id, mapping.id)
    return factory, ids


def add_day(session, asset_id, mapping_id, day: date, value="100"):
    record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"pvyield:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        value=Decimal(value),
        unit="kWh",
        quality="complete",
        completeness="complete",
        metadata={},
    )


def generate(session, asset_id, month: str, today: date):
    assembled = assemble_asset_report(
        session, asset_id=asset_id, period=monthly_period(month), built_by="operador", today=today
    )
    snapshot = snapshot_dataset(
        session, dataset=assembled.dataset, payload=assembled.payload, created_by="operador"
    )
    return assembled, snapshot


def august_missing(session, asset_id, mapping_id, absent=(19, 20, 21, 22, 23)):
    for day in range(1, 32):
        if day not in absent:
            add_day(session, asset_id, mapping_id, date(2026, 8, day))


def test_a_provisional_month_is_left_alone_while_it_is_still_short(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id)
        assembled, _ = generate(session, asset_id, "2026-08", date(2026, 8, 31))
        assert assembled.payload["reporting_state"] == "provisional"

    with factory() as session, session.begin():
        outcome = close_reporting_months(session, today=date(2026, 9, 10))

    assert outcome.examined == 1
    assert outcome.finalised == 0
    assert outcome.still_provisional == 1


def test_the_month_closes_by_itself_once_the_missing_days_arrive(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id)
        _, provisional = generate(session, asset_id, "2026-08", date(2026, 8, 31))
        provisional_id, provisional_digest = provisional.id, provisional.snapshot_digest

    # The backfill lands.
    with factory() as session, session.begin():
        for day in (19, 20, 21, 22, 23):
            add_day(session, asset_id, mapping_id, date(2026, 8, day))

    with factory() as session, session.begin():
        outcome = close_reporting_months(session, today=date(2026, 9, 10))

    assert outcome.finalised == 1
    assert outcome.as_result()["months_closed"] == ["2026-08"]

    with factory() as session:
        final = session.get(ReportSnapshot, outcome.snapshots[0])
        assert final.payload_json["reporting_state"] == "final"
        assert final.payload_json["observed_source_days"] == 31
        # Money appears only now.
        assert final.payload_json["savings_eur"] is not None

        # And the provisional snapshot is exactly as it was.
        old = session.get(ReportSnapshot, provisional_id)
        assert old.snapshot_digest == provisional_digest
        assert old.payload_json["reporting_state"] == "provisional"
        assert old.payload_json["observed_source_days"] == 26
        assert old.payload_json["savings_eur"] is None
        # A new content identity, not a rewritten one.
        assert final.snapshot_digest != provisional_digest


def test_running_the_close_again_over_unchanged_final_input_is_a_no_op(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id, absent=())
        generate(session, asset_id, "2026-08", date(2026, 8, 31))

    with factory() as session, session.begin():
        first = close_reporting_months(session, today=date(2026, 9, 10))
    with factory() as session:
        after_first = session.scalar(select(func.count()).select_from(ReportSnapshot))

    with factory() as session, session.begin():
        second = close_reporting_months(session, today=date(2026, 9, 10))
    with factory() as session:
        after_second = session.scalar(select(func.count()).select_from(ReportSnapshot))

    assert first.finalised == 1
    # Nothing left provisional, so the second pass has nothing to examine.
    assert second.examined == 0
    assert second.finalised == 0
    assert after_second == after_first


def test_a_month_that_has_not_ended_is_never_closed_by_the_calendar(prepared) -> None:
    """Every day present, month still running: the pass must not finalise it."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id, absent=())
        generate(session, asset_id, "2026-08", date(2026, 8, 31))

    with factory() as session, session.begin():
        outcome = close_reporting_months(session, today=date(2026, 8, 31))

    assert outcome.finalised == 0
    assert outcome.still_provisional == 1


def test_the_pass_stops_tracking_a_period_once_it_has_a_final_snapshot(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id, absent=())
        generate(session, asset_id, "2026-08", date(2026, 8, 31))
    with factory() as session, session.begin():
        close_reporting_months(session, today=date(2026, 9, 10))

    with factory() as session:
        assert provisional_periods(session) == []


def test_the_close_writes_no_dataset_for_a_month_it_cannot_finalise(prepared) -> None:
    """The evaluation is cheap on purpose: `build_dataset` inserts every call."""
    from nemsei.reporting.models import ReportingDataset

    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        august_missing(session, asset_id, mapping_id)
        generate(session, asset_id, "2026-08", date(2026, 8, 31))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(ReportingDataset))

    for _ in range(3):
        with factory() as session, session.begin():
            close_reporting_months(session, today=date(2026, 9, 10))

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ReportingDataset)) == before


def test_the_job_type_is_wired_and_makes_no_provider_call() -> None:
    """The handler dispatches without settings, which is the proof it is provider-free."""
    import inspect

    from nemsei.jobs import handlers

    source = inspect.getsource(handlers._execute_report_month_close)
    assert "settings" not in source
    assert "close_reporting_months" in source
