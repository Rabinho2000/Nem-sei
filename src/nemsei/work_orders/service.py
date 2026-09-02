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
    WORK_ORDER_STATUSES,
    WORK_TYPES,
    Visit,
    WorkOrder,
    WorkOrderIncident,
)


def create_work_order(
    session: Session,
    *,
    installation_id: int,
    work_type: str,
    title: str,
    created_by: str,
    status: str = "open",
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
    rather than typing in separately."""
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
