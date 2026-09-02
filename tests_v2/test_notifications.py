"""Notification infrastructure (D3): channel -> policy -> event, mock-only.

Every test proves one of the nine flows required in
docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md's D3 approval, directly --
not just "the code runs". No test in this file, or in the code it exercises,
is capable of a real Telegram call: `MockTelegramClient` never opens a
socket.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import (
    NotificationBaselineSnapshot,
    NotificationChannel,
    NotificationEvent,
    NotificationPolicy,
)
from nemsei.notifications.service import (
    decide_notification_events,
    deliver_pending_notifications,
    evaluate_and_process_notifications,
)
from nemsei.notifications.telegram_client import DeliveryResult, MockTelegramClient


class _CrashingClient:
    """Simulates the *worker process* dying mid-delivery -- an unhandled
    exception, not a delivery failure the client reports. Only used to
    prove restart safety (proof: restart do processador); never a stand-in
    for a real Telegram error, which is `MockTelegramClient(fail_for_chat_ids=...)`.
    """

    def __init__(self, crash_after: int) -> None:
        self.calls = 0
        self.crash_after = crash_after

    def send_message(self, *, chat_id: str, text: str) -> DeliveryResult:
        self.calls += 1
        if self.calls > self.crash_after:
            raise RuntimeError("simulated worker crash mid-delivery")
        return DeliveryResult(delivered=True)


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 12, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def asset_id(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Notification Plant")
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
    session, *, channel: NotificationChannel, enabled: bool = True, min_severity: str = "warning",
    rule_codes: list[str] | None = None, notify_on_open: bool = True, notify_on_resolve: bool = True,
    escalation_after_minutes: int | None = None, baseline_at: datetime | None = None,
    asset_scope: str = "all", reminder_minutes: list[int] | None = None,
) -> NotificationPolicy:
    policy = NotificationPolicy(
        name="Default", enabled=enabled, channel_id=channel.id, min_severity=min_severity,
        rule_codes_json=rule_codes, notify_on_open=notify_on_open, notify_on_resolve=notify_on_resolve,
        escalation_after_minutes=escalation_after_minutes, baseline_at=baseline_at, asset_scope=asset_scope,
        reminder_minutes_json=reminder_minutes,
        created_at=utc(), updated_at=utc(),
    )
    session.add(policy)
    session.flush()
    return policy


def make_incident(
    session, *, asset_id: int, device_id: int | None = None, rule_code: str = "device_unavailable",
    severity: str = "critical", status: str = "open", opened_at: datetime | None = None,
    last_observed_at: datetime | None = None, resolved_at: datetime | None = None,
) -> DiagnosticIncident:
    opened = opened_at or utc(9)
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, device_id=device_id, severity=severity, status=status,
        opened_at=opened, last_observed_at=last_observed_at or opened, resolved_at=resolved_at,
        occurrence_count=1, detector_version="1", evidence_json={}, created_at=opened, updated_at=opened,
    )
    session.add(incident)
    session.flush()
    return incident


def events(factory) -> list[NotificationEvent]:
    with factory() as session:
        return list(session.scalars(select(NotificationEvent)))


# --- 1. a new eligible incident creates exactly one pending event -------------


def test_a_new_eligible_incident_creates_exactly_one_pending_event(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        incident = make_incident(session, asset_id=asset_id, severity="critical", opened_at=utc(9))
        summary = decide_notification_events(session, now=utc(10))

    assert summary.events_created == 1
    assert summary.events_skipped == 0
    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].incident_id == incident.id
    assert rows[0].kind == "opened"
    assert rows[0].status == "pending"  # decision only -- delivery is a separate step
    assert rows[0].policy_id is not None and rows[0].channel_id == channel.id


# --- 2. repeated evaluation of the same incident never duplicates -------------


def test_repeated_evaluation_of_the_same_incident_creates_no_duplicates(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))

    with factory() as session, session.begin():
        first = decide_notification_events(session, now=utc(10))
    with factory() as session, session.begin():
        second = decide_notification_events(session, now=utc(11))
    with factory() as session, session.begin():
        third = decide_notification_events(session, now=utc(12))

    assert first.events_created == 1
    assert second.events_created == 0
    assert third.events_created == 0
    assert len(events(factory)) == 1


# --- 3. an incident that stays open does not renotify without an explicit rule -


def test_an_incident_that_stays_open_does_not_renotify_without_escalation_configured(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, escalation_after_minutes=None)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))

    for hour in (10, 14, 20):
        with factory() as session, session.begin():
            decide_notification_events(session, now=utc(hour))

    assert len(events(factory)) == 1
    assert {event.kind for event in events(factory)} == {"opened"}


def test_escalation_fires_exactly_once_after_the_configured_threshold(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, escalation_after_minutes=240)  # 4h
        make_incident(session, asset_id=asset_id, opened_at=utc(9))

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(9, 30))  # opened, escalation not due
    assert {event.kind for event in events(factory)} == {"opened"}

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(12, 59))  # 3h59 -- still not due
    assert {event.kind for event in events(factory)} == {"opened"}

    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(13, 1))  # 4h01 -- due now
    assert {event.kind for event in events(factory)} == {"opened", "escalated"}

    # And it never fires a second time even if re-evaluated well past the threshold again.
    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(18, 0))
    escalated = [event for event in events(factory) if event.kind == "escalated"]
    assert len(escalated) == 1


# --- 4. resolution produces exactly one recovery notification, if the policy wants it -
#
# Recovery is conditioned on the episode having actually alerted before:
# an incident this policy never told the channel about (never "opened" or
# "escalated" here) clearing is not a recovery from the channel's point of
# view -- see _decide_for_policy's notify_on_resolve comment. So these tests
# create the incident open, let it alert, *then* resolve it, instead of
# creating it pre-resolved.


def test_resolution_produces_exactly_one_recovery_notification_when_the_policy_defines_it(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, notify_on_resolve=True)
        incident = make_incident(session, asset_id=asset_id, status="open", opened_at=utc(9))
        decide_notification_events(session, now=utc(10))  # the prior alert this recovery depends on

    with factory() as session, session.begin():
        stored = session.get(DiagnosticIncident, incident.id)
        stored.status = "resolved"
        stored.resolved_at = utc(11, 30)
        session.flush()  # decide's own query does not autoflush pending changes
        summary = decide_notification_events(session, now=utc(12))

    assert summary.events_created == 1
    rows = [event for event in events(factory) if event.kind == "resolved"]
    assert len(rows) == 1
    assert rows[0].evidence_json["duration_minutes"] == pytest.approx(150.0)  # 09:00 -> 11:30


def test_resolution_produces_nothing_when_the_policy_does_not_want_it(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, notify_on_open=True, notify_on_resolve=False)
        incident = make_incident(session, asset_id=asset_id, status="open", opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    with factory() as session, session.begin():
        stored = session.get(DiagnosticIncident, incident.id)
        stored.status = "resolved"
        stored.resolved_at = utc(11)
        session.flush()
        summary = decide_notification_events(session, now=utc(12))
    assert summary.events_created == 0
    assert {event.kind for event in events(factory)} == {"opened"}


def test_resolution_produces_nothing_for_an_incident_that_never_alerted(factory, asset_id) -> None:
    """The prior-alert requirement itself: notify_on_resolve is on, the
    incident is in scope, but it never generated an "opened"/"escalated"
    event (e.g. it was pre-baseline the whole time it was open) -- silence
    resolving into more silence produces no recovery message."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(8), notify_on_resolve=True)
        make_incident(session, asset_id=asset_id, status="resolved", opened_at=utc(6), resolved_at=utc(7))
        summary = decide_notification_events(session, now=utc(12))
    assert summary.events_created == 0
    assert events(factory) == []


