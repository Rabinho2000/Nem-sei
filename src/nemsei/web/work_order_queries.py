"""Work-order counts keyed by Asset id, for screens whose row is per-Asset.

`work_orders.service` is `installation_id`-scoped, matching how a work order
is dispatched to a site (`work_orders/models.py`). The operational list page's
row is per-Asset (see `installation_queries.py` for why `Asset` stays the
query anchor there); this module is the one place that translation happens,
so it happens once, not once per caller.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import MATERIAL_STATUSES, PRIORITIES, WORK_ORDER_STATUSES, WORK_TYPES, WorkOrder
from nemsei.work_orders.service import (
    TERMINAL_STATUSES,
    incidents_for_work_order,
    open_work_orders_for_incident,
)

_EMPTY_COUNTS = {"overdue": 0, "unscheduled": 0}

WORK_TYPE_LABELS = {"corrective": "Corretiva", "preventive": "Preventiva", "cleaning": "Limpeza"}
WORK_ORDER_STATUS_LABELS = {
    "open": "Aberto",
    "planned": "Planeado",
    "in_progress": "Em curso",
    "waiting_material": "À espera de material",
    "waiting_customer": "À espera do cliente",
    "completed": "Concluído",
    "cancelled": "Cancelado",
}
PRIORITY_LABELS = {"critical": "Crítica", "high": "Alta", "normal": "Normal", "low": "Baixa"}
MATERIAL_STATUS_LABELS = {"not_applicable": "N/A", "pending": "Pendente", "ordered": "Encomendado", "ready": "Pronto"}
# Since when the O&M workflow needs to interrupt someone for it -- see
# `diagnostics/incidents.py`'s own `WARNING_PERSISTENCE_THRESHOLD` for the
# same reasoning applied to opening an incident in the first place. `info`
# incidents are never persisted at all (same module), so there is
# deliberately no entry for it here.
PRIORITY_FROM_SEVERITY = {"critical": "critical", "warning": "normal"}
# How far back "concluídos recentemente" looks in the planning page.
RECENTLY_COMPLETED_WINDOW_DAYS = 7


def overdue_and_unscheduled_counts(session: Session, *, asset_ids: list[int]) -> dict[int, dict[str, int]]:
    """`{overdue, unscheduled}` open work-order counts, per Asset, one query.

    An Asset with no Installation yet (the backfill has not been deployed to
    production) contributes zero rather than raising -- there is nowhere for
    a work order on that asset to be attached yet, which is a true "zero",
    not a hidden error.
    """
    if not asset_ids:
        return {}
    installation_by_asset = dict(
        session.execute(
            select(Asset.id, Asset.installation_id).where(Asset.id.in_(asset_ids), Asset.installation_id.is_not(None))
        ).all()
    )
    result = {asset_id: dict(_EMPTY_COUNTS) for asset_id in asset_ids}
    if not installation_by_asset:
        return result

    today = utc_now().date()
    installation_ids = set(installation_by_asset.values())
    rows = session.execute(
        select(WorkOrder.installation_id, WorkOrder.due_date, WorkOrder.planned_date).where(
            WorkOrder.installation_id.in_(installation_ids), WorkOrder.status.notin_(("completed", "cancelled"))
        )
    ).all()
    by_installation: dict[int, dict[str, int]] = {installation_id: dict(_EMPTY_COUNTS) for installation_id in installation_ids}
    for installation_id, due_date, planned_date in rows:
        if due_date is not None and due_date < today:
            by_installation[installation_id]["overdue"] += 1
        if planned_date is None:
            by_installation[installation_id]["unscheduled"] += 1

    for asset_id, installation_id in installation_by_asset.items():
        result[asset_id] = by_installation[installation_id]
    return result


def work_orders_page(
    session: Session, *, status: str = "", scope: str = "", search: str = "", priority: str = ""
) -> dict[str, Any]:
    """The global "Trabalhos" list: every work order, its installation's name
    (through `Installation` when it exists, else the `Asset`'s own name --
    see `installation_queries.py` for why that fallback exists), overdue and
    unscheduled first when no filter narrows it.

    `scope` is `"overdue"`, `"unscheduled"`, or `""` (everything); `status`
    is one of `WORK_ORDER_STATUSES` or `""`. Neither touches `WorkOrder`'s
    own definition of those states -- this only filters and joins names onto
    rows that already exist.
    """
    today = utc_now().date()
    statement = (
        select(WorkOrder, Installation.display_name, Asset.canonical_name, Asset.id)
        .join(Installation, Installation.id == WorkOrder.installation_id)
        .outerjoin(Asset, Asset.installation_id == Installation.id)
        .order_by(WorkOrder.due_date.is_(None), WorkOrder.due_date, WorkOrder.planned_date.is_(None), WorkOrder.planned_date)
    )
    if status in WORK_ORDER_STATUSES:
        statement = statement.where(WorkOrder.status == status)
    else:
        statement = statement.where(WorkOrder.status.notin_(("completed", "cancelled")))
    if scope == "overdue":
        statement = statement.where(WorkOrder.due_date.is_not(None), WorkOrder.due_date < today)
    elif scope == "unscheduled":
        statement = statement.where(WorkOrder.planned_date.is_(None))
    if priority in PRIORITIES:
        statement = statement.where(WorkOrder.priority == priority)
    if search.strip():
        pattern = f"%{search.strip()}%"
        statement = statement.where(WorkOrder.title.ilike(pattern))

    seen: dict[int, dict[str, Any]] = {}
    for work_order, installation_name, asset_name, asset_id in session.execute(statement).all():
        if work_order.id in seen:
            continue  # the outer join to Asset can repeat a row when >1 asset shares an installation
        seen[work_order.id] = {
            "work_order": work_order,
            "installation_name": installation_name,
            "asset_id": asset_id,
            "asset_name": asset_name,
            "is_overdue": work_order.due_date is not None and work_order.due_date < today,
            "is_unscheduled": work_order.planned_date is None,
        }
    rows = list(seen.values())
    return {
        "rows": rows,
        "status": status,
        "scope": scope,
        "search": search,
        "priority": priority,
        "statuses": WORK_ORDER_STATUSES,
        "priorities": PRIORITIES,
        "overdue_count": sum(1 for row in rows if row["is_overdue"]),
        "unscheduled_count": sum(1 for row in rows if row["is_unscheduled"]),
    }


_PLANNING_HORIZON_DAYS = 7
_BLOCKED_MATERIAL_STATUSES = ("pending", "ordered")


def _planning_row_matches(
    row: dict[str, Any], *, status: str, priority: str, assigned_to: str, client: str, installation: str, work_type: str
) -> bool:
    """The shared filter every planning bucket is drawn from -- filtering
    once before bucketing, never once per bucket, so "estado=X" cannot mean
    something different in one list than in another."""
    work_order = row["work_order"]
    if status and work_order.status != status:
        return False
    if priority and work_order.priority != priority:
        return False
    if assigned_to and assigned_to.lower() not in (work_order.assigned_to or "").lower():
        return False
    if client and client.lower() not in (row["organization_name"] or "").lower():
        return False
    if installation and installation.lower() not in (row["installation_name"] or row["asset_name"] or "").lower():
        return False
    if work_type and work_order.work_type != work_type:
        return False
    return True


def planning_page(
    session: Session,
    *,
    status: str = "",
    priority: str = "",
    assigned_to: str = "",
    client: str = "",
    installation: str = "",
    work_type: str = "",
) -> dict[str, Any]:
    """GOAL.md's dashboard buckets (críticos / esta semana / atrasados /
    hoje / bloqueados / sem data / próximos / concluídos recentemente) as
    their own screen, not just a dashboard count -- a dispatcher needs the
    actual list, not just how many.

    Buckets are independent questions about the same work, not a partition:
    a job can be both overdue and blocked on material, and hiding it from
    one list because it already appeared in the other would lose exactly
    the fact that explains why it is still open. The six filters narrow
    every bucket identically, applied once (`_planning_row_matches`) before
    any bucketing runs -- never five independent queries that could
    disagree about what "estado=X" means.

    `concluidos_recentes` is the one bucket drawn from a second query: every
    other bucket only ever looks at non-terminal work, and folding a
    `completed`/`cancelled` row into that single query would silently widen
    what "trabalho em aberto" means on every other bucket at once.
    """
    today = utc_now().date()
    horizon = today + timedelta(days=_PLANNING_HORIZON_DAYS)
    recent_cutoff = today - timedelta(days=RECENTLY_COMPLETED_WINDOW_DAYS)

    base_statement = (
        select(WorkOrder, Installation.display_name, Asset.canonical_name, Asset.id, Organization.display_name)
        .join(Installation, Installation.id == WorkOrder.installation_id)
        .outerjoin(Asset, Asset.installation_id == Installation.id)
        .outerjoin(Organization, Organization.id == Installation.organization_id)
    )

    def _rows_for(statement) -> list[dict[str, Any]]:
        seen: dict[int, dict[str, Any]] = {}
        for work_order, installation_name, asset_name, asset_id, organization_name in session.execute(statement).all():
            if work_order.id in seen:
                continue  # outer join to Asset can repeat a row when >1 asset shares an installation
            seen[work_order.id] = {
                "work_order": work_order,
                "installation_name": installation_name,
                "asset_id": asset_id,
                "asset_name": asset_name,
                "organization_name": organization_name,
            }
        matching = [
            row for row in seen.values()
            if _planning_row_matches(
                row, status=status, priority=priority, assigned_to=assigned_to,
                client=client, installation=installation, work_type=work_type,
            )
        ]
        return matching

    rows = _rows_for(base_statement.where(WorkOrder.status.notin_(TERMINAL_STATUSES)))
    completed_rows = _rows_for(
        base_statement.where(WorkOrder.status == "completed", WorkOrder.completed_at.is_not(None))
    )

    def sort_by(rows: list[dict[str, Any]], key) -> list[dict[str, Any]]:
        return sorted(rows, key=key)

    criticos = sort_by(
        [row for row in rows if row["work_order"].priority == "critical"],
        lambda row: (row["work_order"].due_date is None, row["work_order"].due_date or today),
    )
    esta_semana = sort_by(
        [row for row in rows if row["work_order"].planned_date is not None and today <= row["work_order"].planned_date <= horizon],
        lambda row: row["work_order"].planned_date,
    )
    atrasados = sort_by(
        [row for row in rows if row["work_order"].due_date is not None and row["work_order"].due_date < today],
        lambda row: row["work_order"].due_date,
    )
    hoje = sort_by(
        [row for row in rows if row["work_order"].planned_date == today],
        lambda row: row["work_order"].title,
    )
    bloqueados = sort_by(
        [
            row for row in rows
            if row["work_order"].material_status in _BLOCKED_MATERIAL_STATUSES
            or row["work_order"].status == "waiting_material"
        ],
        lambda row: (row["work_order"].due_date is None, row["work_order"].due_date or today),
    )
    sem_data = sort_by(
        [row for row in rows if row["work_order"].planned_date is None],
        lambda row: row["work_order"].created_at,
    )
    proximos = sort_by(
        [row for row in rows if row["work_order"].planned_date is not None and row["work_order"].planned_date > horizon],
        lambda row: row["work_order"].planned_date,
    )
    concluidos_recentes = sort_by(
        [row for row in completed_rows if row["work_order"].completed_at.date() >= recent_cutoff],
        lambda row: row["work_order"].completed_at,
    )
    concluidos_recentes.reverse()  # most recently finished first

    return {
        "today": today,
        "horizon_days": _PLANNING_HORIZON_DAYS,
        "recently_completed_window_days": RECENTLY_COMPLETED_WINDOW_DAYS,
        "total_open": len(rows),
        "criticos": criticos,
        "esta_semana": esta_semana,
        "atrasados": atrasados,
        "hoje": hoje,
        "bloqueados": bloqueados,
        "sem_data": sem_data,
        "proximos": proximos,
        "concluidos_recentes": concluidos_recentes,
        "status": status,
        "priority": priority,
        "assigned_to": assigned_to,
        "client": client,
        "installation": installation,
        "work_type": work_type,
        "statuses": WORK_ORDER_STATUSES,
        "priorities": PRIORITIES,
        "work_types": WORK_TYPES,
    }


# --- incident -> work order -------------------------------------------


def suggested_priority(severity: str) -> str:
    """The priority a new work order starts with when created from an
    incident, before the operator can override it -- never a rule the
    incident-evaluator or `contracts.priority` obeys, only a form default.
    `contracts.priority.service_priority` orders *within* a severity band
    and is explicitly never allowed to reorder across one (see
    `docs/v2/DECISIONS.md`); this mapping stays purely severity-based for
    the same reason -- how much money is at risk must never promote a
    stale-reading warning above a plant that is actually down.
    """
    return PRIORITY_FROM_SEVERITY.get(severity, "normal")


def new_work_order_form_context(session: Session, *, incident_id: int) -> dict[str, Any] | None:
    """What the "Criar trabalho" screen needs: the incident and its
    installation already resolved and locked, a suggested title and
    priority, and any work already open against this incident shown first
    -- so creating a second one is always a deliberate secondary action,
    never the accidental default. See `open_work_orders_for_incident`.
    """
    incident = session.get(DiagnosticIncident, incident_id)
    if incident is None:
        return None
    asset = session.get(Asset, incident.asset_id)
    installation = session.get(Installation, asset.installation_id) if asset and asset.installation_id else None
    existing = open_work_orders_for_incident(session, incident_id=incident_id)
    suggested_title = f"{incident.rule_code} — {asset.canonical_name}" if asset else incident.rule_code
    return {
        "incident": incident,
        "asset": asset,
        "installation": installation,
        "has_installation": installation is not None,
        "existing_work_orders": existing,
        "suggested_title": suggested_title[:255],
        "suggested_priority": suggested_priority(incident.severity),
        "work_types": WORK_TYPES,
        "priorities": PRIORITIES,
        "material_statuses": MATERIAL_STATUSES,
        "priority_labels": PRIORITY_LABELS,
        "work_type_labels": WORK_TYPE_LABELS,
        "status_labels": WORK_ORDER_STATUS_LABELS,
    }


# --- work order detail ---------------------------------------------------


def incident_side_banner(*, incident_status: str, work_order_statuses: list[str]) -> str | None:
    """The disagreement an incident's own page must surface, never paper
    over. Neither `create_work_order` nor `update_work_order_status` ever
    touches `DiagnosticIncident.status` (see their own docstrings); this is
    purely a read-time comparison of two independent lifecycles."""
    if not work_order_statuses:
        return None
    any_open = any(s not in TERMINAL_STATUSES for s in work_order_statuses)
    any_completed = any(s == "completed" for s in work_order_statuses)
    if incident_status == "resolved" and any_open:
        return "Monitorização já recuperou; trabalho continua aberto."
    if incident_status == "open" and any_completed and not any_open:
        return "Intervenção concluída; problema ainda não verificado como resolvido."
    return None


def work_order_side_banner(*, work_order_status: str, incident_statuses: list[str]) -> str | None:
    """The same comparison, read from the work order's own page."""
    if not incident_statuses:
        return None
    any_open_incident = any(s == "open" for s in incident_statuses)
    any_resolved_incident = any(s == "resolved" for s in incident_statuses)
    if work_order_status == "completed" and any_open_incident:
        return "Intervenção concluída; problema ainda não verificado como resolvido."
    if work_order_status not in TERMINAL_STATUSES and any_resolved_incident and not any_open_incident:
        return "Monitorização já recuperou; trabalho continua aberto."
    return None


