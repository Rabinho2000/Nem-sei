"""Turn a provisional month into a final one when its data actually closes.

August is generated on the 31st, from an August that is neither over nor
complete. The report is honest about that -- `reporting_state` says
`provisional` and no euro is stated -- but honesty is not enough on its own: at
some point the missing days arrive, the month ends, and someone has to notice.
Before this, "someone" was a person remembering to press Generate again.

So this pass asks one question of every asset period that currently has a
provisional snapshot and no final one: *would it be final now?* Only when the
answer is yes does it write anything. That ordering matters more than it looks:
`build_dataset` inserts a row every time it is called, so evaluating finality
through it would leave a dataset per asset per cycle, for ever, to answer a
question that reads only daily facts.

What it deliberately does not do:

  * touch the provisional snapshot. It is the historical record of what was
    reported on the day it was reported, and `report_snapshots` has a trigger
    that would refuse anyway. The final month is a *new* snapshot with a new
    content identity, beside the old one.
  * approve a portfolio run. Approval is an operator's signature on a set of
    numbers; nothing here is entitled to it. A portfolio whose members have
    just gone final is left for a person to review.
  * call a provider. Nothing in this module can: it reads persisted facts, the
    same as every other report path.

Re-running it over already-final, unchanged inputs writes nothing, because
there is no longer a provisional snapshot without a final one beside it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.reporting.assembler import assemble_asset_report, daily_rows_for
from nemsei.reporting.datasets import snapshot_dataset
from nemsei.reporting.finality import evaluate_period_finality
from nemsei.reporting.models import ReportingDataset, ReportSnapshot
from nemsei.reporting.periods import monthly_period


@dataclass
class MonthCloseOutcome:
    """What one pass looked at and what, if anything, it closed."""

    examined: int = 0
    finalised: int = 0
    still_provisional: int = 0
    snapshots: list[int] = field(default_factory=list)
    months: list[str] = field(default_factory=list)

    def as_result(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "finalised": self.finalised,
            "still_provisional": self.still_provisional,
            "snapshot_ids": list(self.snapshots),
            "months_closed": sorted(set(self.months)),
        }


@dataclass(frozen=True)
class _Candidate:
    asset_id: int
    period_start: date
    period_end: date


def _snapshot_state(snapshot: ReportSnapshot) -> str:
    """What a frozen snapshot says it was. Older ones predate the field.

    A snapshot written before `reporting_state` existed carries
    `production_is_final`, and that is what it was reported as at the time. It
    is read rather than recomputed, because the question here is what the
    stored report claims, not what today's rules would have said about it.
    """
    payload = snapshot.payload_json or {}
    state = payload.get("reporting_state")
    if isinstance(state, str):
        return state
    return "final" if payload.get("production_is_final") else "provisional"


def provisional_periods(session: Session, *, monthly_only: bool = True) -> list[_Candidate]:
    """Asset periods whose latest snapshot is provisional, newest first.

    "Latest" rather than "any": once a final snapshot exists for a period there
    is nothing left to close, and re-examining it every cycle would be work
    with a guaranteed answer.
    """
    rows = session.execute(
        select(ReportSnapshot, ReportingDataset.asset_id, ReportingDataset.period_start, ReportingDataset.period_end)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .where(ReportingDataset.scope == "asset")
        .order_by(ReportSnapshot.created_at.desc(), ReportSnapshot.id.desc())
    ).all()

    latest: dict[tuple[int, date, date], ReportSnapshot] = {}
    for snapshot, asset_id, period_start, period_end in rows:
        latest.setdefault((asset_id, period_start, period_end), snapshot)

    candidates = [
        _Candidate(asset_id=asset_id, period_start=start, period_end=end)
        for (asset_id, start, end), snapshot in latest.items()
        if _snapshot_state(snapshot) != "final"
    ]
    if monthly_only:
        # A month is the only period this pass knows how to rebuild, because
        # `monthly_period` is the only way it can name one back to the
        # assembler. A quarter that went final is closed by regenerating it.
        candidates = [
            candidate
            for candidate in candidates
            if candidate.period_start.day == 1
            and candidate.period_end == _next_month(candidate.period_start)
        ]
    return sorted(candidates, key=lambda candidate: (candidate.period_start, candidate.asset_id), reverse=True)


def _next_month(start: date) -> date:
    return date(start.year + (start.month == 12), start.month % 12 + 1, 1)


def close_reporting_months(
    session: Session,
    *,
    actor: str = "sistema",
    today: date | None = None,
    limit: int = 500,
) -> MonthCloseOutcome:
    """Finalise every provisional month whose data has since closed."""
    reference = today or date.today()
    outcome = MonthCloseOutcome()

    for candidate in provisional_periods(session)[:limit]:
        period = monthly_period(candidate.period_start.strftime("%Y-%m"))
        outcome.examined += 1

        # The cheap question first: nothing is built unless the answer is yes.
        finality = evaluate_period_finality(
            period_start=candidate.period_start,
            period_end_exclusive=candidate.period_end,
            daily_rows=daily_rows_for(session, asset_id=candidate.asset_id, period=period),
            today=reference,
        )
        if not finality.is_final:
            outcome.still_provisional += 1
            continue

        assembled = assemble_asset_report(
            session,
            asset_id=candidate.asset_id,
            period=period,
            built_by=actor,
            today=reference,
        )
        # Belt and braces: the assembler consults the dataset's own month-level
        # quality too, so it can still answer provisional where the day count
        # alone said final. Trust its answer over this module's preview.
        if assembled.payload.get("reporting_state") != "final":
            outcome.still_provisional += 1
            continue

        snapshot = snapshot_dataset(
            session,
            dataset=assembled.dataset,
            payload=assembled.payload,
            created_by=actor,
            quality=dict(assembled.dataset.quality_json or {}),
            notes="Fecho automático do mês: os dados de origem ficaram completos.",
        )
        outcome.finalised += 1
        outcome.snapshots.append(snapshot.id)
        outcome.months.append(candidate.period_start.strftime("%Y-%m"))

    return outcome
