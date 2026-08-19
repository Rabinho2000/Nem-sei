"""Plant availability from per-device availability, ported from V1.

V1 computes a plant's availability by weighting each device's availability by
its rated power, and falls back to a plain mean the moment any device has no
usable rating, rather than silently treating an unknown rating as zero weight.

Only this calculation is portable. The rest of V1's
`services/sampled_availability.py` is SQLite queries against inverter samples
and device realtime snapshots, and V2 holds neither yet: devices exist as
canonical identity but carry no facts. Persisting availability therefore waits
for device-level facts rather than being ported against tables that do not
exist.

A device with no availability makes the plant's availability unknown. It is
never counted as a zero, because "one inverter did not report" and "one inverter
was down all day" are different statements about a customer's plant.
"""
from __future__ import annotations

from typing import Any


def float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def positive_float(value: Any) -> bool:
    parsed = float_or_none(value)
    return parsed is not None and parsed > 0


def weighted_sampled_availability(rows: list[dict[str, Any]]) -> float | None:
    """Weight each device by rated power; any unknown rating drops to a mean.

    Returns ``None`` when there is nothing to say: no devices, or any device
    whose availability is unknown.
    """
    if not rows or any(row.get("availability_pct") is None for row in rows):
        return None
    weighted: list[tuple[float, float]] = []
    for row in rows:
        power = float_or_none(row.get("rated_power_kw"))
        if power is None or power <= 0:
            return round(sum(float(item["availability_pct"]) for item in rows) / len(rows), 2)
        weighted.append((float(row["availability_pct"]), power))
    total_power = sum(power for _value, power in weighted)
    return round(sum(value * power for value, power in weighted) / total_power, 2)
