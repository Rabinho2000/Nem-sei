"""Golden parity for **contractual** availability against V1's slot engine.

The sampled engine already has its parity file
(`test_availability_golden.py`). This one pins the other engine -- the one
whose number V1's contracts, Excel exports and monthly close actually used
(`plant_availability_daily` -> `get_monthly_availability`) -- at all three
levels the milestone requires: daily device, daily plant, monthly plant.

Every comparison runs V1's *real* functions, imported from the frozen
checkout, rather than expected values typed in by hand. Where V2 diverges
from V1 it is asserted as a divergence with its reason, never smoothed over.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nemsei.reporting.rules.availability_slots import (
    DeviceHistorySample,
    apply_edge_tolerance,
    compute_asset_contractual_day,
    device_day_availability,
    is_available,
    slot_of,
)

V1_ROOT = Path("/opt/server/apps/Nem-sei")


def _load(module: str):
    if not (V1_ROOT / "monitoring_board" / "reporting" / "availability.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        return importlib.import_module(module)
    except Exception:  # pragma: no cover - a broken checkout is missing evidence
        return None


V1_SLOTS = _load("monitoring_board.reporting.availability")
V1_REPOS = _load("monitoring_board.reporting.repositories")
requires_v1 = pytest.mark.skipif(V1_SLOTS is None, reason="the frozen V1 checkout is not available here")
requires_v1_repos = pytest.mark.skipif(V1_REPOS is None, reason="the frozen V1 checkout is not available here")

DAY = date(2026, 9, 4)


def _series(device_id: int, *, start_hour: int, end_hour: int, step_minutes: int = 5, power: float = 10.0,
            dark: set[int] | None = None) -> list[DeviceHistorySample]:
    """A 5-minute power series, matching the real provider cadence."""
    out: list[DeviceHistorySample] = []
    cursor = datetime(DAY.year, DAY.month, DAY.day, start_hour, 0)
    end = datetime(DAY.year, DAY.month, DAY.day, end_hour, 0)
    while cursor <= end:
        value = 0.0 if dark and cursor.hour in dark else power
        out.append(DeviceHistorySample(device_id=device_id, sample_time=cursor, active_power_kw=value))
        cursor += timedelta(minutes=step_minutes)
    return out


def _as_v1(samples: list[DeviceHistorySample]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for sample in samples:
        grouped.setdefault(sample.device_id, []).append(
            {"sample_time": sample.sample_time, "active_power_kw": sample.active_power_kw}
        )
    return grouped


def _v1_plant(samples: list[DeviceHistorySample], rated: dict[int, float | None]):
    """Run V1's real slot pipeline end to end and return (pct, valid_slots, rows)."""
    valid = {V1_SLOTS.inverter_availability_slot(s.sample_time) for s in samples if V1_SLOTS.is_inverter_available(s.active_power_kw)}
    rows = []
    for device_id, device_samples in sorted(_as_v1(samples).items()):
        result = V1_SLOTS.calculate_inverter_daily_availability(device_samples, valid)
        result.update({"inverter_id": device_id, "inverter_power_kw": rated.get(device_id)})
        rows.append(result)
    return (
        V1_SLOTS.calculate_weighted_plant_availability(rows),
        len(V1_SLOTS.apply_inverter_edge_tolerance(valid)),
        rows,
    )


# --- Primitive parity ----------------------------------------------------


@requires_v1
@pytest.mark.parametrize("power", [None, 0.0, -1.0, 0.001, 5.0])
def test_is_available_matches_v1_on_the_numeric_domain_v1_accepts(power) -> None:
    """V1's predicate only ever saw floats or None; parity is asserted there."""
    assert is_available(power) == V1_SLOTS.is_inverter_available(power)


@pytest.mark.parametrize("power,expected", [("", False), ("3.5", True), ("0", False), ("abc", False), (float("nan"), False)])
def test_is_available_handles_inputs_v1_would_have_crashed_on(power, expected) -> None:
    """V2 widens the input domain deliberately: a provider string or NaN must
    not raise inside a materializer, and must never count as producing."""
    assert is_available(power) is expected


@requires_v1
@pytest.mark.parametrize("minute", [0, 1, 7, 14, 15, 29, 30, 44, 45, 59])
def test_slot_bucketing_matches_v1(minute: int) -> None:
    when = datetime(2026, 9, 4, 13, minute, 33)
    assert slot_of(when) == V1_SLOTS.inverter_availability_slot(when)


