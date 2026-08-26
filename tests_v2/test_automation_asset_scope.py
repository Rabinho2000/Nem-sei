"""Pointing a notification policy at the O&M portfolio, from the interface.

`asset_scope` arrived with the contract work but with no way to change it, so
narrowing Telegram to the plants Solcor operates meant an UPDATE by hand. These
cover the control, its audit trail, and the preview count that would otherwise
keep reporting the whole fleet for a narrowed policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.contracts.models import AssetServiceContract
from nemsei.contracts.service import set_service_contract
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import NotificationChannel, NotificationPolicy
from nemsei.providers.models import OperatorAuditEvent
from nemsei.system.automations import policy_scope_counts
from tests_v2.test_migrations import upgrade

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def seeded(settings, monkeypatch):
    """One operated plant, one lapsed, one outside -- each with an open critical."""
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    channel = NotificationChannel(
        name="Ops Telegram", kind="telegram", enabled=True, target_chat_id="chat-1",
        created_at=NOW, updated_at=NOW,
    )
    session.add(channel)
    session.flush()
    policy = NotificationPolicy(
        name="Criticos imediatos", enabled=True, channel_id=channel.id, min_severity="critical",
        asset_scope="all", rule_codes_json=None, notify_on_open=True, notify_on_resolve=True,
        escalation_after_minutes=None, baseline_at=None, created_at=NOW, updated_at=NOW,
    )
    session.add(policy)
    ids = {}
    for label in ("operada", "caducada", "fora"):
        asset = create_asset(session, canonical_name=f"Central {label}")
        session.flush()
        ids[label] = asset.id
        session.add(
            DiagnosticIncident(
                rule_code="device_unavailable", asset_id=asset.id, device_id=None,
                severity="critical", status="open", opened_at=NOW - timedelta(hours=2),
                last_observed_at=NOW, resolved_at=None, occurrence_count=1,
                detector_version="1", evidence_json={}, created_at=NOW, updated_at=NOW,
            )
        )
    session.flush()
    set_service_contract(
        session, asset_id=ids["operada"], created_by="importer",
        valid_from=TODAY - timedelta(days=400), valid_to=TODAY + timedelta(days=400),
    )
    set_service_contract(
        session, asset_id=ids["caducada"], created_by="importer",
        valid_from=TODAY - timedelta(days=800), valid_to=TODAY - timedelta(days=30),
    )
    session.commit()
    policy_id = policy.id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client, policy_id, ids


def csrf_from(client) -> str:
    with client.session_transaction() as browser:
        return browser.get("csrf_token", "")


def test_the_page_offers_the_scope_control(settings, monkeypatch):
    client, policy_id, _ = seeded(settings, monkeypatch)
    page = client.get("/automations").get_data(as_text=True)
    assert 'name="asset_scope"' in page
    assert "Só O&amp;M com contrato em vigor" in page
    assert "Todo o parque" in page


def test_an_operator_narrows_a_policy_to_the_om_portfolio(settings, monkeypatch):
    client, policy_id, _ = seeded(settings, monkeypatch)
    client.get("/automations")
    response = client.post(
        f"/automations/policies/{policy_id}/ambito",
        data={"csrf_token": csrf_from(client), "asset_scope": "om_active"},
    )
    assert response.status_code == 302
    session = build_session_factory(build_engine(settings))()
    assert session.get(NotificationPolicy, policy_id).asset_scope == "om_active"
    actions = set(session.scalars(select(OperatorAuditEvent.action)))
    assert "automation_scope_changed" in actions
    session.close()


def test_an_unknown_scope_is_refused(settings, monkeypatch):
    client, policy_id, _ = seeded(settings, monkeypatch)
    client.get("/automations")
    client.post(
        f"/automations/policies/{policy_id}/ambito",
        data={"csrf_token": csrf_from(client), "asset_scope": "todas_menos_as_feias"},
    )
    session = build_session_factory(build_engine(settings))()
    assert session.get(NotificationPolicy, policy_id).asset_scope == "all"
    session.close()


def test_the_preview_count_follows_the_scope(settings, monkeypatch):
    """Otherwise the column reports the whole fleet for a narrowed policy."""
    client, policy_id, _ = seeded(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))

    session = factory()
    assert policy_scope_counts(session)[policy_id] == 3
    session.close()

    client.get("/automations")
    client.post(
        f"/automations/policies/{policy_id}/ambito",
        data={"csrf_token": csrf_from(client), "asset_scope": "om"},
    )
    session = factory()
    assert policy_scope_counts(session)[policy_id] == 2  # operada + caducada
    session.close()

    client.post(
        f"/automations/policies/{policy_id}/ambito",
        data={"csrf_token": csrf_from(client), "asset_scope": "om_active"},
    )
    session = factory()
    assert policy_scope_counts(session)[policy_id] == 1  # só a operada
    session.close()


def test_a_scope_matching_nothing_counts_zero_not_everything(settings, monkeypatch):
    """An empty scope and an absent scope must never look alike."""
    client, policy_id, _ = seeded(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    session = factory()
    # Remove every engagement, so `om` matches no installation at all.
    for contract in session.scalars(select(AssetServiceContract)):
        session.delete(contract)
    session.commit()
    session.close()

    client.get("/automations")
    client.post(
        f"/automations/policies/{policy_id}/ambito",
        data={"csrf_token": csrf_from(client), "asset_scope": "om"},
    )
    session = factory()
    assert policy_scope_counts(session)[policy_id] == 0
    session.close()