# --- 5. resolve then reappear (a new episode) can notify again ----------------


def test_a_resolved_and_reopened_episode_is_a_new_incident_and_can_notify_again(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        first_episode = make_incident(
            session, asset_id=asset_id, rule_code="device_unavailable", status="open", opened_at=utc(9),
        )
        decide_notification_events(session, now=utc(9, 30))  # the prior alert its recovery depends on

    with factory() as session, session.begin():
        stored = session.get(DiagnosticIncident, first_episode.id)
        stored.status = "resolved"
        stored.resolved_at = utc(10)
        session.flush()
        decide_notification_events(session, now=utc(10, 30))
    assert {(event.incident_id, event.kind) for event in events(factory)} == {
        (first_episode.id, "opened"), (first_episode.id, "resolved"),
    }

    with factory() as session, session.begin():
        # A brand-new DiagnosticIncident row -- D1's own episode boundary,
        # not anything this module invents.
        second_episode = make_incident(session, asset_id=asset_id, rule_code="device_unavailable", opened_at=utc(15))
        decide_notification_events(session, now=utc(15, 30))

    kinds_by_incident = {(event.incident_id, event.kind) for event in events(factory)}
    assert kinds_by_incident == {
        (first_episode.id, "opened"), (first_episode.id, "resolved"), (second_episode.id, "opened"),
    }


# --- 6. different incidents on the same asset/device never collide ------------


def test_different_rule_codes_on_the_same_device_do_not_collide(factory, asset_id) -> None:
    with factory() as session, session.begin():
        device = create_device(session, asset_id=asset_id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        channel = make_channel(session)
        make_policy(session, channel=channel)
        first = make_incident(session, asset_id=asset_id, device_id=device.id, rule_code="device_unavailable", severity="critical", opened_at=utc(9))
        second = make_incident(session, asset_id=asset_id, device_id=device.id, rule_code="stale_reading", severity="warning", opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    rows = events(factory)
    assert len(rows) == 2
    assert {(row.incident_id, row.kind) for row in rows} == {(first.id, "opened"), (second.id, "opened")}


# --- 7. restart/concurrency never duplicates -----------------------------------


def test_concurrent_decision_passes_never_duplicate_an_event(settings, factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))

    def run_once(_unused) -> None:
        engine = create_engine(settings.database_url)
        with build_session_factory(engine)() as session, session.begin():
            decide_notification_events(session, now=utc(10))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_once, range(4)))

    assert len(events(factory)) == 1


