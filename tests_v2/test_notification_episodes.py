"""NotificationEpisode: the flap-storm fix (Telegram O&M redesign, reqs 2-4/14/17).

Every test proves one of the concrete scenarios req 17 asks for, directly
against the real `sync_episodes`/`decide_notification_events` -- not a
re-implementation of the rule in the test. No test here is capable of a real
Telegram call: delivery is a separate step this file never reaches.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset
from nemsei.contracts.service import set_service_contract
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.notifications.eligibility import next_reminder_due
from nemsei.notifications.models import NotificationChannel, NotificationEpisode, NotificationEvent, NotificationPolicy
from nemsei.notifications.service import decide_notification_events
from nemsei.work_orders.service import create_work_order


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def asset_id(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="DIACO")
        aid = asset.id
    return aid


def make_channel(session, *, enabled: bool = True, chat_id: str = "chat-1") -> NotificationChannel:
    channel = NotificationChannel(
        name="Ops Telegram", kind="telegram", enabled=enabled, target_chat_id=chat_id,
        created_at=utc(), updated_at=utc(),
    )
    session.add(channel)
    session.flush()
    return channel


def make_policy(
    session, *, channel: NotificationChannel, min_severity: str = "critical",
    rule_codes: list[str] | None = None, notify_on_open: bool = True, notify_on_resolve: bool = True,
    escalation_after_minutes: int | None = None, asset_scope: str = "all",
    reminder_minutes: list[int] | None = None,
) -> NotificationPolicy:
    policy = NotificationPolicy(
        name="Critical immediate", enabled=True, channel_id=channel.id, min_severity=min_severity,
        rule_codes_json=rule_codes, notify_on_open=notify_on_open, notify_on_resolve=notify_on_resolve,
        escalation_after_minutes=escalation_after_minutes, asset_scope=asset_scope,
        reminder_minutes_json=reminder_minutes, created_at=utc(), updated_at=utc(),
    )
    session.add(policy)
    session.flush()
    return policy


def make_plant_offline_incident(
    session, *, asset_id: int, opened_at: datetime, resolved_at: datetime | None = None
) -> DiagnosticIncident:
    status = "resolved" if resolved_at is not None else "open"
    incident = DiagnosticIncident(
        rule_code="plant_offline", asset_id=asset_id, device_id=None, severity="critical", status=status,
        opened_at=opened_at, last_observed_at=resolved_at or opened_at, resolved_at=resolved_at,
        occurrence_count=1, detector_version="2", evidence_json={}, created_at=opened_at, updated_at=opened_at,
    )
    session.add(incident)
    session.flush()
    return incident


def resolve(session, incident_id: int, *, resolved_at: datetime) -> None:
    incident = session.get(DiagnosticIncident, incident_id)
    incident.status = "resolved"
    incident.resolved_at = resolved_at
    incident.last_observed_at = resolved_at
    session.flush()


def events(factory) -> list[NotificationEvent]:
    with factory() as session:
        return list(session.scalars(select(NotificationEvent)))


def episode_for(factory, *, asset_id: int) -> NotificationEpisode | None:
    with factory() as session:
        return session.scalar(select(NotificationEpisode).where(NotificationEpisode.asset_id == asset_id))


# --- 1. offline 16 min -> silence -----------------------------------------------


def test_plant_offline_16_minutes_produces_no_notification(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        summary = decide_notification_events(session, now=utc(9, 16))

    assert summary.events_created == 0
    assert events(factory) == []
    episode = episode_for(factory, asset_id=asset_id)
    assert episode is not None and episode.eligible_at is None  # req 2: still inside the 0-30min window


# --- 2. offline 31 min -> exactly one notification ------------------------------


def test_plant_offline_31_minutes_produces_exactly_one_notification(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        summary = decide_notification_events(session, now=utc(9, 31))

    assert summary.events_created == 1
    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].kind == "opened"
    episode = episode_for(factory, asset_id=asset_id)
    assert episode.eligible_at == utc(9, 30)
    assert episode.notified_at == utc(9, 31)


# --- 3. the next poll never duplicates ------------------------------------------


def test_the_next_poll_after_notifying_creates_no_duplicate(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 31))

    with factory() as session, session.begin():
        second = decide_notification_events(session, now=utc(9, 46))
    with factory() as session, session.begin():
        third = decide_notification_events(session, now=utc(10, 1))

    assert second.events_created == 0
    assert third.events_created == 0
    assert len(events(factory)) == 1


# --- 4. flap storm: offline/online/offline within minutes never spams ----------


def test_a_flapping_plant_offline_never_produces_more_than_one_notification(factory, asset_id) -> None:
    """The exact real-world scenario reported: an installation cycling
    offline/online every ~16 minutes. Three separate `DiagnosticIncident`
    episodes over 25 minutes, none individually reaching 30 minutes old, must
    fold into one `NotificationEpisode` and produce exactly one message once
    their *combined* age crosses the threshold -- not one message per flap.
    """
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        first = make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 5))  # 5min old -- silent
    assert events(factory) == []

    with factory() as session, session.begin():
        resolve(session, first.id, resolved_at=utc(9, 16))
        decide_notification_events(session, now=utc(9, 16))  # flapped back online
    assert events(factory) == []
    episode = episode_for(factory, asset_id=asset_id)
    assert episode.status == "closed" and episode.flap_count == 1

    with factory() as session, session.begin():
        second = make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 18))
        decide_notification_events(session, now=utc(9, 18))  # offline again, 2min later -- merged
    assert events(factory) == []
    episode = episode_for(factory, asset_id=asset_id)
    assert episode.status == "open" and episode.flap_count == 2
    assert episode.opened_at == utc(9, 0)  # the ORIGINAL start, not the flap's own

    with factory() as session, session.begin():
        resolve(session, second.id, resolved_at=utc(9, 22))
        decide_notification_events(session, now=utc(9, 22))
    with factory() as session, session.begin():
        third = make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 25))
        decide_notification_events(session, now=utc(9, 25))  # offline a third time
    assert events(factory) == []  # still under 30min combined, still silent

    with factory() as session, session.begin():
        summary = decide_notification_events(session, now=utc(9, 31))  # 31min since the ORIGINAL start

    assert summary.events_created == 1
    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].incident_id == third.id  # evidence points at the currently-driving incident
    episode = episode_for(factory, asset_id=asset_id)
    assert episode.flap_count == 3


# --- 5. recovery of an episode that was never notified produces nothing --------


def test_recovery_of_a_never_notified_episode_produces_nothing(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        incident = make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 10))  # 10min -- never eligible

    with factory() as session, session.begin():
        resolve(session, incident.id, resolved_at=utc(9, 12))
        summary = decide_notification_events(session, now=utc(10, 0))  # well past the merge window too

    assert summary.events_created == 0
    assert events(factory) == []


# --- 6. recovery of a notified critical episode sends a message ----------------


def test_recovery_of_a_notified_critical_episode_sends_a_message(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        incident = make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 31))  # notifies

    with factory() as session, session.begin():
        resolve(session, incident.id, resolved_at=utc(11, 0))
        # Past the flap-merge window (60min) with nothing reopening it, so the
        # episode is genuinely, durably closed by the time this runs.
        summary = decide_notification_events(session, now=utc(12, 5))

    assert summary.events_created == 1
    rows = [event for event in events(factory) if event.kind == "resolved"]
    assert len(rows) == 1
    assert rows[0].evidence_json["duration_minutes"] == pytest.approx(120.0)  # 09:00 -> 11:00


# --- 7. reminder cadence (req 3) ------------------------------------------------


def test_reminder_fires_once_past_the_4h_tier_while_still_critical(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, reminder_minutes=[240, 1440])
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 31))  # notifies

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(12, 0))  # 3h -- not due
    assert {event.kind for event in events(factory)} == {"opened"}

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(13, 5))  # 4h05 -- due
    assert {event.kind for event in events(factory)} == {"opened", "reminder"}
    assert len([event for event in events(factory) if event.kind == "reminder"]) == 1

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(14, 0))  # same tier already sent -- no repeat
    assert len([event for event in events(factory) if event.kind == "reminder"]) == 1


def test_a_policy_without_reminder_minutes_never_reminds(factory, asset_id) -> None:
    """The opt-in itself: neither real production policy sets
    `reminder_minutes_json`, and a policy that never opted in must behave
    exactly as it did before this mechanism existed, however long an episode
    stays open."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, reminder_minutes=None)
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 31))

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(9, 31, day=25))  # 24h+ later, still critical, still open

    assert {event.kind for event in events(factory)} == {"opened"}


