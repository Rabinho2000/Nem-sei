"""Calendar rendering helpers shared by legacy and extracted routes."""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from monitoring_board.constants import MONTH_NAMES_PT, PROBLEM_MONITORING_STATUSES


def normalize_calendar_month(value: str | None) -> str:
    if value:
        try:
            return datetime.strptime(value.strip(), "%Y-%m").strftime("%Y-%m")
        except ValueError:
            pass
    return date.today().strftime("%Y-%m")


def calendar_month_bounds(month_value: str) -> tuple[date, date, str, str]:
    month_start = datetime.strptime(month_value, "%Y-%m").date().replace(day=1)
    _, last_day = calendar.monthrange(month_start.year, month_start.month)
    month_end = month_start.replace(day=last_day)
    previous_month_date = (month_start - timedelta(days=1)).replace(day=1)
    next_month_date = (month_end + timedelta(days=1)).replace(day=1)
    return month_start, month_end, previous_month_date.strftime("%Y-%m"), next_month_date.strftime("%Y-%m")


def build_error_calendar(month_value: str, records: list[sqlite3.Row]) -> dict[str, Any]:
    month_start, month_end, _, _ = calendar_month_bounds(month_value)
    records_by_day: dict[str, list[sqlite3.Row]] = {}
    for record in records:
        records_by_day.setdefault(record["record_date"], []).append(record)

    weeks = []
    week = [{"date": None, "records": []} for _ in range(month_start.weekday())]
    current_day = month_start
    while current_day <= month_end:
        week.append({"date": current_day, "records": records_by_day.get(current_day.isoformat(), [])})
        if len(week) == 7:
            weeks.append(week)
            week = []
        current_day += timedelta(days=1)
    if week:
        week.extend({"date": None, "records": []} for _ in range(7 - len(week)))
        weeks.append(week)
    return {
        "label": f"{MONTH_NAMES_PT[month_start.month]} {month_start.year}",
        "weeks": weeks,
        "record_count": sum(len(rows) for rows in records_by_day.values()),
    }


def build_intervention_calendar(month_value: str, records: list[sqlite3.Row]) -> dict[str, Any]:
    month_start, month_end, _, _ = calendar_month_bounds(month_value)
    records_by_day: dict[str, list[sqlite3.Row]] = {}
    for record in records:
        if record["planned_date"]:
            records_by_day.setdefault(record["planned_date"], []).append(record)

    weeks = []
    week = [{"date": None, "records": []} for _ in range(month_start.weekday())]
    current_day = month_start
    while current_day <= month_end:
        week.append({"date": current_day, "records": records_by_day.get(current_day.isoformat(), [])})
        if len(week) == 7:
            weeks.append(week)
            week = []
        current_day += timedelta(days=1)
    if week:
        week.extend({"date": None, "records": []} for _ in range(7 - len(week)))
        weeks.append(week)
    return {
        "label": f"{MONTH_NAMES_PT[month_start.month]} {month_start.year}",
        "weeks": weeks,
        "record_count": sum(len(rows) for rows in records_by_day.values()),
    }


def intervention_ready_for_route(row: sqlite3.Row | dict[str, Any]) -> bool:
    return not (
        row["status"] == "Fechado"
        or row["material_status"] == "Bloqueado"
        or row["latitude"] is None
        or row["longitude"] is None
        or row["coordinates_confidence"] in {"suspect", "review"}
    )


def build_asset_error_calendar(month_value: str, records: list[sqlite3.Row]) -> dict[str, Any]:
    month_start, month_end, _, _ = calendar_month_bounds(month_value)
    events_by_day: dict[str, list[dict[str, Any]]] = {}
    previous_problem = False
    for record in records:
        record_date = record["record_date"]
        is_problem = record["status"] in PROBLEM_MONITORING_STATUSES
        event_type = ""
        event_label = ""
        if is_problem and not previous_problem:
            event_type, event_label = "start", "Apareceu"
        elif is_problem:
            event_type, event_label = "active", "Mantem-se"
        elif previous_problem:
            event_type, event_label = "end", "Desapareceu"
        if month_start.isoformat() <= record_date <= month_end.isoformat() and event_type:
            events_by_day.setdefault(record_date, []).append(
                {
                    "status": record["status"],
                    "label": event_label,
                    "type": event_type,
                    "notes": record["notes"],
                    "source": record["source"],
                    "record_id": record["id"],
                }
            )
        previous_problem = is_problem

    weeks = []
    week = [{"date": None, "events": []} for _ in range(month_start.weekday())]
    current_day = month_start
    while current_day <= month_end:
        week.append({"date": current_day, "events": events_by_day.get(current_day.isoformat(), [])})
        if len(week) == 7:
            weeks.append(week)
            week = []
        current_day += timedelta(days=1)
    if week:
        week.extend({"date": None, "events": []} for _ in range(7 - len(week)))
        weeks.append(week)
    return {
        "label": f"{MONTH_NAMES_PT[month_start.month]} {month_start.year}",
        "weeks": weeks,
        "event_count": sum(len(rows) for rows in events_by_day.values()),
    }
