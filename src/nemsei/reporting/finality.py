"""Decide whether a reporting period is closed, or still being reported on.

`aggregate_rows` already answered a narrower question -- "did every month in
this period produce a total?" -- and called the answer `production_is_final`.
On 2026-08-31, with 25 of August's daily facts persisted for Expertcom and none
for the 19th to the 23rd, that answer was `True`: every month the period covers
(one) had a total, so the period looked closed. `prepare_customer_report` reads
that flag to decide whether to state euros, so a month still five days short of
its data would have been invoiced as if it were complete.

The missing input was never quality. It was **the calendar**. A monthly total is
a sum over source days, and nothing downstream of `_sum_actual` knew how many
source days the month was supposed to have. This module supplies that, from two
facts a report can actually defend:

  1. the period has ended -- a month still running cannot be final, whatever it
     already holds, because tomorrow will add to it;
  2. every day the period covers has a current, non-missing daily fact.

Both must hold. Neither is a proxy for the other: (1) alone is the calendar rule
this module exists to refuse -- "August ended, therefore August is final" -- and
(2) alone would declare a running month closed the moment it happened to have no
gap yet.

Nothing here reads a provider, and nothing here writes. It is a pure function of
persisted daily rows plus a reference date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable


#: What an operator sees. `blocked` is not a worse `provisional`: it means the
#: period has no production at all, so there is no report to be provisional
#: about, and the next action is fetching data rather than waiting for it.
REPORTING_STATES = ("final", "provisional", "blocked")


@dataclass(frozen=True)
class PeriodFinality:
    """Whether a period is closed, and the evidence for the answer."""

    state: str
    period_has_ended: bool
    expected_days: int
    observed_days: int
    missing_days: tuple[date, ...]
    reasons: tuple[str, ...]

    @property
    def is_final(self) -> bool:
        return self.state == "final"

    @property
    def coverage_pct(self) -> float:
        if not self.expected_days:
            return 0.0
        return self.observed_days / self.expected_days * 100.0

    def as_payload(self) -> dict[str, Any]:
        """The shape the report payload and the dataset quality both carry."""
        return {
            "reporting_state": self.state,
            "reporting_state_reasons": list(self.reasons),
            "period_has_ended": self.period_has_ended,
            "expected_source_days": self.expected_days,
            "observed_source_days": self.observed_days,
            "missing_source_days": [day.isoformat() for day in self.missing_days],
            "day_coverage_pct": self.coverage_pct,
        }


def _days_between(start: date, end_exclusive: date) -> list[date]:
    from datetime import timedelta

    days: list[date] = []
    cursor = start
    while cursor < end_exclusive:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def evaluate_period_finality(
    *,
    period_start: date,
    period_end_exclusive: date,
    daily_rows: Iterable[dict[str, Any]],
    today: date,
    months_missing_entirely: Iterable[str] = (),
    has_partial_month: bool = False,
) -> PeriodFinality:
    """Answer whether this period is closed, from days rather than from months.

    `daily_rows` are the assembler's own rows: one per source day that has at
    least one current fact, `production_kwh` being None when the day is
    persisted as explicitly missing. A day counts as observed only when it
    carries a production number, because an explicitly-missing day is evidence
    that the day is *absent*, not that it was covered.
    """
    expected = _days_between(period_start, period_end_exclusive)
    observed = {
        row["date"]
        for row in daily_rows
        if row.get("production_kwh") is not None and isinstance(row.get("date"), date)
    }
    missing = tuple(day for day in expected if day not in observed)
    period_has_ended = period_end_exclusive <= today

    reasons: list[str] = []
    if not period_has_ended:
        reasons.append("period_still_open")
    if missing:
        reasons.append(f"missing_source_days:{len(missing)}")
    for month in months_missing_entirely:
        reasons.append(f"missing_month:{month}")
    if has_partial_month:
        reasons.append("partial_quality_facts")

    if not observed:
        state = "blocked"
    elif period_has_ended and not missing and not has_partial_month and not list(months_missing_entirely):
        state = "final"
    else:
        state = "provisional"

    return PeriodFinality(
        state=state,
        period_has_ended=period_has_ended,
        expected_days=len(expected),
        observed_days=len(observed),
        missing_days=missing,
        reasons=tuple(reasons),
    )
