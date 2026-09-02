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
from datetime import date, datetime
from typing import Callable

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.findings import SEVERITY_ORDER
from nemsei.contracts.service import scoped_asset_ids
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.eligibility import next_reminder_due
from nemsei.notifications.episodes import sync_episodes
from nemsei.notifications.models import (
    NotificationBaselineSnapshot,
    NotificationChannel,
    NotificationEpisode,
    NotificationEvent,
    NotificationPolicy,
)
from nemsei.config import external_capability_enabled
from nemsei.notifications.telegram_client import TelegramClient, default_client_factory
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import WorkOrder


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
    # One decision point for the whole process: telegram_client.py returns the
    # real client only when a bot token is configured, and the mock otherwise.
    return default_client_factory(channel)


def decide_notification_events(session: Session, *, now: datetime | None = None) -> NotificationDecisionSummary:
    now_value = now or utc_now()
    # Reconcile `NotificationEpisode` against current `DiagnosticIncident`
    # state first, unconditionally -- see `notifications/episodes.py`. This
    # is what actually stops a flapping detector from producing a fresh
    # notification identity on every flap (`incident_id` used to be that
    # identity; `episode_id` is now). `jobs/handlers.py` also calls this
    # right after the incident evaluator itself, so episodes stay fresh for
    # callers (a future priority score, the morning briefing) that never go
    # through this function at all -- calling it again here is cheap and
    # idempotent, and guarantees every path into `_decide_for_policy` below
    # sees episode state that matches the incidents it is deciding about,
    # regardless of what did or did not run before it.
    sync_episodes(session, now=now_value)
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
    notifications_enabled: bool | None = None,
) -> NotificationDeliverySummary:
    """Attempt delivery for every `pending`/`failed` event, one commit each.

    `notifications_enabled` is the global kill switch (`NEMSEI_V2_NOTIFICATIONS`),
    resolved from the process environment when not given. Off means nothing is
    attempted at all: no client is built, no event is touched, and events stay
    `pending` rather than being marked `failed` by a switch that was never
    about a delivery failure. Turning the switch back on delivers the backlog,
    which is the same thing re-enabling a channel already does.

    It sits above the channel switch on purpose. The hierarchy, outermost
    first, is: global capability, then the scheduler's
    `NEMSEI_V2_NOTIFICATION_PROCESSING_ENABLED` (whether the job runs at all),
    then policy `enabled` (what becomes an event), then channel `enabled`
    (where an event may go), then the client (which needs a token to be real).

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
    if notifications_enabled is None:
        notifications_enabled = external_capability_enabled("notifications")
    if not notifications_enabled:
        return NotificationDeliverySummary(delivery_attempted=0, delivery_sent=0, delivery_failed=0)

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
    notifications_enabled: bool | None = None,
) -> NotificationProcessingSummary:
    """Decide, then deliver.

    Deciding is left running even when the global switch is off: it makes no
    network call, and it is what keeps each policy's baseline moving, so the
    switch suspends delivery rather than rewriting what would have been
    noticed while it was off.
    """
    now_value = now or utc_now()
    with session_factory() as session, session.begin():
        decision = decide_notification_events(session, now=now_value)
    delivery = deliver_pending_notifications(
        session_factory, now=now_value, client_factory=client_factory, notifications_enabled=notifications_enabled
    )
    return NotificationProcessingSummary(
        policies_evaluated=decision.policies_evaluated,
        events_created=decision.events_created,
        events_skipped=decision.events_skipped,
        delivery_attempted=delivery.delivery_attempted,
        delivery_sent=delivery.delivery_sent,
        delivery_failed=delivery.delivery_failed,
    )


def _in_scope_for(severity: str, rule_code: str, *, policy: NotificationPolicy) -> bool:
    if SEVERITY_ORDER.get(severity, 99) > SEVERITY_ORDER.get(policy.min_severity, 0):
        return False
    if policy.rule_codes_json and rule_code not in policy.rule_codes_json:
        return False
    return True


def _asset_in_scope_id(asset_id: int, *, scoped_assets: set[int] | None) -> bool:
    """Whether this policy speaks for the installation an episode/incident is
    on. `None` is "the whole fleet", never "nothing" -- see
    `contracts.service.scoped_asset_ids` for why those must stay distinct."""
    return scoped_assets is None or asset_id in scoped_assets


def _rule_code_in_scope(rule_code: str, *, policy: NotificationPolicy) -> bool:
    # Severity deliberately not consulted: this is the one half of "in scope"
    # stable enough to gate which episodes are candidates for a baseline
    # snapshot at all -- severity is exactly the dimension a snapshot exists
    # to watch for changing, so it cannot also gate whether one gets captured.
    return not policy.rule_codes_json or rule_code in policy.rule_codes_json


def _is_backlog(episode: NotificationEpisode, *, policy: NotificationPolicy) -> bool:
    # Episode-level `opened_at`, not the current incident's -- preserved
    # across a flap merge (notifications/episodes.py), so a reopened episode
    # stays correctly classified as the same pre-existing backlog it always
    # was, instead of looking "new" just because the underlying
    # DiagnosticIncident got a fresh id.
    return policy.baseline_at is not None and episode.opened_at < policy.baseline_at


def _capture_backlog_snapshots(
    session: Session,
    *,
    policy: NotificationPolicy,
    now: datetime,
    scoped_assets: set[int] | None,
    open_episodes: list[NotificationEpisode],
    last_incident_by_episode: dict[int, DiagnosticIncident],
) -> None:
    """Freeze a baseline severity for every open, pre-existing episode this
    policy could ever care about -- run on *every* decide pass, before the
    per-kind branches below, specifically so a snapshot exists from the
    earliest possible evaluation, even for an episode currently out of scope
    on severity alone (a warning under a critical-only policy, most
    commonly). Capturing lazily only once an episode is already in scope
    would be too late: by then its *current* severity is whatever it just
    transitioned to, and comparing that against itself can never detect a
    transition. Idempotent per (episode's first incident, policy) -- see
    `_get_or_create_baseline_snapshot` -- so this is a no-op on every pass
    after the first for a given episode, including across a flap.
    """
    if policy.baseline_at is None:
        return
    for episode in open_episodes:
        if episode.opened_at >= policy.baseline_at:
            continue
        if not _asset_in_scope_id(episode.asset_id, scoped_assets=scoped_assets):
            continue
        incident = last_incident_by_episode.get(episode.id)
        if incident is None or not _rule_code_in_scope(incident.rule_code, policy=policy):
            continue
        _get_or_create_baseline_snapshot(
            session, incident_id=episode.first_incident_id, severity=incident.severity, policy=policy, now=now
        )


def _get_or_create_baseline_snapshot(
    session: Session, *, incident_id: int, severity: str, policy: NotificationPolicy, now: datetime
) -> NotificationBaselineSnapshot:
    """Freeze, once, what a pre-existing episode's severity looked like the
    first time this policy ever evaluated it at/after its own `baseline_at`
    -- see the model docstring for why `opened_at` alone cannot answer "did
    this change since baseline". Keyed on `incident_id`, which the caller
    always passes as `episode.first_incident_id` -- the one incident id an
    episode never changes, even across a flap merge, which is what makes
    "already captured" survive a reopen instead of re-triggering on every
    flap. Idempotent and restart-safe like `_create_event`: a SAVEPOINT
    absorbs a concurrent insert racing for the same (incident_id, policy_id)
    identity, and the loser re-reads what the winner just committed instead
    of erroring or creating a second one.
    """
    existing = session.scalar(
        select(NotificationBaselineSnapshot).where(
            NotificationBaselineSnapshot.incident_id == incident_id,
            NotificationBaselineSnapshot.policy_id == policy.id,
        )
    )
    if existing is not None:
        return existing
    snapshot = NotificationBaselineSnapshot(
        incident_id=incident_id, policy_id=policy.id, captured_at=now,
        severity_at_capture=severity, created_at=now, updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(snapshot)
            session.flush()
    except IntegrityError:
        won = session.scalar(
            select(NotificationBaselineSnapshot).where(
                NotificationBaselineSnapshot.incident_id == incident_id,
                NotificationBaselineSnapshot.policy_id == policy.id,
            )
        )
        assert won is not None  # the only thing that could have made our insert fail
        return won
    return snapshot


def _has_event(session: Session, *, episode_id: int, kind: str, channel_id: int) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    NotificationEvent.episode_id == episode_id,
                    NotificationEvent.kind == kind,
                    NotificationEvent.channel_id == channel_id,
                )
            )
        )
    )


def _has_active_work(session: Session, *, asset_id: int, on: date) -> bool:
    """Whether the installation this asset sits on already has a visit
    planned for today or work in progress -- req 14: this suppresses the
    duration-driven reminder cadence, not because the problem stopped
    mattering, but because someone already knows and is on it.
    """
    installation_id = session.scalar(select(Asset.installation_id).where(Asset.id == asset_id))
    if installation_id is None:
        return False
    return bool(
        session.scalar(
            select(
                exists().where(
                    WorkOrder.installation_id == installation_id,
                    WorkOrder.status.in_(("planned", "in_progress")),
                    (WorkOrder.status == "in_progress") | (WorkOrder.planned_date == on),
                )
            )
        )
    )


def _decide_for_policy(
    session: Session, *, policy: NotificationPolicy, channel: NotificationChannel, now: datetime
) -> tuple[int, int]:
    created = skipped = 0
    # Resolved once per pass rather than per episode: the O&M portfolio does
    # not change while a single evaluation runs, and asking per episode would
    # turn one query into one per open episode.
    scoped = scoped_asset_ids(session, asset_scope=policy.asset_scope, on=now.date())

    open_episodes = list(session.scalars(select(NotificationEpisode).where(NotificationEpisode.status == "open")))
    last_incident_by_episode = {
        episode.id: session.get(DiagnosticIncident, episode.last_incident_id) for episode in open_episodes
    }

    if policy.notify_on_open or policy.escalation_after_minutes:
        _capture_backlog_snapshots(
            session, policy=policy, now=now, scoped_assets=scoped,
            open_episodes=open_episodes, last_incident_by_episode=last_incident_by_episode,
        )

    def _in_scope(episode: NotificationEpisode) -> DiagnosticIncident | None:
        """The episode's current driving incident, if this policy speaks
        for it right now -- `None` otherwise. A single gate shared by every
        branch below so scope is judged identically everywhere."""
        if not _asset_in_scope_id(episode.asset_id, scoped_assets=scoped):
            return None
        incident = last_incident_by_episode.get(episode.id)
        if incident is None or not _in_scope_for(incident.severity, incident.rule_code, policy=policy):
            return None
        return incident

    if policy.notify_on_open:
        for episode in open_episodes:
            incident = _in_scope(episode)
            if incident is None:
                continue
            if _is_backlog(episode, policy=policy):
                # Pre-existing history, not automatically a new problem to
                # announce -- the same reason V1 needed ALERT_BASELINE_AT
                # before it could turn alerts on at all without a storm.
                # But "pre-existing" must not mean "permanently invisible":
                # a snapshot of the severity this policy saw at/just after
                # baseline is the fixed reference point that tells the two
                # cases apart -- see NotificationBaselineSnapshot.
                snapshot = _get_or_create_baseline_snapshot(
                    session, incident_id=episode.first_incident_id, severity=incident.severity, policy=policy, now=now
                )
                if _in_scope_for(snapshot.severity_at_capture, incident.rule_code, policy=policy):
                    continue  # already matched this policy's scope at baseline -- nothing changed
                # else: a genuine post-baseline transition into scope --
                # treated exactly like a fresh "opened", not a "skipped".
            if episode.eligible_at is None:
                continue  # req 2: still inside the 0-30min silence window
            if episode.notified_at is not None:
                continue  # already told someone about this episode -- a flap must not re-tell them
            if _has_event(session, episode_id=episode.id, kind="opened", channel_id=channel.id):
                continue
            event = _create_event(session, episode=episode, incident=incident, policy=policy, channel=channel, kind="opened", now=now)
            created, skipped = _tally(created, skipped, event)
            if event is not None:
                episode.notified_at = episode.notified_at or now

    if policy.escalation_after_minutes:
        threshold = policy.escalation_after_minutes
        for episode in open_episodes:
            incident = _in_scope(episode)
            if incident is None:
                continue
            if _is_backlog(episode, policy=policy):
                # Same snapshot rule as notify_on_open, and duration since
                # opened_at is deliberately *not* consulted here at all: a
                # backlog episode has been "old" since before this policy
                # existed, so age alone can never be this branch's signal
                # for it -- only a genuine scope transition can be. Without
                # this, every already-old backlog episode would clear the
                # age check on the very first evaluation and escalate in a
                # storm, baseline or not.
                snapshot = _get_or_create_baseline_snapshot(
                    session, incident_id=episode.first_incident_id, severity=incident.severity, policy=policy, now=now
                )
                if _in_scope_for(snapshot.severity_at_capture, incident.rule_code, policy=policy):
                    continue  # unchanged backlog -- never escalates on age alone
                # transitioned into scope after baseline: the transition
                # itself is the trigger, notify now, do not also wait out
                # the duration threshold on top of it.
            else:
                age_minutes = (now - episode.opened_at).total_seconds() / 60
                if age_minutes < threshold:
                    continue
            if _has_event(session, episode_id=episode.id, kind="escalated", channel_id=channel.id):
                continue
            event = _create_event(session, episode=episode, incident=incident, policy=policy, channel=channel, kind="escalated", now=now)
            created, skipped = _tally(created, skipped, event)
            if event is not None:
                episode.notified_at = episode.notified_at or now

    if policy.reminder_minutes_json:
        # Reminder cadence (req 3) -- an explicit opt-in per policy
        # (`reminder_minutes_json`, e.g. `[240, 1440]`), not a behaviour
        # every `notify_on_open` policy gains automatically: neither of the
        # two real production policies sets this, and a test or deployment
        # that never configured it must keep seeing exactly what it saw
        # before this mechanism existed. Independent of
        # `escalation_after_minutes`, which is a different, pre-existing
        # mechanism (a warning's one-shot transition into scope), not a
        # cadence -- a policy can use either, both, or neither.
        reminder_minutes = tuple(policy.reminder_minutes_json)
        for episode in open_episodes:
            incident = _in_scope(episode)
            if incident is None:
                continue
            has_work = _has_active_work(session, asset_id=episode.asset_id, on=now.date())
            decision = next_reminder_due(episode, now=now, has_active_work=has_work, reminder_minutes=reminder_minutes)
            if not decision.due:
                continue
            event = _create_event(session, episode=episode, incident=incident, policy=policy, channel=channel, kind="reminder", now=now)
            created, skipped = _tally(created, skipped, event)
            if event is not None:
                episode.reminder_count += 1
                episode.last_reminder_at = now

    if policy.notify_on_resolve:
        # Baseline does not apply here on purpose: a genuinely old problem
        # finally clearing is worth saying regardless of when the policy
        # started watching it. But a recovery message only makes sense for
        # an episode that actually *told someone it was a problem* --
        # `episode.notified_at is None` means this policy never announced it
        # (baseline-excluded, or never in scope while open), and that
        # clearing is not a recovery from the channel's point of view, it is
        # silence resolving into more silence.
        closed_episodes = session.scalars(select(NotificationEpisode).where(NotificationEpisode.status == "closed"))
        for episode in closed_episodes:
            if not _asset_in_scope_id(episode.asset_id, scoped_assets=scoped):
                continue
            incident = session.get(DiagnosticIncident, episode.last_incident_id)
            if incident is None or not _in_scope_for(incident.severity, incident.rule_code, policy=policy):
                continue
            if episode.notified_at is None:
                continue
            if _has_event(session, episode_id=episode.id, kind="resolved", channel_id=channel.id):
                continue
            event = _create_event(session, episode=episode, incident=incident, policy=policy, channel=channel, kind="resolved", now=now)
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
    episode: NotificationEpisode,
    incident: DiagnosticIncident,
    policy: NotificationPolicy,
    channel: NotificationChannel,
    kind: str,
    now: datetime,
) -> NotificationEvent | None:
    asset = session.get(Asset, incident.asset_id)
    device = session.get(Device, incident.device_id) if incident.device_id else None
    message, render_error = _render_enriched_or_fallback(
        session, episode=episode, incident=incident, asset=asset, device=device, kind=kind, now=now
    )
    evidence = {
        "rule_code": incident.rule_code,
        "severity": incident.severity,
        "problem_family": episode.problem_family,
        "asset_name": asset.canonical_name if asset else None,
        "device_label": device.label if device else None,
        "opened_at": episode.opened_at.isoformat(),
        "occurrence_count": incident.occurrence_count,
        "flap_count": episode.flap_count,
    }
    if render_error is not None:
        evidence["render_error"] = render_error
    if kind == "resolved" and episode.closed_at is not None:
        evidence["duration_minutes"] = round((episode.closed_at - episode.opened_at).total_seconds() / 60, 1)
    if kind == "reminder":
        evidence["reminder_count"] = episode.reminder_count + 1

    channel_ready = policy.enabled and channel.enabled
    event = NotificationEvent(
        incident_id=incident.id,
        episode_id=episode.id,
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
    # index on (episode_id, kind, channel_id) is what actually prevents the
    # duplicate for every kind except `reminder`; this only makes losing
    # that race a graceful no-op instead of aborting the whole evaluation
    # pass (proof #7, DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md) -- without a
    # SAVEPOINT, Postgres would leave the entire outer transaction unusable
    # after the first IntegrityError, not just this one insert.
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        return None
    return event


def _render_enriched_or_fallback(
    session: Session,
    *,
    episode: NotificationEpisode,
    incident: DiagnosticIncident,
    asset: Asset | None,
    device: Device | None,
    kind: str,
    now: datetime,
) -> tuple[str, str | None]:
    """The compact, enriched Telegram text (req 9) when it can be built;
    the older, structural fallback text otherwise -- never an exception
    escaping into `decide_notification_events`'s single transaction for the
    whole pass, which one bad message must not be able to abort for every
    other policy/episode being decided in the same run. `render_error`,
    when non-`None`, is recorded on the event's own `evidence_json` --
    a degraded message is a real, auditable event, not a silent one.
    """
    import os

    from nemsei.notifications.enrichment import build_context
    from nemsei.notifications.render_telegram import render_message

    try:
        context = build_context(session, episode=episode, now=now)
        base_url = os.environ.get("NEMSEI_V2_WEB_PUBLIC_BASE_URL", "").strip() or None
        return render_message(context, kind=kind, now=now, base_url=base_url), None
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        fallback = _render_message(incident=incident, episode=episode, asset=asset, device=device, kind=kind, now=now)
        return fallback, f"{type(exc).__name__}: {exc}"


def _render_message(
    *, incident: DiagnosticIncident, episode: NotificationEpisode, asset: Asset | None, device: Device | None,
    kind: str, now: datetime,
) -> str:
    """The internal, structural message this D3-era renderer has always
    produced -- kept as the fallback shape for `reminder` (a kind D3 never
    had) and as-is for the rest. The compact, enriched Telegram format (req
    9) is a later slice's `notifications/render_telegram.py`, wired in on
    top of this, not a replacement for the auditable evidence recorded here.
    """
    asset_name = asset.canonical_name if asset else f"asset #{incident.asset_id}"
    device_part = f" — {device.label}" if device else ""
    if kind == "opened":
        icon = "🔴" if incident.severity == "critical" else "🟠"
        return f"{icon} {incident.rule_code}{device_part} — {asset_name}"
    if kind == "escalated":
        age_minutes = round((now - episode.opened_at).total_seconds() / 60)
        return f"⏫ {incident.rule_code}{device_part} — {asset_name} (aberto há {age_minutes} min)"
    if kind == "reminder":
        age_minutes = round((now - episode.opened_at).total_seconds() / 60)
        return f"🔁 {incident.rule_code}{device_part} — {asset_name} (ainda aberto, há {age_minutes} min)"
    duration = ""
    if episode.closed_at is not None:
        minutes = round((episode.closed_at - episode.opened_at).total_seconds() / 60)
        duration = f" (durou {minutes} min)"
    return f"🟢 {incident.rule_code}{device_part} — {asset_name} recuperado{duration}"


