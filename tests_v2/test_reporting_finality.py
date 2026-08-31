"""A period is final when it has ended and every source day is accounted for.

These tests exist because production disagreed with the code's own intent. On
2026-08-31 Expertcom's August held 25 daily facts of a possible 31, none for the
19th to the 23rd, and `production_is_final` was `True` -- so
`prepare_customer_report`, which withholds every monetary field on a
non-final period, would have stated a month's savings and a month's ESCO
invoice from five-sixths of a month that had not finished.
"""
from __future__ import annotations

from datetime import date

import pytest

from nemsei.reporting.finality import evaluate_period_finality


def rows(*days: int, month: int = 8, value: float | None = 10.0) -> list[dict]:
    return [{"date": date(2026, month, day), "production_kwh": value} for day in days]


def test_a_month_still_running_is_never_final_however_complete_it_looks() -> None:
    """The calendar rule the product refuses: "August ended, so August is final"."""
    finality = evaluate_period_finality(
        period_start=date(2026, 8, 1),
        period_end_exclusive=date(2026, 9, 1),
        daily_rows=rows(*range(1, 32)),
        today=date(2026, 8, 31),
    )
    assert finality.state == "provisional"
    assert finality.period_has_ended is False
    assert "period_still_open" in finality.reasons
    assert finality.observed_days == 31


def test_a_closed_month_with_every_day_present_is_final() -> None:
    finality = evaluate_period_finality(
        period_start=date(2026, 7, 1),
        period_end_exclusive=date(2026, 8, 1),
        daily_rows=rows(*range(1, 32), month=7),
        today=date(2026, 8, 31),
    )
    assert finality.state == "final"
    assert finality.is_final is True
    assert finality.missing_days == ()
    assert finality.reasons == ()


def test_a_closed_month_missing_days_stays_provisional() -> None:
    """Expertcom's August, evaluated after the month closed: still five short."""
    present = [day for day in range(1, 32) if day not in {19, 20, 21, 22, 23}]
    finality = evaluate_period_finality(
        period_start=date(2026, 8, 1),
        period_end_exclusive=date(2026, 9, 1),
        daily_rows=rows(*present),
        today=date(2026, 9, 5),
    )
    assert finality.state == "provisional"
    assert finality.period_has_ended is True
    assert finality.observed_days == 26
    assert finality.expected_days == 31
    assert finality.missing_days == (
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
        date(2026, 8, 23),
    )
    assert "missing_source_days:5" in finality.reasons


def test_a_day_persisted_as_explicitly_missing_does_not_count_as_observed() -> None:
    """`missing != 0` reaches this rule too: a null day is a gap, not coverage."""
    daily = rows(*range(1, 31), month=7) + [{"date": date(2026, 7, 31), "production_kwh": None}]
    finality = evaluate_period_finality(
        period_start=date(2026, 7, 1),
        period_end_exclusive=date(2026, 8, 1),
        daily_rows=daily,
        today=date(2026, 8, 31),
    )
    assert finality.state == "provisional"
    assert finality.missing_days == (date(2026, 7, 31),)


def test_a_period_with_no_production_at_all_is_blocked_not_provisional() -> None:
    """Nothing to wait for: the next action is fetching, not re-evaluating."""
    finality = evaluate_period_finality(
        period_start=date(2026, 8, 1),
        period_end_exclusive=date(2026, 9, 1),
        daily_rows=[],
        today=date(2026, 9, 5),
    )
    assert finality.state == "blocked"
    assert finality.observed_days == 0
    assert finality.coverage_pct == 0.0


def test_partial_quality_facts_hold_a_closed_and_complete_month_open() -> None:
    finality = evaluate_period_finality(
        period_start=date(2026, 7, 1),
        period_end_exclusive=date(2026, 8, 1),
        daily_rows=rows(*range(1, 32), month=7),
        today=date(2026, 8, 31),
        has_partial_month=True,
    )
    assert finality.state == "provisional"
    assert "partial_quality_facts" in finality.reasons


def test_a_missing_month_in_a_multi_month_period_holds_it_open() -> None:
    finality = evaluate_period_finality(
        period_start=date(2026, 7, 1),
        period_end_exclusive=date(2026, 8, 1),
        daily_rows=rows(*range(1, 32), month=7),
        today=date(2026, 8, 31),
        months_missing_entirely=["2026-08"],
    )
    assert finality.state == "provisional"
    assert "missing_month:2026-08" in finality.reasons


def test_coverage_is_reported_as_days_not_months() -> None:
    finality = evaluate_period_finality(
        period_start=date(2026, 8, 1),
        period_end_exclusive=date(2026, 9, 1),
        daily_rows=rows(*range(1, 16)),
        today=date(2026, 9, 5),
    )
    assert finality.coverage_pct == pytest.approx(15 / 31 * 100.0)
    payload = finality.as_payload()
    assert payload["reporting_state"] == "provisional"
    assert payload["observed_source_days"] == 15
    assert payload["expected_source_days"] == 31
    assert len(payload["missing_source_days"]) == 16
