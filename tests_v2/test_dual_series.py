"""Produção × Consumo: two metrics, one shared scale, real facts only.

Reuses `test_asset_charts.py`'s fixture shape: a real Postgres, real
`ProductionFact` rows, no mocking of the fact repository. The dedicated chart
geometry tests (`test_charts.py`) already prove `dual_bar_chart`'s arithmetic;
these prove the query layer feeds it correctly from `production_facts`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.web.series import (
    PRODUCTION_CONSUMPTION_PERIODS,
    dual_daily_series,
    dual_monthly_series,
    production_consumption_series,
)
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    asset = create_asset(session, canonical_name="Central Dupla", timezone="Europe/Lisbon")
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="k", display_name="FS",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
    session.commit()
    ids = (asset.id, mapping.id)
    session.close()
    return ids


def add_fact(settings, *, asset_id, mapping_id, day: date, value, metric):
    session = build_session_factory(build_engine(settings))()
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    session.add(
        ProductionFact(
            asset_id=asset_id, provider_mapping_id=mapping_id,
            source_fact_key=f"{metric}:{day.isoformat()}", source_revision=1,
            metric_kind=metric, period_start=start, period_end=start + timedelta(days=1),
            granularity="day", value=Decimal(str(value)) if value is not None else None,
            unit="kWh", quality="complete" if value is not None else "missing",
            completeness="complete", ingested_at=utc_now(), metadata_json={},
        )
    )
    session.commit()
    session.close()


def test_production_and_consumption_come_from_their_own_metric_kinds(settings, monkeypatch) -> None:
    asset_id, mapping_id = seeded(settings, monkeypatch)
    today = utc_now().date()
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=100, metric="production_energy")
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=40, metric="consumption_energy")

    session = build_session_factory(build_engine(settings))()
    result = dual_daily_series(session, asset_id=asset_id, days=1)

    assert result["production_total"] == 100.0
    assert result["consumption_total"] == 40.0
    assert result["chart"].empty is False


def test_a_plant_with_production_but_no_consumption_meter_still_charts_production(settings, monkeypatch) -> None:
    """Most of the fleet has a production meter and no consumption meter at
    all -- this must not read as "no chart", only as an honest gap on one
    of the two series."""
    asset_id, mapping_id = seeded(settings, monkeypatch)
    today = utc_now().date()
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=100, metric="production_energy")

    session = build_session_factory(build_engine(settings))()
    result = dual_daily_series(session, asset_id=asset_id, days=1)

    assert result["production_total"] == 100.0
    assert result["consumption_total"] is None
    assert result["chart"].production_bars[0].point.missing is False
    assert result["chart"].consumption_bars[0].point.missing is True


def test_a_superseded_revision_is_not_summed_into_the_dual_chart(settings, monkeypatch) -> None:
    asset_id, mapping_id = seeded(settings, monkeypatch)
    today = utc_now().date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    session = build_session_factory(build_engine(settings))()
    session.add(
        ProductionFact(
            asset_id=asset_id, provider_mapping_id=mapping_id, source_fact_key="production_energy:x",
            source_revision=1, metric_kind="production_energy", period_start=start, period_end=start + timedelta(days=1),
            granularity="day", value=Decimal("59.56"), unit="kWh", quality="complete", completeness="complete",
            ingested_at=utc_now(), metadata_json={},
        )
    )
    session.add(
        ProductionFact(
            asset_id=asset_id, provider_mapping_id=mapping_id, source_fact_key="production_energy:x",
            source_revision=2, metric_kind="production_energy", period_start=start, period_end=start + timedelta(days=1),
            granularity="day", value=Decimal("129.28"), unit="kWh", quality="complete", completeness="complete",
            ingested_at=utc_now(), metadata_json={},
        )
    )
    session.commit()
    session.close()

    session = build_session_factory(build_engine(settings))()
    result = dual_daily_series(session, asset_id=asset_id, days=1)
    assert result["production_total"] == 129.28


def test_monthly_dual_series_uses_the_same_month_buckets_as_the_single_series(settings, monkeypatch) -> None:
    asset_id, mapping_id = seeded(settings, monkeypatch)
    today = utc_now().date()
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=500, metric="production_energy")
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=200, metric="consumption_energy")

    session = build_session_factory(build_engine(settings))()
    result = dual_monthly_series(session, asset_id=asset_id, months=3)

    assert result["months"] == 3
    assert result["chart"].empty is False
    assert len(result["chart"].production_bars) == 3
    # Caught by the installation detail page's own smoke test rendering
    # `production_chart.production_total` for period="year": this function
    # returned no such key at all, and Jinja's Undefined has no __format__.
    assert result["production_total"] == 500.0
    assert result["consumption_total"] == 200.0


def test_monthly_dual_series_with_no_facts_reports_none_not_a_missing_key(settings, monkeypatch) -> None:
    asset_id, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    result = dual_monthly_series(session, asset_id=asset_id, months=3)
    assert result["production_total"] is None
    assert result["consumption_total"] is None


def test_an_installation_with_no_facts_at_all_is_empty_not_zero(settings, monkeypatch) -> None:
    asset_id, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    result = dual_daily_series(session, asset_id=asset_id, days=7)
    assert result["production_total"] is None
    assert result["consumption_total"] is None
    assert all(bar.point.missing for bar in result["chart"].production_bars)


def test_every_period_option_resolves_to_a_chart_without_raising(settings, monkeypatch) -> None:
    asset_id, mapping_id = seeded(settings, monkeypatch)
    today = utc_now().date()
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=today, value=10, metric="production_energy")

    session = build_session_factory(build_engine(settings))()
    for period in PRODUCTION_CONSUMPTION_PERIODS:
        result = production_consumption_series(session, asset_id=asset_id, period=period)
        assert result["period"] == period
        assert "chart" in result


def test_an_unknown_period_falls_back_to_a_week_rather_than_raising(settings, monkeypatch) -> None:
    asset_id, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    result = production_consumption_series(session, asset_id=asset_id, period="nonsense")
    assert result["period"] == "week"
    assert result["days"] == 7
