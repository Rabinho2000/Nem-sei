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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Device
from nemsei.diagnostics.findings import RULES_VERSION, DiagnosticFinding, evaluate_asset_findings, evaluate_plant_findings
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.diagnostics.repository import DeviceStatusRepository
from nemsei.diagnostics.service import current_device_status
from nemsei.monitoring.models import MonitoringCurrentState, MonitoringObservation
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import utc_now
from nemsei.shared.json_safe import json_safe


@dataclass(frozen=True)
class IncidentEvaluationSummary:
    assets_evaluated: int
    incidents_opened: int
    incidents_confirmed: int
    incidents_resolved: int


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

    opened = confirmed = resolved = 0
    for asset_id in asset_ids:
        rows = current_device_status(session, asset_id=asset_id) if asset_id in device_asset_ids else []
        findings = evaluate_asset_findings(
            rows,
            asset_id=asset_id,
            now=now_value,
            history_for_device=lambda device_id: device_repo.history_for_device(device_id=device_id),
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

    return IncidentEvaluationSummary(
        assets_evaluated=len(asset_ids), incidents_opened=opened, incidents_confirmed=confirmed, incidents_resolved=resolved
    )


def _identity(finding: DiagnosticFinding) -> tuple[str, int, int | None]:
    return (finding.rule_code, finding.asset_id, finding.device_id)


def _reconcile_asset_incidents(
    session: Session, *, asset_id: int, findings: list[DiagnosticFinding], now: datetime
) -> tuple[int, int, int]:
    current_by_identity = {_identity(finding): finding for finding in findings}
    open_incidents = list(
        session.scalars(
            select(DiagnosticIncident).where(DiagnosticIncident.asset_id == asset_id, DiagnosticIncident.status == "open")
        )
    )
    open_by_identity = {(incident.rule_code, incident.asset_id, incident.device_id): incident for incident in open_incidents}

    opened = confirmed = resolved = 0

    for identity, finding in current_by_identity.items():
        incident = open_by_identity.get(identity)
        if incident is None:
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
    return opened, confirmed, resolved
