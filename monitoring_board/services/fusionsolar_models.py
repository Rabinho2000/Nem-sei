from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class FusionSolarEndpoints:
    base_url: str
    login_endpoint: str
    plants_endpoint: str
    real_time_endpoint: str
    device_list_endpoint: str
    device_real_time_endpoint: str
    device_history_endpoint: str
    alarms_endpoint: str
    day_kpi_endpoint: str
    month_kpi_endpoint: str


@dataclass(frozen=True)
class FusionSolarCredentials:
    username: str
    password: str


def collect_time_start_of_day_ms(collect_date: date) -> int:
    """Current behavior: local process timezone midnight for the requested day."""
    # TODO: Verify with FusionSolar whether collectTime must be portal timezone,
    # station timezone, or UTC. Existing app behavior uses local process timezone.
    return int(datetime.combine(collect_date, datetime.min.time()).timestamp() * 1000)


def collect_time_noon_of_month_ms(collect_date: date) -> int:
    """Current behavior: local process timezone noon on the first day of month."""
    # TODO: Verify why the month/list endpoint is queried at noon. This preserves
    # existing production report behavior.
    month_start = collect_date.replace(day=1)
    return int(datetime.combine(month_start, datetime.min.time().replace(hour=12)).timestamp() * 1000)


def closed_day_window_ms(target_date: date) -> tuple[int, int]:
    """Current behavior: local process timezone [00:00:00, 23:59:59.999]."""
    start_time = int(datetime.combine(target_date, datetime.min.time()).timestamp() * 1000)
    end_time = int(datetime.combine(target_date + timedelta(days=1), datetime.min.time()).timestamp() * 1000) - 1
    return start_time, end_time


def normalize_kpi_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("list")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return [data]
    return []


def parse_collect_date(row: dict[str, Any], fallback_date: date | None = None) -> date | None:
    for key in ("collectTime", "collect_time", "time", "timestamp"):
        raw_value = row.get(key)
        if raw_value in (None, ""):
            continue
        try:
            timestamp = int(float(str(raw_value).strip()))
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            return datetime.fromtimestamp(timestamp).date()
        except (TypeError, ValueError, OSError, OverflowError):
            parsed = _parse_isoish_date(str(raw_value))
            if parsed:
                return parsed
    for key in ("collectDate", "date", "day", "periodDate"):
        parsed = _parse_isoish_date(str(row.get(key) or "").strip())
        if parsed:
            return parsed
    return fallback_date


def _parse_isoish_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None
