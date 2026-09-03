"""Fleet-wide ESCO: "instalação ESCO" is whatever the billing configuration
itself says, never a second guess from `contracts.priority` or
`report_type_for`'s contract-text heuristic.
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
from nemsei.reporting.commercial import set_billing_config
from nemsei.shared.clock import utc_now
from nemsei.web.esco_queries import esco_page


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


def _fact(*, asset_id, mapping_id, key, metric_kind, value, on: date):
    start = datetime.combine(on, datetime.min.time(), tzinfo=timezone.utc)
    return ProductionFact(
        asset_id=asset_id, provider_mapping_id=mapping_id, source_fact_key=key, source_revision=0,
        metric_kind=metric_kind, period_start=start, period_end=start + timedelta(days=1),
        granularity="day", value=Decimal(str(value)), unit="kWh", quality="complete", completeness="complete",
        ingested_at=utc_now(), metadata_json={},
    )


def test_esco_page_is_empty_when_no_asset_has_an_esco_billing_config(factory) -> None:
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Alpha")

    with factory() as session:
        result = esco_page(session, on=date(2026, 6, 15))

    assert result["rows"] == []
    assert result["asset_count"] == 0


def test_esco_page_follows_the_billing_config_not_the_contract_type_guess(factory) -> None:
    """`asset.contract_type` says nothing ESCO-shaped at all; the billing
    configuration is what actually drives `calculate_billing`'s ESCO
    branch, and that is the only signal this page is allowed to trust."""
    today = date(2026, 6, 15)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central X")
        asset.contract_type = "Sem classificação"
        set_billing_config(
            session, asset_id=asset.id, report_type="esco", valid_from=date(2026, 1, 1), created_by="op",
            solcor_price_per_kwh=Decimal("0.10"), default_electricity_price=Decimal("0.06"),
            default_export_price=Decimal("0.045"),
        )
        mapping = _mapping(session, asset_id=asset.id)
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", metric_kind="production_energy", value="1000", on=today))
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="s:1", metric_kind="self_use_energy", value="600", on=today))
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="e:1", metric_kind="export_energy", value="400", on=today))

    with factory() as session:
        result = esco_page(session, on=today)

    assert result["asset_count"] == 1
    assert result["rows"][0]["name"] == "Central X"


def test_esco_page_computes_solcor_revenue_and_customer_savings(factory) -> None:
    today = date(2026, 6, 15)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central Y")
        set_billing_config(
            session, asset_id=asset.id, report_type="esco", valid_from=date(2026, 1, 1), created_by="op",
            solcor_price_per_kwh=Decimal("0.10"), default_electricity_price=Decimal("0.06"),
            default_export_price=Decimal("0.045"),
        )
        mapping = _mapping(session, asset_id=asset.id)
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="p:1", metric_kind="production_energy", value="1000", on=today))
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="s:1", metric_kind="self_use_energy", value="600", on=today))
        session.add(_fact(asset_id=asset.id, mapping_id=mapping.id, key="e:1", metric_kind="export_energy", value="400", on=today))

    with factory() as session:
        result = esco_page(session, on=today)

    row = result["rows"][0]
    billing = row["billing"]
    # Solcor revenue: 600 kWh autoconsumo * 0.10 EUR/kWh (energy billing mode, self-consumption base).
    assert billing.solcor_payment_eur == Decimal("60.0")
    # Poupança bruta: 600*0.06 + 400*0.045 = 36 + 18 = 54; líquida = 54 - 60 = -6.
    assert billing.net_benefit_eur == Decimal("-6.0")
    assert result["totals"]["solcor_revenue_eur"] == Decimal("60.0")


def test_esco_page_shows_installations_with_a_config_but_no_production_this_month(factory) -> None:
    today = date(2026, 6, 15)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central Sem Leitura")
        set_billing_config(session, asset_id=asset.id, report_type="esco", valid_from=date(2026, 1, 1), created_by="op")

    with factory() as session:
        result = esco_page(session, on=today)

    assert result["asset_count"] == 1
    assert result["billed_count"] == 0
    assert result["rows"][0]["billing"] is None
    assert result["totals"] is None


def test_the_esco_page_renders(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(create_engine(settings.database_url))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Z")
        set_billing_config(session, asset_id=asset.id, report_type="esco", valid_from=date(2026, 1, 1), created_by="op")
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/esco")
    assert response.status_code == 200
    assert "ESCO" in response.text
    assert "Central Z" in response.text
