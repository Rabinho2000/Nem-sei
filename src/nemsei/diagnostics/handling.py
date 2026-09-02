"""The human side of an incident: who has it, where it is, and what was said.

Deliberately separate from `diagnostics/incidents.py`, which owns the machine
side. That module opens and resolves incidents from evidence; this one records
what a person decided. Neither writes the other's columns.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import INCIDENT_HANDLING_STATES, DiagnosticIncident, IncidentNote
from nemsei.shared.clock import utc_now


# The same strings `diagnostics/incident.html` and `diagnostics/incidents.html`
# already carry as a Jinja `{% set %}` literal, made available here too so a
# non-template consumer (`timeline/service.py`) has a Python-side source
# instead of a fourth ad-hoc guess at the same five words.
HANDLING_STATE_LABELS: dict[str, str] = {
    "new": "Por triar",
    "acknowledged": "Reconhecido",
    "investigating": "Em análise",
    "visit_scheduled": "Visita marcada",
    "done": "Concluído",
}


def incident_notes(session: Session, *, incident_id: int) -> list[IncidentNote]:
    """The handling history, oldest first. Replaying it gives the whole story."""
    return list(
        session.scalars(
            select(IncidentNote).where(IncidentNote.incident_id == incident_id).order_by(IncidentNote.id)
        )
    )


def record_incident_handling(
    session: Session,
    *,
    incident_id: int,
    actor: str,
    handling_state: str | None = None,
    assigned_to: str | None = None,
    clear_assignment: bool = False,
    note: str | None = None,
) -> IncidentNote:
    """Move an incident along, hand it to someone, leave a note, or all three.

    Never touches `status` or `resolved_at`. Marking an incident `done` says a
    person is finished with it, not that the condition went away -- if the rule
    is still true the detector keeps the incident open, and that disagreement is
    information, not a bug to paper over.
    """
    author = (actor or "").strip()
    if not author:
        raise ValueError("Uma alteração tem de registar quem a fez.")
    incident = session.get(DiagnosticIncident, incident_id)
    if incident is None:
        raise ValueError("Incidente desconhecido.")
    if handling_state is not None and handling_state not in INCIDENT_HANDLING_STATES:
        raise ValueError("Estado de tratamento desconhecido.")

    owner = (assigned_to or "").strip() or None
    body = (note or "").strip() or None
    state_changed = handling_state is not None and handling_state != incident.handling_state
    owner_changed = clear_assignment and incident.assigned_to is not None
    if owner is not None and owner != incident.assigned_to:
        owner_changed = True
    if not state_changed and not owner_changed and body is None:
        raise ValueError("Nada a registar: escreva uma nota ou mude o estado.")

    now = utc_now()
    if state_changed:
        incident.handling_state = handling_state
    if clear_assignment:
        incident.assigned_to = None
    elif owner is not None:
        incident.assigned_to = owner
    if state_changed or owner_changed:
        incident.handling_updated_at = now
    incident.updated_at = now

    entry = IncidentNote(
        incident_id=incident.id,
        author=author[:120],
        body=body,
        handling_state_after=incident.handling_state if state_changed else None,
        assigned_to_after=incident.assigned_to if owner_changed else None,
        created_at=now,
    )
    session.add(entry)
    session.flush()
    return entry
