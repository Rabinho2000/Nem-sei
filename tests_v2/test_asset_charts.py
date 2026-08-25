"""Fase 2: the installation page draws its own production, from canonical facts.

The series layer must reduce `production_facts` to its newest revision before
totalling anything. Summing the raw rows adds a corrected value to the value it
was meant to replace -- the defect that once reported 129.28 kWh for a day that
produced 59.56 -- so there is a test here that supersedes a fact on purpose.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.web.series import daily_series, energy_balance, headline, month_calendar
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    asset = create_asset(session, canonical_name="Central Gráfica", timezone="Europe/Lisbon")
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="k", display_name="FS",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
    session.commit()
    ids = (asset.id, mapping.id)
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    return client, ids


def add_fact(settings, *, asset_id, mapping_id, day: date, value, revision=1, metric="production_energy", key=None):
    session = build_session_factory(build_engine(settings))()
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    session.add(
        ProductionFact(
            asset_id=asset_id, provider_mapping_id=mapping_id,
            source_fact_key=key or f"{metric}:{day.isoformat()}", source_revision=revision,
            metric_kind=metric, period_start=start, period_end=start + timedelta(days=1),
            granularity="day", value=Decimal(str(value)) if value is not None else None,
            unit="kWh", quality="complete" if value is not None else "missing",
            completeness="complete", ingested_at=utc_now(), metadata_json={},
        )
    )
    session.commit()
    session.close()


def test_a_superseded_fact_is_not_added_to_the_one_that_replaced_it(settings, monkeypatch) -> None:
    client, (asset_id, mapping_id) = seeded(settings, monkeypatch)
    yesterday = utc_now().date() - timedelta(days=1)
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=yesterday, value="129.28", revision=1)
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=yesterday, value="59.56", revision=2)

    session = build_session_factory(build_engine(settings))()
    result = daily_series(session, asset_id=asset_id, days=7)
    session.close()

    # 188.84 would be the raw sum of both rows. Only the current revision counts.
    assert abs(result["total"] - 59.56) < 0.01
    assert result["days_with_data"] == 1


def test_days_without_a_fact_stay_missing_rather_than_zero(settings, monkeypatch) -> None:
    client, (asset_id, mapping_id) = seeded(settings, monkeypatch)
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=utc_now().date() - timedelta(days=1), value="10")

    session = build_session_factory(build_engine(settings))()
    result = daily_series(session, asset_id=asset_id, days=7)
    session.close()

    missing = [bar.point.missing for bar in result["chart"].bars]
    assert missing.count(True) == 6
    assert missing.count(False) == 1


def test_the_calendar_counts_only_days_that_reported(settings, monkeypatch) -> None:
    client, (asset_id, mapping_id) = seeded(settings, monkeypatch)
    today = utc_now().date()
    for offset in (0, 1, 2):
        day = date(today.year, today.month, 1) + timedelta(days=offset)
        add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=day, value="20")

    session = build_session_factory(build_engine(settings))()
    result = month_calendar(session, asset_id=asset_id, year=today.year, month=today.month)
    session.close()

    assert result["with_data"] == 3
    assert result["calendar"].empty is False


def test_the_energy_balance_reads_each_metric_separately(settings, monkeypatch) -> None:
    client, (asset_id, mapping_id) = seeded(settings, monkeypatch)
    today = utc_now().date()
    first = date(today.year, today.month, 1)
    for metric, value in (("production_energy", "100"), ("self_use_energy", "60"), ("export_energy", "40"), ("grid_import_energy", "25"), ("consumption_energy", "85")):
        add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=first, value=value, metric=metric)

    session = build_session_factory(build_engine(settings))()
    nxt = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    result = energy_balance(session, asset_id=asset_id, start=first, end=nxt)
    session.close()

    assert result["metrics"]["production_energy"] == 100
    assert abs(result["self_use_share"] - 0.6) < 0.001
    assert result["stack"].empty is False


def test_an_installation_with_no_facts_says_so_without_pretending(settings, monkeypatch) -> None:
    client, (asset_id, _) = seeded(settings, monkeypatch)

    session = build_session_factory(build_engine(settings))()
    summary = headline(session, asset_id=asset_id)
    session.close()

    assert summary["total_kwh"] is None
    assert summary["latest_fact_on"] is None
    assert summary["stale_days"] is None
    assert summary["spark"].empty is True


def test_the_asset_page_renders_charts_and_never_a_zero_for_a_gap(settings, monkeypatch) -> None:
    client, (asset_id, mapping_id) = seeded(settings, monkeypatch)
    add_fact(settings, asset_id=asset_id, mapping_id=mapping_id, day=utc_now().date() - timedelta(days=1), value="42.5")

    page = client.get(f"/assets/{asset_id}")

    assert page.status_code == 200
    assert 'class="chart"' in page.text
    assert "Produção mensal" in page.text
    assert "Cobertura do mês" in page.text
    assert "Balanço energético" in page.text
    # The honest-gap mark must be present for the days with no reading.
    assert 'class="c-void"' in page.text
    assert "sem leitura" in page.text


def test_an_asset_with_no_production_still_renders_the_page(settings, monkeypatch) -> None:
    client, (asset_id, _) = seeded(settings, monkeypatch)

    page = client.get(f"/assets/{asset_id}")

    assert page.status_code == 200
    assert "Sem dados para desenhar" in page.text or "Nenhum dia deste mês tem leituras" in page.text
