"""The monthly workflow: validate coverage, generate, review, approve.

Nothing here calculates a number. Validating coverage builds the same
`PortfolioDataset` the "Instalações"/"Produção" screens already show; generating
a run freezes that into a decision and produces each ready member's individual
report through the exact path an individual report uses on its own —
`assemble_asset_report` then `snapshot_dataset`. If a total here ever disagreed
with a member's own report, that would be a bug in this module, not a
difference of method.

A member with genuinely no production for the period is `blocked`, not
generated with an empty document: `assemble_asset_report` can technically
produce a payload for it, but a report with nothing to say is not a report an
operator should be asked to review. Everything else — partial coverage,
missing tariffs, a defaulted report type — still generates, because those are
exactly what `unavailable_fields` and the coverage counts exist to surface.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.portfolios.datasets import build_portfolio_dataset
from nemsei.portfolios.models import Portfolio, PortfolioReportRun, PortfolioReportRunMember
from nemsei.portfolios.service import freeze_snapshot
from nemsei.reporting.assembler import assemble_asset_report
from nemsei.reporting.datasets import snapshot_dataset
from nemsei.reporting.models import ReportingDataset
from nemsei.reporting.periods import ReportingPeriodError, exclusive_end, monthly_period
from nemsei.shared.clock import utc_now


BLOCKED_REASON_LABELS = {
    "sem_dados_de_producao_no_periodo": "Sem dados de produção no período",
}


def _require_actor(value: str, *, verb: str) -> str:
    actor = value.strip()
    if not actor:
        raise ValueError(f"{verb} must record who did it.")
    return actor


def validate_coverage(session: Session, *, portfolio_id: int, report_month: str, actor: str) -> dict[str, Any]:
    """Freeze the period's membership and aggregate it — the same "Construir"
    step the dashboard already offers, named here as what it actually is: the
    coverage check the workflow's first step needs, before anything commits to
    generating individual reports.
    """
    actor = _require_actor(actor, verb="Validating coverage")
    try:
        period = monthly_period(report_month)
    except ReportingPeriodError as error:
        raise ValueError(str(error)) from error
    snapshot = freeze_snapshot(
        session, portfolio_id=portfolio_id, period_start=period.start, period_end=exclusive_end(period),
        created_by=actor,
    )
    dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by=actor)
    return {"period": period, "snapshot": snapshot, "dataset": dataset}


def existing_run(session: Session, *, portfolio_id: int, period_start, period_end) -> PortfolioReportRun | None:
    return session.scalar(
        select(PortfolioReportRun).where(
            PortfolioReportRun.portfolio_id == portfolio_id,
            PortfolioReportRun.period_start == period_start,
            PortfolioReportRun.period_end == period_end,
        )
    )


def generate_report_run(session: Session, *, portfolio_id: int, report_month: str, actor: str) -> PortfolioReportRun:
    """Validate coverage, then turn it into a run: an individual report for
    every ready member, and a blocked entry with a reason for every other one.

    Safe to call again before approval — it rebuilds the aggregate against
    whatever facts exist now and replaces the run's members accordingly. A run
    that was `reviewed` on stale numbers goes back to `generated`: a review is
    a statement about the numbers it was given, and it stops being true the
    moment those numbers change.
    """
    actor = _require_actor(actor, verb="Generating a report run")
    if session.get(Portfolio, portfolio_id) is None:
        raise ValueError("Unknown portfolio.")
    coverage = validate_coverage(session, portfolio_id=portfolio_id, report_month=report_month, actor=actor)
    period, dataset = coverage["period"], coverage["dataset"]
    # `PortfolioSnapshot` and `PortfolioDataset` both key a period by its
    # exclusive end; a run must use the same bound or it is simply never found
    # again by the query every other lookup in this module uses.
    period_end = exclusive_end(period)

    run = existing_run(session, portfolio_id=portfolio_id, period_start=period.start, period_end=period_end)
    if run is not None and run.status == "approved":
        raise ValueError(
            f"{period.label} was already approved on {run.approved_at.date().isoformat()}; "
            "an approved run cannot be regenerated."
        )

    now = utc_now()
    if run is None:
        run = PortfolioReportRun(
            portfolio_id=portfolio_id,
            period_start=period.start,
            period_end=period_end,
            status="generated",
            portfolio_dataset_id=dataset.id,
            coverage_json=dataset.coverage_json,
            generated_at=now,
            generated_by=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()
    else:
        for member in list(run.members):
            session.delete(member)
        session.flush()
        run.portfolio_dataset_id = dataset.id
        run.coverage_json = dataset.coverage_json
        run.status = "generated"
        run.generated_at = now
        run.generated_by = actor
        run.reviewed_at = run.reviewed_by = run.review_notes = None
        run.approved_at = run.approved_by = None
        run.updated_at = now

    for member in dataset.members:
        production_state = (member.states_json or {}).get("production", "missing")
        if production_state == "missing":
            session.add(
                PortfolioReportRunMember(
                    run_id=run.id,
                    asset_id=member.asset_id,
                    status="blocked",
                    reason="sem_dados_de_producao_no_periodo",
                )
            )
            continue
        # Reuse the exact ReportingDataset the aggregate already built for this
        # asset, so assembling its individual report does not rebuild it.
        member_dataset = session.get(ReportingDataset, member.reporting_dataset_id)
        assembled = assemble_asset_report(
            session, asset_id=member.asset_id, period=period, built_by=actor, dataset=member_dataset
        )
        report_snapshot = snapshot_dataset(
            session, dataset=assembled.dataset, payload=assembled.payload, created_by=actor
        )
        session.add(
            PortfolioReportRunMember(
                run_id=run.id, asset_id=member.asset_id, status="ready", report_snapshot_id=report_snapshot.id
            )
        )
    session.flush()
    return run


def mark_run_reviewed(session: Session, *, run_id: int, actor: str, notes: str | None = None) -> PortfolioReportRun:
    run = session.get(PortfolioReportRun, run_id)
    if run is None:
        raise ValueError("Unknown report run.")
    if run.status != "generated":
        raise ValueError(f"Only a generated run can be marked reviewed (this one is {run.status}).")
    actor = _require_actor(actor, verb="Reviewing a report run")
    run.status = "reviewed"
    run.reviewed_at = utc_now()
    run.reviewed_by = actor
    run.review_notes = notes.strip() if notes else None
    run.updated_at = run.reviewed_at
    session.flush()
    return run


def approve_run(session: Session, *, run_id: int, actor: str) -> PortfolioReportRun:
    run = session.get(PortfolioReportRun, run_id)
    if run is None:
        raise ValueError("Unknown report run.")
    if run.status != "reviewed":
        raise ValueError(f"Only a reviewed run can be approved (this one is {run.status}).")
    actor = _require_actor(actor, verb="Approving a report run")
    run.status = "approved"
    run.approved_at = utc_now()
    run.approved_by = actor
    run.updated_at = run.approved_at
    session.flush()
    return run


def run_member_rows(session: Session, run: PortfolioReportRun) -> list[dict[str, Any]]:
    """One row per member of a run, named and ordered for a review screen."""
    rows = []
    for member in run.members:
        asset = session.get(Asset, member.asset_id)
        rows.append(
            {
                "asset_id": member.asset_id,
                "name": asset.canonical_name if asset else f"#{member.asset_id}",
                "status": member.status,
                "reason": member.reason,
                "reason_label": BLOCKED_REASON_LABELS.get(member.reason, member.reason),
                "report_snapshot_id": member.report_snapshot_id,
            }
        )
    rows.sort(key=lambda row: (row["status"] != "blocked", row["name"] or ""))
    return rows


def recent_runs(session: Session, *, portfolio_id: int | None = None, limit: int = 20) -> list[PortfolioReportRun]:
    statement = select(PortfolioReportRun).order_by(PortfolioReportRun.period_start.desc(), PortfolioReportRun.id.desc())
    if portfolio_id is not None:
        statement = statement.where(PortfolioReportRun.portfolio_id == portfolio_id)
    return list(session.scalars(statement.limit(limit)).all())
