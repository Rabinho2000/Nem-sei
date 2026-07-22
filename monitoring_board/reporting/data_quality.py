from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable


MONTHLY_DAILY_ABSOLUTE_TOLERANCE_KWH = 1.0
MONTHLY_DAILY_RELATIVE_TOLERANCE = 0.01
FINAL_PRODUCTION_STATUSES = {"complete"}
INSUFFICIENT_PRODUCTION_STATUSES = {"partial", "missing", "conflict"}


@dataclass(frozen=True)
class MonthlyProductionQuality:
    asset_id: int | None
    month_start: date
    status: str
    production_kwh: float | None
    raw_daily_total_kwh: float | None
    source: str
    expected_days: int
    available_days: int
    missing_dates: tuple[date, ...]
    coverage_ratio: float
    daily_coverage: str
    warnings: tuple[str, ...]

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_PRODUCTION_STATUSES

    @property
    def requires_fallback(self) -> bool:
        return self.status in INSUFFICIENT_PRODUCTION_STATUSES and "future_production_period" not in self.warnings

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "month": self.month_start.strftime("%Y-%m"),
            "status": self.status,
            "production_kwh": self.production_kwh,
            "raw_daily_total_kwh": self.raw_daily_total_kwh,
            "source": self.source,
            "expected_days": self.expected_days,
            "available_days": self.available_days,
            "missing_dates": [item.isoformat() for item in self.missing_dates],
            "coverage_ratio": self.coverage_ratio,
            "daily_coverage": self.daily_coverage,
            "warnings": list(self.warnings),
        }


def monthly_daily_tolerance_kwh(monthly_production_kwh: float) -> float:
    """Return the centralized reconciliation tolerance: max(1 kWh, 1%)."""

    return max(
        MONTHLY_DAILY_ABSOLUTE_TOLERANCE_KWH,
        abs(monthly_production_kwh) * MONTHLY_DAILY_RELATIVE_TOLERANCE,
    )


def evaluate_monthly_production_quality(
    *,
    asset_id: int | None,
    month_start: date,
    reference_date: date,
    monthly_records: Iterable[Any] = (),
    daily_records: Iterable[Any] = (),
) -> MonthlyProductionQuality:
    """Evaluate whether one installation-month has publishable production.

    The caller must provide ``reference_date`` so current-month behavior is
    deterministic and testable. Daily coverage counts distinct calendar dates.
    """

    month_start = month_start.replace(day=1)
    expected_days = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = month_start.replace(day=expected_days)
    warnings: set[str] = set()

    monthly_values: list[float] = []
    monthly_record_count = 0
    for record in monthly_records:
        record_date = _record_date(record)
        if record_date is not None and record_date.replace(day=1) != month_start:
            continue
        monthly_record_count += 1
        value = _valid_production(_record_value(record))
        if value is None:
            warnings.add("invalid_monthly_production")
        else:
            monthly_values.append(value)
    if monthly_record_count > 1:
        warnings.add("duplicate_monthly_production")
    monthly_value = monthly_values[-1] if monthly_values else None

    daily_by_date: dict[date, float] = {}
    seen_daily_dates: set[date] = set()
    for record in daily_records:
        record_date = _record_date(record)
        if record_date is None or not month_start <= record_date <= month_end:
            continue
        if record_date in seen_daily_dates:
            warnings.add("duplicate_daily_production")
        seen_daily_dates.add(record_date)
        value = _valid_production(_record_value(record))
        if value is None:
            warnings.add(f"invalid_daily_production:{record_date.isoformat()}")
            continue
        # Last valid value wins, preventing duplicate local/API rows being added.
        daily_by_date[record_date] = value

    available_days = len(daily_by_date)
    all_dates = tuple(month_start + timedelta(days=offset) for offset in range(expected_days))
    missing_dates = tuple(item for item in all_dates if item not in daily_by_date)
    raw_daily_total = sum(daily_by_date.values()) if daily_by_date else None
    coverage_ratio = available_days / expected_days if expected_days else 0.0
    daily_coverage = "complete" if available_days == expected_days else ("partial" if available_days else "missing")
    if daily_coverage == "partial":
        warnings.add("partial_daily_coverage")
    elif daily_coverage == "missing":
        warnings.add("missing_daily_coverage")

    source = "monthly" if monthly_value is not None else ("daily" if daily_by_date else "none")
    reference_month = reference_date.replace(day=1)
    if month_start == reference_month:
        warnings.add("production_in_progress")
        return _result(
            asset_id, month_start, "in_progress", None, raw_daily_total, source,
            expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
        )
    if month_start > reference_month:
        warnings.add("future_production_period")
        return _result(
            asset_id, month_start, "missing", None, raw_daily_total, source,
            expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
        )

    if monthly_value is not None:
        tolerance = monthly_daily_tolerance_kwh(monthly_value)
        complete_daily_conflict = (
            daily_coverage == "complete"
            and raw_daily_total is not None
            and abs(raw_daily_total - monthly_value) > tolerance
        )
        impossible_partial_total = raw_daily_total is not None and raw_daily_total > monthly_value + tolerance
        if complete_daily_conflict or impossible_partial_total:
            warnings.add("monthly_daily_production_conflict")
            return _result(
                asset_id, month_start, "conflict", None, raw_daily_total, "monthly",
                expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
            )
        return _result(
            asset_id, month_start, "complete", monthly_value, raw_daily_total, "monthly",
            expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
        )

    if daily_coverage == "complete":
        return _result(
            asset_id, month_start, "complete", raw_daily_total, raw_daily_total, "daily",
            expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
        )
    status = "partial" if available_days else "missing"
    warnings.add(f"{status}_monthly_production")
    return _result(
        asset_id, month_start, status, None, raw_daily_total, source,
        expected_days, available_days, missing_dates, coverage_ratio, daily_coverage, warnings,
    )


def production_quality_notice(quality: MonthlyProductionQuality) -> str | None:
    coverage = f"{quality.available_days}/{quality.expected_days} dias disponíveis"
    if quality.status == "partial":
        return f"Rascunho — produção incompleta: {coverage}"
    if quality.status == "missing":
        return f"Rascunho — produção indisponível: {coverage}"
    if quality.status == "conflict":
        return f"Rascunho — conflito entre produção mensal e diária: {coverage}"
    if quality.status == "in_progress":
        return f"Rascunho — mês em curso: {coverage}"
    return None


def _result(
    asset_id: int | None,
    month_start: date,
    status: str,
    production_kwh: float | None,
    raw_daily_total_kwh: float | None,
    source: str,
    expected_days: int,
    available_days: int,
    missing_dates: tuple[date, ...],
    coverage_ratio: float,
    daily_coverage: str,
    warnings: set[str],
) -> MonthlyProductionQuality:
    return MonthlyProductionQuality(
        asset_id=asset_id,
        month_start=month_start,
        status=status,
        production_kwh=production_kwh,
        raw_daily_total_kwh=raw_daily_total_kwh,
        source=source,
        expected_days=expected_days,
        available_days=available_days,
        missing_dates=missing_dates,
        coverage_ratio=coverage_ratio,
        daily_coverage=daily_coverage,
        warnings=tuple(sorted(warnings)),
    )


def _record_value(record: Any) -> Any:
    return _record_get(record, "production_kwh")


def _record_date(record: Any) -> date | None:
    value = _record_get(record, "date")
    if value is None:
        value = _record_get(record, "period_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value not in (None, ""):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _record_get(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    try:
        return record[key]
    except (KeyError, TypeError, IndexError):
        return getattr(record, key, None)


def _valid_production(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None
