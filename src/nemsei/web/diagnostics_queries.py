"""Read models for the diagnostic screens.

Routes stay thin; this is where a template's data comes from. Nothing here
computes anything `diagnostics.service` does not already compute — a query
module orders and filters, it does not derive a device's state.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device, Organization
from nemsei.assets.service import asset_search_clause
from nemsei.diagnostics.service import current_device_status


# Worse first: a device with no reading at all is at least as concerning as
# one whose last reading said it was down, because "never reported" could mean
# "down since before this table existed" and nobody has looked.
_SEVERITY = {"unavailable": 0, "unknown": 1, "standby": 2, "available": 3}


def searchable_assets_with_devices(session: Session, *, search: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """Installations that actually have a device to diagnose, matching a search."""
    statement = (
        select(Asset, func.count(Device.id))
        .join(Device, Device.asset_id == Asset.id)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .group_by(Asset.id)
        .order_by(Asset.canonical_name)
        .limit(limit)
    )
    clause = asset_search_clause(search) if search else None
    if clause is not None:
        statement = statement.where(clause)
    return [{"asset": asset, "device_count": count} for asset, count in session.execute(statement).all()]


def asset_diagnostics(session: Session, *, asset_id: int) -> dict[str, Any] | None:
    asset = session.get(Asset, asset_id)
    if asset is None:
        return None
    rows = current_device_status(session, asset_id=asset_id)
    rows.sort(key=lambda row: (_SEVERITY.get(row["availability_status"], 1), row["label"] or ""))
    return {
        "asset": asset,
        "rows": rows,
        "counts": {
            "total": len(rows),
            "available": sum(1 for row in rows if row["availability_status"] == "available"),
            "attention": sum(1 for row in rows if row["availability_status"] != "available"),
            "no_reading": sum(1 for row in rows if not row["has_reading"]),
        },
    }
