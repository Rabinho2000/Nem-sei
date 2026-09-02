"""Fold a flapping detector into one notifiable identity.

Telegram O&M redesign. `sync_episodes` is called once per incident-evaluation
pass, immediately after `diagnostics.incidents.evaluate_and_persist_incidents`
in the same transaction (`jobs/handlers.py`) -- it never re-derives a rule's
logic and never touches `diagnostic_incidents`; it only reads the open/just-
resolved rows that pass already wrote and reconciles `NotificationEpisode`
against them.

Two things happen here that D1 deliberately does not do, because they are
notification questions, not detection questions:

1. **Flap merge.** A `DiagnosticIncident` that resolves and reopens within
   `flap_merge_minutes` is, for notification purposes, still the same
   problem -- reopening the same `NotificationEpisode` (`flap_count += 1`,
   `opened_at` preserved) rather than letting a fresh `incident_id` look like
   a fresh problem to `notifications/service.py`.
2. **Eligibility timing.** `eligible_at` is set once an episode's *own*
   `opened_at` has held for its family's `notification_min_duration_minutes`
   -- the 0-30min silence window (req 2). This is a different gate from D1's
   `WARNING_PERSISTENCE_THRESHOLD`: that one decides whether a *warning*
   finding becomes an incident at all, and never applies to `critical`
   findings (which is exactly why a critical `plant_offline` flapping every
   ~16 minutes reached D3 immediately, unfiltered, before this module
   existed). This gate applies after an incident already exists, regardless
   of severity, and only to the notification decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nemsei.diagnostics.findings import SEVERITY_ORDER
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import NotificationEpisode
from nemsei.notifications.problem_families import problem_family_for
from nemsei.shared.clock import utc_now

# How long a resolved episode's identity stays "reopenable" instead of
# starting a fresh one. 60 minutes, not the 30-minute eligibility threshold:
# the two answer different questions (how long silence must persist before
# a *new* problem is worth notifying, versus how long a *closed* problem's
# identity survives so a real flap does not read as six new problems) and
# conflating them would make whichever number is chosen wrong for one of the
# two purposes. Configurable per deployment via `sync_episodes`'s own
# parameter, not currently exposed as a `Settings` field beyond the default
# used by the scheduled job (`jobs/handlers.py`).
DEFAULT_FLAP_MERGE_MINUTES = 60

# How long an episode's own condition must have held, by `opened_at`, before
# it is worth interrupting anyone for -- req 2's own concrete example is
# `plant_offline` specifically ("0-30min -> silêncio, >=30min ->
# notificável"), which is the whole `communication` family: a plant cycling
# offline/online is a connectivity artefact until it has genuinely persisted.
#
# `fault` stays at 0 on purpose, not generalised to 30 like `communication`:
# `device_unavailable`/`plant_fault`/`zero_power_while_peers_active` are real
# equipment alarms, already reviewed and approved as "imediato" for critical
# severity (§28 of docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md, dry-run
# confirmed against production). Silencing those for 30 minutes too would be
# a second, uninvited policy change riding on top of the one actually asked
# for.
#
# `coverage` is 0 for the same reason: those rule_codes already have their
# own timing decision made at the `NotificationPolicy` layer -- no policy at
# all is ever created for them (digest-only, same §28), so this module does
# not add a second, competing timer for them. `fault` also covers the
# production-disparity rule_codes (`zero_power_while_peers_active` and
# friends already classify as `fault` in
# `diagnostics.incident_categories`, a confirmed production loss *is* a
# fault) -- their own timing decision is `escalation_after_minutes` on the
# policy that scopes them, not this table.
DEFAULT_MIN_DURATION_MINUTES = {
    "communication": 30,
    "fault": 0,
    "coverage": 0,
}


def min_duration_for(problem_family: str, *, overrides: dict[str, int] | None = None) -> timedelta:
    table = overrides or DEFAULT_MIN_DURATION_MINUTES
    return timedelta(minutes=table.get(problem_family, 30))


def _worse_severity(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.get(a, 99) <= SEVERITY_ORDER.get(b, 99) else b


def _identity(incident: DiagnosticIncident) -> tuple[int, str, int | None]:
    return (incident.asset_id, problem_family_for(incident.rule_code), incident.device_id)


def _episode_identity(episode: NotificationEpisode) -> tuple[int, str, int | None]:
    return (episode.asset_id, episode.problem_family, episode.device_id)


@dataclass(frozen=True)
class EpisodeSyncSummary:
    episodes_created: int
    episodes_confirmed: int
    episodes_reopened: int
    episodes_closed: int


def sync_episodes(
    session: Session,
    *,
    now: datetime | None = None,
    flap_merge_minutes: int = DEFAULT_FLAP_MERGE_MINUTES,
    min_duration_minutes: dict[str, int] | None = None,
) -> "EpisodeSyncSummary":
    """One reconciliation pass. Idempotent and restart-safe, same discipline
    as `evaluate_and_persist_incidents`: the only state read is
    `diagnostic_incidents` (open rows) and `notification_episodes` itself --
    nothing held in memory between calls.
    """
    now_value = now or utc_now()
    merge_window = timedelta(minutes=flap_merge_minutes)

    open_incidents = list(session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status == "open")))
    open_episodes = list(session.scalars(select(NotificationEpisode).where(NotificationEpisode.status == "open")))
    open_episode_by_identity = {_episode_identity(episode): episode for episode in open_episodes}

    live_identities: set[tuple[int, str, int | None]] = set()
    created = confirmed = reopened = closed = 0

    for incident in open_incidents:
        identity = _identity(incident)
        live_identities.add(identity)
        episode = open_episode_by_identity.get(identity)

        if episode is not None:
            episode.last_incident_id = incident.id
            episode.last_activity_at = now_value
            episode.severity_peak = _worse_severity(episode.severity_peak, incident.severity)
            episode.updated_at = now_value
            confirmed += 1
        else:
            candidate = session.scalar(
                select(NotificationEpisode)
                .where(
                    NotificationEpisode.asset_id == identity[0],
                    NotificationEpisode.problem_family == identity[1],
                    NotificationEpisode.device_id == identity[2]
                    if identity[2] is not None
                    else NotificationEpisode.device_id.is_(None),
                    NotificationEpisode.status == "closed",
                )
                .order_by(NotificationEpisode.closed_at.desc())
                .limit(1)
            )
            if candidate is not None and candidate.closed_at is not None and now_value - candidate.closed_at <= merge_window:
                candidate.status = "open"
                candidate.closed_at = None
                candidate.flap_count += 1
                candidate.last_incident_id = incident.id
                candidate.last_activity_at = now_value
                candidate.severity_peak = _worse_severity(candidate.severity_peak, incident.severity)
                candidate.updated_at = now_value
                episode = candidate
                reopened += 1
            else:
                # A SAVEPOINT, not the outer transaction: two concurrent
                # `sync_episodes` passes (a restart racing the standing
                # scheduler, most plausibly) can both reach this branch for
                # the same identity between the query above and this insert.
                # `uq_notification_episodes_open_identity` is what actually
                # prevents the duplicate; this only makes losing that race a
                # graceful "read what the winner just created" instead of
                # aborting the whole reconciliation pass -- same pattern as
                # `notifications/service.py::_create_event`.
                new_episode = NotificationEpisode(
                    asset_id=identity[0],
                    device_id=identity[2],
                    problem_family=identity[1],
                    status="open",
                    severity_peak=incident.severity,
                    opened_at=incident.opened_at,
                    last_activity_at=now_value,
                    flap_count=1,
                    first_incident_id=incident.id,
                    last_incident_id=incident.id,
                    reminder_count=0,
                    recovery_notified=False,
                    created_at=now_value,
                    updated_at=now_value,
                )
                try:
                    with session.begin_nested():
                        session.add(new_episode)
                        session.flush()
                    episode = new_episode
                    created += 1
                except IntegrityError:
                    won = session.scalar(
                        select(NotificationEpisode).where(
                            NotificationEpisode.asset_id == identity[0],
                            NotificationEpisode.problem_family == identity[1],
                            NotificationEpisode.device_id == identity[2]
                            if identity[2] is not None
                            else NotificationEpisode.device_id.is_(None),
                            NotificationEpisode.status == "open",
                        )
                    )
                    if won is None:
                        # Some other constraint failed -- not the identity
                        # race this branch exists to absorb. Re-raise the
                        # real error rather than a bare `assert`, which would
                        # discard it behind an unrelated AssertionError and
                        # make a genuine bug (or e.g. a starved connection
                        # pool under concurrent load) far harder to diagnose
                        # than the IntegrityError already was.
                        raise
                    won.last_incident_id = incident.id
                    won.last_activity_at = now_value
                    won.severity_peak = _worse_severity(won.severity_peak, incident.severity)
                    won.updated_at = now_value
                    episode = won
                    confirmed += 1
            open_episode_by_identity[identity] = episode

        if episode.eligible_at is None:
            threshold = min_duration_for(identity[1], overrides=min_duration_minutes)
            crosses_at = episode.opened_at + threshold
            if now_value >= crosses_at:
                episode.eligible_at = crosses_at
                episode.updated_at = now_value

    to_close = [episode for identity, episode in open_episode_by_identity.items() if identity not in live_identities]
    resolved_at_by_incident_id = {}
    if to_close:
        resolved_at_by_incident_id = dict(
            session.execute(
                select(DiagnosticIncident.id, DiagnosticIncident.resolved_at).where(
                    DiagnosticIncident.id.in_({episode.last_incident_id for episode in to_close})
                )
            ).all()
        )
    for episode in to_close:
        # The driving incident's own `resolved_at` is real evidence, exactly
        # when the condition actually stopped -- not `now_value`, which is
        # only ever when this reconciliation pass happened to run and would
        # otherwise inflate every episode's recorded duration by however
        # stale the last poll was. Falls back to `now_value` only if the
        # incident row itself somehow has none (defensive; D1 always sets
        # `resolved_at` alongside `status = 'resolved'`).
        episode.status = "closed"
        episode.closed_at = resolved_at_by_incident_id.get(episode.last_incident_id) or now_value
        episode.updated_at = now_value
        closed += 1

    session.flush()
    return EpisodeSyncSummary(episodes_created=created, episodes_confirmed=confirmed, episodes_reopened=reopened, episodes_closed=closed)
