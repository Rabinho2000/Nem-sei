"""Bloco A: the asset detail page can finally be written to.

Before this, `assets/detail.html` had no `<form>` at all. Three POST routes
existed, were tested at service level and were unreachable from a browser, so
all 266 imported assets were frozen at "needs review" with no way out.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from nemsei.app import create_app
from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset, create_organization
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.models import OperatorAuditEvent
from nemsei.providers.service import create_connection
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    organization = create_organization(session, display_name="Sol PT")
    asset = create_asset(
        session,
        canonical_name="Central de Teste",
        owner_id=organization.id,
        installed_dc_power_kw=Decimal("642.1"),
        locality="Aveiro",
        timezone=None,
        # Como as 132 centrais reais sem fuso: a origem diz "unknown", nao
        # "manual" -- ninguem decidiu que nao ha fuso, so nunca se soube.
        timezone_source="unknown",
        review_status="needs_review",
        review_note="timezone missing",
    )
    create_connection(
        session,
        provider_code="fusionsolar",
        connection_key="legacy",
        display_name="FusionSolar legacy",
        credential_reference="do-not-render",
        enabled=False,
        configuration_status="disabled",
    )
    session.commit()
    asset_id, owner_id = asset.id, organization.id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client, asset_id, owner_id


def reload(settings, asset_id: int) -> Asset:
    session = build_session_factory(build_engine(settings))()
    try:
        asset = session.get(Asset, asset_id)
        session.expunge(asset)
        return asset
    finally:
        session.close()


def audit_actions(settings) -> list[str]:
    session = build_session_factory(build_engine(settings))()
    try:
        return list(session.scalars(select(OperatorAuditEvent.action).order_by(OperatorAuditEvent.id)))
    finally:
        session.close()


def form_payload(client, asset_id: int, **overrides) -> dict[str, str]:
    page = client.get(f"/assets/{asset_id}")
    assert "<form" in page.text, "a página de detalhe voltou a ficar só de leitura"
    payload = {
        "csrf_token": "test",
        "canonical_name": "Central de Teste",
        "lifecycle_status": "unknown",
        "installed_dc_power_kw": "642.1",
        "locality": "Aveiro",
    }
    payload.update(overrides)
    return payload


def post(client, path: str, data: dict[str, str]):
    with client.session_transaction() as browser:
        browser["csrf_token"] = "test"
    return client.post(path, data=data)


def test_detail_page_offers_the_write_actions_it_always_had_routes_for(settings, monkeypatch) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch)

    page = client.get(f"/assets/{asset_id}")

    assert page.status_code == 200
    assert 'name="timezone"' in page.text
    assert 'name="commissioned_on"' in page.text
    assert f"/assets/{asset_id}/aliases" in page.text
    assert f"/assets/{asset_id}/mappings" in page.text


def test_editing_an_asset_fills_the_gaps_and_is_audited(settings, monkeypatch) -> None:
    client, asset_id, owner_id = seeded(settings, monkeypatch)

    response = post(
        client,
        f"/assets/{asset_id}",
        form_payload(
            client,
            asset_id,
            owner_id=str(owner_id),
            timezone="Atlantic/Madeira",
            commissioned_on="2024-03-15",
            lifecycle_status="active",
        ),
    )

    assert response.status_code == 302
    asset = reload(settings, asset_id)
    assert asset.timezone == "Atlantic/Madeira"
    assert asset.timezone_source == "manual"
    assert asset.commissioned_on.isoformat() == "2024-03-15"
    assert asset.lifecycle_status == "active"
    assert audit_actions(settings) == ["asset_updated"]


def test_a_missing_timezone_field_no_longer_stamps_lisbon(settings, monkeypatch) -> None:
    # The route used to read `request.form.get("timezone", "Europe/Lisbon")`
    # and then record `timezone_source="manual"`, inventing both the value and
    # the provenance. 132 of 266 real assets have no timezone.
    client, asset_id, _ = seeded(settings, monkeypatch)

    response = post(client, f"/assets/{asset_id}", form_payload(client, asset_id, locality="Ílhavo"))

    assert response.status_code == 302
    asset = reload(settings, asset_id)
    assert asset.locality == "Ílhavo"
    assert asset.timezone is None
    # E a proveniencia continua a dizer a verdade: ninguem decidiu nada sobre
    # o fuso desta central.
    assert asset.timezone_source == "unknown"


def test_marking_an_asset_reviewed_clears_the_badge(settings, monkeypatch) -> None:
    # `update_asset` did not accept `review_status` at all, so this transition
    # was impossible from anywhere except a Python shell.
    client, asset_id, _ = seeded(settings, monkeypatch)

    post(
        client,
        f"/assets/{asset_id}",
        form_payload(client, asset_id, review_status="clear", review_note="Fuso confirmado no local."),
    )

    asset = reload(settings, asset_id)
    assert asset.review_status == "clear"
    assert asset.review_note == "Fuso confirmado no local."
    assert audit_actions(settings) == ["asset_reviewed"]
    assert "Precisa de revisão" not in client.get(f"/assets/{asset_id}").text


def test_an_edit_that_changes_nothing_writes_no_audit_row(settings, monkeypatch) -> None:
    client, asset_id, owner_id = seeded(settings, monkeypatch)

    post(client, f"/assets/{asset_id}", form_payload(client, asset_id, owner_id=str(owner_id)))

    assert audit_actions(settings) == []


def test_bulk_edit_touches_only_the_selected_assets(settings, monkeypatch) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    untouched = create_asset(session, canonical_name="Fora da seleção", timezone=None)
    session.commit()
    untouched_id = untouched.id
    session.close()


    response = post(
        client,
        "/assets/bulk",
        {"csrf_token": "test", "field": "lifecycle_status", "value": "active", "asset_ids": [str(asset_id)]},
    )

    assert response.status_code == 302
    assert reload(settings, asset_id).lifecycle_status == "active"
    assert reload(settings, untouched_id).lifecycle_status == "unknown"
    assert audit_actions(settings) == ["asset_updated"]


def test_bulk_edit_refuses_to_blank_a_timezone(settings, monkeypatch) -> None:
    # Clearing a value across many rows at once is a different, more dangerous
    # act than clearing one; the single-asset form is where that belongs.
    client, asset_id, _ = seeded(settings, monkeypatch)

    post(client, "/assets/bulk", {"csrf_token": "test", "field": "timezone", "value": "  ", "asset_ids": [str(asset_id)]})

    assert reload(settings, asset_id).timezone is None
    assert audit_actions(settings) == []


def test_bulk_edit_rejects_an_invalid_lifecycle_value(settings, monkeypatch) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch)

    post(client, "/assets/bulk", {"csrf_token": "test", "field": "lifecycle_status", "value": "banana", "asset_ids": [str(asset_id)]})

    assert reload(settings, asset_id).lifecycle_status == "unknown"
    assert audit_actions(settings) == []


def test_asset_list_exposes_the_bulk_form_and_the_fields_it_edits(settings, monkeypatch) -> None:
    client, _, _ = seeded(settings, monkeypatch)

    page = client.get("/assets")

    assert page.status_code == 200
    assert "/assets/bulk" in page.text
    assert 'name="asset_ids"' in page.text
    assert "em falta" in page.text  # a coluna de fuso diz que falta, em vez de calar
