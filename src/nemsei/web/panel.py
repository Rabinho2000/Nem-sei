"""The operational panel: what needs attention now, and nothing else.

The home page used to report three database row counts -- 219 360 production
facts, 51 297 device states, 35 sync runs. None of them answers an operational
question, and the first is precisely the number this codebase's own rules say
must never be read raw, because `production_facts` is append-only.

What replaces them follows one borrowed rule: in a control room the normal
state has no colour. A quiet portfolio should look quiet, and only the abnormal
earns attention.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import utc_now
from nemsei.sync.models import SyncRun

# How long an installation may go without a reading before it is worth a look.
# Two days rather than one: a provider that labels its daily buckets in UTC can
# legitimately be a day behind without anything being wrong.
STALE_AFTER_DAYS = 2


def _latest_fact_per_asset(session: Session) -> dict[int, Any]:
    rows = session.execute(
        select(ProductionFact.asset_id, func.max(ProductionFact.period_start))
        .where(ProductionFact.metric_kind == "production_energy")
        .group_by(ProductionFact.asset_id)
    ).all()
    return {int(asset_id): latest for asset_id, latest in rows}


def attention_rows(session: Session, *, limit: int = 12) -> list[dict[str, Any]]:
    """Installations worth looking at, worst first.

    Ordered by what an operator can actually act on: a plant with critical
    incidents outranks one that is merely quiet, and a plant with no provider
    connection at all outranks both -- it is not broken, it was never wired up,
    and no amount of monitoring will change that until someone connects it.
    """
    today = utc_now().date()
    latest = _latest_fact_per_asset(session)
    severities = dict(
        session.execute(
            select(DiagnosticIncident.asset_id, func.count(DiagnosticIncident.id))
            .where(DiagnosticIncident.status == "open", DiagnosticIncident.severity == "critical")
            .group_by(DiagnosticIncident.asset_id)
        ).all()
    )
    warnings = dict(
        session.execute(
            select(DiagnosticIncident.asset_id, func.count(DiagnosticIncident.id))
            .where(DiagnosticIncident.status == "open", DiagnosticIncident.severity == "warning")
            .group_by(DiagnosticIncident.asset_id)
        ).all()
    )
    mapped = {
        int(asset_id)
        for asset_id in session.scalars(select(func.distinct(AssetProviderMapping.asset_id)))
    }
    rows: list[dict[str, Any]] = []
    for asset, organization_name in session.execute(
        select(Asset, Organization.display_name).outerjoin(Organization, Organization.id == Asset.owner_id)
    ).all():
        newest = latest.get(asset.id)
        stale_days = (today - newest.date()).days if newest else None
        critical = int(severities.get(asset.id, 0))
        warning = int(warnings.get(asset.id, 0))
        unmapped = asset.id not in mapped
        if not (critical or unmapped or (stale_days is not None and stale_days > STALE_AFTER_DAYS) or newest is None):
            continue
        rows.append(
            {
                "asset": asset,
                "organization_name": organization_name,
                "critical": critical,
                "warning": warning,
                "unmapped": unmapped,
                "stale_days": stale_days,
                "never_reported": newest is None,
                "power_kw": float(asset.installed_dc_power_kw) if asset.installed_dc_power_kw is not None else None,
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row["critical"] else 1 if row["unmapped"] else 2,
            -(row["power_kw"] or 0.0),
            -(row["stale_days"] or 0),
        )
    )
    return rows[:limit]


def operational_panel(session: Session) -> dict[str, Any]:
    now = utc_now()
    today = now.date()
    latest = _latest_fact_per_asset(session)
    total_assets = int(session.scalar(select(func.count(Asset.id))) or 0)
    mapped_assets = int(session.scalar(select(func.count(func.distinct(AssetProviderMapping.asset_id)))) or 0)

    fresh = sum(1 for value in latest.values() if (today - value.date()).days <= STALE_AFTER_DAYS)
    incidents = dict(
        session.execute(
            select(DiagnosticIncident.severity, func.count(DiagnosticIncident.id))
            .where(DiagnosticIncident.status == "open")
            .group_by(DiagnosticIncident.severity)
        ).all()
    )
    untouched = int(
        session.scalar(
            select(func.count(DiagnosticIncident.id)).where(
                DiagnosticIncident.status == "open", DiagnosticIncident.handling_state == "new"
            )
        )
        or 0
    )
    last_run = session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(1)).first()
    recent_runs = session.execute(
        select(SyncRun.status, func.count(SyncRun.id))
        .where(SyncRun.started_at >= now - timedelta(hours=24))
        .group_by(SyncRun.status)
    ).all()
    run_counts = {str(status): int(count) for status, count in recent_runs}
    delivered = run_counts.get("success", 0)

    return {
        "total_assets": total_assets,
        "mapped_assets": mapped_assets,
        "unmapped_assets": total_assets - mapped_assets,
        "reporting_recently": fresh,
        "silent_assets": total_assets - fresh,
        "critical": int(incidents.get("critical", 0)),
        "warning": int(incidents.get("warning", 0)),
        "untouched_incidents": untouched,
        "needs_review": int(session.scalar(select(func.count(Asset.id)).where(Asset.review_status == "needs_review")) or 0),
        "pending_mappings": int(
            session.scalar(select(func.count(AssetProviderMapping.id)).where(AssetProviderMapping.mapping_status == "pending_review")) or 0
        ),
        "last_run": last_run,
        "run_counts": run_counts,
        "runs_last_day": sum(run_counts.values()),
        "runs_delivered": delivered,
        "attention": attention_rows(session),
    }