def test_the_database_itself_rejects_a_second_event_for_the_same_identity(factory, asset_id) -> None:
    """Not just application logic -- the unique constraint on
    (incident_id, kind, channel_id) must reject this even bypassing the service."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        policy = make_policy(session, channel=channel)
        incident = make_incident(session, asset_id=asset_id, opened_at=utc(9))
        incident_id, policy_id, channel_id = incident.id, policy.id, channel.id

    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            for _ in range(2):
                session.add(
                    NotificationEvent(
                        incident_id=incident_id, policy_id=policy_id, channel_id=channel_id, kind="opened",
                        status="pending", decided_at=utc(10), attempt_count=0, message="x", evidence_json={},
                        created_at=utc(10), updated_at=utc(10),
                    )
                )
                session.flush()


# --- 8. a Telegram client failure is auditable/retryable, never falsely "sent" -


def test_a_failed_delivery_is_recorded_and_never_falsely_marked_sent(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session, chat_id="chat-fails")
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    failing_client = MockTelegramClient(fail_for_chat_ids=frozenset({"chat-fails"}))
    summary = deliver_pending_notifications(factory, now=utc(10, 1), client_factory=lambda _channel: failing_client, notifications_enabled=True)

    assert summary.delivery_attempted == 1
    assert summary.delivery_sent == 0
    assert summary.delivery_failed == 1
    row = events(factory)[0]
    assert row.status == "failed"
    assert row.sent_at is None
    assert row.last_error is not None
    assert row.attempt_count == 1
    assert len(failing_client.sent) == 1  # the attempt really happened, auditable on the mock too


def test_a_failed_delivery_can_be_retried_and_then_succeeds(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session, chat_id="chat-fails")
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    failing_client = MockTelegramClient(fail_for_chat_ids=frozenset({"chat-fails"}))
    deliver_pending_notifications(factory, now=utc(10, 1), client_factory=lambda _channel: failing_client, notifications_enabled=True)

    succeeding_client = MockTelegramClient()  # a later retry, e.g. after the operator fixed the chat id
    summary = deliver_pending_notifications(factory, now=utc(11), client_factory=lambda _channel: succeeding_client, notifications_enabled=True)

    assert summary.delivery_sent == 1
    rows = events(factory)
    assert len(rows) == 1  # the retry updated the same row, never created a second one
    row = rows[0]
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.last_error is None
    assert row.attempt_count == 2  # both attempts are preserved, not reset


# --- 9. a disabled channel makes zero external calls ---------------------------


def test_a_disabled_channel_never_calls_a_client_at_all(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session, enabled=False)
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    row = events(factory)[0]
    assert row.status == "skipped"
    assert row.skipped_reason == "channel_disabled"

    def explode(_channel):
        raise AssertionError("a disabled channel must never reach the client factory")

    summary = deliver_pending_notifications(factory, now=utc(11), client_factory=explode, notifications_enabled=True)
    assert summary.delivery_attempted == 0


def test_a_disabled_policy_produces_no_events_at_all(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, enabled=False)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))
        summary = decide_notification_events(session, now=utc(10))
    assert summary.policies_evaluated == 0
    assert events(factory) == []


# --- restart safety of the delivery processor itself ---------------------------


def test_a_crash_mid_batch_never_resends_an_already_delivered_message(factory, asset_id) -> None:
    """Each event commits in its own transaction (notifications/service.py),
    so a crash right after event 1's real send but before event 2's can only
    ever leave event 2 unresolved -- event 1 must never be attempted again on
    the next run ("restart" of the processor)."""
    with factory() as session, session.begin():
        device_a = create_device(session, asset_id=asset_id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
        device_b = create_device(session, asset_id=asset_id, device_kind="inverter", label="B", valid_from=date(2026, 1, 1))
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, device_id=device_a.id, rule_code="device_unavailable", severity="critical", opened_at=utc(9))
        make_incident(session, asset_id=asset_id, device_id=device_b.id, rule_code="stale_reading", severity="warning", opened_at=utc(9))
        decide_notification_events(session, now=utc(10))

    assert len(events(factory)) == 2

    crashing_client = _CrashingClient(crash_after=1)
    with pytest.raises(RuntimeError):
        deliver_pending_notifications(factory, now=utc(10, 1), client_factory=lambda _channel: crashing_client, notifications_enabled=True)

    rows = events(factory)
    assert len(rows) == 2  # the crash never lost or duplicated a row
    sent_rows = [row for row in rows if row.status == "sent"]
    pending_rows = [row for row in rows if row.status == "pending"]
    assert len(sent_rows) == 1  # event 1's real send survives, committed on its own
    assert len(pending_rows) == 1  # event 2 never got a chance to commit anything
    assert crashing_client.calls == 2

    # "Restart": a fresh, working client resumes -- it must only see what is
    # still pending, never the one already sent.
    recovering_client = MockTelegramClient()
    summary = deliver_pending_notifications(factory, now=utc(11), client_factory=lambda _channel: recovering_client, notifications_enabled=True)
    assert summary.delivery_attempted == 1
    assert summary.delivery_sent == 1
    assert len(recovering_client.sent) == 1  # never re-sent the one that already went out

    final_rows = events(factory)
    assert len(final_rows) == 2  # still exactly two events, never duplicated
    assert all(row.status == "sent" for row in final_rows)
    assert sent_rows[0].sent_at == [row for row in final_rows if row.id == sent_rows[0].id][0].sent_at  # untouched by the restart


# --- scope: severity, rule_codes, baseline -------------------------------------


def test_min_severity_excludes_a_lower_severity_incident(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, min_severity="critical")
        make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))
        summary = decide_notification_events(session, now=utc(10))
    assert summary.events_created == 0
    assert events(factory) == []


def test_rule_codes_scope_excludes_an_incident_outside_it(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, rule_codes=["device_unavailable"])
        make_incident(session, asset_id=asset_id, rule_code="stale_reading", severity="warning", opened_at=utc(9))
        summary = decide_notification_events(session, now=utc(10))
    assert summary.events_created == 0


def test_baseline_excludes_an_incident_opened_before_it(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(12))
        make_incident(session, asset_id=asset_id, opened_at=utc(9))  # before baseline
        summary = decide_notification_events(session, now=utc(13))
    assert summary.events_created == 0


def test_baseline_excludes_escalation_of_a_pre_baseline_incident_too(factory, asset_id) -> None:
    """The baseline exclusion is not just about "opened": an escalation is
    that same suppressed alert's natural continuation, so a backlog incident
    already older than the escalation threshold must not fire the moment the
    policy turns on -- that is not "mudança relevante depois da ativação",
    it is the baseline exclusion leaking through a second door."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(12), escalation_after_minutes=60, notify_on_open=False)
        make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))  # 3h old before baseline
        summary = decide_notification_events(session, now=utc(13))  # 4h old, well past the 60min threshold
    assert summary.events_created == 0
    assert events(factory) == []


