"""Portfolio Diagnostics (D5): aggregate DiagnosticIncident, never re-derive it.

`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` -- a portfolio's diagnostic
view is a *count and a ranking* over incidents that already exist (D1), for
assets that already resolve to this portfolio's current membership
(`portfolios/service.py:resolve_members`, unchanged). Nothing here calls
`diagnostics/findings.py`, evaluates a rule, or writes a row. One incident on
one device is one incident, in every portfolio it happens to belong to --
this module counts it, it never creates a second one at the portfolio level.

Two failure modes this module is deliberately built to avoid, because they
are easy to get wrong silently:

- **Missing must never become healthy.** An installation resolves to an
  asset with zero devices (never onboarded at device level) is not the same
  as an installation with devices and zero open incidents. Both would
  naively compute to "0 incidents" -- only the second one is real evidence
  of health. See `_classify_coverage`.
- **Backlog must never look like a new problem.** D1's initialization
  materialized months of V1-imported history as `opened_at`-dated episodes
  (`DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` ss25) -- a portfolio summary that
  only says "6 installations with incidents" without separating how old
  those incidents are would misrepresent a static backlog as urgent. Every
  aggregate here also reports the recent/historical split.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.findings import SEVERITY_ORDER
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.portfolios.service import resolve_members
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.shared.clock import utc_now


# An incident younger than this is "recent" -- a genuinely new signal worth
# looking at first. Anything older is "backlog": still real, still open, but
# not something that just happened. Chosen from real evidence, not a guess:
# D3's dry-run against the live 644 incidents found nothing between 7 and 30
# days old -- the real distribution is bimodal (a handful of days-old
# episodes, everything else 30+ days from D1's historical materialization),
# so 7 days cleanly separates the two populations that actually exist today.
RECENT_THRESHOLD_DAYS = 7


@dataclass(frozen=True)
class PortfolioDiagnosticsSummary:
    """The compact numbers for a Portfolio Overview tile -- nothing here
    needs a second query against `diagnostic_incidents`; `installation_rows`
    and `incident_rows` (below) share the same underlying fetch."""

    portfolio_id: int
    as_of: date
    total_installations: int
    installations_with_incidents: int
    installations_healthy: int
    installations_no_devices: int
    installations_full_coverage: int
    incidents_critical: int
    incidents_warning: int
    incidents_info: int
    devices_affected: int
    recent_incidents: int
    historical_backlog_incidents: int
    oldest_incident_age_days: float | None


def asset_ids_for_portfolio(session: Session, *, portfolio_id: int, on: date) -> list[int]:
    """Distinct, resolved asset ids currently in this portfolio.

    `resolve_members` already deduplicates an asset claimed by both an
    explicit membership row and a rule (`portfolios/service.py`) -- this
    only additionally drops unresolved/placeholder members, which have no
    `asset_id` to look up an incident against.
    """
    members = resolve_members(session, portfolio_id=portfolio_id, on=on)
    return sorted({member.asset_id for member in members if member.asset_id is not None})


def _classify_coverage(
    asset_id: int, *, device_counts: dict[int, int], missing_device_ids: dict[int, set[int]]
) -> str:
    """"complete" / "partial" / "none" / "no_devices" -- never "healthy" for
    an asset nothing was ever monitored on."""
    total = device_counts.get(asset_id, 0)
    if total == 0:
        return "no_devices"
    missing = len(missing_device_ids.get(asset_id, set()))
    if missing == 0:
        return "complete"
    if missing >= total:
        return "none"
    return "partial"


def _open_incidents_for_assets(session: Session, *, asset_ids: list[int]) -> list[DiagnosticIncident]:
    if not asset_ids:
        return []
    return list(
        session.scalars(
            select(DiagnosticIncident).where(
                DiagnosticIncident.asset_id.in_(asset_ids), DiagnosticIncident.status == "open"
            )
        )
    )


def portfolio_diagnostics_summary(
    session: Session, *, portfolio_id: int, on: date | None = None, now: datetime | None = None
) -> PortfolioDiagnosticsSummary:
    on_value = on or date.today()
    now_value = now or utc_now()
    asset_ids = asset_ids_for_portfolio(session, portfolio_id=portfolio_id, on=on_value)

    device_counts = _device_counts_by_asset(session, asset_ids=asset_ids)
    incidents = _open_incidents_for_assets(session, asset_ids=asset_ids)
    missing_device_ids = _missing_device_ids(incidents)

    by_asset: dict[int, list[DiagnosticIncident]] = {}
    for incident in incidents:
        by_asset.setdefault(incident.asset_id, []).append(incident)

    installations_with_incidents = len(by_asset)
    installations_no_devices = sum(1 for asset_id in asset_ids if device_counts.get(asset_id, 0) == 0)
    installations_full_coverage = sum(
        1
        for asset_id in asset_ids
        if _classify_coverage(asset_id, device_counts=device_counts, missing_device_ids=missing_device_ids) == "complete"
    )
    installations_healthy = sum(
        1
        for asset_id in asset_ids
        if device_counts.get(asset_id, 0) > 0 and asset_id not in by_asset
    )

    devices_affected = len({(incident.asset_id, incident.device_id) for incident in incidents if incident.device_id is not None})

    ages_days = [(now_value - incident.opened_at).total_seconds() / 86400 for incident in incidents]
    recent = sum(1 for age in ages_days if age <= RECENT_THRESHOLD_DAYS)
    backlog = len(incidents) - recent

    return PortfolioDiagnosticsSummary(
        portfolio_id=portfolio_id,
        as_of=on_value,
        total_installations=len(asset_ids),
        installations_with_incidents=installations_with_incidents,
        installations_healthy=installations_healthy,
        installations_no_devices=installations_no_devices,
        installations_full_coverage=installations_full_coverage,
        incidents_critical=sum(1 for incident in incidents if incident.severity == "critical"),
        incidents_warning=sum(1 for incident in incidents if incident.severity == "warning"),
        incidents_info=sum(1 for incident in incidents if incident.severity == "info"),
        devices_affected=devices_affected,
        recent_incidents=recent,
        historical_backlog_incidents=backlog,
        oldest_incident_age_days=max(ages_days) if ages_days else None,
    )


def _device_counts_by_asset(session: Session, *, asset_ids: list[int]) -> dict[int, int]:
    if not asset_ids:
        return {}
    rows = session.execute(
        select(Device.asset_id, func.count(Device.id)).where(Device.asset_id.in_(asset_ids)).group_by(Device.asset_id)
    ).all()
    return {asset_id: count for asset_id, count in rows}


def _missing_device_ids(incidents: list[DiagnosticIncident]) -> dict[int, set[int]]:
    """Per asset, which device ids have an open `device_no_history` incident."""
    missing: dict[int, set[int]] = {}
    for incident in incidents:
        if incident.rule_code == "device_no_history" and incident.device_id is not None:
            missing.setdefault(incident.asset_id, set()).add(incident.device_id)
    return missing


def portfolio_installation_rows(
    session: Session, *, portfolio_id: int, on: date | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """One row per resolved installation, worst-first -- "instalações mais
    problemáticas". Coverage is evidence-based (`_classify_coverage`), never
    a guess: an installation with zero devices is `no_devices`, not healthy.
    """
    on_value = on or date.today()
    now_value = now or utc_now()
    asset_ids = asset_ids_for_portfolio(session, portfolio_id=portfolio_id, on=on_value)
    if not asset_ids:
        return []

    assets_by_id = {asset.id: asset for asset in session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))}
    device_counts = _device_counts_by_asset(session, asset_ids=asset_ids)
    incidents = _open_incidents_for_assets(session, asset_ids=asset_ids)
    missing_device_ids = _missing_device_ids(incidents)

    by_asset: dict[int, list[DiagnosticIncident]] = {}
    for incident in incidents:
        by_asset.setdefault(incident.asset_id, []).append(incident)

    rows: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        asset = assets_by_id.get(asset_id)
        asset_incidents = by_asset.get(asset_id, [])
        critical = sum(1 for incident in asset_incidents if incident.severity == "critical")
        warning = sum(1 for incident in asset_incidents if incident.severity == "warning")
        info = sum(1 for incident in asset_incidents if incident.severity == "info")
        oldest = min((incident.opened_at for incident in asset_incidents), default=None)
        rows.append(
            {
                "asset_id": asset_id,
                "name": asset.canonical_name if asset else f"asset #{asset_id}",
                "device_count": device_counts.get(asset_id, 0),
                "coverage": _classify_coverage(asset_id, device_counts=device_counts, missing_device_ids=missing_device_ids),
                "critical_count": critical,
                "warning_count": warning,
                "info_count": info,
                "incident_count": len(asset_incidents),
                "oldest_incident_at": oldest,
                "oldest_incident_age_days": (now_value - oldest).total_seconds() / 86400 if oldest else None,
                "has_recent_incident": any((now_value - incident.opened_at).total_seconds() / 86400 <= RECENT_THRESHOLD_DAYS for incident in asset_incidents),
            }
        )

    rows.sort(key=lambda row: (-row["critical_count"], -row["warning_count"], -row["info_count"], row["name"] or ""))
    return rows


def portfolio_incident_rows(
    session: Session, *, portfolio_id: int, on: date | None = None, now: datetime | None = None, filters: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """One row per open incident belonging to a resolved installation,
    worst-first then oldest-first -- filterable by severity/rule/asset/
    provider/status, same filter vocabulary the rest of the portfolio UI
    already uses.
    """
    on_value = on or date.today()
    now_value = now or utc_now()
    filters = filters or {}
    asset_ids = asset_ids_for_portfolio(session, portfolio_id=portfolio_id, on=on_value)
    if not asset_ids:
        return []

    status = (filters.get("status") or "open").strip() or "open"
    statement = select(DiagnosticIncident).where(DiagnosticIncident.asset_id.in_(asset_ids))
    statement = statement.where(DiagnosticIncident.status == status) if status != "all" else statement
    incidents = list(session.scalars(statement))

    assets_by_id = {asset.id: asset for asset in session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))}
    devices_by_id = {
        device.id: device
        for device in session.scalars(select(Device).where(Device.asset_id.in_(asset_ids)))
    }
    provider_by_asset = _active_provider_by_asset(session, asset_ids=asset_ids)

    rows: list[dict[str, Any]] = []
    for incident in incidents:
        asset = assets_by_id.get(incident.asset_id)
        device = devices_by_id.get(incident.device_id) if incident.device_id else None
        age_days = (now_value - incident.opened_at).total_seconds() / 86400
        rows.append(
            {
                "incident": incident,
                "asset_id": incident.asset_id,
                "asset_name": asset.canonical_name if asset else f"asset #{incident.asset_id}",
                "device_label": device.label if device else None,
                "provider_code": provider_by_asset.get(incident.asset_id),
                "age_days": age_days,
                "is_recent": age_days <= RECENT_THRESHOLD_DAYS,
            }
        )

    for key, field in (("severity", "incident"), ("rule", "incident"), ("asset_id", "asset_id"), ("provider_code", "provider_code")):
        wanted = (filters.get(key) or "").strip()
        if not wanted:
            continue
        if key == "severity":
            rows = [row for row in rows if row["incident"].severity == wanted]
        elif key == "rule":
            rows = [row for row in rows if row["incident"].rule_code == wanted]
        elif key == "asset_id":
            rows = [row for row in rows if str(row["asset_id"]) == wanted]
        elif key == "provider_code":
            rows = [row for row in rows if row["provider_code"] == wanted]

    rows.sort(key=lambda row: (SEVERITY_ORDER.get(row["incident"].severity, 99), row["incident"].opened_at))
    return rows


def _active_provider_by_asset(session: Session, *, asset_ids: list[int]) -> dict[int, str]:
    if not asset_ids:
        return {}
    rows = session.execute(
        select(AssetProviderMapping.asset_id, ProviderConnection.provider_code)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(
            AssetProviderMapping.asset_id.in_(asset_ids),
            AssetProviderMapping.resource_kind == "plant",
            AssetProviderMapping.mapping_status == "active",
        )
    ).all()
    return {asset_id: provider_code for asset_id, provider_code in rows}
