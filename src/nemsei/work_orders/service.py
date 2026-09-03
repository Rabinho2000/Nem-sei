"""Creating and reading work orders, visits, and their link to incidents."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import (
    MATERIAL_STATUSES,
    PRIORITIES,
    WORK_ORDER_STATUSES,
    WORK_TYPES,
    Visit,
    WorkOrder,
    WorkOrderIncident,
)

# Non-terminal: what "open work" means everywhere in this module --
# `overdue_work_orders`/`unscheduled_work_orders`/`planning_page` and the
# incident-linking helpers below all use this same pair, never a
# locally-redefined one that could drift from it.
TERMINAL_STATUSES = ("completed", "cancelled")


def create_work_order(
    session: Session,
    *,
    installation_id: int,
    work_type: str,
    title: str,
    created_by: str,
    status: str = "open",
    priority: str = "normal",
    description: str | None = None,
    planned_date: date | None = None,
    due_date: date | None = None,
    assigned_to: str | None = None,
    material_status: str = "not_applicable",
    material_notes: str | None = None,
    estimated_cost_eur: Decimal | None = None,
    incident_ids: Sequence[int] | None = None,
) -> WorkOrder:
    """Open a work order, optionally linked to the incident(s) it addresses."""
    actor = (created_by or "").strip()
    if not actor:
        raise ValueError("Um trabalho tem de registar quem o criou.")
    if session.get(Installation, installation_id) is None:
        raise ValueError("Instalação desconhecida.")
    if work_type not in WORK_TYPES:
        raise ValueError("Tipo de trabalho desconhecido.")
    if status not in WORK_ORDER_STATUSES:
        raise ValueError("Estado de trabalho desconhecido.")
    if priority not in PRIORITIES:
        raise ValueError("Prioridade desconhecida.")
    if material_status not in MATERIAL_STATUSES:
        raise ValueError("Estado de material desconhecido.")
    name = (title or "").strip()
    if not name:
        raise ValueError("Um trabalho tem de ter um título.")
    if due_date is not None and planned_date is not None and due_date < planned_date:
        raise ValueError("A data limite não pode ser anterior à data planeada.")
    if status == "completed":
        raise ValueError("Um trabalho não pode nascer já concluído; registe a visita que o concluiu.")
    if estimated_cost_eur is not None and estimated_cost_eur < 0:
        raise ValueError("O custo estimado não pode ser negativo.")

    now = utc_now()
    work_order = WorkOrder(
        installation_id=installation_id,
        work_type=work_type,
        status=status,
        priority=priority,
        title=name,
        description=(description or "").strip() or None,
        planned_date=planned_date,
        due_date=due_date,
        assigned_to=(assigned_to or "").strip() or None,
        material_status=material_status,
        material_notes=(material_notes or "").strip() or None,
        estimated_cost_eur=estimated_cost_eur,
        created_by=actor[:120],
        created_at=now,
        updated_at=now,
    )
    session.add(work_order)
    session.flush()

    for incident_id in dict.fromkeys(incident_ids or ()):
        link_incident(session, work_order_id=work_order.id, incident_id=incident_id)
    return work_order


def link_incident(session: Session, *, work_order_id: int, incident_id: int) -> WorkOrderIncident:
    """Attach one incident to one work order. Idempotent: linking twice is a
    no-op, not a duplicate row -- `uq_work_order_incidents_pair` enforces it,
    this just avoids relying on the caller to catch the constraint."""
    if session.get(WorkOrder, work_order_id) is None:
        raise ValueError("Trabalho desconhecido.")
    if session.get(DiagnosticIncident, incident_id) is None:
        raise ValueError("Incidente desconhecido.")
    existing = session.scalar(
        select(WorkOrderIncident).where(
            WorkOrderIncident.work_order_id == work_order_id, WorkOrderIncident.incident_id == incident_id
        )
    )
    if existing is not None:
        return existing
    link = WorkOrderIncident(work_order_id=work_order_id, incident_id=incident_id, created_at=utc_now())
    session.add(link)
    session.flush()
    return link


def update_work_order_status(
    session: Session, *, work_order_id: int, status: str, actor: str, completed_at: datetime | None = None
) -> WorkOrder:
    """Move a work order along. Completing it requires a `completed_at`,
    which a caller normally derives from the visit that finished the job
    rather than typing in separately.

    Marking a work order `completed` never touches any incident it is
    linked to -- "trabalho concluído" and "problema tecnicamente resolvido"
    are different questions, and only the diagnostics evaluator answers the
    second one, from evidence. See `web/work_order_queries.py` for the
    banner that surfaces the two disagreeing to an operator.
    """
    author = (actor or "").strip()
    if not author:
        raise ValueError("Uma mudança de estado tem de registar quem a fez.")
    work_order = session.get(WorkOrder, work_order_id)
    if work_order is None:
        raise ValueError("Trabalho desconhecido.")
    if status not in WORK_ORDER_STATUSES:
        raise ValueError("Estado de trabalho desconhecido.")
    if status == "completed" and completed_at is None:
        completed_at = utc_now()
    if status != "completed":
        completed_at = None
    work_order.status = status
    work_order.completed_at = completed_at
    work_order.updated_by = author[:120]
    work_order.updated_at = utc_now()
    session.flush()
    return work_order


def add_visit(
    session: Session,
    *,
    work_order_id: int,
    visit_date: date,
    created_by: str,
    technician: str | None = None,
    outcome: str | None = None,
    notes: str | None = None,
) -> Visit:
    """Record one physical visit against a work order."""
    actor = (created_by or "").strip()
    if not actor:
        raise ValueError("Uma visita tem de registar quem a registou.")
    if session.get(WorkOrder, work_order_id) is None:
        raise ValueError("Trabalho desconhecido.")
    now = utc_now()
    visit = Visit(
        work_order_id=work_order_id,
        visit_date=visit_date,
        technician=(technician or "").strip() or None,
        outcome=(outcome or "").strip() or None,
        notes=(notes or "").strip() or None,
        created_by=actor[:120],
        created_at=now,
    )
    session.add(visit)
    session.flush()
    return visit


def work_orders_for_installation(session: Session, *, installation_id: int) -> list[WorkOrder]:
    """Every work order at one site, most recently planned first."""
    rows = session.scalars(
        select(WorkOrder).where(WorkOrder.installation_id == installation_id)
    ).all()
    return sorted(rows, key=lambda wo: (wo.planned_date or date.min, wo.id), reverse=True)


def incidents_for_work_order(session: Session, *, work_order_id: int) -> list[DiagnosticIncident]:
    return list(
        session.scalars(
            select(DiagnosticIncident)
            .join(WorkOrderIncident, WorkOrderIncident.incident_id == DiagnosticIncident.id)
            .where(WorkOrderIncident.work_order_id == work_order_id)
            .order_by(DiagnosticIncident.id)
        )
    )


def work_orders_for_incident(session: Session, *, incident_id: int) -> list[WorkOrder]:
    """Every work order addressing one incident -- an incident can spawn more
    than one, if the first attempt did not fix it."""
    return list(
        session.scalars(
            select(WorkOrder)
            .join(WorkOrderIncident, WorkOrderIncident.work_order_id == WorkOrder.id)
            .where(WorkOrderIncident.incident_id == incident_id)
            .order_by(WorkOrder.id)
        )
    )


def open_work_order_counts(session: Session, *, installation_ids: Iterable[int]) -> dict[int, int]:
    """How many non-terminal work orders each installation has, for a list
    screen that must not run one query per row."""
    ids = list(dict.fromkeys(installation_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(WorkOrder.installation_id, func.count(WorkOrder.id))
        .where(WorkOrder.installation_id.in_(ids), WorkOrder.status.notin_(("completed", "cancelled")))
        .group_by(WorkOrder.installation_id)
    ).all()
    counts = {installation_id: int(count) for installation_id, count in rows}
    return {installation_id: counts.get(installation_id, 0) for installation_id in ids}


def overdue_work_orders(session: Session, *, on: date | None = None) -> list[WorkOrder]:
    """Work orders whose due date has passed and are still open work,
    soonest-overdue first -- the "trabalhos atrasados" list."""
    moment = on or utc_now().date()
    rows = session.scalars(
        select(WorkOrder)
        .where(
            WorkOrder.due_date.is_not(None),
            WorkOrder.due_date < moment,
            WorkOrder.status.notin_(("completed", "cancelled")),
        )
        .order_by(WorkOrder.due_date)
    ).all()
    return list(rows)


def unscheduled_work_orders(session: Session) -> list[WorkOrder]:
    """Open work with no planned date -- the "trabalhos sem data" list."""
    rows = session.scalars(
        select(WorkOrder)
        .where(WorkOrder.planned_date.is_(None), WorkOrder.status.notin_(("completed", "cancelled")))
        .order_by(WorkOrder.created_at)
    ).all()
    return list(rows)


def open_work_orders_for_incident(session: Session, *, incident_id: int) -> list[WorkOrder]:
    """Non-terminal work orders already addressing one incident, most recent
    first -- what the "criar trabalho" screen shows before it lets an
    operator create another, so a second trabalho for the same incident is
    always a deliberate, secondary action, never the accidental default."""
    return list(
        session.scalars(
            select(WorkOrder)
            .join(WorkOrderIncident, WorkOrderIncident.work_order_id == WorkOrder.id)
            .where(WorkOrderIncident.incident_id == incident_id, WorkOrder.status.notin_(TERMINAL_STATUSES))
            .order_by(WorkOrder.created_at.desc())
        )
    )


def open_work_order_summary_for_incidents(
    session: Session, *, incident_ids: Iterable[int]
) -> dict[int, WorkOrder]:
    """One representative open work order per incident id -- the most
    recently created -- for a list page (the incidents list, an
    installation's Operação tab) that must not run one query per row to
    answer "does this incident already have work open".

    An incident linked to more than one open work order (a repeat attempt
    still in progress) picks the newest; the page only needs to say "yes,
    something is open", not enumerate every one -- `work_orders_for_incident`
    is there for whoever needs the full list.
    """
    ids = list(dict.fromkeys(incident_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(WorkOrderIncident.incident_id, WorkOrder)
        .join(WorkOrder, WorkOrder.id == WorkOrderIncident.work_order_id)
        .where(WorkOrderIncident.incident_id.in_(ids), WorkOrder.status.notin_(TERMINAL_STATUSES))
        .order_by(WorkOrderIncident.incident_id, WorkOrder.created_at.desc())
    ).all()
    summary: dict[int, WorkOrder] = {}
    for incident_id, work_order in rows:
        summary.setdefault(incident_id, work_order)
    return summary
