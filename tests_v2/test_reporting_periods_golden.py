"""Golden parity for reporting periods: V2 must cover what V1 covered.

A period decides which months a report includes and the title the customer
reads. If V2's "T3 2026" spans different months than V1's, every downstream
number is right about the wrong thing, so the port is compared against the
frozen V1 module rather than against expectations written from the same
assumptions as the code.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from nemsei.reporting import periods as v2_periods
from nemsei.reporting.rules.types import ReportPeriodType


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1() -> ModuleType | None:
    """Import the frozen V1 periods module without adding a static import."""
    if not (V1_ROOT / "monitoring_board" / "reporting" / "periods.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        if importlib.util.find_spec("monitoring_board.reporting.periods") is None:  # pragma: no cover
            return None
        return importlib.import_module("monitoring_board.reporting.periods")
    except Exception:  # pragma: no cover - a broken V1 checkout is missing evidence
        return None


V1 = load_v1()
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


# Every period V1 can build, including the year boundaries and a leap February.
PERIOD_CASES = [
    ("monthly", {"report_month": "2026-07"}),
    ("monthly", {"report_month": "2026-01"}),
    ("monthly", {"report_month": "2026-12"}),
    ("monthly", {"report_month": "2024-02"}),
    ("monthly", {"year": 2026, "month": 3}),
    ("quarterly", {"year": 2026, "quarter": 1}),
    ("quarterly", {"year": 2026, "quarter": 3}),
    ("quarterly", {"year": 2026, "quarter": 4}),
    ("semiannual", {"year": 2026, "semester": 1}),
    ("semiannual", {"year": 2026, "semester": 2}),
    ("annual", {"year": 2026}),
    ("annual", {"year": 2024}),
]


@requires_v1
@pytest.mark.parametrize(("period_type", "kwargs"), PERIOD_CASES)
def test_every_period_matches_v1_field_for_field(period_type: str, kwargs: dict) -> None:
    mine = v2_periods.build_period(period_type, **kwargs)
    theirs = V1.build_period(period_type, **kwargs)
    assert mine.start == theirs.start
    assert mine.end == theirs.end
    assert mine.label == theirs.label
    assert mine.month_count == theirs.month_count
    assert mine.included_months == theirs.included_months
    assert mine.period_type.value == theirs.period_type.value


@requires_v1
@pytest.mark.parametrize(
    ("period_type", "kwargs"),
    [
        ("monthly", {"report_month": "not-a-month"}),
        ("quarterly", {"year": 2026, "quarter": 5}),
        ("quarterly", {"year": 1999, "quarter": 1}),
        ("semiannual", {"year": 2026, "semester": 0}),
        ("annual", {"year": "abc"}),
        ("weekly", {"year": 2026}),
    ],
)
def test_the_same_inputs_are_refused_by_both(period_type: str, kwargs: dict) -> None:
    """Two implementations agreeing on a wrong answer would still pass parity."""
    with pytest.raises(ValueError) as theirs:
        V1.build_period(period_type, **kwargs)
    with pytest.raises(ValueError) as mine:
        v2_periods.build_period(period_type, **kwargs)
    assert str(mine.value) == str(theirs.value)


@requires_v1
def test_the_portuguese_month_labels_are_identical() -> None:
    assert v2_periods.MONTH_LABELS_PT == V1.MONTH_LABELS_PT


@requires_v1
@pytest.mark.parametrize("report_month", ["2026-02", "2026-04", "2026-12", "2024-02"])
def test_month_bounds_agree_including_leap_years(report_month: str) -> None:
    assert v2_periods.month_bounds(report_month) == V1.month_bounds(report_month)


def test_the_exclusive_end_is_the_day_after_v1s_inclusive_one() -> None:
    """The one place V2 deliberately differs, and the reason it is explicit.

    V1's period end is the last day of the period; V2's persistence layer uses a
    half-open interval. Mixing the two silently drops or duplicates a day, so the
    conversion is a named function rather than an inline `+ 1` at each call site.
    """
    for report_month in ("2026-01", "2026-02", "2024-02", "2026-12"):
        period = v2_periods.monthly_period(report_month)
        assert v2_periods.exclusive_end(period) == period.end + timedelta(days=1)
        assert v2_periods.exclusive_end(period).day == 1

    quarter = v2_periods.build_period(ReportPeriodType.QUARTERLY, year=2026, quarter=3)
    assert quarter.start == date(2026, 7, 1)
    assert quarter.end == date(2026, 9, 30)
    assert v2_periods.exclusive_end(quarter) == date(2026, 10, 1)
