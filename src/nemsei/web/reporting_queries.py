"""Read models for the top-level Reporting area.

Individual reports and portfolio runs are both read from what already exists —
`ReportSnapshot`, `PortfolioReportRun` — never recomputed here. This is the
first screen either one has ever had: before this, generating a report meant
calling a Python function directly.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.assets.service import asset_search_clause
from nemsei.portfolios.models import Portfolio, PortfolioReportRun
from nemsei.reporting.models import ReportingDataset, ReportSnapshot


def reports_overview(session: Session) -> dict[str, Any]:
    recent_snapshots = session.execute(
        select(ReportSnapshot, ReportingDataset.asset_id, Asset.canonical_name)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .join(Asset, Asset.id == ReportingDataset.asset_id)
        .order_by(ReportSnapshot.created_at.desc())
        .limit(10)
    ).all()
    recent_runs = session.execute(
        select(PortfolioReportRun, Portfolio.name)
        .join(Portfolio, Portfolio.id == PortfolioReportRun.portfolio_id)
        .order_by(PortfolioReportRun.updated_at.desc())
        .limit(10)
    ).all()
    pending_review = session.scalar(
        select(PortfolioReportRun.id).where(PortfolioReportRun.status == "generated").limit(1)
    )
    return {
        "recent_snapshots": [
            {"snapshot": snapshot, "asset_id": asset_id, "asset_name": name}
            for snapshot, asset_id, name in recent_snapshots
        ],
        "recent_runs": [{"run": run, "portfolio_name": name} for run, name in recent_runs],
        "has_pending_review": pending_review is not None,
    }


def searchable_assets(session: Session, *, search: str = "", limit: int = 30) -> list[Asset]:
    clause = asset_search_clause(search) if search else None
    statement = select(Asset).outerjoin(Organization, Organization.id == Asset.owner_id)
    if clause is not None:
        statement = statement.where(clause)
    return list(session.scalars(statement.order_by(Asset.canonical_name).limit(limit)).all())


def asset_report_history(session: Session, *, asset_id: int) -> dict[str, Any] | None:
    asset = session.get(Asset, asset_id)
    if asset is None:
        return None
    snapshots = session.execute(
        select(ReportSnapshot, ReportingDataset.period_start, ReportingDataset.period_end)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .where(ReportingDataset.asset_id == asset_id)
        .order_by(ReportSnapshot.created_at.desc())
        .limit(24)
    ).all()
    return {
        "asset": asset,
        "snapshots": [
            {"snapshot": snapshot, "period_start": start, "period_end": end} for snapshot, start, end in snapshots
        ],
        "default_month": date.today().strftime("%Y-%m"),
    }


def portfolio_runs_overview(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PortfolioReportRun, Portfolio.name)
        .join(Portfolio, Portfolio.id == PortfolioReportRun.portfolio_id)
        .order_by(PortfolioReportRun.period_start.desc(), PortfolioReportRun.id.desc())
        .limit(60)
    ).all()
    return [{"run": run, "portfolio_name": name} for run, name in rows]