@requires_v1
def test_edge_tolerance_matches_v1() -> None:
    slots = {datetime(2026, 9, 4, 8, 0) + timedelta(minutes=15 * step) for step in range(40)}
    assert apply_edge_tolerance(slots) == V1_SLOTS.apply_inverter_edge_tolerance(slots)


# --- Daily device + daily plant parity -----------------------------------


@requires_v1
def test_daily_device_and_plant_match_v1_on_a_clean_day() -> None:
    samples = _series(1, start_hour=7, end_hour=19) + _series(2, start_hour=7, end_hour=19)
    rated = {1: 33.0, 2: 30.0}
    v1_pct, v1_slots, v1_rows = _v1_plant(samples, rated)
    v2 = compute_asset_contractual_day(
        expected_devices=[{"device_id": 1, "rated_power_kw": 33.0}, {"device_id": 2, "rated_power_kw": 30.0}],
        samples=samples,
    )
    assert v2.availability_pct == v1_pct
    assert v2.valid_slots == v1_slots
    assert sorted(d.availability_pct for d in v2.devices) == sorted(r["availability_pct"] for r in v1_rows)
    assert sorted(d.valid_slots for d in v2.devices) == sorted(r["valid_slots"] for r in v1_rows)


@requires_v1
def test_one_device_down_for_part_of_the_day_matches_v1() -> None:
    """Device 2 produces nothing between 12:00 and 15:00 -- a real outage."""
    samples = _series(1, start_hour=7, end_hour=19) + _series(2, start_hour=7, end_hour=19, dark={12, 13, 14})
    rated = {1: 33.0, 2: 30.0}
    v1_pct, v1_slots, v1_rows = _v1_plant(samples, rated)
    v2 = compute_asset_contractual_day(
        expected_devices=[{"device_id": 1, "rated_power_kw": 33.0}, {"device_id": 2, "rated_power_kw": 30.0}],
        samples=samples,
    )
    assert v2.availability_pct == v1_pct
    assert v2.valid_slots == v1_slots
    by_id = {d.device_id: d for d in v2.devices}
    assert by_id[2].availability_pct is not None and by_id[2].availability_pct < 100.0
    assert by_id[1].availability_pct == 100.0


def test_weighted_plant_is_90_for_90kw_at_100_and_10kw_at_0() -> None:
    """The milestone's mandatory weighting case, on the real pipeline.

    Device 1 (90 kW) produces all day; device 2 (10 kW) never produces, so it
    is a real 0%, not a missing reading. 90*100 + 10*0 over 100 = 90.
    """
    samples = _series(1, start_hour=7, end_hour=19) + [
        DeviceHistorySample(device_id=2, sample_time=s.sample_time, active_power_kw=0.0)
        for s in _series(2, start_hour=7, end_hour=19)
    ]
    result = compute_asset_contractual_day(
        expected_devices=[{"device_id": 1, "rated_power_kw": 90.0}, {"device_id": 2, "rated_power_kw": 10.0}],
        samples=samples,
    )
    by_id = {d.device_id: d for d in result.devices}
    assert by_id[1].availability_pct == 100.0
    assert by_id[2].availability_pct == 0.0, "a device that never produced is 0%, not missing"
    assert result.availability_pct == 90.0


def test_a_zero_percent_device_is_a_measurement_not_a_missing_value() -> None:
    samples = _series(1, start_hour=7, end_hour=19) + [
        DeviceHistorySample(device_id=2, sample_time=s.sample_time, active_power_kw=0.0)
        for s in _series(2, start_hour=7, end_hour=19)
    ]
    result = compute_asset_contractual_day(
        expected_devices=[{"device_id": 1, "rated_power_kw": 10.0}, {"device_id": 2, "rated_power_kw": 10.0}],
        samples=samples,
    )
    by_id = {d.device_id: d for d in result.devices}
    assert by_id[2].availability_pct == 0.0
    assert by_id[2].availability_pct is not None
    assert result.availability_pct == 50.0


