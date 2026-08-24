"""Read models for the diagnostic screens.

Routes stay thin; this is where a template's data comes from. Nothing here
computes anything `diagnostics.service`/`diagnostics.findings` does not
already compute — a query module orders, filters and aggregates already-
persisted `DiagnosticIncident` rows, it never re-evaluates a rule.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device, Organization
from nemsei.assets.service import asset_search_clause
from nemsei.diagnostics.findings import SEVERITY_ORDER, evaluate_asset_findings
from nemsei.diagnostics.handling import incident_notes
from nemsei.diagnostics.models import INCIDENT_HANDLING_STATES, DiagnosticIncident
from nemsei.diagnostics.repository import DeviceStatusRepository
from nemsei.diagnostics.service import current_device_status
from nemsei.shared.clock import utc_now


# Worse first: a device with no reading at all is at least as concerning as
# one whose last reading said it was down, because "never reported" could mean
# "down since before this table existed" and nobody has looked.
_SEVERITY = {"unavailable": 0, "unknown": 1, "standby": 2, "available": 3}

# D2 (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md §11): the display cap
# has to apply *after* sorting worst-first, never before -- an alphabetically
# -limited list could hide a genuinely critical installation that just does
# not start with an early letter. This is the ceiling on how many
# installations the overview ever fetches before sorting, not the number
# shown; large enough that today's real portfolio never hits it.
_OVERVIEW_FETCH_CEILING = 2000


def duration_label(delta: timedelta) -> str:
    """A short, human string for "how long has this been going on"."""
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    if total_minutes < 60:
        return f"{total_minutes} min"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


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


def _open_incident_counts_by_asset(session: Session, *, asset_ids: list[int]) -> dict[int, dict[str, int]]:
    if not asset_ids:
        return {}
    statement = (
        select(DiagnosticIncident.asset_id, DiagnosticIncident.severity, func.count())
        .where(DiagnosticIncident.status == "open", DiagnosticIncident.asset_id.in_(asset_ids))
        .group_by(DiagnosticIncident.asset_id, DiagnosticIncident.severity)
    )
    counts: dict[int, dict[str, int]] = {}
    for asset_id, severity, count in session.execute(statement).all():
        counts.setdefault(asset_id, {})[severity] = count
    return counts


def diagnostics_overview(session: Session, *, search: str = "", limit: int = 50) -> dict[str, Any]:
    """Every installation with a device, worst-first by open incident severity.

    This is "Overview" from the D2 nav proposal, folded into the existing
    index rather than a separate page: a second page that also just lists
    installations would be the "navegação excessiva" the plan was asked to
    avoid. The search box stays; what changes is that the list now answers
    "which installations need attention" without opening any of them.
    """
    rows = searchable_assets_with_devices(session, search=search, limit=_OVERVIEW_FETCH_CEILING)
    counts_by_asset = _open_incident_counts_by_asset(session, asset_ids=[row["asset"].id for row in rows])

    for row in rows:
        counts = counts_by_asset.get(row["asset"].id, {})
        row["critical_count"] = counts.get("critical", 0)
        row["warning_count"] = counts.get("warning", 0)
        row["info_count"] = counts.get("info", 0)
        row["healthy"] = not counts

    rows.sort(
        key=lambda row: (
            -row["critical_count"],
            -row["warning_count"],
            -row["info_count"],
            row["asset"].canonical_name or "",
        )
    )
    truncated = len(rows) > limit
    visible = rows[:limit]

    return {
        "assets": visible,
        "truncated": truncated,
        "shown": len(visible),
        "matched": len(rows),
        "summary": {
            "total": len(rows),
            "with_critical": sum(1 for row in rows if row["critical_count"] > 0),
            "with_warning": sum(1 for row in rows if row["critical_count"] == 0 and row["warning_count"] > 0),
            "healthy": sum(1 for row in rows if row["healthy"]),
        },
    }


def open_incidents_overview(session: Session, *, search: str = "", handling: str = "") -> list[dict[str, Any]]:
    """Every open incident, portfolio-wide, worst-first then longest-open-first.

    Duration is the point of this page (`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`'s
    "Incidents" tab) -- an installation nobody has looked at for the longest is
    at least as worth surfacing as the most recently opened one at the same
    severity.
    """
    statement = (
        select(DiagnosticIncident, Asset, Device)
        .join(Asset, Asset.id == DiagnosticIncident.asset_id)
        .outerjoin(Device, Device.id == DiagnosticIncident.device_id)
        # `asset_search_clause` references `Organization.display_name` --
        # without this join present, SQLAlchemy adds it as an implicit,
        # unjoined FROM element (a real cartesian product, not just a lint
        # warning): with zero organizations in the database this silently
        # returns zero rows for *every* search, matched or not. Same join
        # `searchable_assets_with_devices` already carries for exactly this
        # reason.
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(DiagnosticIncident.status == "open")
    )
    clause = asset_search_clause(search) if search else None
    if clause is not None:
        statement = statement.where(clause)
    if handling == "untouched":
        # The question this page exists to answer today: 644 open incidents,
        # how many has nobody looked at at all.
        statement = statement.where(DiagnosticIncident.handling_state == "new")
    elif handling in INCIDENT_HANDLING_STATES:
        statement = statement.where(DiagnosticIncident.handling_state == handling)

    now = utc_now()
    rows: list[dict[str, Any]] = []
    for incident, asset, device in session.execute(statement).all():
        rows.append(
            {
                "incident": incident,
                "asset": asset,
                "device_label": device.label if device else None,
                "duration": duration_label(now - incident.opened_at),
                "since_confirmed": duration_label(now - incident.last_observed_at),
            }
        )
    rows.sort(key=lambda row: (SEVERITY_ORDER.get(row["incident"].severity, 99), row["incident"].opened_at))
    return rows


def incident_detail(session: Session, *, incident_id: int) -> dict[str, Any] | None:
    """One incident with its handling history. Computes no rule of its own."""
    incident = session.get(DiagnosticIncident, incident_id)
    if incident is None:
        return None
    now = utc_now()
    return {
        "incident": incident,
        "asset": session.get(Asset, incident.asset_id),
        "device": session.get(Device, incident.device_id) if incident.device_id else None,
        "notes": incident_notes(session, incident_id=incident.id),
        "duration": duration_label(now - incident.opened_at),
        "since_confirmed": duration_label(now - incident.last_observed_at),
        "handling_states": INCIDENT_HANDLING_STATES,
    }


def handling_summary(session: Session) -> dict[str, int]:
    """How the open backlog is distributed across the human states."""
    counts = dict(
        session.execute(
            select(DiagnosticIncident.handling_state, func.count(DiagnosticIncident.id))
            .where(DiagnosticIncident.status == "open")
            .group_by(DiagnosticIncident.handling_state)
        ).all()
    )
    return {state: int(counts.get(state, 0)) for state in INCIDENT_HANDLING_STATES}


def asset_diagnostics(session: Session, *, asset_id: int) -> dict[str, Any] | None:
    asset = session.get(Asset, asset_id)
    if asset is None:
        return None
    rows = current_device_status(session, asset_id=asset_id)
    rows.sort(key=lambda row: (_SEVERITY.get(row["availability_status"], 1), row["label"] or ""))
    device_repo = DeviceStatusRepository(session)
    findings = evaluate_asset_findings(
        rows,
        asset_id=asset_id,
        now=utc_now(),
        # Real history, not just the latest reading -- lets device-level
        # findings answer "desde quando", not just "agora".
        history_for_device=lambda device_id: device_repo.history_for_device(device_id=device_id),
    )
    return {
        "asset": asset,
        "rows": rows,
        "findings": findings,
        "counts": {
            "total": len(rows),
            "available": sum(1 for row in rows if row["availability_status"] == "available"),
            "attention": sum(1 for row in rows if row["availability_status"] != "available"),
            "no_reading": sum(1 for row in rows if not row["has_reading"]),
        },
    }
