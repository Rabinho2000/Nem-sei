"""Generate everything a month has left to generate, then close it.

Two distinct actions, both new, both built entirely from pieces that already
exist and are unchanged by this module:

  * `generate_all_reports` -- one pass over the fleet that calls the exact
    `assemble_asset_report` + `snapshot_dataset` pair every other generate
    button already uses, for every installation that has not been generated
    yet this month, plus `generate_report_run` for every portfolio without a
    run. It never invents a report for an installation with no energy at
    all, the same rule `generate_report_run` already applies.

  * `close_month` -- approves every portfolio run the month still owes an
    approval to, via the existing `approve_run`. The one new behaviour: it
    first asks `fleet_readiness` whether any ESCO installation still lacks
    billing configuration or a tariff, and refuses outright unless the
    caller explicitly overrides. This is deliberately named distinctly from
    `reporting/close.py` (`close_reporting_months`), which is a different
    thing -- promoting an asset-period from provisional to final once its
    production days are all in, with no notion of a "month" as a unit or of
    financial completeness at all.

Nothing here computes a euro figure or reaches a provider. Both of these are
orchestration over `readiness.py`, `assembler.py`, `datasets.py` and
`portfolios/reporting.py`, unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.portfolios.models import Portfolio, PortfolioReportRun
from nemsei.portfolios.reporting import approve_run, existing_run, generate_report_run, mark_run_reviewed
from nemsei.providers.audit import record_operator_action
from nemsei.reporting.assembler import assemble_asset_report
from nemsei.reporting.datasets import snapshot_dataset
from nemsei.reporting.periods import ReportingPeriodError, exclusive_end, monthly_period
from nemsei.reporting.readiness import AssetReadiness, fleet_readiness


@dataclass(frozen=True)
class BulkGenerationResult:
    month: str
    generated_assets: int
    skipped_no_energy: int
    generated_portfolio_runs: int
    already_had_run: int
    blocking: tuple[AssetReadiness, ...] = field(default_factory=tuple)


def _require_actor(value: str, *, verb: str) -> str:
    actor = value.strip()
    if not actor:
        raise ValueError(f"{verb} must record who did it.")
    return actor


def _financially_blocked(rows: list[AssetReadiness]) -> tuple[AssetReadiness, ...]:
    """ESCO installations still missing billing configuration or a tariff.

    Not `state == "needs_commercial"` alone: that state only ever reflects
    `has_commercial`, and an ESCO can have a billing configuration and still
    have no tariff in force -- `readiness.py` tracks that as its own flag,
    not as a fifth state, so the check reads both directly.
    """
    return tuple(row for row in rows if row.is_esco and (not row.has_commercial or not row.has_tariff))


def generate_all_reports(session: Session, *, month: str, actor: str) -> BulkGenerationResult:
    """Generate every report this month has outstanding: one pass, no
    provider call, safe to run again (each piece it calls already is).
    """
    actor = _require_actor(actor, verb="Generating a month's reports")
    try:
        period = monthly_period(month)
    except ReportingPeriodError as error:
        raise ValueError(str(error)) from error
    period_end = exclusive_end(period)

    rows = fleet_readiness(session, month=month)
    generated_assets = 0
    skipped_no_energy = 0
    for row in rows:
        if not row.has_energy:
            skipped_no_energy += 1
            continue
        if session.get(Asset, row.asset_id) is None:
            continue
        assembled = assemble_asset_report(session, asset_id=row.asset_id, period=period, built_by=actor)
        snapshot_dataset(session, dataset=assembled.dataset, payload=assembled.payload, created_by=actor)
        generated_assets += 1

    generated_runs = 0
    already_had_run = 0
    for portfolio in session.scalars(select(Portfolio)).all():
        if existing_run(session, portfolio_id=portfolio.id, period_start=period.start, period_end=period_end) is not None:
            already_had_run += 1
            continue
        generate_report_run(session, portfolio_id=portfolio.id, report_month=month, actor=actor)
        generated_runs += 1

    # Re-read: the assembling above may have turned a `needs_commercial` row
    # `final`, or left it exactly where it was -- either way this reflects
    # what generation actually produced, not what it started from.
    blocking = _financially_blocked(fleet_readiness(session, month=month))
    return BulkGenerationResult(
        month=month,
        generated_assets=generated_assets,
        skipped_no_energy=skipped_no_energy,
        generated_portfolio_runs=generated_runs,
        already_had_run=already_had_run,
        blocking=blocking,
    )


def month_close_blockers(session: Session, *, month: str) -> tuple[AssetReadiness, ...]:
    """What closing this month is waiting on, right now -- read-only."""
    return _financially_blocked(fleet_readiness(session, month=month))


def close_month(
    session: Session, *, month: str, actor: str, override_reason: str = "",
) -> list[PortfolioReportRun]:
    """Approve every portfolio run this month still owes an approval to.

    Refuses outright while an ESCO installation lacks billing configuration
    or a tariff, unless `override_reason` is given -- a non-empty string the
    caller must actually type, not a checkbox alone, because "I know it's
    incomplete and I'm closing it anyway" is a sentence an operator should
    have to mean. The reason itself is never persisted (the audit trail this
    action writes to only ever carries closed-vocabulary fields, deliberately
    never free text -- see `providers/audit.py`); what's recorded is that an
    override happened and how many installations it covered.
    """
    actor = _require_actor(actor, verb="Closing a month")
    try:
        period = monthly_period(month)
    except ReportingPeriodError as error:
        raise ValueError(str(error)) from error
    period_end = exclusive_end(period)

    blocking = _financially_blocked(fleet_readiness(session, month=month))
    if blocking and not override_reason.strip():
        names = ", ".join(row.name for row in blocking[:5])
        more = f" e mais {len(blocking) - 5}" if len(blocking) > 5 else ""
        raise ValueError(
            f"{len(blocking)} instalação(ões) ESCO sem tarifa/configuração comercial para {period.label}: "
            f"{names}{more}. Preencher, ou fechar mesmo assim com um motivo."
        )

    approved: list[PortfolioReportRun] = []
    for portfolio in session.scalars(select(Portfolio)).all():
        run = existing_run(session, portfolio_id=portfolio.id, period_start=period.start, period_end=period_end)
        if run is None or run.status == "approved":
            continue
        # `approve_run` only accepts a `reviewed` run. Closing the month is
        # itself the deliberate human look this run was waiting on -- the
        # readiness checklist the operator just cleared serves the same
        # purpose "rever" exists for -- so a `generated` run is carried
        # through review on the way to approval, not skipped around it.
        if run.status == "generated":
            run = mark_run_reviewed(session, run_id=run.id, actor=actor)
        approved.append(approve_run(session, run_id=run.id, actor=actor))

    record_operator_action(
        session,
        actor_username=actor,
        action="month_closed",
        entity_type="reporting_month",
        entity_id=None,
        metadata={
            "decision": "closed_with_gaps" if blocking else "closed_clean",
            "asset_count": len(blocking),
        },
    )
    return approved