def work_order_detail(session: Session, *, work_order_id: int) -> dict[str, Any] | None:
    """Everything the work-order detail page shows: header, origin
    (incidents), planning, execution (visits) and the technical-status
    split GOAL.md's lifecycle rules require -- "trabalho concluído" and
    "problema verificado como resolvido" are always shown as two separate
    facts, never folded into one badge.
    """
    work_order = session.get(WorkOrder, work_order_id)
    if work_order is None:
        return None
    installation = session.get(Installation, work_order.installation_id)
    organization = session.get(Organization, installation.organization_id) if installation and installation.organization_id else None
    asset = session.scalar(select(Asset).where(Asset.installation_id == work_order.installation_id))
    incidents = incidents_for_work_order(session, work_order_id=work_order.id)

    now = utc_now()
    open_since = now - work_order.created_at

    problem_status = "not_linked"
    if incidents:
        problem_status = "active" if any(incident.status == "open" for incident in incidents) else "resolved"

    return {
        "work_order": work_order,
        "installation": installation,
        "organization_name": organization.display_name if organization else None,
        "asset": asset,
        "incidents": incidents,
        "visits": list(work_order.visits),
        "open_since_days": open_since.days,
        "work_completed": work_order.status == "completed",
        "problem_status": problem_status,
        "banner": work_order_side_banner(
            work_order_status=work_order.status, incident_statuses=[incident.status for incident in incidents]
        ),
        "statuses": WORK_ORDER_STATUSES,
        "priorities": PRIORITIES,
        "material_statuses": MATERIAL_STATUSES,
        "priority_labels": PRIORITY_LABELS,
        "work_type_labels": WORK_TYPE_LABELS,
        "status_labels": WORK_ORDER_STATUS_LABELS,
        "material_status_labels": MATERIAL_STATUS_LABELS,
    }
