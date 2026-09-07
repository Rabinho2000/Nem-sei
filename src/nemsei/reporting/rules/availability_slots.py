"""Contractual daily availability from a dense device-history series.

This is the port of V1's **slot** engine (`monitoring_board/reporting/
availability.py`), the one whose output V1's own reports, Excel exports and
monthly close actually used (`plant_availability_daily` ->
`get_monthly_availability`). Its sibling `availability_window.py` ports V1's
other, *sampled* engine, which V1 only ever showed on an internal panel.

The two are kept as separate modules on purpose. They are not variants of one
algorithm: they consume different data (a 5-minute closed-day history pull vs
a sparse realtime poll), bucket differently (fixed 15-minute slots vs raw
sample instants), and answer different questions (a contract-grade figure for
a closed day vs an operational indicator for today). Merging them would mean
picking one set of constants for both, which is exactly how a realtime
estimate ends up standing in for a warranted number.

Pure calculation, no I/O, so it can be run side by side with V1's real
functions in a golden test (`tests_v2/test_availability_contractual_golden.py`).

Verified against V1's real code on real provider data: asset 153,
2026-09-04, two inverters -> 47 valid slots, 100.0% both devices, 100.0%
plant, identical in both implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from nemsei.reporting.rules.availability import float_or_none, weighted_sampled_availability

# Ported verbatim from V1's `DEFAULT_SLOT_MINUTES` / `DEFAULT_EDGE_TOLERANCE_MINUTES`.
SLOT_MINUTES = 15
EDGE_TOLERANCE_MINUTES = 30


def is_available(active_power_kw: Any) -> bool:
    """V1's `is_inverter_available`: strictly positive power, nothing else.

    Note what this is *not*: it does not consult `inverter_state`. The
    history payload carries one (verified live: 512 running, 40960 standby,
    and nulls), but V1's contractual number was defined on power alone, and
    redefining it here would silently move every customer's percentage.
    `inverter_state` is still persisted on the underlying fact, so a future,
    explicitly-decided rule can use it without a new provider call.
    """
    power = float_or_none(active_power_kw)
    return power is not None and power > 0


def slot_of(sample_time: datetime, *, slot_minutes: int = SLOT_MINUTES) -> datetime:
    """V1's `inverter_availability_slot`: floor the minute onto the slot grid."""
    return sample_time.replace(minute=sample_time.minute - (sample_time.minute % slot_minutes), second=0, microsecond=0)


def apply_edge_tolerance(valid_slots: set[datetime], *, tolerance_minutes: int = EDGE_TOLERANCE_MINUTES) -> set[datetime]:
    """V1's `apply_inverter_edge_tolerance`, ported including its grouping.

    Trims `tolerance_minutes` off both ends of each **calendar day's own**
    observed slot range -- so sunrise/sunset ramps, where one inverter
    legitimately starts a few minutes after another, cannot count as
    unavailability. Grouped by `slot.date()` exactly as V1 did.
    """
    slots_by_date: dict[date, list[datetime]] = {}
    for slot in valid_slots:
        slots_by_date.setdefault(slot.date(), []).append(slot)
    tolerance = timedelta(minutes=max(tolerance_minutes, 0))
    considered: set[datetime] = set()
    for day_slots in slots_by_date.values():
        ordered = sorted(day_slots)
        if not ordered:
            continue
        first_slot, last_slot = ordered[0], ordered[-1]
        considered.update(slot for slot in ordered if slot - first_slot >= tolerance and last_slot - slot >= tolerance)
    return considered


@dataclass(frozen=True)
class DeviceHistorySample:
    """One history reading for one device, already restricted to one day."""

    device_id: int
    sample_time: datetime
    active_power_kw: Any


@dataclass(frozen=True)
class ContractualDeviceDay:
    device_id: int
    availability_pct: float | None
    valid_slots: int
    available_slots: int
    unavailable_slots: int


@dataclass(frozen=True)
class ContractualAssetDay:
    availability_pct: float | None
    valid_slots: int
    expected_device_count: int
    observed_device_count: int
    window_start: datetime | None
    window_end: datetime | None
    devices: tuple[ContractualDeviceDay, ...]


