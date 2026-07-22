from __future__ import annotations

from datetime import date, timedelta

import pytest

from monitoring_board.reporting.data_quality import evaluate_monthly_production_quality


def daily_rows(month_start: date, days: int, *, value: float = 1.0) -> list[dict[str, object]]:
    return [
        {"date": month_start + timedelta(days=offset), "production_kwh": value}
        for offset in range(days)
    ]


def evaluate(
    month_start: date,
    *,
    monthly_records=(),
    daily_records=(),
    reference_date: date = date(2026, 7, 22),
):
    return evaluate_monthly_production_quality(
        asset_id=7,
        month_start=month_start,
        reference_date=reference_date,
        monthly_records=monthly_records,
        daily_records=daily_records,
    )


def test_closed_31_day_month_is_complete_from_distinct_daily_values() -> None:
    result = evaluate(date(2026, 1, 1), daily_records=daily_rows(date(2026, 1, 1), 31))

    assert result.status == "complete"
    assert result.production_kwh == 31
    assert result.expected_days == result.available_days == 31
    assert result.coverage_ratio == 1
    assert result.source == "daily"


def test_closed_month_with_one_missing_day_is_partial_and_not_final() -> None:
    result = evaluate(date(2026, 4, 1), daily_records=daily_rows(date(2026, 4, 1), 29))

    assert result.status == "partial"
    assert result.production_kwh is None
    assert result.raw_daily_total_kwh == 29
    assert result.available_days == 29
    assert result.missing_dates == (date(2026, 4, 30),)


def test_leap_year_february_expects_29_days() -> None:
    result = evaluate(
        date(2024, 2, 1),
        daily_records=daily_rows(date(2024, 2, 1), 29),
        reference_date=date(2024, 3, 1),
    )

    assert result.status == "complete"
    assert result.expected_days == 29


def test_zero_is_valid_daily_production_and_counts_for_coverage() -> None:
    rows = daily_rows(date(2026, 4, 1), 30)
    rows[12]["production_kwh"] = 0

    result = evaluate(date(2026, 4, 1), daily_records=rows)

    assert result.status == "complete"
    assert result.available_days == 30
    assert result.production_kwh == 29


def test_no_data_is_missing_without_a_synthetic_zero() -> None:
    result = evaluate(date(2026, 3, 1))

    assert result.status == "missing"
    assert result.production_kwh is None
    assert result.raw_daily_total_kwh is None
    assert result.source == "none"


@pytest.mark.parametrize("invalid", [None, "not-a-number", float("nan"), float("inf"), -1])
def test_invalid_values_do_not_count_as_coverage(invalid) -> None:
    result = evaluate(
        date(2026, 3, 1),
        monthly_records=[{"date": date(2026, 3, 1), "production_kwh": invalid}],
        daily_records=[{"date": date(2026, 3, 1), "production_kwh": invalid}],
    )

    assert result.status == "missing"
    assert result.production_kwh is None
    assert result.available_days == 0
    assert "invalid_monthly_production" in result.warnings


def test_complete_daily_sum_conflicting_with_monthly_value_is_not_final() -> None:
    result = evaluate(
        date(2026, 4, 1),
        monthly_records=[{"date": date(2026, 4, 1), "production_kwh": 100}],
        daily_records=daily_rows(date(2026, 4, 1), 30, value=4),
    )

    assert result.status == "conflict"
    assert result.production_kwh is None
    assert result.raw_daily_total_kwh == 120


def test_valid_monthly_value_stays_complete_with_partial_daily_coverage() -> None:
    result = evaluate(
        date(2026, 4, 1),
        monthly_records=[{"date": date(2026, 4, 1), "production_kwh": 100}],
        daily_records=daily_rows(date(2026, 4, 1), 10, value=5),
    )

    assert result.status == "complete"
    assert result.production_kwh == 100
    assert result.source == "monthly"
    assert result.daily_coverage == "partial"


def test_partial_daily_total_above_monthly_value_is_already_a_conflict() -> None:
    result = evaluate(
        date(2026, 4, 1),
        monthly_records=[{"date": date(2026, 4, 1), "production_kwh": 100}],
        daily_records=daily_rows(date(2026, 4, 1), 11, value=10),
    )

    assert result.status == "conflict"
    assert result.production_kwh is None


def test_current_month_is_in_progress_even_with_a_monthly_value() -> None:
    result = evaluate(
        date(2026, 7, 1),
        monthly_records=[{"date": date(2026, 7, 1), "production_kwh": 100}],
    )

    assert result.status == "in_progress"
    assert result.production_kwh is None
    assert result.source == "monthly"
