"""Work-order counts keyed by Asset id, for screens whose row is per-Asset.

`work_orders.service` is `installation_id`-scoped, matching how a work order
is dispatched to a site (`work_orders/models.py`). The operational list page's
row is per-Asset (see `installation_queries.py` for why `Asset` stays the
query anchor there); this module is the one place that translation happens,
so it happens once, not once per caller.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.installations.models import Installation
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import WORK_ORDER_STATUSES, WorkOrder

_EMPTY_COUNTS = {"overdue": 0, "unscheduled": 0}


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
    session: Session, *, status: str = "", scope: str = "", search: str = ""
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
        "statuses": WORK_ORDER_STATUSES,
        "overdue_count": sum(1 for row in rows if row["is_overdue"]),
        "unscheduled_count": sum(1 for row in rows if row["is_unscheduled"]),
    }
