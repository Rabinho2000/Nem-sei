from __future__ import annotations

from datetime import date


def calculate_degradation_factor(mounting_date: date | None, report_month: date) -> float:
    if mounting_date is None:
        return 1.0
    months = (report_month.year - mounting_date.year) * 12 + (report_month.month - mounting_date.month)
    years_since_mounting = max(0, months) / 12
    return max(0.0, 1 - 0.025 - years_since_mounting * 0.0055)

