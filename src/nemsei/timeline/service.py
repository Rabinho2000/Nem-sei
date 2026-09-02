"""One installation's operational history, merged from where it already lives.

Deliberately a projection, not a table. Every event this can show already has
a row somewhere -- a monitoring observation, a diagnostic incident, a
handling note, a work order, a visit -- each already timestamped, already
indexed by asset/device/installation, and each already the one place its own
edits are recorded. Copying that into a `timeline_events` table would either
duplicate a source of truth or drift from it the first time a note is added
or an incident's evidence refreshes. GOAL.md allows either design ("Podes
usar uma entidade TimelineEvent ou uma projeção/read-model ... Evita
duplicação desnecessária de dados") -- nothing here demands the extra table:
the volumes in play (thousands of device readings, low hundreds of
incidents, a handful of work orders) are well inside what one bounded window
can query and merge in Python per request.

The one thing collapsed rather than shown raw: `device_status_facts` is a
poll history, not a change log. A device polled every 30 minutes for a week
produces hundreds of identical "available" rows, and showing all of them
would turn a timeline into a wall of noise. Walking the ordered history and
emitting only the moments `availability_status` actually changes is what
produces GOAL.md's own example -- "14:05 Online → 14:32 Offline → 15:10
Online" -- instead of one line per poll. `monitoring_observations` needs no
equivalent collapse: its own model only gets a new row when the provider's
stated condition changes, so every row already *is* a transition.

Ordering is chronological, oldest first, matching how GOAL.md's own example
reads top to bottom. A caller wanting newest-first reverses the list; this
module does not guess which a screen wants.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.handling import HANDLING_STATE_LABELS
from nemsei.diagnostics.models import DeviceStatusFact, DiagnosticIncident, IncidentNote
from nemsei.monitoring.models import MonitoringObservation
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import Visit, WorkOrder


# How far back a timeline looks when the caller does not say. Wide enough to
# show a real incident's whole life (opened, worked, resolved commonly spans
# days) without scanning years of device polls by default.
DEFAULT_WINDOW = timedelta(days=30)

EVENT_LABELS = {
    "plant_state": "Estado da instalação",
    "device_state": "Estado de equipamento",
    "incident_opened": "Incidente aberto",
    "incident_note": "Andamento do incidente",
    "incident_resolved": "Incidente resolvido",
    "work_order_created": "Trabalho criado",
    "work_order_completed": "Trabalho concluído",
    "visit": "Visita",
}

PLANT_CONDITION_TONES = {"operational": "success", "warning": "warning", "fault": "danger", "offline": "danger"}
AVAILABILITY_TONES = {"available": "success", "standby": "muted", "unavailable": "danger", "unknown": "muted"}
INCIDENT_SEVERITY_TONES = {"critical": "danger", "warning": "warning", "info": "muted"}


@dataclass(frozen=True)
class TimelineEvent:
    """One entry. Never persisted -- see the module docstring."""

    occurred_at: datetime
    #: `"minute"` for anything with a real timestamp, `"day"` for the one
    #: source (`Visit.visit_date`) that only ever records a date. Rendering a
    #: fabricated time for that source would be precision the data does not
    #: have -- the same rule `DATA_RULES.md` states for every other fact here.
    precision: str
    kind: str
    label: str
    detail: str | None
    tone: str
    source_table: str
    source_id: int

    @property
    def kind_label(self) -> str:
        return EVENT_LABELS.get(self.kind, self.kind)


def _as_utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _device_transitions(
    history: list[DeviceStatusFact], *, device_label: str
) -> list[TimelineEvent]:
    """Collapse a poll history into the moments status actually changed.

    `history` is newest-first, per `DeviceStatusRepository.history_for_device`
    -- reversed here to walk it oldest-first, which is the direction a
    transition ("from A to B") reads in.
    """
    events: list[TimelineEvent] = []
    previous: str | None = None
    for fact in reversed(history):
        if fact.availability_status == previous:
            continue
        detail = (
            f"{device_label}: {previous} → {fact.availability_status}"
            if previous is not None
            else f"{device_label}: {fact.availability_status}"
        )
        events.append(
            TimelineEvent(
                occurred_at=_as_utc(fact.observed_at),
                precision="minute",
                kind="device_state",
                label=EVENT_LABELS["device_state"],
                detail=detail,
                tone=AVAILABILITY_TONES.get(fact.availability_status, "muted"),
                source_table="device_status_facts",
                source_id=fact.id,
            )
        )
        previous = fact.availability_status
    return events


def installation_timeline(
    session: Session,
    *,
    installation_id: int,
    since: datetime | None = None,
    until: datetime | None = None,
    device_history_limit: int = 2000,
) -> list[TimelineEvent]:
    """The merged operational history for one installation, oldest first.

    `device_history_limit` bounds each device's fetch to its `limit` most
    recent readings before the window is applied, newest-first. A device with
    more than that many readings in total and a `since` older than what the
    limit reaches will miss a transition that happened before the cutoff --
    an honest bound on a poll history that can run into the thousands per
    device, not a silent one: raise the limit for a genuinely long look-back.

    Spans every `Asset` currently linked to the installation -- one today,
    but the query does not assume it stays that way (GOAL.md's own
    Installation-may-hold-several-Assets case). `Asset.installation_id`
    changing later (an asset moving between sites) is out of scope here: this
    reads the *current* link, the same simplification `installation_state`
    and `contracts.service` already make.
    """
    now = until or utc_now()
    window_start = since or (now - DEFAULT_WINDOW)

    asset_ids = list(session.scalars(select(Asset.id).where(Asset.installation_id == installation_id)))
    events: list[TimelineEvent] = []
    if not asset_ids:
        return events

    # Plant-level condition. Every row already is a transition -- see the
    # module docstring -- so no collapsing is needed here.
    for asset_id, condition, observed_at in session.execute(
        select(MonitoringObservation.asset_id, MonitoringObservation.condition, MonitoringObservation.observed_at)
        .where(
            MonitoringObservation.asset_id.in_(asset_ids),
            MonitoringObservation.observed_at >= window_start,
            MonitoringObservation.observed_at <= now,
        )
        .order_by(MonitoringObservation.observed_at)
    ).all():
        events.append(
            TimelineEvent(
                occurred_at=_as_utc(observed_at),
                precision="minute",
                kind="plant_state",
                label=EVENT_LABELS["plant_state"],
                detail=condition,
                tone=PLANT_CONDITION_TONES.get(condition, "muted"),
                source_table="monitoring_observations",
                source_id=asset_id,
            )
        )

    # Device-level, per device, collapsed to transitions -- widened by one
    # reading on either side of the window so a status already in force when
    # the window opens is not misreported as starting mid-window.
    device_rows = session.execute(
        select(Device.id, Device.label, Device.device_kind).where(Device.asset_id.in_(asset_ids))
    ).all()
    for device_id, label, device_kind in device_rows:
        # Not `DeviceStatusRepository.history_for_device`: that method takes
        # a lower bound (`since`) but no upper one, and this needs both --
        # `until` matters for a timeline asked about a past period, not only
        # "up to now".
        history = list(
            session.scalars(
                select(DeviceStatusFact)
                .where(DeviceStatusFact.device_id == device_id, DeviceStatusFact.observed_at <= now)
                .order_by(DeviceStatusFact.observed_at.desc())
                .limit(device_history_limit)
            )
        )
        device_label = label or f"{device_kind} #{device_id}"
        for event in _device_transitions(history, device_label=device_label):
            if event.occurred_at >= window_start:
                events.append(event)

    # Incidents: opened, every handling note, and resolution.
    incidents = list(
        session.scalars(
            select(DiagnosticIncident).where(
                DiagnosticIncident.asset_id.in_(asset_ids),
                DiagnosticIncident.opened_at <= now,
            )
        )
    )
    incident_ids = [incident.id for incident in incidents]
    for incident in incidents:
        if incident.opened_at >= window_start:
            events.append(
                TimelineEvent(
                    occurred_at=_as_utc(incident.opened_at),
                    precision="minute",
                    kind="incident_opened",
                    label=EVENT_LABELS["incident_opened"],
                    detail=incident.rule_code,
                    tone=INCIDENT_SEVERITY_TONES.get(incident.severity, "warning"),
                    source_table="diagnostic_incidents",
                    source_id=incident.id,
                )
            )
        if incident.resolved_at is not None and window_start <= incident.resolved_at <= now:
            events.append(
                TimelineEvent(
                    occurred_at=_as_utc(incident.resolved_at),
                    precision="minute",
                    kind="incident_resolved",
                    label=EVENT_LABELS["incident_resolved"],
                    detail=incident.rule_code,
                    tone="success",
                    source_table="diagnostic_incidents",
                    source_id=incident.id,
                )
            )

    if incident_ids:
        for note in session.scalars(
            select(IncidentNote).where(
                IncidentNote.incident_id.in_(incident_ids),
                IncidentNote.created_at >= window_start,
                IncidentNote.created_at <= now,
            )
        ):
            state_label = HANDLING_STATE_LABELS.get(note.handling_state_after, note.handling_state_after)
            parts = [part for part in (state_label, note.body) if part]
            events.append(
                TimelineEvent(
                    occurred_at=_as_utc(note.created_at),
                    precision="minute",
                    kind="incident_note",
                    label=EVENT_LABELS["incident_note"],
                    detail=" -- ".join(parts) or None,
                    tone="muted",
                    source_table="incident_notes",
                    source_id=note.id,
                )
            )

    # Work orders and visits are already scoped to the installation directly.
    work_orders = list(
        session.scalars(select(WorkOrder).where(WorkOrder.installation_id == installation_id))
    )
    for work_order in work_orders:
        if window_start <= _as_utc(work_order.created_at) <= now:
            events.append(
                TimelineEvent(
                    occurred_at=_as_utc(work_order.created_at),
                    precision="minute",
                    kind="work_order_created",
                    label=EVENT_LABELS["work_order_created"],
                    detail=work_order.title,
                    tone="muted",
                    source_table="work_orders",
                    source_id=work_order.id,
                )
            )
        if work_order.completed_at is not None and window_start <= work_order.completed_at <= now:
            events.append(
                TimelineEvent(
                    occurred_at=_as_utc(work_order.completed_at),
                    precision="minute",
                    kind="work_order_completed",
                    label=EVENT_LABELS["work_order_completed"],
                    detail=work_order.title,
                    tone="success",
                    source_table="work_orders",
                    source_id=work_order.id,
                )
            )

    work_order_ids = [work_order.id for work_order in work_orders]
    if work_order_ids:
        for visit in session.scalars(select(Visit).where(Visit.work_order_id.in_(work_order_ids))):
            visit_at = _as_utc(visit.visit_date)
            if window_start <= visit_at <= now:
                detail = ", ".join(part for part in (visit.technician, visit.outcome) if part) or None
                events.append(
                    TimelineEvent(
                        occurred_at=visit_at,
                        # A date, never a time -- Visit.visit_date carries no
                        # clock reading. Showing one would be precision this
                        # data does not have.
                        precision="day",
                        kind="visit",
                        label=EVENT_LABELS["visit"],
                        detail=detail,
                        tone="muted",
                        source_table="visits",
                        source_id=visit.id,
                    )
                )

    events.sort(key=lambda event: (event.occurred_at, event.source_table, event.source_id))
    return events
