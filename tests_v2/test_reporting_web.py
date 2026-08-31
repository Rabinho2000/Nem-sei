"""The top-level Reporting area: individual reports, generated and downloaded
from the browser rather than a Python shell."""
from __future__ import annotations

import re
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
from nemsei.providers.service import create_connection, create_mapping


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return create_app(settings)


@pytest.fixture
def asset_id(app, settings):
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Quinta do Relatório")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
        record_production_fact(
            session, asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="p:1",
            period_start=datetime.combine(date(2026, 3, 10), time.min, tzinfo=timezone.utc),
            period_end=datetime.combine(date(2026, 3, 11), time.min, tzinfo=timezone.utc),
            granularity="day", value=Decimal("250"), unit="kWh",
            quality="complete", completeness="complete", metadata={},
        )
    return asset.id


def login(client) -> None:
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "ines"


def csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def test_the_index_requires_authentication(app) -> None:
    assert app.test_client().get("/reports").status_code in (302, 401)


def test_the_index_renders_with_nothing_generated_yet(app) -> None:
    client = app.test_client()
    login(client)
    response = client.get("/reports")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Nenhum relatório gerado ainda." in body
    # The landing page leads with what can be produced, not with a file list.
    assert "Fecho de" in body


def test_the_index_counts_the_fleet_and_names_the_three_absences(app, asset_id) -> None:
    """One installation, no facts, no billing: blocked, and said so out loud."""
    client = app.test_client()
    login(client)
    body = client.get("/reports?month=2026-08").get_data(as_text=True)

    assert "Fecho de 2026-08" in body
    assert "Sem energia registada" in body
    assert "ESCO sem configuração comercial" in body
    assert "Por gerar" in body


def test_the_installation_list_filters_by_state(app, asset_id) -> None:
    client = app.test_client()
    login(client)

    blocked = client.get("/reports/assets?month=2026-08&state=blocked").get_data(as_text=True)
    assert "Quinta do Relatório" in blocked

    final = client.get("/reports/assets?month=2026-08&state=final").get_data(as_text=True)
    assert "Quinta do Relatório" not in final
    assert "Nenhuma instalação corresponde a estes filtros." in final


def test_the_installation_list_filters_by_contract(app, asset_id) -> None:
    client = app.test_client()
    login(client)
    esco = client.get("/reports/assets?month=2026-08&contract=esco").get_data(as_text=True)
    epc = client.get("/reports/assets?month=2026-08&contract=epc").get_data(as_text=True)
    # The fixture asset states no contract type, so it counts as neither ESCO
    # nor -- for the purposes of this screen -- a decided EPC. It must appear
    # in exactly one of the two lists rather than both.
    assert ("Quinta do Relatório" in esco) != ("Quinta do Relatório" in epc)


def test_the_asset_page_says_what_is_missing_and_offers_the_three_rates(app, asset_id) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/reports/assets/{asset_id}?month=2026-08").get_data(as_text=True)

    assert "Sem energia registada para o mês" in body
    assert "Sem configuração comercial" in body
    for label in ("Taxa de venda", "Taxa de poupança", "Venda de excedente"):
        assert label in body
    # Availability and the tariff-period splits are declared absent, never zero.
    assert "N/D — sem amostragem fiável" in body
    assert "N/D — nenhuma origem o indica" in body


def test_searching_assets_finds_it_by_name(app, asset_id) -> None:
    client = app.test_client()
    login(client)
    body = client.get("/reports/assets?search=Relatório").get_data(as_text=True)
    assert "Quinta do Relatório" in body


def test_an_unknown_asset_history_is_404(app) -> None:
    client = app.test_client()
    login(client)
    assert client.get("/reports/assets/999999").status_code == 404


def test_generating_from_the_browser_creates_a_downloadable_report(app, asset_id) -> None:
    client = app.test_client()
    login(client)
    form = client.get(f"/reports/assets/{asset_id}")
    assert "Quinta do Relatório" in form.get_data(as_text=True)

    generate = client.post(
        f"/reports/assets/{asset_id}/generate", data={"csrf_token": csrf_token(form), "month": "2026-03"}
    )
    assert generate.status_code == 302

    history = client.get(f"/reports/assets/{asset_id}")
    body = history.get_data(as_text=True)
    assert "2026-03" in body
    match = re.search(rf"/reports/assets/{asset_id}/snapshots/(\d+)\.pdf", body)
    assert match, "no PDF download link on the history page"
    snapshot_id = match.group(1)

    pdf = client.get(f"/reports/assets/{asset_id}/snapshots/{snapshot_id}.pdf")
    assert pdf.status_code == 200
    assert pdf.data[:5] == b"%PDF-"

    xlsx = client.get(f"/reports/assets/{asset_id}/snapshots/{snapshot_id}.xlsx")
    assert xlsx.status_code == 200
    assert xlsx.data[:2] == b"PK"  # an xlsx is a zip archive


def test_generating_the_same_period_twice_returns_the_same_report(app, asset_id) -> None:
    client = app.test_client()
    login(client)
    form = client.get(f"/reports/assets/{asset_id}")
    for _ in range(2):
        client.post(f"/reports/assets/{asset_id}/generate", data={"csrf_token": csrf_token(form), "month": "2026-03"})
    body = client.get(f"/reports/assets/{asset_id}").get_data(as_text=True)
    assert body.count("2026-03") == 1


def test_a_snapshot_belonging_to_another_asset_is_not_served(app, asset_id, settings) -> None:
    """The download route is keyed by asset id in the URL; it must actually check it."""
    client = app.test_client()
    login(client)
    form = client.get(f"/reports/assets/{asset_id}")
    client.post(f"/reports/assets/{asset_id}/generate", data={"csrf_token": csrf_token(form), "month": "2026-03"})
    body = client.get(f"/reports/assets/{asset_id}").get_data(as_text=True)
    snapshot_id = re.search(rf"/reports/assets/{asset_id}/snapshots/(\d+)\.pdf", body).group(1)

    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        other = create_asset(session, canonical_name="A Different Installation")
        other_id = other.id

    assert client.get(f"/reports/assets/{other_id}/snapshots/{snapshot_id}.pdf").status_code == 404
