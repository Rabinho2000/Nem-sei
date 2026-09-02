"""Pure notification-eligibility gates over a `NotificationEpisode`.

Telegram O&M redesign, reqs 2-4 and 14. Every function here takes plain
values (a `NotificationEpisode` row already fetched, or scalars) and returns
a plain answer -- no session, no Telegram, no clock reached internally. That
is what makes the scenarios in req 17 (16min -> nothing, 31min -> one
message, reminder at 4h, a planned WorkOrder suppressing it...) testable as
direct function calls, not integration tests.

This module answers *whether*; `notifications/service.py` is still the only
place that *acts* (creates a `NotificationEvent`), same separation D3 already
established between deciding and delivering.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from nemsei.notifications.models import NotificationEpisode

# "reminder após 4h se continuar crítico; reminder após 24h" -- req 3's own
# suggested numbers, applied in order: the first reminder not yet sent whose
# threshold has been reached is the one due. A deployment that wants a
# different cadence passes its own list; nothing here hardcodes exactly two.
DEFAULT_REMINDER_MINUTES: tuple[int, ...] = (240, 1440)

# What counts as "duração significativa" for an immediate (not digested)
# recovery, when the episode was never actually critical. Deliberately the
# same number as the first reminder tier -- one definition of "this was a big
# deal", not two competing ones.
DEFAULT_SIGNIFICANT_DURATION_MINUTES = 240


def is_silent_period(episode: NotificationEpisode) -> bool:
    """The 0-30min window (req 2): still too young to be worth an
    interruption, whatever its severity."""
    return episode.eligible_at is None


def should_notify_open(episode: NotificationEpisode) -> bool:
    """Whether an initial "opened" notification is still owed for this
    episode.

    `notified_at is None` is what actually prevents the storm reported in
    the request: a flap that reopens the same `NotificationEpisode`
    (`notifications/episodes.py`) never clears `notified_at`, so it is never
    "still owed" a second time just because the underlying
    `DiagnosticIncident` got a new id.
    """
    return episode.status == "open" and episode.eligible_at is not None and episode.notified_at is None


def should_notify_recovery_immediately(
    episode: NotificationEpisode, *, significant_duration_minutes: int = DEFAULT_SIGNIFICANT_DURATION_MINUTES
) -> bool:
    """Req 4's hard floor plus its immediate/digest split.

    The floor: an episode that was never actually notified (`notified_at is
    None`) never gets a recovery message of any kind, immediate or digested
    -- "nunca enviar recuperado se a falha correspondente nunca foi
    enviada". Among episodes that *were* notified, immediate is reserved for
    the ones a digest would be too slow for: still critical at its worst, a
    reminder had already gone out (it was already escalating), or it ran
    long enough to matter even at a lower severity. Everything else that was
    notified but does not clear this bar is still eligible for the grouped
    recovery digest (`notifications/digests.py`) -- not silence, a slower
    channel.
    """
    if episode.notified_at is None:
        return False
    if episode.severity_peak == "critical":
        return True
    if episode.reminder_count > 0:
        return True
    if episode.closed_at is not None:
        duration = episode.closed_at - episode.opened_at
        if duration >= timedelta(minutes=significant_duration_minutes):
            return True
    return False


def eligible_for_recovery_digest(episode: NotificationEpisode) -> bool:
    """The complement of `should_notify_recovery_immediately`, restricted to
    episodes the hard floor allows to be mentioned at all. Used by
    `notifications/digests.py` to build the grouped "recuperações" digest
    (req 13) without re-deriving the floor.
    """
    if episode.notified_at is None:
        return False
    return not should_notify_recovery_immediately(episode)


@dataclass(frozen=True)
class ReminderDecision:
    due: bool
    threshold_minutes: int | None
    reason: str


def next_reminder_due(
    episode: NotificationEpisode,
    *,
    now: datetime,
    has_active_work: bool = False,
    reminder_minutes: tuple[int, ...] = DEFAULT_REMINDER_MINUTES,
) -> ReminderDecision:
    """Whether a reminder is due right now, and why (or why not) -- req 3's
    cadence, req 14's WorkOrder suppression.

    Reminders only apply to episodes still genuinely open, already notified
    once (a reminder about something nobody was ever told about is not a
    reminder, it is a first notification -- `should_notify_open`'s job), and
    still at their worst (critical) severity: a warning that already
    escalated into a critical `opened` moves through `should_notify_open`
    again for that transition, not through this reminder cadence.

    `has_active_work` -- a planned visit today or work already in progress
    for this installation -- suppresses the *cadence-driven* reminder
    entirely (req 14: "não quero receber o mesmo reminder de 4h como se
    ninguém estivesse a tratar"). It does not suppress a severity-increase
    notification, because that is new information regardless of whether
    someone is already working the problem -- and a severity increase is not
    this function's concern at all; it always reaches the operator through
    `should_notify_open` the moment the episode's `eligible_at`/`notified_at`
    state next allows it, which `notifications/episodes.py` keeps live on
    every evaluation pass.
    """
    if episode.status != "open":
        return ReminderDecision(False, None, "episode is not open")
    if episode.notified_at is None:
        return ReminderDecision(False, None, "episode was never notified; not a reminder candidate")
    if episode.severity_peak != "critical":
        return ReminderDecision(False, None, "reminder cadence only applies while still critical")
    if has_active_work:
        return ReminderDecision(False, None, "work is already planned or in progress for this installation")

    tier_index = episode.reminder_count
    if tier_index >= len(reminder_minutes):
        return ReminderDecision(False, None, "every configured reminder tier has already been sent")
    threshold = reminder_minutes[tier_index]
    elapsed = now - episode.opened_at
    if elapsed < timedelta(minutes=threshold):
        return ReminderDecision(False, threshold, f"only {elapsed} elapsed, {threshold}min tier not reached yet")
    return ReminderDecision(True, threshold, f"{threshold}min tier reached, still critical, no active work")
