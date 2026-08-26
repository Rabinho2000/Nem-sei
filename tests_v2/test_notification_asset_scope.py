"""Bloco E: a policy speaks for part of the fleet, not all of it.

V1 gated every alert on `ALERT_SCOPE`, defaulting to `only_o&m`. V2 narrowed
only by severity and rule code, so a policy switched on would have alerted for
all 267 installations -- including the 175 Solcor does not operate. These prove
the gate, and prove it is derived from the contract rather than from a flag.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset
from nemsei.contracts.service import set_service_contract
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import NotificationChannel, NotificationEvent, NotificationPolicy
from nemsei.notifications.service import decide_notification_events

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def seed(session, *, asset_scope: str):
    """One operated plant, one lapsed, one never in scope -- each with an
    open critical incident, so only the scope can tell the outcomes apart."""
    channel = NotificationChannel(
        name="Ops Telegram", kind="telegram", enabled=True, target_chat_id="chat-1",
        created_at=NOW, updated_at=NOW,
    )
    session.add(channel)
    session.flush()
    policy = NotificationPolicy(
        name="Default", enabled=True, channel_id=channel.id, min_severity="warning",
        asset_scope=asset_scope, rule_codes_json=None, notify_on_open=True, notify_on_resolve=False,
        escalation_after_minutes=None, baseline_at=None, created_at=NOW, updated_at=NOW,
    )
    session.add(policy)

    names = {}
    for label in ("operada", "caducada", "fora"):
        asset = create_asset(session, canonical_name=f"Central {label}")
        session.flush()
        names[label] = asset.id
        incident = DiagnosticIncident(
            rule_code="device_unavailable", asset_id=asset.id, device_id=None, severity="critical",
            status="open", opened_at=NOW - timedelta(hours=3), last_observed_at=NOW,
            resolved_at=None, occurrence_count=1, detector_version="1", evidence_json={},
            created_at=NOW, updated_at=NOW,
        )
        session.add(incident)
    session.flush()

    set_service_contract(
        session, asset_id=names["operada"], created_by="importer",
        valid_from=TODAY - timedelta(days=400), valid_to=TODAY + timedelta(days=400),
    )
    set_service_contract(
        session, asset_id=names["caducada"], created_by="importer",
        valid_from=TODAY - timedelta(days=800), valid_to=TODAY - timedelta(days=30),
    )
    session.flush()
    return names


def notified_assets(session) -> set[int]:
    return set(
        session.scalars(
            select(DiagnosticIncident.asset_id)
            .join(NotificationEvent, NotificationEvent.incident_id == DiagnosticIncident.id)
            .where(NotificationEvent.kind == "opened")
        )
    )


def test_all_keeps_the_pre_existing_meaning(factory):
    with factory() as session, session.begin():
        names = seed(session, asset_scope="all")
        decide_notification_events(session, now=NOW)
        assert notified_assets(session) == set(names.values())


def test_om_covers_the_operated_portfolio_including_a_lapsed_contract(factory):
    with factory() as session, session.begin():
        names = seed(session, asset_scope="om")
        decide_notification_events(session, now=NOW)
        assert notified_assets(session) == {names["operada"], names["caducada"]}


def test_om_active_stops_alerting_the_day_a_contract_lapses(factory):
    """The consequence of the chosen scope, stated as a test.

    An installation whose contract has ended goes quiet. That is deliberate,
    and it is why the renewals screen and the panel tile exist: the lapse has
    to be loud somewhere, since it is no longer loud here.
    """
    with factory() as session, session.begin():
        names = seed(session, asset_scope="om_active")
        decide_notification_events(session, now=NOW)
        assert notified_assets(session) == {names["operada"]}


def test_scope_follows_the_contract_without_anything_being_rewritten(factory):
    """Renewing brings an installation back into scope by date arithmetic."""
    with factory() as session, session.begin():
        names = seed(session, asset_scope="om_active")
        decide_notification_events(session, now=NOW)
        assert notified_assets(session) == {names["operada"]}

    with factory() as session, session.begin():
        # The lapsed plant is renewed from today. No flag is touched.
        set_service_contract(
            session, asset_id=names["caducada"], created_by="operador",
            valid_from=TODAY, valid_to=TODAY + timedelta(days=365),
        )

    with factory() as session, session.begin():
        decide_notification_events(session, now=NOW)
        assert notified_assets(session) == {names["operada"], names["caducada"]}


def test_an_asset_with_no_engagement_is_never_in_an_om_scope(factory):
    with factory() as session, session.begin():
        names = seed(session, asset_scope="om")
        decide_notification_events(session, now=NOW)
        assert names["fora"] not in notified_assets(session)
