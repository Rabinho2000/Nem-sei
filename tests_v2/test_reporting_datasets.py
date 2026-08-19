from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.reporting.datasets import build_dataset, digest_of, month_starts, snapshot_dataset
from nemsei.reporting.models import ReportSnapshot, ReportingDataset, ReportingDatasetRow
from nemsei.providers.service import create_connection, create_mapping


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


@pytest.fixture
def prepared(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
        ids = (asset.id, mapping.id)
    return factory, ids


def add_fact(session, asset_id, mapping_id, day: date, value, *, quality="complete"):
    return record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"test:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        value=None if value is None else Decimal(str(value)),
        unit="kWh",
        quality=quality,
        completeness="complete" if quality == "complete" else "partial",
        metadata={},
    )


def test_month_boundaries_cover_the_period_exactly() -> None:
    assert month_starts(date(2026, 1, 1), date(2026, 4, 1)) == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    assert month_starts(date(2026, 11, 15), date(2027, 2, 1)) == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]
    with pytest.raises(ValueError, match="end after it starts"):
        month_starts(date(2026, 2, 1), date(2026, 2, 1))


def test_a_month_without_facts_stays_missing_and_never_becomes_zero(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 1, 10), "40")
        add_fact(session, asset_id, mapping_id, date(2026, 1, 11), "60")
        dataset = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 3, 1), built_by="operator")
        dataset_id = dataset.id

    with factory() as session:
        rows = session.scalars(
            select(ReportingDatasetRow).where(ReportingDatasetRow.dataset_id == dataset_id).order_by(ReportingDatasetRow.period_start)
        ).all()
        assert [row.period_start for row in rows] == [date(2026, 1, 1), date(2026, 2, 1)]
        assert rows[0].actual_production_kwh == Decimal("100") and rows[0].actual_state == "measured"
        # February has no facts at all. That is not a zero-production month.
        assert rows[1].actual_production_kwh is None and rows[1].actual_state == "missing"
        dataset = session.get(ReportingDataset, dataset_id)
        assert "actual_production_missing:2026-02-01" in dataset.warnings_json
        assert dataset.quality_json["months"] == 2 and dataset.quality_json["months_with_actual"] == 1


def test_the_database_refuses_a_missing_row_that_carries_a_value(prepared) -> None:
    factory, (asset_id, _) = prepared
    with factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO reporting_datasets (scope, asset_id, period_start, period_end, status, input_digest,"
                    " quality_json, warnings_json, built_at, built_by)"
                    " VALUES ('asset', :asset, '2026-01-01', '2026-02-01', 'ready', 'd', '{}', '[]', now(), 'op')"
                    " RETURNING id"
                ),
                {"asset": asset_id},
            ).scalar_one() and session.execute(
                text(
                    "INSERT INTO reporting_dataset_rows (dataset_id, asset_id, period_start, period_end,"
                    " actual_production_kwh, actual_state, expected_state, provenance_json)"
                    " SELECT id, :asset, '2026-01-01', '2026-02-01', 10, 'missing', 'missing', '{}' FROM reporting_datasets LIMIT 1"
                ),
                {"asset": asset_id},
            )
        session.rollback()


def test_a_partial_month_is_marked_partial_rather_than_silently_totalled(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 1, 10), "40")
        add_fact(session, asset_id, mapping_id, date(2026, 1, 11), None, quality="missing")
        dataset = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 2, 1), built_by="operator")
        dataset_id = dataset.id
    with factory() as session:
        row = session.scalar(select(ReportingDatasetRow).where(ReportingDatasetRow.dataset_id == dataset_id))
        assert row.actual_production_kwh == Decimal("40")
        assert row.actual_state == "partial"


def test_rebuilding_from_the_same_facts_yields_the_same_digest(prepared) -> None:
    """Reproducibility is the whole point: same facts in, same dataset identity."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 1, 10), "40")
        first = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 2, 1), built_by="operator")
        first_digest = first.input_digest
    with factory() as session, session.begin():
        second = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 2, 1), built_by="someone else")
        assert second.input_digest == first_digest
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 1, 12), "5")
        third = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 2, 1), built_by="operator")
        assert third.input_digest != first_digest


def test_a_snapshot_is_reused_for_identical_input_and_frozen_afterwards(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 1, 10), "40")
        dataset = build_dataset(session, asset_id=asset_id, period_start=date(2026, 1, 1), period_end=date(2026, 2, 1), built_by="operator")
        payload = {"rows": [{"asset_id": asset_id, "production_kwh": "40"}]}
        first = snapshot_dataset(session, dataset=dataset, payload=payload, created_by="operator")
        again = snapshot_dataset(session, dataset=dataset, payload=payload, created_by="operator")
        assert again.id == first.id
        changed = snapshot_dataset(session, dataset=dataset, payload={"rows": []}, created_by="operator")
        assert changed.id != first.id
        snapshot_id = first.id

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ReportSnapshot)) == 2
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("UPDATE report_snapshots SET payload_json = '{}' WHERE id = :id"), {"id": snapshot_id})
        session.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("DELETE FROM report_snapshots WHERE id = :id"), {"id": snapshot_id})
        session.rollback()


def test_the_digest_ignores_ordering_but_not_content() -> None:
    assert digest_of({"a": 1, "b": 2}) == digest_of({"b": 2, "a": 1})
    assert digest_of({"a": 1}) != digest_of({"a": 2})


def test_dataset_building_never_reaches_a_provider() -> None:
    """The report path must be answerable from the database alone."""
    import inspect

    from nemsei.reporting import datasets

    source = inspect.getsource(datasets)
    for forbidden in ("integrations", "requests", "urllib", "http", "FusionSolar", "provider_reads"):
        assert forbidden not in source, forbidden
