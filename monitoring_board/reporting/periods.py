from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any

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

VALID_PERIOD_TYPES = {period_type.value for period_type in ReportPeriodType}


class ReportingPeriodError(ValueError):
    pass


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


def validate_period_type(value: str | ReportPeriodType | None) -> ReportPeriodType:
    if isinstance(value, ReportPeriodType):
        return value
    raw = str(value or ReportPeriodType.MONTHLY.value).strip().lower()
    try:
        return ReportPeriodType(raw)
    except ValueError as exc:
        raise ReportingPeriodError(f"Tipo de periodo invalido: {value}") from exc


def validate_year(value: str | int | None) -> int:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReportingPeriodError("Ano invalido.") from exc
    if year < 2000 or year > 2100:
        raise ReportingPeriodError("Ano invalido.")
    return year


def validate_month_value(value: str | int | None) -> int:
    try:
        month = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReportingPeriodError("Mes invalido.") from exc
    if month < 1 or month > 12:
        raise ReportingPeriodError("Mes invalido.")
    return month


def validate_quarter(value: str | int | None) -> int:
    try:
        quarter = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReportingPeriodError("Trimestre invalido.") from exc
    if quarter < 1 or quarter > 4:
        raise ReportingPeriodError("Trimestre invalido.")
    return quarter


def validate_semester(value: str | int | None) -> int:
    try:
        semester = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReportingPeriodError("Semestre invalido.") from exc
    if semester < 1 or semester > 2:
        raise ReportingPeriodError("Semestre invalido.")
    return semester


def included_months(start: date, month_count: int) -> tuple[date, ...]:
    months = []
    year = start.year
    month = start.month
    for _ in range(month_count):
        months.append(date(year, month, 1))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return tuple(months)


def period_label(period_type: ReportPeriodType, start: date, end: date) -> str:
    if period_type == ReportPeriodType.MONTHLY:
        return f"{MONTH_LABELS_PT[start.month]} {start.year}"
    if period_type == ReportPeriodType.QUARTERLY:
        quarter = ((start.month - 1) // 3) + 1
        return f"T{quarter} {start.year}"
    if period_type == ReportPeriodType.SEMIANNUAL:
        semester = 1 if start.month == 1 else 2
        return f"S{semester} {start.year}"
    if period_type == ReportPeriodType.ANNUAL:
        return f"{start.year}"
    raise ReportingPeriodError(f"Tipo de periodo invalido: {period_type}")


def build_period(
    period_type: str | ReportPeriodType,
    *,
    year: str | int | None = None,
    month: str | int | None = None,
    quarter: str | int | None = None,
    semester: str | int | None = None,
    report_month: str | None = None,
) -> ReportingPeriod:
    parsed_type = validate_period_type(period_type)
    if parsed_type == ReportPeriodType.MONTHLY:
        if report_month:
            try:
                start, end = month_bounds(report_month.strip())
            except ValueError as exc:
                raise ReportingPeriodError("Mes invalido.") from exc
        else:
            parsed_year = validate_year(year)
            parsed_month = validate_month_value(month)
            start, end = month_bounds(f"{parsed_year:04d}-{parsed_month:02d}")
        return ReportingPeriod(
            period_type=parsed_type,
            start=start,
            end=end,
            label=period_label(parsed_type, start, end),
            month_count=1,
            included_months=(start,),
        )

    parsed_year = validate_year(year)
    if parsed_type == ReportPeriodType.QUARTERLY:
        parsed_quarter = validate_quarter(quarter)
        start_month = (parsed_quarter - 1) * 3 + 1
        month_count = 3
    elif parsed_type == ReportPeriodType.SEMIANNUAL:
        parsed_semester = validate_semester(semester)
        start_month = 1 if parsed_semester == 1 else 7
        month_count = 6
    elif parsed_type == ReportPeriodType.ANNUAL:
        start_month = 1
        month_count = 12
    else:
        raise ReportingPeriodError(f"Tipo de periodo invalido: {period_type}")

    start = date(parsed_year, start_month, 1)
    end_month = start_month + month_count - 1
    _, last_day = calendar.monthrange(parsed_year, end_month)
    end = date(parsed_year, end_month, last_day)
    return ReportingPeriod(
        period_type=parsed_type,
        start=start,
        end=end,
        label=period_label(parsed_type, start, end),
        month_count=month_count,
        included_months=included_months(start, month_count),
    )


def monthly_period(report_month: str) -> ReportingPeriod:
    return build_period(ReportPeriodType.MONTHLY, report_month=report_month)


def period_from_form(data: Any) -> ReportingPeriod:
    get_value = data.get if hasattr(data, "get") else lambda key, default=None: default
    period_type = validate_period_type(get_value("period_type") or get_value("report_period_type") or ReportPeriodType.MONTHLY.value)
    if period_type == ReportPeriodType.MONTHLY:
        report_month = get_value("report_month")
        if report_month:
            return build_period(period_type, report_month=str(report_month))
        return build_period(period_type, year=get_value("report_year"), month=get_value("report_month_number"))
    if period_type == ReportPeriodType.QUARTERLY:
        return build_period(period_type, year=get_value("report_year"), quarter=get_value("report_quarter"))
    if period_type == ReportPeriodType.SEMIANNUAL:
        return build_period(period_type, year=get_value("report_year"), semester=get_value("report_semester"))
    if period_type == ReportPeriodType.ANNUAL:
        return build_period(period_type, year=get_value("report_year"))
    raise ReportingPeriodError(f"Tipo de periodo invalido: {period_type}")