def test_baseline_excludes_resolution_of_an_incident_that_never_alerted(factory, asset_id) -> None:
    """A pre-baseline incident that resolves without ever having crossed
    into scope while open never told the channel it was a problem, so its
    resolution is not a recovery message either -- see the prior-alert
    requirement on notify_on_resolve."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(12), notify_on_resolve=True)
        make_incident(session, asset_id=asset_id, status="resolved", opened_at=utc(9), resolved_at=utc(10))
        summary = decide_notification_events(session, now=utc(13))
    assert summary.events_created == 0
    assert events(factory) == []


def test_unchanged_backlog_stays_excluded_from_escalation_no_matter_how_old_it_gets(factory, asset_id) -> None:
    """A backlog incident that never changes must never escalate on age
    alone: `NotificationBaselineSnapshot` freezes its severity at the first
    post-baseline evaluation, and since that snapshot still matches the
    policy's scope (nothing changed), every later evaluation -- no matter
    how long the incident has been open -- keeps skipping it. Duration
    since `opened_at` is deliberately never consulted for a backlog
    incident at all; only a genuine scope transition can be its trigger."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(
            session, channel=channel, baseline_at=utc(10), escalation_after_minutes=60,
            notify_on_open=False, notify_on_resolve=True,
        )
        make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))  # pre-baseline
        decide_notification_events(session, now=utc(9, 30))  # before baseline: nothing yet, snapshot captured
    assert events(factory) == []
    assert len(list(factory().scalars(select(NotificationBaselineSnapshot)))) == 1

    with factory() as session, session.begin():
        # Long past both baseline and the 60min escalation threshold --
        # still nothing, because the frozen snapshot still matches.
        summary = decide_notification_events(session, now=utc(20, 0))
    assert summary.events_created == 0
    assert events(factory) == []


