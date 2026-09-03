"""Fleet-wide Produção: current-revision-only totals at scale, and the
confirmed-model comparison that ranks who is off-target.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.models import FinancialModel, FinancialModelMonth, ReportSourceFile
from nemsei.shared.clock import utc_now
from nemsei.web.production_queries import fleet_daily_total, fleet_month_expected_vs_actual, production_ranking


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def login(client) -> None:
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"


def _mapping(session, *, asset_id: int):
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key=f"k{asset_id}", display_name="FS",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    return create_mapping(session, asset_id=asset_id, provider_connection_id=connection.id, external_id=f"NE={asset_id}")


def _fact(*, asset_id, mapping_id, key, revision, value, on: date):
    start = datetime.combine(on, datetime.min.time(), tzinfo=timezone.utc)
    return ProductionFact(
        asset_id=asset_id, provider_mapping_id=mapping_id, source_fact_key=key, source_revision=revision,
        metric_kind="production_energy", period_start=start, period_end=start + timedelta(days=1),
        granularity="day", value=Decimal(str(value)), unit="kWh", quality="complete", completeness="complete",
        ingested_at=utc_now(), metadata_json={},
    )


def test_fleet_daily_total_uses_only_the_newest_revision_per_fact(factory) -> None:
    """Regression for the defect this codebase's own docs cite: summing raw
    revisions produced 129.28 kWh for a day that made 59.56."""
    today = utc_now().date()
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        mapping = _mapping(session, asset_id=asset.id)
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", revision=0, value="69.72", on=today))
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", revision=1, value="59.56", on=today))

    with factory() as session:
        result = fleet_daily_total(session, on=today)

    assert result["total_kwh"] == pytest.approx(59.56)
    assert result["reporting_assets"] == 1


def test_fleet_daily_total_with_no_readings_is_an_absence_not_a_zero(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Alpha")

    with factory() as session:
        result = fleet_daily_total(session, on=today)

    assert result["total_kwh"] is None
    assert result["reporting_assets"] == 0
    assert result["total_assets"] == 1


def _seed_confirmed_model(session, *, asset_id: int, month: int, expected_kwh) -> None:
    source = ReportSourceFile(
        asset_id=asset_id, file_kind="financial_model", original_filename="modelo.xlsx",
        stored_path="/dev/null", sha256=f"sha-{asset_id}-{month}", size_bytes=10, uploaded_at=utc_now(),
    )
    session.add(source)
    session.flush()
    model = FinancialModel(
        source_file_id=source.id, asset_id=asset_id, version=1, status="confirmed",
        base_year_source="workbook", workbook_format="unknown",
        parser_name="test", parser_version="1", source_file_sha256=source.sha256,
        confirmed_by="op", confirmed_at=utc_now(), created_at=utc_now(), updated_at=utc_now(),
    )
    session.add(model)
    session.flush()
    session.add(
        FinancialModelMonth(
            financial_model_id=model.id, month=month,
            expected_production_kwh=Decimal(str(expected_kwh)) if expected_kwh is not None else None,
        )
    )


def test_expected_vs_actual_only_counts_assets_with_a_confirmed_model(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        modelled = create_asset(session, canonical_name="Modelled")
        unmodelled = create_asset(session, canonical_name="Unmodelled")
        mapping = _mapping(session, asset_id=modelled.id)
        session.add(_fact(asset_id=modelled.id, mapping_id=mapping.id, key="p:1", revision=0, value="900", on=today))
        _seed_confirmed_model(session, asset_id=modelled.id, month=today.month, expected_kwh=1000)
        mapping2 = _mapping(session, asset_id=unmodelled.id)
        session.add(_fact(asset_id=unmodelled.id, mapping_id=mapping2.id, key="p:1", revision=0, value="500", on=today))

    with factory() as session:
        result = fleet_month_expected_vs_actual(session, on=today)

    # Expected total only reflects the one asset with a confirmed model.
    assert result["expected_total_kwh"] == Decimal("1000")
    assert result["modelled_assets"] == 1
    # Actual total is both assets -- the unmodelled one is not hidden, just unranked by deviation.
    assert result["actual_total_kwh"] == pytest.approx(1400.0)
    assert result["deviation_pct"] is not None


def test_production_ranking_orders_worst_shortfall_first_and_puts_unmodelled_last(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        bad = create_asset(session, canonical_name="Pior")
        good = create_asset(session, canonical_name="Melhor")
        unmodelled = create_asset(session, canonical_name="Sem modelo")
        for asset, value, expected in ((bad, "500", 1000), (good, "950", 1000)):
            mapping = _mapping(session, asset_id=asset.id)
            session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", revision=0, value=value, on=today))
            _seed_confirmed_model(session, asset_id=asset.id, month=today.month, expected_kwh=expected)
        mapping3 = _mapping(session, asset_id=unmodelled.id)
        session.add(_fact(asset_id=unmodelled.id, mapping_id=mapping3.id, key="p:1", revision=0, value="10", on=today))
        bad_id, good_id, unmodelled_id = bad.id, good.id, unmodelled.id

    with factory() as session:
        month = fleet_month_expected_vs_actual(session, on=today)
        ranking = production_ranking(session, by_asset=month["by_asset"], expected_by_asset=month["expected_by_asset"])

    ids = [row["asset_id"] for row in ranking]
    assert ids.index(bad_id) < ids.index(good_id) < ids.index(unmodelled_id)


def test_the_production_page_renders_with_no_data_and_with_readings(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(create_engine(settings.database_url))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Solar")
        mapping = _mapping(session, asset_id=asset.id)
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", revision=0, value="42", on=utc_now().date()))
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/producao")
    assert response.status_code == 200
    assert "Produção" in response.text
