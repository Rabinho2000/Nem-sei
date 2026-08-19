"""The portfolio screens render, filter, and never leak a wrong total."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.portfolios.datasets import build_portfolio_dataset
from nemsei.portfolios.service import add_member, create_portfolio, freeze_snapshot
from nemsei.providers.service import create_connection, create_mapping
from nemsei.web.portfolio_queries import SECTIONS


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return create_app(settings)


@pytest.fixture
def seeded(app, settings):
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="Solcorelios I", created_by="op")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        reporting = create_asset(
            session, canonical_name="Reporting Plant", country_code="PT",
            installed_dc_power_kw=Decimal("120.5"),
        )
        mapping = create_mapping(
            session, asset_id=reporting.id, provider_connection_id=connection.id, external_id="NE=1"
        )
        record_production_fact(
            session, asset_id=reporting.id, provider_mapping_id=mapping.id,
            source_fact_key="p:1", period_start=datetime.combine(date(2026, 3, 10), time.min, tzinfo=timezone.utc),
            period_end=datetime.combine(date(2026, 3, 11), time.min, tzinfo=timezone.utc),
            granularity="day", value=Decimal("1000"), unit="kWh",
            quality="complete", completeness="complete", metadata={},
        )
        silent = create_asset(session, canonical_name="Silent Plant", country_code="ES")
        for asset in (reporting, silent):
            add_member(
                session, portfolio_id=portfolio.id, asset_id=asset.id,
                valid_from=date(2026, 1, 1), created_by="op",
            )
        add_member(
            session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="op",
            sub_account="040", external_name="Consumidor Final", tax_id="999999990",
        )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        build_portfolio_dataset(session, snapshot=snapshot, built_by="op")
        portfolio_id = portfolio.id
    return portfolio_id


def login(client) -> None:
    """Set the session directly, as the other web tests do."""
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "admin"


def test_every_section_renders(app, seeded) -> None:
    client = app.test_client()
    login(client)
    for key, label in SECTIONS:
        response = client.get(f"/portfolios/{seeded}/{key}?month=2026-03")
        assert response.status_code == 200, f"{key} did not render"
        assert "Solcorelios I" in response.get_data(as_text=True)


def test_an_unknown_section_is_not_invented(app, seeded) -> None:
    client = app.test_client()
    login(client)
    assert client.get(f"/portfolios/{seeded}/nonsense").status_code == 404


def test_the_screens_require_authentication(app, seeded) -> None:
    response = app.test_client().get(f"/portfolios/{seeded}")
    assert response.status_code in (302, 401)


def test_the_overview_names_the_installation_needing_attention(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/overview?month=2026-03").get_data(as_text=True)
    assert "Silent Plant" in body
    assert "Precisam de atenção" in body
    # The coverage the operator asks for, stated plainly.
    assert "1/2" in body


def test_a_partial_total_is_labelled_partial_rather_than_shown_as_complete(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/production?month=2026-03").get_data(as_text=True)
    assert "1 000" in body or "1,000" in body or "1000" in body
    assert "parcial" in body


def test_country_is_a_filter_and_not_a_sub_portfolio(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03&country_code=PT").get_data(as_text=True)
    assert "Reporting Plant" in body
    assert "Silent Plant" not in body
    # Filtering never creates another portfolio.
    assert client.get("/portfolios").get_data(as_text=True).count("Solcorelios I") == 1


def test_the_attention_filter_narrows_to_what_is_wrong(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03&attention=1").get_data(as_text=True)
    assert "Silent Plant" in body
    assert "Reporting Plant" not in body


def test_an_unresolved_member_is_shown_as_unresolved_not_as_a_plant(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03").get_data(as_text=True)
    assert "Consumidor Final" in body
    assert "por resolver" in body


def test_a_period_without_a_dataset_says_so_instead_of_showing_zeros(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/overview?month=2026-09").get_data(as_text=True)
    assert "Sem dados construídos" in body
    assert "Construir" in body