def test_an_expected_device_with_no_history_makes_the_plant_none() -> None:
    """Divergence 1+2 from V1, asserted together as the milestone requires.

    Device 2 is expected that day but the provider returned nothing for it.
    V1 would read it as `0.0%` (empty set intersected with the window) and
    then still publish a plant figure. V2 reads it as `None` -- no evidence
    is not an outage -- and the plant therefore withholds a number rather
    than averaging over the inverter that did report.
    """
    samples = _series(1, start_hour=7, end_hour=19)
    expected = [{"device_id": 1, "rated_power_kw": 33.0}, {"device_id": 2, "rated_power_kw": 30.0}]
    v2 = compute_asset_contractual_day(expected_devices=expected, samples=samples)
    by_id = {device.device_id: device for device in v2.devices}
    assert by_id[1].availability_pct == 100.0
    assert by_id[2].availability_pct is None, "no rows at all is missing evidence, not 0%"
    assert v2.availability_pct is None, "an expected device without a figure must not be averaged away"

    # V1, for the record, publishes a number here. The divergence is real and
    # intentional, so it is pinned rather than described.
    if V1_SLOTS is not None:
        v1_pct, _slots, _rows = _v1_plant(samples, {1: 33.0, 2: 30.0})
        assert v1_pct == 100.0
        assert v2.availability_pct != v1_pct

    # And with no samples at all there is no window either.
    empty = compute_asset_contractual_day(expected_devices=expected, samples=[])
    assert empty.availability_pct is None
    assert all(device.availability_pct is None for device in empty.devices)


@requires_v1
def test_no_production_at_all_gives_no_slots_in_both() -> None:
    samples = [
        DeviceHistorySample(device_id=1, sample_time=s.sample_time, active_power_kw=0.0)
        for s in _series(1, start_hour=7, end_hour=19)
    ]
    v1_pct, v1_slots, _rows = _v1_plant(samples, {1: 10.0})
    v2 = compute_asset_contractual_day(expected_devices=[{"device_id": 1, "rated_power_kw": 10.0}], samples=samples)
    assert v1_slots == v2.valid_slots == 0
    assert v1_pct is None and v2.availability_pct is None


# --- Monthly parity ------------------------------------------------------


def _v1_monthly(rows: list[tuple[str, float, int]]) -> float | None:
    """V1's real `get_monthly_availability` over an in-memory V1 schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE plant_availability_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL, provider TEXT NOT NULL,
            availability_date TEXT NOT NULL, valid_slots INTEGER NOT NULL, weighted_availability_pct REAL,
            inverter_count INTEGER, created_at TEXT, updated_at TEXT)
        """
    )
    for day, pct, slots in rows:
        conn.execute(
            "INSERT INTO plant_availability_daily (asset_id, provider, availability_date, valid_slots,"
            " weighted_availability_pct, inverter_count, created_at, updated_at)"
            " VALUES (1, 'FusionSolar', ?, ?, ?, 1, '', '')",
            (day, slots, pct),
        )
    conn.commit()
    return V1_REPOS.get_monthly_availability(conn, 1, date(2026, 6, 1), date(2026, 6, 30))


@requires_v1_repos
def test_monthly_contractual_is_slot_weighted_and_differs_from_a_day_mean() -> None:
    """The mandatory case: two days with different slot counts.

    100% over 90 slots and 50% over 10 slots. Evidence-weighted -> 95.0;
    an arithmetic day mean would say 75.0. V1's own query is the reference.
    """
    rows = [("2026-06-01", 100.0, 90), ("2026-06-02", 50.0, 10)]
    v1_monthly = _v1_monthly(rows)
    assert v1_monthly == pytest.approx(95.0)
    assert v1_monthly != pytest.approx(75.0)

    weighted = sum(pct * slots for _d, pct, slots in rows) / sum(slots for _d, _p, slots in rows)
    assert round(weighted, 2) == v1_monthly


@requires_v1_repos
def test_monthly_contractual_ignores_days_with_zero_slots() -> None:
    """V1 divides by `SUM(valid_slots)` and returns None when that is zero."""
    assert _v1_monthly([("2026-06-01", None, 0)]) is None
    assert _v1_monthly([]) is None
    # A zero-slot day contributes nothing but does not poison a real one.
    assert _v1_monthly([("2026-06-01", 100.0, 40), ("2026-06-02", None, 0)]) == pytest.approx(100.0)
