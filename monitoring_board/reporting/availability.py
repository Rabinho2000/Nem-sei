from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


DEFAULT_SLOT_MINUTES = 15
DEFAULT_EDGE_TOLERANCE_MINUTES = 30


def parse_nonnegative_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def is_inverter_available(active_power_kw: float | None) -> bool:
    return active_power_kw is not None and active_power_kw > 0


def inverter_availability_slot(sample_time: datetime, *, slot_minutes: int = DEFAULT_SLOT_MINUTES) -> datetime:
    minute = sample_time.minute - (sample_time.minute % slot_minutes)
    return sample_time.replace(minute=minute, second=0, microsecond=0)


def apply_inverter_edge_tolerance(
    valid_slots: set[datetime],
    *,
    tolerance_minutes: int = DEFAULT_EDGE_TOLERANCE_MINUTES,
) -> set[datetime]:
    slots_by_date: dict[date, list[datetime]] = {}
    for slot in valid_slots:
        slots_by_date.setdefault(slot.date(), []).append(slot)
    considered: set[datetime] = set()
    tolerance = timedelta(minutes=max(tolerance_minutes, 0))
    for day_slots in slots_by_date.values():
        ordered = sorted(day_slots)
        if not ordered:
            continue
        first_slot = ordered[0]
        last_slot = ordered[-1]
        considered.update(
            slot
            for slot in ordered
            if slot - first_slot >= tolerance and last_slot - slot >= tolerance
        )
    return considered


def calculate_inverter_daily_availability(
    samples: list[dict[str, Any]],
    valid_slots: set[datetime] | None = None,
    *,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
    edge_tolerance_minutes: int = DEFAULT_EDGE_TOLERANCE_MINUTES,
) -> dict[str, Any]:
    available_slots = {
        inverter_availability_slot(sample["sample_time"], slot_minutes=slot_minutes)
        for sample in samples
        if isinstance(sample.get("sample_time"), datetime) and is_inverter_available(sample.get("active_power_kw"))
    }
    raw_valid_slots = set(valid_slots) if valid_slots is not None else set(available_slots)
    considered_slots = apply_inverter_edge_tolerance(
        raw_valid_slots,
        tolerance_minutes=edge_tolerance_minutes,
    )
    available_count = len(available_slots & considered_slots)
    valid_count = len(considered_slots)
    return {
        "valid_slots": valid_count,
        "available_slots": available_count,
        "unavailable_slots": max(valid_count - available_count, 0),
        "availability_pct": round(available_count / valid_count * 100, 2) if valid_count else None,
    }


def calculate_weighted_plant_availability(inverter_rows: list[dict[str, Any]]) -> float | None:
    rows = [row for row in inverter_rows if row.get("availability_pct") is not None]
    if not rows:
        return None
    powers = [parse_nonnegative_float(row.get("inverter_power_kw")) for row in rows]
    if all(power is not None and power > 0 for power in powers):
        total_power = sum(float(power) for power in powers if power is not None)
        return round(
            sum(float(row["availability_pct"]) * float(power) for row, power in zip(rows, powers) if power is not None)
            / total_power,
            2,
        )
    return round(sum(float(row["availability_pct"]) for row in rows) / len(rows), 2)

