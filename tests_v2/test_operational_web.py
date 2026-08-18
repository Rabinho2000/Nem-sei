from __future__ import annotations

from decimal import Decimal

from nemsei.app import create_app
from nemsei.assets.service import add_alias, create_asset, create_organization
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.service import create_connection, create_mapping
from tests_v2.test_migrations import upgrade


def seeded_client(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    session = build_session_factory(engine)()
    organization = create_organization(session, display_name="Sol PT")
    reviewed = create_asset(
        session,
        canonical_name="Central Norte",
        owner_id=organization.id,
        installed_dc_power_kw=Decimal("120.5"),
        country_code="PT",
        locality="Braga",
        review_status="needs_review",
        review_note="País legacy requer confirmação.",
    )
    create_asset(session, canonical_name="Central Sul", owner_id=organization.id, locality="Faro")
    add_alias(session, asset_id=reviewed.id, alias="Norte antigo")
    connection = create_connection(
        session,
        provider_code="fusionsolar",
        connection_key="legacy",
        display_name="FusionSolar legacy",
        credential_reference="do-not-render",
        enabled=False,
        configuration_status="disabled",
    )
    create_mapping(
        session,
        asset_id=reviewed.id,
        provider_connection_id=connection.id,
        external_id="plant-123",
        mapping_status="pending_review",
    )
    session.commit()
    asset_id = reviewed.id
    session.close()
    app = create_app(settings)
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "admin"
    return client, asset_id


def test_authenticated_home_uses_real_counts_and_honest_empty_state(settings, monkeypatch) -> None:
    client, _ = seeded_client(settings, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert "2" in response.text
    assert "A monitorização ainda não foi ativada." in response.text
    assert "0 kWh" not in response.text
    assert "provider unavailable" not in response.text


def test_assets_search_and_filters_use_canonical_data(settings, monkeypatch) -> None:
    client, _ = seeded_client(settings, monkeypatch)

    alias_response = client.get("/assets?q=Norte+antigo")
    review_response = client.get("/assets?needs_review=yes")
    provider_response = client.get("/assets?provider=fusionsolar")
    absent_response = client.get("/assets?mapping=absent")

    assert "Central Norte" in alias_response.text
    assert "Central Sul" not in alias_response.text
    assert "Precisa de revisão" in review_response.text
    assert "FusionSolar" in provider_response.text
    assert "Central Sul" in absent_response.text
    assert "Central Norte" not in absent_response.text


def test_asset_detail_shows_mapping_and_empty_operational_states(settings, monkeypatch) -> None:
    client, asset_id = seeded_client(settings, monkeypatch)

    response = client.get(f"/assets/{asset_id}")

    assert response.status_code == 200
    assert "País legacy requer confirmação." in response.text
    assert "FusionSolar" in response.text
    assert "Pendente" in response.text
    assert "Ainda sem observações de monitorização." in response.text
    assert "Ainda sem dados de produção." in response.text
    assert "do-not-render" not in response.text


def test_organizations_and_provider_connections_are_operational_lists(settings, monkeypatch) -> None:
    client, _ = seeded_client(settings, monkeypatch)

    organizations = client.get("/organizations?q=Sol+PT")
    connections = client.get("/provider-connections")

    assert organizations.status_code == 200
    assert "Sol PT" in organizations.text
    assert "2" in organizations.text
    assert connections.status_code == 200
    assert "FusionSolar" in connections.text
    assert "Desativada" in connections.text
    assert "Não configuradas" in connections.text
    assert "do-not-render" not in connections.text


def test_read_only_reconciliation_exposes_import_counts(settings, monkeypatch) -> None:
    client, _ = seeded_client(settings, monkeypatch)

    response = client.get("/reconciliation")

    assert response.status_code == 200
    assert "Revisão de dados" in response.text
    assert "mappings pendentes" in response.text


def test_operational_routes_remain_authenticated(settings) -> None:
    client = create_app(settings).test_client()

    for path in "/", "/assets", "/organizations", "/provider-connections", "/reconciliation":
        assert client.get(path).status_code == 302
