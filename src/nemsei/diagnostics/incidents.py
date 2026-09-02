"""Diagnostic incidents (D1): deduplicated, persisted episodes of a finding.

`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` -- Option A. `findings.py`
stays exactly as it was: recomputed per request, no persistence, still the
only place a rule's logic lives. This module adds one thing findings.py
deliberately does not have: memory across evaluation runs, so a persistent
problem is one row that gets confirmed repeatedly, not a hundred independent
observations, and a fixed problem's incident actually closes.

The evaluator here is the only writer of `diagnostic_incidents`. It never
re-derives a rule's logic -- it calls `evaluate_asset_findings` (the same
function the UI calls) and only decides what to do with the *persistence* of
what that function returns: open a new incident, confirm an existing one, or
resolve one whose finding stopped appearing this run.

Persistence policy, closed here rather than left implicit: `info` severity
never becomes an incident -- `partial_device_coverage` is the only rule at
that severity today and has never fired in production, but the code path
existed and would have created one the day it did, against the explicit
"alarme informativo não cria incidente" policy. `warning` severity is
deferred until the finding's own evidenced `active_since` has held for
`WARNING_PERSISTENCE_THRESHOLD` -- a transient blip stays a finding
`findings.py` can still show on the live diagnostics page, it just does not
become a persisted, notifiable incident until it has actually persisted.
`critical` bypasses both: an alarme crítico creates immediately, exactly as
before this module existed.

The gate reads `active_since`, not "how long has this evaluator been
running" -- a warning already 25 hours old by real evidence the *first* time
this code sees it (a backfilled fact, a restarted evaluator catching up)
qualifies immediately, because it has, in fact, already persisted that long.
The 15-minute wait only ever applies to a genuinely new condition.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.findings import RULES_VERSION, DiagnosticFinding, evaluate_asset_findings, evaluate_plant_findings
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.diagnostics.repository import DeviceStatusRepository
from nemsei.diagnostics.service import current_device_status
from nemsei.installations.models import Installation
from nemsei.monitoring.models import MonitoringCurrentState, MonitoringObservation
from nemsei.monitoring.production_window import window_for
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import utc_now
from nemsei.shared.json_safe import json_safe


# How long a `warning`-severity finding must have actually held, by its own
# evidenced `active_since`, before it is worth interrupting anyone for.
# GOAL.md's own number ("Warning persistente >= 15 min | cria").
WARNING_PERSISTENCE_THRESHOLD = timedelta(minutes=15)

# Rolling window a recurring problem is judged within. GOAL.md's own number
# ("Mesmo problema >= 3 vezes em 24h | marcar/criar como recorrente").
RECURRENCE_WINDOW = timedelta(hours=24)
RECURRENCE_THRESHOLD = 3


@dataclass(frozen=True)
class IncidentEvaluationSummary:
    assets_evaluated: int
    incidents_opened: int
    incidents_confirmed: int
    incidents_resolved: int
    # Findings seen this run that did not (yet) become an incident: an `info`
    # finding, or a `warning` finding still short of
    # `WARNING_PERSISTENCE_THRESHOLD`. Visible here rather than silently
    # dropped -- a run that looks like it did less work than the raw finding
    # count would suggest should say why.
    deferred: int = 0


def _latest_plant_states(session: Session) -> dict[int, tuple[str, datetime, datetime | None]]:
    """Each installation's newest plant reading, and when it was last confirmed.

    Ordered by `observed_at` then `id` so a superseding revision wins over the
    row it replaced -- `monitoring_observations` is append-only, and the older
    revision is still a row.

    The third value is deliberately a different clock. A new observation row is
    written only when the evidence *changes*, so a plant that stays operational
    keeps the same row for days while `MonitoringCurrentState.last_confirmed_at`
    advances on every successful read. "When did we last look" and "since when
    has it been like this" are different questions, and staleness only ever
    asks the first.
    """
    rows = session.execute(
        select(MonitoringObservation.asset_id, MonitoringObservation.condition, MonitoringObservation.observed_at)
        .distinct(MonitoringObservation.asset_id)
        .order_by(MonitoringObservation.asset_id, MonitoringObservation.observed_at.desc(), MonitoringObservation.id.desc())
    ).all()
    confirmations = dict(
        session.execute(
            select(AssetProviderMapping.asset_id, func.max(MonitoringCurrentState.last_confirmed_at))
            .join(MonitoringCurrentState, MonitoringCurrentState.provider_mapping_id == AssetProviderMapping.id)
            .group_by(AssetProviderMapping.asset_id)
        ).all()
    )
    return {
        asset_id: (condition, observed_at, confirmations.get(asset_id))
        for asset_id, condition, observed_at in rows
    }


def evaluate_and_persist_incidents(session: Session, *, now: datetime | None = None) -> IncidentEvaluationSummary:
    """One evaluation pass over every asset with a device or a plant reading.

    Idempotent and restart-safe by construction: the only state this reads is
    the `diagnostic_incidents` table itself (which open incidents exist right
    now) and `device_status_facts` (via `evaluate_asset_findings`) -- nothing
    is held in memory between calls, so a crashed or restarted process picks
    up from exactly what is persisted, the same way `Scheduler`/`Worker`
    already do for jobs. Running this twice in a row with unchanged facts is
    safe: the second run confirms the same incidents again (advancing
    `last_observed_at`/`occurrence_count`, which is the correct behaviour for
    two real evaluation runs, not a bug) rather than duplicating anything --
    the partial unique index on `(rule_code, asset_id, device_id) WHERE
    status='open'` makes a duplicate open incident structurally impossible
    even under a concurrent evaluator race.
    """
    now_value = now or utc_now()
    device_repo = DeviceStatusRepository(session)
    # Two populations, overlapping but neither contained in the other: most
    # installations have a plant reading and no imported devices, and the
    # device-only ones still have to keep being evaluated. Before plant
    # findings existed this was just "assets that own a device", which is why
    # a plant going offline produced no incident and therefore no alert.
    device_asset_ids = set(session.scalars(select(Device.asset_id).distinct()).all())
    plant_states = _latest_plant_states(session)
    asset_ids = sorted(device_asset_ids | set(plant_states))
    coordinates = _asset_coordinates(session, asset_ids=asset_ids)

    opened = confirmed = resolved = deferred = 0
    for asset_id in asset_ids:
        rows = current_device_status(session, asset_id=asset_id) if asset_id in device_asset_ids else []
        latitude, longitude = coordinates.get(asset_id, (None, None))
        findings = evaluate_asset_findings(
            rows,
            asset_id=asset_id,
            now=now_value,
            history_for_device=lambda device_id: device_repo.history_for_device(device_id=device_id),
            production_window=window_for(latitude=latitude, longitude=longitude, at=now_value),
        )
        condition, observed_at, confirmed_at = plant_states.get(asset_id, (None, None, None))
        findings.extend(
            evaluate_plant_findings(
                asset_id=asset_id, condition=condition, observed_at=observed_at, confirmed_at=confirmed_at, now=now_value
            )
        )
        counts = _reconcile_asset_incidents(session, asset_id=asset_id, findings=findings, now=now_value)
        opened += counts[0]
        confirmed += counts[1]
        resolved += counts[2]
        deferred += counts[3]

    return IncidentEvaluationSummary(
        assets_evaluated=len(asset_ids),
        incidents_opened=opened,
        incidents_confirmed=confirmed,
        incidents_resolved=resolved,
        deferred=deferred,
    )


def _asset_coordinates(session: Session, *, asset_ids: list[int]) -> dict[int, tuple[Decimal | None, Decimal | None]]:
    """`(latitude, longitude)` per asset, read through its Installation.

    One query for the whole evaluation run rather than one per asset -- the
    same reasoning `_latest_plant_states` already applies to plant readings.
    An asset with no Installation yet, or an Installation with no
    coordinates, is simply absent from the result; `window_for` already
    reads a missing pair as `unknown`, not as `dark`.
    """
    if not asset_ids:
        return {}
    rows = session.execute(
        select(Asset.id, Installation.latitude, Installation.longitude)
        .join(Installation, Installation.id == Asset.installation_id)
        .where(Asset.id.in_(asset_ids))
    ).all()
    return {asset_id: (latitude, longitude) for asset_id, latitude, longitude in rows}


def _identity(finding: DiagnosticFinding) -> tuple[str, int, int | None]:
    return (finding.rule_code, finding.asset_id, finding.device_id)


def _should_persist(finding: DiagnosticFinding, *, now: datetime) -> bool:
    """Whether a *newly detected* finding is worth becoming an incident yet.

    Only gates the decision to open a new row -- see the module docstring.
    Never consulted for an identity that already has an open incident: a
    finding's severity dropping from critical to warning after opening does
    not retroactively un-open it, and does not need to re-clear this gate to
    keep being confirmed.
    """
    if finding.severity == "info":
        return False
    if finding.severity == "critical":
        return True
    if finding.active_since is None:
        # No duration evidence at all -- `device_no_history` is the one rule
        # that reaches here this way, because there is no reading to walk a
        # history from. `since = finding.active_since or now` would make the
        # elapsed time exactly zero on every single evaluation run forever,
        # since a fresh `now` replaces the missing `active_since` every time
        # -- not "not yet persistent", but structurally unable to ever
        # become persistent. The gate exists to filter transient blips using
        # real duration evidence; with none available, "never had a
        # reading" is not a transient condition waiting to resolve itself,
        # so it is treated the way `critical` is: actionable now.
        return True
    return now - finding.active_since >= WARNING_PERSISTENCE_THRESHOLD


def _reconcile_asset_incidents(
    session: Session, *, asset_id: int, findings: list[DiagnosticFinding], now: datetime
) -> tuple[int, int, int, int]:
    current_by_identity = {_identity(finding): finding for finding in findings}
    open_incidents = list(
        session.scalars(
            select(DiagnosticIncident).where(DiagnosticIncident.asset_id == asset_id, DiagnosticIncident.status == "open")
        )
    )
    open_by_identity = {(incident.rule_code, incident.asset_id, incident.device_id): incident for incident in open_incidents}

    opened = confirmed = resolved = deferred = 0

    for identity, finding in current_by_identity.items():
        incident = open_by_identity.get(identity)
        if incident is None:
            if not _should_persist(finding, now=now):
                deferred += 1
                continue
            # First detection: use the finding's own best-evidenced start
            # (`active_since`, walked from real history when available) as
            # `opened_at` -- not "now", so an episode that was already
            # underway when this evaluator first ran does not falsely claim
            # to have started at evaluator-startup time. `last_observed_at`
            # is always the evaluator's own run time, deliberately distinct:
            # it answers "when did we last confirm this", not "what does the
            # underlying fact say", which is a different question.
            session.add(
                DiagnosticIncident(
                    rule_code=finding.rule_code,
                    asset_id=finding.asset_id,
                    device_id=finding.device_id,
                    severity=finding.severity,
                    status="open",
                    opened_at=finding.active_since or now,
                    last_observed_at=now,
                    occurrence_count=1,
                    detector_version=RULES_VERSION,
                    evidence_json=json_safe(finding.evidence),
                    created_at=now,
                    updated_at=now,
                )
            )
            opened += 1
        else:
            incident.last_observed_at = now
            incident.occurrence_count += 1
            incident.evidence_json = json_safe(finding.evidence)
            incident.severity = finding.severity
            incident.updated_at = now
            confirmed += 1

    for identity, incident in open_by_identity.items():
        if identity not in current_by_identity:
            incident.status = "resolved"
            incident.resolved_at = now
            incident.updated_at = now
            resolved += 1

    session.flush()
    return opened, confirmed, resolved, deferred


def recurrence_count(
    session: Session,
    *,
    rule_code: str,
    asset_id: int,
    device_id: int | None,
    at: datetime | None = None,
    window: timedelta = RECURRENCE_WINDOW,
) -> int:
    """How many episodes of this exact identity opened within `window`
    ending at `at` (inclusive of `at` itself).

    Deliberately a query, not a stored column -- like `contracts.om_status`,
    the fact this counts (`opened_at` on past rows) already exists, and
    storing a second, derived number invites the two disagreeing the moment
    an old row's identity is ever corrected. "Recurring" is a read-time
    question about a stable, append-only history, not a write-time decision.

    Counts episodes, never raw confirmations: each `DiagnosticIncident` row
    is already one deduplicated episode (`models.py`'s own identity
    contract), so counting rows here does not have the density problem
    V1's `count_problem_occurrences_since` had counting raw fact rows.
    """
    moment = at or utc_now()
    device_clause = (
        DiagnosticIncident.device_id == device_id if device_id is not None else DiagnosticIncident.device_id.is_(None)
    )
    return int(
        session.scalar(
            select(func.count(DiagnosticIncident.id)).where(
                DiagnosticIncident.rule_code == rule_code,
                DiagnosticIncident.asset_id == asset_id,
                device_clause,
                DiagnosticIncident.opened_at > moment - window,
                DiagnosticIncident.opened_at <= moment,
            )
        )
        or 0
    )


def is_recurring(session: Session, *, incident: DiagnosticIncident) -> bool:
    """Whether this incident is the `RECURRENCE_THRESHOLD`th-or-later episode
    of its identity within `RECURRENCE_WINDOW`, judged as of its own opening.

    Judged at `incident.opened_at`, not "now": whether an incident *was*
    recurring when it opened does not change afterwards just because time
    passes and the window slides -- that would make an incident quietly stop
    being "the 3rd one this happened" a day later, for no reason connected to
    the incident itself.
    """
    return (
        recurrence_count(
            session,
            rule_code=incident.rule_code,
            asset_id=incident.asset_id,
            device_id=incident.device_id,
            at=incident.opened_at,
        )
        >= RECURRENCE_THRESHOLD
    )
