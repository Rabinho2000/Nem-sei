"""Read models for the Reporting area.

The screens this feeds used to answer "which reports exist?". An operator with
267 installations and a month to close needs the other question -- what can be
produced, what is missing, and what to do next -- so most of what is here is
`reporting/readiness.py` projected into the shapes a template can render.

Nothing recomputes a report. Snapshots and portfolio runs are read as they were
frozen; readiness is derived from persisted facts and never from a provider.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.portfolios.models import Portfolio, PortfolioMembership, PortfolioReportRun
from nemsei.reporting.commercial import resolve_billing_config, resolve_tariff
from nemsei.reporting.commercial_models import BILLING_ENERGY_BASES, BILLING_MODES, REPORT_TYPES
from nemsei.reporting.models import ReportingDataset, ReportSnapshot
from nemsei.reporting.readiness import (
    STATE_LABELS_PT,
    fleet_readiness,
    filter_readiness,
    month_bounds,
    summarise,
)


def default_month(today: date | None = None) -> str:
    """The month an operator is most likely reporting on.

    The current one while it is running, because that is what "prepare August"
    means on the 31st of August. A finished month is not silently substituted:
    a provisional report of a running month is a legitimate thing to want, and
    the report itself says which it is.
    """
    return (today or date.today()).strftime("%Y-%m")


def _snapshot_state(snapshot: ReportSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    payload = snapshot.payload_json or {}
    state = payload.get("reporting_state")
    if isinstance(state, str):
        return state
    return "final" if payload.get("production_is_final") else "provisional"


def _portfolio_overview(session: Session, *, month: str) -> list[dict[str, Any]]:
    """Each portfolio's state for the month, and how ready its members are."""
    start, end = month_bounds(month)
    readiness = {row.asset_id: row for row in fleet_readiness(session, month=month)}

    overview: list[dict[str, Any]] = []
    for portfolio in session.scalars(select(Portfolio).order_by(Portfolio.name)).all():
        memberships = session.scalars(
            select(PortfolioMembership).where(
                PortfolioMembership.portfolio_id == portfolio.id,
                PortfolioMembership.valid_from < end,
            )
        ).all()
        members = [
            membership
            for membership in memberships
            if membership.valid_to is None or membership.valid_to > start
        ]
        resolved = [readiness[m.asset_id] for m in members if m.asset_id and m.asset_id in readiness]
        run = session.scalar(
            select(PortfolioReportRun).where(
                PortfolioReportRun.portfolio_id == portfolio.id,
                PortfolioReportRun.period_start == start,
                PortfolioReportRun.period_end == end,
            )
        )
        overview.append(
            {
                "portfolio": portfolio,
                "run": run,
                "members": len(members),
                # A member with no asset behind it cannot be reported at all,
                # and a portfolio total that quietly omits it is wrong rather
                # than partial. Counted separately for exactly that reason.
                "unresolved": sum(1 for m in members if not m.asset_id),
                "ready": sum(1 for row in resolved if row.state in ("final", "provisional")),
                "provisional": sum(1 for row in resolved if row.state == "provisional"),
                "final": sum(1 for row in resolved if row.state == "final"),
                "blocked": sum(1 for row in resolved if row.state == "blocked"),
                "needs_commercial": sum(1 for row in resolved if row.state == "needs_commercial"),
            }
        )
    return overview


def workspace_overview(session: Session, *, month: str) -> dict[str, Any]:
    """Everything the `/reports` landing page leads with, for one month."""
    rows = fleet_readiness(session, month=month)
    esco_first = [row for row in rows if row.is_esco][:12]

    recent_snapshots = session.execute(
        select(ReportSnapshot, ReportingDataset.asset_id, Asset.canonical_name, ReportingDataset.period_start)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .join(Asset, Asset.id == ReportingDataset.asset_id)
        .order_by(ReportSnapshot.created_at.desc())
        .limit(8)
    ).all()

    portfolios = _portfolio_overview(session, month=month)
    return {
        "month": month,
        "summary": summarise(rows),
        "esco_queue": esco_first,
        "state_labels": STATE_LABELS_PT,
        "portfolios": portfolios,
        "pending_review": [row for row in portfolios if row["run"] is not None and row["run"].status == "generated"],
        "recent_snapshots": [
            {
                "snapshot": snapshot,
                "asset_id": asset_id,
                "asset_name": name,
                "period_start": period_start,
                "state": _snapshot_state(snapshot),
            }
            for snapshot, asset_id, name, period_start in recent_snapshots
        ],
    }


def readiness_index(
    session: Session,
    *,
    month: str,
    contract: str = "",
    state: str = "",
    generated: str = "",
    search: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """The filtered installation list, plus the counts for the whole month."""
    rows = fleet_readiness(session, month=month)
    filtered = filter_readiness(rows, contract=contract, state=state, generated=generated, search=search)
    return {
        "month": month,
        "summary": summarise(rows),
        "matched": len(filtered),
        "assets": filtered[:limit],
        "truncated": max(len(filtered) - limit, 0),
        "state_labels": STATE_LABELS_PT,
        "filters": {"contract": contract, "state": state, "generated": generated, "search": search},
    }


def searchable_assets(session: Session, *, search: str = "", limit: int = 30) -> list[Asset]:
    """Kept for the plain name search the old screen offered."""
    from nemsei.assets.service import asset_search_clause

    clause = asset_search_clause(search) if search else None
    statement = select(Asset).outerjoin(Organization, Organization.id == Asset.owner_id)
    if clause is not None:
        statement = statement.where(clause)
    return list(session.scalars(statement.order_by(Asset.canonical_name).limit(limit)).all())


def asset_report_history(session: Session, *, asset_id: int, month: str | None = None) -> dict[str, Any] | None:
    """One installation's reporting page: state, commercial terms, history."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        return None
    month = month or default_month()
    start, _ = month_bounds(month)

    readiness = next(
        (row for row in fleet_readiness(session, month=month) if row.asset_id == asset_id),
        None,
    )
    snapshots = session.execute(
        select(ReportSnapshot, ReportingDataset.period_start, ReportingDataset.period_end)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .where(ReportingDataset.asset_id == asset_id)
        .order_by(ReportSnapshot.created_at.desc())
        .limit(24)
    ).all()

    return {
        "asset": asset,
        "month": month,
        "readiness": readiness,
        "state_labels": STATE_LABELS_PT,
        "billing": resolve_billing_config(session, asset_id=asset_id, on=start),
        "tariff": resolve_tariff(session, asset_id=asset_id, on=start),
        "report_types": REPORT_TYPES,
        "billing_modes": BILLING_MODES,
        "billing_energy_bases": BILLING_ENERGY_BASES,
        "default_valid_from": start.isoformat(),
        "snapshots": [
            {
                "snapshot": snapshot,
                "period_start": period_start,
                "period_end": period_end,
                "state": _snapshot_state(snapshot),
                "payload": snapshot.payload_json or {},
            }
            for snapshot, period_start, period_end in snapshots
        ],
        "default_month": month,
    }


def portfolio_runs_overview(session: Session, *, month: str | None = None) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PortfolioReportRun, Portfolio.name)
        .join(Portfolio, Portfolio.id == PortfolioReportRun.portfolio_id)
        .order_by(PortfolioReportRun.period_start.desc(), PortfolioReportRun.id.desc())
        .limit(60)
    ).all()
    return [{"run": run, "portfolio_name": name} for run, name in rows]


def reports_overview(session: Session) -> dict[str, Any]:
    """The pre-workspace landing shape, still used where only lists are wanted."""
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
