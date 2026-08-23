"""Notification policy evaluation and mock delivery (D3).

Two separable steps, both exposed on purpose, and deliberately transactioned
differently because one has an external side effect and the other does not:

`decide_notification_events` is the only writer that ever *creates* a
`NotificationEvent` -- it never reads `diagnostics/findings.py` directly and
never decides whether a *finding* is true, that question is already
answered by `DiagnosticIncident` (D1). It only decides whether an
already-open-or-resolved incident's episode is worth telling a channel
about, and leaves every new row `pending` (or `skipped`, an equally real,
equally final decision) -- it never delivers anything itself. Pure
database work, so one transaction for the whole pass is fine: a crash
midway loses nothing that was not already re-derivable from
`diagnostic_incidents` on the next run.

`deliver_pending_notifications` is the only step that ever calls a
`TelegramClient`, and only ever touches rows already `pending`/`failed`.
**Each event gets its own transaction, committed immediately after its own
delivery attempt** -- not one transaction for the whole batch. This is not
a style choice: a `TelegramClient.send_message` call is a real external
side effect. If the whole batch shared one transaction and the process
died after message 3 of 5 was actually sent but before the transaction
committed, a restart would replay all 5 -- resending 3 real messages. One
transaction per event means a crash can only ever leave an event's own
single attempt uncommitted, never un-send a message that already went out
and already got recorded.

`evaluate_and_process_notifications` runs both in sequence -- the one job
this codebase actually schedules -- but keeping them separate functions
means each of D3's required proofs can address the step it is actually
about (a decision existing vs. a delivery outcome) without the other step's
behaviour in the way.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.findings import SEVERITY_ORDER
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import NotificationChannel, NotificationEvent, NotificationPolicy
from nemsei.notifications.telegram_client import MockTelegramClient, TelegramClient
from nemsei.shared.clock import utc_now


@dataclass(frozen=True)
class NotificationDecisionSummary:
    policies_evaluated: int
    events_created: int
    events_skipped: int


@dataclass(frozen=True)
class NotificationDeliverySummary:
    delivery_attempted: int
    delivery_sent: int
    delivery_failed: int


@dataclass(frozen=True)
class NotificationProcessingSummary:
    policies_evaluated: int
    events_created: int
    events_skipped: int
    delivery_attempted: int
    delivery_sent: int
    delivery_failed: int


def _default_client_factory(channel: NotificationChannel) -> TelegramClient:
    # The only client this codebase can build today: no factory path here
    # can produce anything that makes a real HTTP call. See
    # notifications/telegram_client.py.
    return MockTelegramClient()


def decide_notification_events(session: Session, *, now: datetime | None = None) -> NotificationDecisionSummary:
    now_value = now or utc_now()
    policies = list(session.scalars(select(NotificationPolicy).where(NotificationPolicy.enabled.is_(True))))

    created = skipped = 0
    for policy in policies:
        channel = session.get(NotificationChannel, policy.channel_id)
        if channel is None:
            continue
        created_here, skipped_here = _decide_for_policy(session, policy=policy, channel=channel, now=now_value)
        created += created_here
        skipped += skipped_here

    return NotificationDecisionSummary(policies_evaluated=len(policies), events_created=created, events_skipped=skipped)


def deliver_pending_notifications(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    client_factory: Callable[[NotificationChannel], TelegramClient] = _default_client_factory,
) -> NotificationDeliverySummary:
    """Attempt delivery for every `pending`/`failed` event, one commit each.

    Restart-safe by construction, not just by retrying: the id list is read
    once, but each event is re-checked (`status in (pending, failed)`)
    inside its *own* transaction right before acting on it, so a second
    process racing this one (or this same process retried after a crash)
    can never act twice on an event another run already finished -- the
    re-check, not just the initial query, is what makes a concurrent or
    restarted delivery pass safe.

    `failed` is retried every processing run, unbounded -- D3 has no real
    failure mode to design a backoff/max-attempts policy against yet (the
    mock only fails when a test explicitly configures it to); that belongs
    with the real client in D4, not invented here.
    """
    now_value = now or utc_now()
    with session_factory() as session:
        pending_ids = list(
            session.scalars(
                select(NotificationEvent.id)
                .where(NotificationEvent.status.in_(("pending", "failed")))
                .order_by(NotificationEvent.decided_at, NotificationEvent.id)
            )
        )

    attempted = sent = failed = 0
    for event_id in pending_ids:
        with session_factory() as session, session.begin():
            event = session.get(NotificationEvent, event_id)
            if event is None or event.status not in ("pending", "failed"):
                continue  # already resolved by a concurrent or prior run
            channel = session.get(NotificationChannel, event.channel_id)
            if channel is None or not channel.enabled:
                continue
            client = client_factory(channel)
            attempted += 1
            event.attempt_count += 1
            event.last_attempted_at = now_value
            event.updated_at = now_value
            result = client.send_message(chat_id=channel.target_chat_id or "", text=event.message)
            if result.delivered:
                event.status = "sent"
                event.sent_at = now_value
                event.last_error = None
                sent += 1
            else:
                event.status = "failed"
                event.last_error = result.error
                failed += 1
        # `with session.begin()` commits here, per event -- see the module
        # docstring for why this cannot be one transaction for the batch.

    return NotificationDeliverySummary(delivery_attempted=attempted, delivery_sent=sent, delivery_failed=failed)


def evaluate_and_process_notifications(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    client_factory: Callable[[NotificationChannel], TelegramClient] = _default_client_factory,
) -> NotificationProcessingSummary:
    now_value = now or utc_now()
    with session_factory() as session, session.begin():
        decision = decide_notification_events(session, now=now_value)
    delivery = deliver_pending_notifications(session_factory, now=now_value, client_factory=client_factory)
    return NotificationProcessingSummary(
        policies_evaluated=decision.policies_evaluated,
        events_created=decision.events_created,
        events_skipped=decision.events_skipped,
        delivery_attempted=delivery.delivery_attempted,
        delivery_sent=delivery.delivery_sent,
        delivery_failed=delivery.delivery_failed,
    )


def _in_scope(incident: DiagnosticIncident, *, policy: NotificationPolicy) -> bool:
    if SEVERITY_ORDER.get(incident.severity, 99) > SEVERITY_ORDER.get(policy.min_severity, 0):
        return False
    if policy.rule_codes_json and incident.rule_code not in policy.rule_codes_json:
        return False
    return True


def _has_event(session: Session, *, incident_id: int, kind: str, channel_id: int) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    NotificationEvent.incident_id == incident_id,
                    NotificationEvent.kind == kind,
                    NotificationEvent.channel_id == channel_id,
                )
            )
        )
    )


def _decide_for_policy(
    session: Session, *, policy: NotificationPolicy, channel: NotificationChannel, now: datetime
) -> tuple[int, int]:
    created = skipped = 0

    if policy.notify_on_open:
        candidates = session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status == "open"))
        for incident in candidates:
            if not _in_scope(incident, policy=policy):
                continue
            # Baseline: an incident already open before the policy started
            # watching is pre-existing history, not a new problem to
            # announce -- the same reason V1 needed ALERT_BASELINE_AT before
            # it could turn alerts on at all without a storm. Not a
            # "skipped" decision, deliberately: it was never a candidate,
            # the same way an out-of-scope rule_code or severity is not.
            if policy.baseline_at is not None and incident.opened_at < policy.baseline_at:
                continue
            if _has_event(session, incident_id=incident.id, kind="opened", channel_id=channel.id):
                continue
            event = _create_event(session, incident=incident, policy=policy, channel=channel, kind="opened", now=now)
            created, skipped = _tally(created, skipped, event)

    if policy.escalation_after_minutes:
        candidates = session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status == "open"))
        threshold = policy.escalation_after_minutes
        for incident in candidates:
            if not _in_scope(incident, policy=policy):
                continue
            # Same baseline rule as notify_on_open, and for the same reason:
            # an escalation is the natural continuation of an "opened" alert
            # that was itself suppressed as pre-existing history. Without
            # this check, activating an escalation policy against a backlog
            # where every incident is already older than the threshold would
            # fire an "escalated" message for the entire backlog on its very
            # first evaluation -- not "mudança relevante depois da ativação",
            # just the baseline exclusion leaking through a second door.
            if policy.baseline_at is not None and incident.opened_at < policy.baseline_at:
                continue
            age_minutes = (now - incident.opened_at).total_seconds() / 60
            if age_minutes < threshold:
                continue
            if _has_event(session, incident_id=incident.id, kind="escalated", channel_id=channel.id):
                continue
            event = _create_event(session, incident=incident, policy=policy, channel=channel, kind="escalated", now=now)
            created, skipped = _tally(created, skipped, event)

    if policy.notify_on_resolve:
        # Baseline does not apply here on purpose: a genuinely old problem
        # finally clearing is worth saying regardless of when the policy
        # started watching it (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md).
        # But a recovery message only makes sense for an episode that
        # actually *told someone it was a problem* -- an incident this
        # policy never announced (baseline-excluded, or never in scope while
        # open) clearing is not a recovery from the channel's point of view,
        # it is silence resolving into more silence. Require a prior
        # "opened" or "escalated" event under this same policy/channel.
        candidates = session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status == "resolved"))
        for incident in candidates:
            if not _in_scope(incident, policy=policy):
                continue
            if not (
                _has_event(session, incident_id=incident.id, kind="opened", channel_id=channel.id)
                or _has_event(session, incident_id=incident.id, kind="escalated", channel_id=channel.id)
            ):
                continue
            if _has_event(session, incident_id=incident.id, kind="resolved", channel_id=channel.id):
                continue
            event = _create_event(session, incident=incident, policy=policy, channel=channel, kind="resolved", now=now)
            created, skipped = _tally(created, skipped, event)

    return created, skipped


def _tally(created: int, skipped: int, event: NotificationEvent | None) -> tuple[int, int]:
    # `event is None` means a concurrent decision pass already won this exact
    # identity between our `_has_event` check and our insert (proof #7,
    # DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md) -- not our event to count,
    # created or skipped, since we did not actually create anything.
    if event is None:
        return created, skipped
    if event.status == "skipped":
        return created + 1, skipped + 1
    return created + 1, skipped


def _create_event(
    session: Session,
    *,
    incident: DiagnosticIncident,
    policy: NotificationPolicy,
    channel: NotificationChannel,
    kind: str,
    now: datetime,
) -> NotificationEvent | None:
    asset = session.get(Asset, incident.asset_id)
    device = session.get(Device, incident.device_id) if incident.device_id else None
    message = _render_message(incident=incident, asset=asset, device=device, kind=kind, now=now)
    evidence = {
        "rule_code": incident.rule_code,
        "severity": incident.severity,
        "asset_name": asset.canonical_name if asset else None,
        "device_label": device.label if device else None,
        "opened_at": incident.opened_at.isoformat(),
        "occurrence_count": incident.occurrence_count,
    }
    if kind == "resolved" and incident.resolved_at is not None:
        evidence["duration_minutes"] = round((incident.resolved_at - incident.opened_at).total_seconds() / 60, 1)

    channel_ready = policy.enabled and channel.enabled
    event = NotificationEvent(
        incident_id=incident.id,
        policy_id=policy.id,
        channel_id=channel.id,
        kind=kind,
        status="pending" if channel_ready else "skipped",
        decided_at=now,
        attempt_count=0,
        skipped_reason=None if channel_ready else "channel_disabled",
        message=message,
        evidence_json=evidence,
        created_at=now,
        updated_at=now,
    )
    # A SAVEPOINT, not the outer transaction: our own `_has_event` check just
    # above is a read, not a lock, so a concurrent decision pass can still
    # win the same identity between that check and this insert. The unique
    # constraint on (incident_id, kind, channel_id) is what actually
    # prevents the duplicate; this only makes losing that race a graceful
    # no-op instead of aborting the whole evaluation pass (proof #7,
    # DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md) -- without a SAVEPOINT,
    # Postgres would leave the entire outer transaction unusable after the
    # first IntegrityError, not just this one insert.
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        return None
    return event


def _render_message(
    *, incident: DiagnosticIncident, asset: Asset | None, device: Device | None, kind: str, now: datetime
) -> str:
    asset_name = asset.canonical_name if asset else f"asset #{incident.asset_id}"
    device_part = f" — {device.label}" if device else ""
    if kind == "opened":
        icon = "🔴" if incident.severity == "critical" else "🟠"
        return f"{icon} {incident.rule_code}{device_part} — {asset_name}"
    if kind == "escalated":
        age_minutes = round((now - incident.opened_at).total_seconds() / 60)
        return f"⏫ {incident.rule_code}{device_part} — {asset_name} (aberto há {age_minutes} min)"
    duration = ""
    if incident.resolved_at is not None:
        minutes = round((incident.resolved_at - incident.opened_at).total_seconds() / 60)
        duration = f" (durou {minutes} min)"
    return f"🟢 {incident.rule_code}{device_part} — {asset_name} recuperado{duration}"


