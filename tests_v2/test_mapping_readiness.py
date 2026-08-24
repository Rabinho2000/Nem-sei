"""Bloco B: the mapping review screen knows what it can actually approve.

All 460 pending mappings in production sit on the two disabled V1-legacy
connections and every asset they point at is still `needs_review`, so a bulk
approve button on its own would have failed 460 times out of 460. The screen
now computes the refusal reasons from the very function `approve_mapping`
obeys, so it can never promise an approval the service would reject.
"""
from __future__ import annotations

from nemsei.app import create_app
from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset, create_organization
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.service import create_connection, create_mapping, mapping_approval_blockers
from tests_v2.test_migrations import upgrade


def build(settings, monkeypatch, *, asset_reviewed: bool, connection_ready: bool):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    organization = create_organization(session, display_name="Sol PT")
    asset = create_asset(
        session,
        canonical_name="Central Pendente",
        owner_id=organization.id,
        timezone="Europe/Lisbon",
        review_status="clear" if asset_reviewed else "needs_review",
    )
    connection = create_connection(
        session,
        provider_code="fusionsolar",
        connection_key="legacy" if not connection_ready else "live",
        display_name="FusionSolar",
        credential_reference="secret-ref" if connection_ready else None,
        enabled=connection_ready,
        configuration_status="configured" if connection_ready else "disabled",
    )
    mapping = create_mapping(
        session,
        asset_id=asset.id,
        provider_connection_id=connection.id,
        external_id="NE=154743789",
        mapping_status="pending_review",
    )
    session.commit()
    ids = (asset.id, connection.id, mapping.id)
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    return client, ids


def blockers_for(settings, mapping_id: int) -> list[str]:
    session = build_session_factory(build_engine(settings))()
    try:
        mapping = session.get(AssetProviderMapping, mapping_id)
        asset = session.get(Asset, mapping.asset_id)
        from nemsei.providers.models import ProviderConnection

        connection = session.get(ProviderConnection, mapping.provider_connection_id)
        return [item.code for item in mapping_approval_blockers(session, mapping=mapping, asset=asset, connection=connection)]
    finally:
        session.close()


def status_of(settings, mapping_id: int) -> str:
    session = build_session_factory(build_engine(settings))()
    try:
        return session.get(AssetProviderMapping, mapping_id).mapping_status
    finally:
        session.close()


def test_blockers_name_both_real_production_reasons(settings, monkeypatch) -> None:
    _, (_, _, mapping_id) = build(settings, monkeypatch, asset_reviewed=False, connection_ready=False)

    assert blockers_for(settings, mapping_id) == ["asset_needs_review", "connection_not_ready"]


def test_screen_reports_zero_ready_and_offers_no_approve_button(settings, monkeypatch) -> None:
    client, _ = build(settings, monkeypatch, asset_reviewed=False, connection_ready=False)

    page = client.get("/mappings")

    assert page.status_code == 200
    assert "Central por rever" in page.text
    assert "Ligação não configurada" in page.text
    assert "Nenhum destes mappings pode ser aprovado já" in page.text
    assert "Aprovar selecionados" in page.text  # o lote existe...
    assert ">Aprovar</button>" not in page.text  # ...mas a linha bloqueada nao


def test_a_ready_mapping_says_so_and_can_be_approved(settings, monkeypatch) -> None:
    client, (_, _, mapping_id) = build(settings, monkeypatch, asset_reviewed=True, connection_ready=True)

    assert blockers_for(settings, mapping_id) == []
    page = client.get("/mappings")
    assert ">Pronto</span>" in page.text
    assert ">Aprovar</button>" in page.text

    client.post(f"/mappings/{mapping_id}/approve", data={"csrf_token": "test"})

    assert status_of(settings, mapping_id) == "active"


def test_bulk_approve_keeps_what_passed_and_counts_what_did_not(settings, monkeypatch) -> None:
    # One approvable mapping, one blocked by its own unreviewed asset. A
    # refusal must not discard the mapping that was fine.
    client, (_, connection_id, ready_id) = build(settings, monkeypatch, asset_reviewed=True, connection_ready=True)
    session = build_session_factory(build_engine(settings))()
    blocked_asset = create_asset(session, canonical_name="Central por rever", timezone="Europe/Lisbon", review_status="needs_review")
    blocked = create_mapping(session, asset_id=blocked_asset.id, provider_connection_id=connection_id, external_id="NE=999", mapping_status="pending_review")
    session.commit()
    blocked_id = blocked.id
    session.close()

    response = client.post(
        "/mappings/bulk",
        data={"csrf_token": "test", "decision": "approve", "mapping_ids": [str(ready_id), str(blocked_id)]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert status_of(settings, ready_id) == "active"
    assert status_of(settings, blocked_id) == "pending_review"
    assert "1 de 2 mappings aprovados." in response.text
    assert "1 recusados" in response.text


def test_bulk_reject_marks_them_invalid(settings, monkeypatch) -> None:
    client, (_, _, mapping_id) = build(settings, monkeypatch, asset_reviewed=False, connection_ready=False)

    client.post("/mappings/bulk", data={"csrf_token": "test", "decision": "reject", "mapping_ids": [str(mapping_id)]})

    assert status_of(settings, mapping_id) == "invalid"


def test_bulk_needs_a_decision_and_a_selection(settings, monkeypatch) -> None:
    client, (_, _, mapping_id) = build(settings, monkeypatch, asset_reviewed=True, connection_ready=True)

    client.post("/mappings/bulk", data={"csrf_token": "test", "decision": "approve"})
    client.post("/mappings/bulk", data={"csrf_token": "test", "decision": "", "mapping_ids": [str(mapping_id)]})

    assert status_of(settings, mapping_id) == "pending_review"