def test_a_work_order_planned_today_suppresses_the_reminder(factory, asset_id) -> None:
    with factory() as session, session.begin():
        installation = Installation(
            display_name="DIACO", timezone_source="manual", created_at=utc(), updated_at=utc(),
        )
        session.add(installation)
        session.flush()
        session.get(Asset, asset_id).installation_id = installation.id
        create_work_order(
            session, installation_id=installation.id, work_type="corrective", title="String fault",
            created_by="ops", status="planned", planned_date=utc(13, 5).date(),
        )
        channel = make_channel(session)
        make_policy(session, channel=channel, reminder_minutes=[240, 1440])
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        decide_notification_events(session, now=utc(9, 31))

    with factory() as session, session.begin():
        summary = decide_notification_events(session, now=utc(13, 5))  # 4h05 -- would be due

    assert summary.events_created == 0
    assert {event.kind for event in events(factory)} == {"opened"}


# --- 8. only O&M-active installations get an operational alert (req 1) --------


def test_an_installation_without_active_om_gets_no_operational_alert(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, asset_scope="om_active")
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        summary = decide_notification_events(session, now=utc(9, 31))

    assert summary.events_created == 0
    assert events(factory) == []


def test_an_installation_with_active_om_gets_the_operational_alert(factory, asset_id) -> None:
    with factory() as session, session.begin():
        set_service_contract(
            session, asset_id=asset_id, created_by="ops", valid_from=utc(1).date(), valid_to=None, service_kind="om",
        )
        channel = make_channel(session)
        make_policy(session, channel=channel, asset_scope="om_active")
        make_plant_offline_incident(session, asset_id=asset_id, opened_at=utc(9, 0))
        summary = decide_notification_events(session, now=utc(9, 31))

    assert summary.events_created == 1


# --- next_reminder_due, direct (pure, no DB) ------------------------------------


def test_next_reminder_due_explains_itself_when_not_due() -> None:
    episode = NotificationEpisode(
        asset_id=1, device_id=None, problem_family="communication", status="open", severity_peak="critical",
        opened_at=utc(9, 0), last_activity_at=utc(9, 31), flap_count=1, first_incident_id=1, last_incident_id=1,
        eligible_at=utc(9, 30), notified_at=utc(9, 31), reminder_count=0, recovery_notified=False,
        created_at=utc(9, 0), updated_at=utc(9, 31),
    )
    decision = next_reminder_due(episode, now=utc(10, 0), reminder_minutes=(240, 1440))
    assert decision.due is False
    assert decision.threshold_minutes == 240
    assert "elapsed" in decision.reason
