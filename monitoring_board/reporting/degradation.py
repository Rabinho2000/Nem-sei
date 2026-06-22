from __future__ import annotations

from datetime import date


def calculate_degradation_factor(mounting_date: date | None, report_month: date) -> float:
    if mounting_date is None:
        return 1.0
    months = (report_month.year - mounting_date.year) * 12 + (report_month.month - mounting_date.month)
    elapsed_months = max(0, months)
    if elapsed_months <= 12:
        return 0.975
    factor = 0.975 - ((elapsed_months - 12) / 12 * 0.0055)
    return min(max(factor, 0.0), 1.0)

