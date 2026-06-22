from __future__ import annotations

import calendar
from datetime import date, datetime

from monitoring_board.reporting.models import ReportPeriodType, ReportingPeriod


MONTH_LABELS_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Marco",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}


def normalize_report_month(value: str | None, *, today: date | None = None) -> str:
    if value:
        try:
            return datetime.strptime(value.strip(), "%Y-%m").strftime("%Y-%m")
        except ValueError:
            pass
    reference = today or date.today()
    return reference.strftime("%Y-%m")


def normalize_report_year(value: str | None, *, today: date | None = None) -> int:
    reference = today or date.today()
    if value and value.strip().isdigit():
        year = int(value.strip())
        if 2000 <= year <= reference.year + 1:
            return year
    return reference.year


def month_bounds(report_month: str) -> tuple[date, date]:
    start = datetime.strptime(report_month, "%Y-%m").date()
    _, last_day = calendar.monthrange(start.year, start.month)
    return start, start.replace(day=last_day)


def monthly_period(report_month: str) -> ReportingPeriod:
    start, end = month_bounds(report_month)
    return ReportingPeriod(
        period_type=ReportPeriodType.MONTHLY,
        start=start,
        end=end,
        label=f"{MONTH_LABELS_PT[start.month]} {start.year}",
        month_count=1,
        included_months=(start,),
    )

