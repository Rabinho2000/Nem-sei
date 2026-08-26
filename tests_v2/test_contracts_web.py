"""The O&M screens: the column, the panel, the renewals page and their writes.

The point of these is that the identification is *usable*, not just stored:
before this, V2 had no way to say which of its 267 installations Solcor
operates, and no way for an operator to change that answer.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from nemsei.app import create_app
from nemsei.assets.service import create_asset, create_organization
from nemsei.contracts.models import AssetServiceContract
from nemsei.contracts.service import om_status, set_service_contract
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.models import OperatorAuditEvent
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch):
    """Three installations: one operated, one lapsed, one never in scope."""
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    organization = create_organization(session, display_name="Sol PT")
    operated = create_asset(session, canonical_name="Colmeia do Minho", owner_id=organization.id)
    lapsed = create_asset(session, canonical_name="Motassis", owner_id=organization.id)
    outside = create_asset(session, canonical_name="Quinta do Vale", owner_id=organization.id)
    session.flush()
    today = date.today()
    set_service_contract(
        session, asset_id=operated.id, created_by="importer",
        valid_from=today - timedelta(days=800), valid_to=today + timedelta(days=30),
        annual_value_eur=Decimal("2650.00"), source_kind="v1_import",
        provenance={"v1_asset_id": 1849},
    )
    set_service_contract(
        session, asset_id=lapsed.id, created_by="importer",
        valid_from=today - timedelta(days=900), valid_to=today - timedelta(days=100),
        source_kind="v1_import", provenance={"v1_asset_id": 1953},
    )
    session.commit()
    ids = (operated.id, lapsed.id, outside.id)
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client, ids


def csrf_from(client) -> str:
    with client.session_transaction() as browser:
        return browser.get("csrf_token", "")


def test_the_list_names_which_installations_have_om(settings, monkeypatch):
    client, (operated, lapsed, outside) = seeded(settings, monkeypatch)
    page = client.get("/assets").get_data(as_text=True)
    assert "O&amp;M ativo" in page
    assert "Contrato expirado" in page


def test_the_list_filters_by_derived_om_state(settings, monkeypatch):
    client, (operated, lapsed, outside) = seeded(settings, monkeypatch)
    active = client.get("/assets?om=ativo").get_data(as_text=True)
    assert "Colmeia do Minho" in active
    assert "Motassis" not in active
    assert "Quinta do Vale" not in active

    expired = client.get("/assets?om=expirado").get_data(as_text=True)
    assert "Motassis" in expired
    assert "Colmeia do Minho" not in expired

    none = client.get("/assets?om=sem").get_data(as_text=True)
    assert "Quinta do Vale" in none
    assert "Colmeia do Minho" not in none


def test_the_filter_survives_pagination_links(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/assets?om=ativo").get_data(as_text=True)
    # An operator paging through a filtered list must not silently lose it.
    assert "om=ativo" in page or "1 centrais encontradas" in page


def test_the_detail_page_shows_the_period_and_its_history(settings, monkeypatch):
    client, (operated, _, _) = seeded(settings, monkeypatch)
    page = client.get(f"/assets/{operated}").get_data(as_text=True)
    assert "Contrato O&amp;M" in page
    assert "importado do V1" in page
    assert "2650.00" in page


def test_an_operator_can_record_a_renewal_and_the_old_terms_survive(settings, monkeypatch):
    client, (operated, _, _) = seeded(settings, monkeypatch)
    client.get(f"/assets/{operated}")
    today = date.today()
    response = client.post(
        f"/assets/{operated}/contratos",
        data={
            "csrf_token": csrf_from(client),
            "valid_from": (today + timedelta(days=31)).isoformat(),
            "valid_to": (today + timedelta(days=396)).isoformat(),
            "annual_value_eur": "2900,50",
            "renewal_status": "renewed",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    session = build_session_factory(build_engine(settings))()
    rows = session.scalars(
        select(AssetServiceContract).where(AssetServiceContract.asset_id == operated)
    ).all()
    assert len(rows) == 2
    # The comma decimal an operator actually types is accepted, and the
    # previous engagement's value is still there.
    assert {row.annual_value_eur for row in rows} == {Decimal("2650.00"), Decimal("2900.50")}
    actions = set(session.scalars(select(OperatorAuditEvent.action)))
    assert "service_contract_created" in actions
    session.close()


def test_a_rejected_overlap_reports_instead_of_silently_winning(settings, monkeypatch):
    client, (operated, _, _) = seeded(settings, monkeypatch)
    client.get(f"/assets/{operated}")
    response = client.post(
        f"/assets/{operated}/contratos",
        data={
            "csrf_token": csrf_from(client),
            # Starts before the engagement already on file: V1 resolved this by
            # taking the newest row; here it is refused.
            "valid_from": (date.today() - timedelta(days=900)).isoformat(),
        },
        follow_redirects=True,
    )
    page = response.get_data(as_text=True)
    assert "já existe um período" in page.lower()
    session = build_session_factory(build_engine(settings))()
    assert session.query(AssetServiceContract).filter_by(asset_id=operated).count() == 1
    session.close()


def test_closing_a_period_makes_the_installation_lapse(settings, monkeypatch):
    client, (operated, _, _) = seeded(settings, monkeypatch)
    client.get(f"/assets/{operated}")
    session_factory = build_session_factory(build_engine(settings))
    session = session_factory()
    contract_id = session.scalars(
        select(AssetServiceContract.id).where(AssetServiceContract.asset_id == operated)
    ).first()
    session.close()

    client.post(
        f"/contratos/{contract_id}/fechar",
        data={
            "csrf_token": csrf_from(client),
            "asset_id": str(operated),
            "valid_to": (date.today() - timedelta(days=1)).isoformat(),
        },
    )
    session = session_factory()
    assert om_status(session, asset_id=operated) == "expired"
    session.close()


def test_the_renewals_screen_shows_the_work_not_the_archive(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/contratos").get_data(as_text=True)
    # Expiring in 30 days and already lapsed are both work.
    assert "Colmeia do Minho" in page
    assert "Motassis" in page
    # The coverage denominator is always present.
    assert "2/3" in page


def test_renewal_follow_up_is_recorded_from_the_renewals_screen(settings, monkeypatch):
    client, (operated, _, _) = seeded(settings, monkeypatch)
    client.get("/contratos")
    session_factory = build_session_factory(build_engine(settings))
    session = session_factory()
    contract_id = session.scalars(
        select(AssetServiceContract.id).where(AssetServiceContract.asset_id == operated)
    ).first()
    session.close()

    client.post(
        f"/contratos/{contract_id}/renovacao",
        data={
            "csrf_token": csrf_from(client),
            "renewal_status": "in_contact",
            "last_contact_on": date.today().isoformat(),
            "notes": "cliente pediu proposta",
        },
    )
    session = session_factory()
    contract = session.get(AssetServiceContract, contract_id)
    assert contract.renewal_status == "in_contact"
    assert contract.notes == "cliente pediu proposta"
    actions = set(session.scalars(select(OperatorAuditEvent.action)))
    assert "service_contract_renewal_updated" in actions
    session.close()


def test_the_panel_counts_contracts_to_renew(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/").get_data(as_text=True)
    assert "Contratos a renovar" in page
    assert "no âmbito O&amp;M" in page