def device_day_availability(
    samples: list[DeviceHistorySample], considered_slots: set[datetime]
) -> tuple[float | None, int, int, int]:
    """V1's `calculate_inverter_daily_availability`, given the plant's slots.

    Returns `(availability_pct, valid, available, unavailable)`. The device's
    *own* available slots are intersected with the plant-level considered
    set, so a device is measured only over the window the plant as a whole
    was demonstrably producing in -- V1's rule, and the reason a plant with
    one inverter on a tracker does not punish the others.

    `availability_pct` is `None` in exactly two cases, and they are different
    questions with the same answer shape:

    - there is no considered slot at all (the plant produced nowhere that
      day, so there is no window to measure anyone against); or
    - **this device returned no history rows at all** -- no evidence, so no
      figure.

    That second case is a deliberate divergence from V1, documented here
    rather than left to be discovered. V1's slot engine intersects an empty
    set with the considered slots, gets zero, and reports the device as
    `0.0%` -- indistinguishable from an inverter that was genuinely dead all
    day. Those are different facts about a customer's plant, and this
    milestone's rule is explicit that an absent reading must never become a
    zero. A device that *did* report and produced in none of the considered
    slots is still a real `0.0`.
    """
    if not samples:
        return None, len(considered_slots), 0, 0
    available = {slot_of(sample.sample_time) for sample in samples if is_available(sample.active_power_kw)}
    available_count = len(available & considered_slots)
    valid_count = len(considered_slots)
    return (
        (round(available_count / valid_count * 100, 2) if valid_count else None),
        valid_count,
        available_count,
        max(valid_count - available_count, 0),
    )


def compute_asset_contractual_day(
    *,
    expected_devices: list[dict[str, Any]],
    samples: list[DeviceHistorySample],
) -> ContractualAssetDay:
    """One asset's contractual availability for one closed day.

    `expected_devices` comes from the temporal device configuration
    (`diagnostics/availability_service.expected_devices_for_date`), not from
    whatever the provider happened to return -- so an inverter that was
    installed that day and has since been removed is still expected, and one
    installed later is not.

    **Two deliberate divergences from V1, documented rather than silent.**
    Both make this strictly more conservative than V1: each can only withhold
    a number V1 would have published, never publish one V1 withheld.

    1. An expected device that returned *no* history rows reads `None`, not
       `0.0` (see `device_day_availability`) -- missing evidence is not an
       outage.
    2. Plant aggregation uses the already-golden
       `weighted_sampled_availability`, which returns `None` if any expected
       device is unknown. V1's `calculate_weighted_plant_availability`
       instead *drops* such devices and averages the rest, so a plant could
       report 100% while an inverter contributed nothing at all.

    Together they satisfy this milestone's explicit rule: an expected device
    without a valid figure makes the plant `None`, and is neither ignored nor
    silently zeroed.

    The rated-power weighting and its fallback are untouched V1 behavior,
    shared with the sampled engine through that same golden function.
    """
    expected_ids = {int(device["device_id"]) for device in expected_devices}
    evaluable = [sample for sample in samples if sample.device_id in expected_ids]

    # Plant-level valid slots: any expected inverter producing in that slot.
    # V1 built this union across the plant before evaluating any single
    # device, which is what makes the denominator common to all of them.
    plant_slots = {slot_of(sample.sample_time) for sample in evaluable if is_available(sample.active_power_kw)}
    considered = apply_edge_tolerance(plant_slots)

    by_device: dict[int, list[DeviceHistorySample]] = {device_id: [] for device_id in expected_ids}
    for sample in evaluable:
        by_device[sample.device_id].append(sample)

    device_results: list[ContractualDeviceDay] = []
    for device in expected_devices:
        device_id = int(device["device_id"])
        pct, valid, available, unavailable = device_day_availability(by_device[device_id], considered)
        device_results.append(
            ContractualDeviceDay(
                device_id=device_id,
                availability_pct=pct,
                valid_slots=valid,
                available_slots=available,
                unavailable_slots=unavailable,
            )
        )

    rated_by_device = {int(device["device_id"]): device.get("rated_power_kw") for device in expected_devices}
    plant_pct = weighted_sampled_availability(
        [
            {"availability_pct": result.availability_pct, "rated_power_kw": rated_by_device[result.device_id]}
            for result in device_results
        ]
    ) if device_results else None

    ordered = sorted(considered)
    return ContractualAssetDay(
        availability_pct=plant_pct,
        # V1's `plant_availability_daily.valid_slots`: the *tolerated* plant
        # slot count, and the exact weight its monthly query uses. Storing
        # anything else here would silently change the monthly number.
        valid_slots=len(considered),
        expected_device_count=len(expected_devices),
        observed_device_count=sum(1 for device_id in expected_ids if by_device[device_id]),
        window_start=ordered[0] if ordered else None,
        window_end=ordered[-1] if ordered else None,
        devices=tuple(device_results),
    )