# --- baseline transition: closing the opened_at-only gap (docs s28) -----------
#
# The six scenarios asked for explicitly. Policy A-shaped (critical,
# rule_codes=None, notify_on_open=True) unless a test needs escalation.


def test_1_old_warning_unchanged_produces_zero_events(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(12), min_severity="critical")
        make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))  # pre-baseline, never critical
        summary = decide_notification_events(session, now=utc(13))
    assert summary.events_created == 0
    assert events(factory) == []
    # Re-evaluated again, much later, still nothing -- not a one-time grace period.
    with factory() as session, session.begin():
        summary2 = decide_notification_events(session, now=utc(20))
    assert summary2.events_created == 0
    assert events(factory) == []


def test_2_old_warning_becomes_critical_after_baseline_produces_one_event(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(10), min_severity="critical")
        incident = make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))  # pre-baseline
        # First post-baseline evaluation, still warning: out of scope for a
        # critical-only policy, so nothing fires -- but this is also the
        # moment the (irrelevant here, since out-of-scope) snapshot logic
        # would apply were it in scope. Included to mirror a real periodic
        # evaluator having run before the transition happened.
        decide_notification_events(session, now=utc(10, 5))
    assert events(factory) == []

    with factory() as session, session.begin():
        stored = session.get(DiagnosticIncident, incident.id)
        stored.severity = "critical"  # a real re-evaluation by D1 worsened it
        session.flush()
        summary = decide_notification_events(session, now=utc(11))
    assert summary.events_created == 1
    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].kind == "opened"
    assert rows[0].incident_id == incident.id

    # Re-evaluating again does not create a second one.
    with factory() as session, session.begin():
        summary2 = decide_notification_events(session, now=utc(12))
    assert summary2.events_created == 0
    assert len(events(factory)) == 1


