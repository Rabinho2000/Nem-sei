"""The operational screens render, before and after the Installation backfill,
for a fleet that mixes real faults, communication problems and coverage
gaps -- and never confuses the three on the page.
"""
from __future__ import annotations

from datetime import date, timedelta

from nemsei.app import create_app
from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.db import build_engine
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.shared.clock import utc_now
from nemsei.work_orders.service import create_work_order
from tests_v2.test_migrations import upgrade


def login(client) -> None:
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"


def seed_incident(session, *, asset_id: int, rule_code: str, severity: str = "warning") -> None:
    now = utc_now()
    session.add(
        DiagnosticIncident(
            rule_code=rule_code, asset_id=asset_id, severity=severity, status="open",
            opened_at=now, last_observed_at=now, detector_version="test", created_at=now, updated_at=now,
        )
    )


def test_the_installation_list_renders_with_a_mixed_fleet(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Mista")
        seed_incident(session, asset_id=asset.id, rule_code="device_unavailable", severity="critical")
        seed_incident(session, asset_id=asset.id, rule_code="stale_reading")
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/instalacoes")
    assert response.status_code == 200
    assert "Central Mista" in response.text
    # The word "Avaria" appears as a column header, distinct from "Cobertura".
    assert "Avaria" in response.text and "Cobertura" in response.text


def test_the_list_shows_the_whole_fleet_by_default_not_only_om_scope(settings, monkeypatch) -> None:
    """Regression for the `om=""` default that showed zero rows for an
    installation with no O&M contract at all."""
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        # No "&" in the name: Jinja autoescapes it to "&amp;" and a literal
        # match against the raw string would fail for a reason that has
        # nothing to do with the om filter this test actually checks.
        create_asset(session, canonical_name="Sem Contrato OM")
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/instalacoes")
    assert "Sem Contrato OM" in response.text


def test_the_detail_page_renders_before_the_backfill_has_run(settings, monkeypatch) -> None:
    """The real state of production today: no Installation exists yet."""
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Sem Instalacao")
        asset_id = asset.id
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get(f"/instalacoes/{asset_id}")
    assert response.status_code == 200
    assert "Sem Instalação associada" in response.text


def test_every_tab_renders_without_error(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Completa")
        create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        backfill_installations_from_assets(session)
        installation_id = installation_for_asset(session, asset_id=asset.id).id
        create_work_order(session, installation_id=installation_id, work_type="corrective", title="Reparar", created_by="op")
        asset_id = asset.id
    session.close()

    client = create_app(settings).test_client()
    login(client)
    for tab in ("resumo", "operacao", "trabalhos", "equipamentos", "performance", "timeline"):
        response = client.get(f"/instalacoes/{asset_id}", query_string={"tab": tab})
        assert response.status_code == 200, f"tab={tab} failed"


def test_a_nonexistent_installation_is_a_404(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    login(client)
    assert client.get("/instalacoes/999999").status_code == 404


def test_the_production_consumption_chart_renders_on_every_period(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Grafico")
        asset_id = asset.id
    session.close()

    client = create_app(settings).test_client()
    login(client)
    for period in ("today", "week", "month", "year"):
        response = client.get(f"/instalacoes/{asset_id}", query_string={"tab": "resumo", "period": period})
        assert response.status_code == 200, f"period={period} failed"


def test_the_work_orders_page_renders_and_lists_overdue_work(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Trabalho")
        backfill_installations_from_assets(session)
        installation_id = installation_for_asset(session, asset_id=asset.id).id
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Trocar Disjuntor",
            created_by="op", due_date=date.today() - timedelta(days=3),
        )
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/trabalhos")
    assert response.status_code == 200
    assert "Trocar Disjuntor" in response.text
    response = client.get("/trabalhos", query_string={"scope": "overdue"})
    assert response.status_code == 200
    assert "Trocar Disjuntor" in response.text


def test_the_nav_links_to_installations_not_only_to_assets(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/instalacoes"' in response.text
    assert 'href="/trabalhos"' in response.text


def test_an_unauthenticated_request_is_redirected_not_served(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    assert client.get("/instalacoes").status_code in (302, 401, 403)
    assert client.get("/trabalhos").status_code in (302, 401, 403)