def test_3_old_critical_already_critical_at_baseline_produces_zero_events(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(10), min_severity="critical")
        make_incident(session, asset_id=asset_id, severity="critical", opened_at=utc(9))  # pre-baseline, already critical
        summary = decide_notification_events(session, now=utc(11))
    assert summary.events_created == 0
    assert events(factory) == []


def test_4_old_critical_that_never_alerted_resolves_with_zero_recovery(factory, asset_id) -> None:
    """Follows directly from #3: since the incident never earned an
    "opened"/"escalated" event (already critical at baseline, so never a
    new development), its later resolution is not a recovery either."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(10), min_severity="critical", notify_on_resolve=True)
        incident = make_incident(session, asset_id=asset_id, severity="critical", opened_at=utc(9))
        decide_notification_events(session, now=utc(11))
    assert events(factory) == []

    with factory() as session, session.begin():
        stored = session.get(DiagnosticIncident, incident.id)
        stored.status = "resolved"
        stored.resolved_at = utc(12)
        session.flush()
        summary = decide_notification_events(session, now=utc(13))
    assert summary.events_created == 0
    assert events(factory) == []


def test_5_new_critical_after_baseline_produces_one_opened(factory, asset_id) -> None:
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(10), min_severity="critical")
        make_incident(session, asset_id=asset_id, severity="critical", opened_at=utc(11))  # after baseline: not backlog
        summary = decide_notification_events(session, now=utc(11, 30))
    assert summary.events_created == 1
    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].kind == "opened"


def test_6_repeated_and_concurrent_evaluation_never_duplicates_the_transition_event(factory, asset_id) -> None:
    """Both halves of "no duplication": repeated sequential evaluation after
    the transition, and a real concurrency race hitting the snapshot and
    the event at the same time."""
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel, baseline_at=utc(10), min_severity="critical")
        incident = make_incident(session, asset_id=asset_id, severity="warning", opened_at=utc(9))
        decide_notification_events(session, now=utc(10, 5))  # captures the warning snapshot
        stored = session.get(DiagnosticIncident, incident.id)
        stored.severity = "critical"
        session.flush()

    def evaluate() -> None:
        with factory() as session, session.begin():
            decide_notification_events(session, now=utc(11))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: evaluate(), range(8)))

    rows = events(factory)
    assert len(rows) == 1
    assert rows[0].kind == "opened"
    with factory() as session:
        snapshots = list(
            session.scalars(
                select(NotificationBaselineSnapshot).where(NotificationBaselineSnapshot.incident_id == incident.id)
            )
        )
    assert len(snapshots) == 1

    # Sequential re-evaluation afterwards still does not add a second one.
    with factory() as session, session.begin():
        decide_notification_events(session, now=utc(15))
    assert len(events(factory)) == 1


# --- end-to-end: the one function the job actually calls -----------------------


def test_evaluate_and_process_notifications_decides_and_delivers_in_one_call(factory, asset_id, monkeypatch) -> None:
    # No injected client factory here, so this goes through
    # `default_client_factory` -- which needs the global capability on, exactly
    # as a real deployment does. With no bot token mounted it still lands on
    # the mock, so nothing leaves the process.
    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    with factory() as session, session.begin():
        channel = make_channel(session)
        make_policy(session, channel=channel)
        make_incident(session, asset_id=asset_id, opened_at=utc(9))

    summary = evaluate_and_process_notifications(factory, now=utc(10), notifications_enabled=True)

    assert summary.events_created == 1
    assert summary.delivery_sent == 1
    row = events(factory)[0]
    assert row.status == "sent"
