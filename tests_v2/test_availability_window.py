"""Quality tests for the ported sampled-availability window engine.

Pure-function tests, no database -- `availability_window.py` takes plain
dataclasses/dicts in and returns a dataclass out. Golden parity against V1's
own `materialize_sampled_availability_day` lives in
`test_availability_golden.py`; this file is the "incomplete data, gaps,
invalid power, timezone" quality coverage item 8 of
`docs/v2/AVAILABILITY_MIGRATION_PLAN.md` asks for.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from nemsei.reporting.rules.availability_window import (
    COVERAGE_COMPLETE,
    COVERAGE_INDETERMINATE,
    COVERAGE_MISSING,
    COVERAGE_PARTIAL,
    LISBON,
    WARNING_EARLY_LAST_SAMPLE,
    WARNING_INCOMPLETE_COVERAGE,
    WARNING_INSUFFICIENT_SAMPLES,
    WARNING_LATE_FIRST_SAMPLE,
    WARNING_MISSING_CONFIGURATION,
    WARNING_MISSING_INVERTER,
    WARNING_NO_OPERATING_WINDOW,
    WARNING_SAMPLE_GAP,
    DeviceDaySample,
    compute_asset_day_availability,
    lisbon_day_bounds,
    rollup_availability,
)


def _at(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=LISBON)


def _sample(device_id: int, when: datetime, *, power: float | None = 5.0, status: str = "available") -> DeviceDaySample:
    return DeviceDaySample(device_id=device_id, observed_at=when, active_power_kw=power, availability_status=status)


DEVICES_2 = [
    {"device_id": 1, "rated_power_kw": 20.0},
    {"device_id": 2, "rated_power_kw": 20.0},
]


def _clean_day_samples(device_ids: list[int], *, start_hour: int = 6, end_hour: int = 20, step_minutes: int = 30) -> list[DeviceDaySample]:
    samples = []
    hour, minute = start_hour, 0
    while hour < end_hour or (hour == end_hour and minute == 0):
        when = _at(hour, minute)
        for device_id in device_ids:
            samples.append(_sample(device_id, when))
        minute += step_minutes
        if minute >= 60:
            hour += minute // 60
            minute %= 60
    return samples


def test_no_expected_devices_is_missing_not_indeterminate() -> None:
    result = compute_asset_day_availability(expected_devices=[], samples=[])
    assert result.coverage_status == COVERAGE_MISSING
    assert result.warning_codes == (WARNING_MISSING_CONFIGURATION,)
    assert result.availability_pct is None


def test_expected_devices_but_zero_samples_is_missing() -> None:
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=[])
    assert result.coverage_status == COVERAGE_MISSING
    assert result.warning_codes == (WARNING_NO_OPERATING_WINDOW,)


def test_samples_exist_but_never_positive_power_is_indeterminate() -> None:
    samples = [_sample(1, _at(10), power=0.0), _sample(2, _at(10), power=None)]
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    assert result.coverage_status == COVERAGE_INDETERMINATE
    assert result.warning_codes == (WARNING_NO_OPERATING_WINDOW,)


def test_clean_full_day_is_complete_with_full_availability() -> None:
    samples = _clean_day_samples([1, 2])
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    assert result.coverage_status == COVERAGE_COMPLETE
    assert result.availability_pct == 100.0
    assert result.expected_device_count == 2
    assert result.observed_device_count == 2
    assert all(device.coverage_status == COVERAGE_COMPLETE for device in result.devices)
    assert result.operational_window_start == _at(6)
    assert result.operational_window_end == _at(20)


def test_gap_over_90_minutes_makes_device_and_asset_partial() -> None:
    samples = _clean_day_samples([2])  # device 2: fully clean
    # device 1: clean, except a 2-hour hole around midday
    hole_start, hole_end = _at(11, 0), _at(13, 0)
    samples += [s for s in _clean_day_samples([1]) if not (hole_start < s.observed_at < hole_end)]
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    assert result.coverage_status == COVERAGE_PARTIAL
    assert result.warning_codes == (WARNING_INCOMPLETE_COVERAGE,)
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert WARNING_SAMPLE_GAP in device_1.warning_codes
    assert device_1.coverage_status == COVERAGE_PARTIAL
    device_2 = next(d for d in result.devices if d.device_id == 2)
    assert device_2.coverage_status == COVERAGE_COMPLETE
    # A partial device carries no availability_pct, and neither does the asset.
    assert device_1.availability_pct is None
    assert result.availability_pct is None


def test_late_first_sample_flags_the_device() -> None:
    samples = _clean_day_samples([2])
    # device 1 misses its first 40 minutes relative to the window start (06:00)
    samples += [s for s in _clean_day_samples([1]) if s.observed_at >= _at(6, 40)]
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert WARNING_LATE_FIRST_SAMPLE in device_1.warning_codes


def test_early_last_sample_flags_the_device() -> None:
    samples = _clean_day_samples([2])
    samples += [s for s in _clean_day_samples([1]) if s.observed_at <= _at(19, 0)]
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert WARNING_EARLY_LAST_SAMPLE in device_1.warning_codes


def test_insufficient_sample_count_below_the_computed_minimum() -> None:
    # A short 20-minute window: minimum_required_samples = max(4, ceil(20/90)+1) = 4.
    # Both devices sample only at the two edges -- 2 samples each, no gap >90min,
    # no late/early edge violation, but still below the minimum of 4.
    samples = [
        _sample(1, _at(10, 0)), _sample(1, _at(10, 20)),
        _sample(2, _at(10, 0)), _sample(2, _at(10, 20)),
    ]
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert result.minimum_required_samples == 4
    assert WARNING_INSUFFICIENT_SAMPLES in device_1.warning_codes
    assert WARNING_SAMPLE_GAP not in device_1.warning_codes
    assert WARNING_LATE_FIRST_SAMPLE not in device_1.warning_codes


def test_one_missing_device_keeps_the_other_evaluated_not_dropped() -> None:
    samples = _clean_day_samples([2])  # device 1 never reports at all
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    assert result.expected_device_count == 2
    assert result.observed_device_count == 1
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert device_1.warning_codes == (WARNING_MISSING_INVERTER,)
    assert device_1.valid_sample_count == 0
    device_2 = next(d for d in result.devices if d.device_id == 2)
    assert device_2.coverage_status == COVERAGE_COMPLETE
    assert result.coverage_status == COVERAGE_PARTIAL
    assert result.availability_pct is None


@pytest.mark.parametrize("power", [None, 0, 0.0, -1, -0.001, "not-a-number", "", float("nan")])
def test_invalid_or_nonpositive_power_never_counts_as_positive(power: object) -> None:
    samples = [_sample(1, _at(10), power=power, status="unavailable"), _sample(2, _at(10), power=5.0)]
    # Should not raise, and device 1's reading must not open an operating window by itself.
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=samples)
    assert result.coverage_status in (COVERAGE_INDETERMINATE, COVERAGE_PARTIAL, COVERAGE_MISSING)


def test_unavailable_status_lowers_availability_pct_within_a_complete_day() -> None:
    samples = _clean_day_samples([1, 2])
    # Flip half of device 1's readings to unavailable -- still complete coverage,
    # but availability_pct for device 1 (and thus the weighted plant figure) drops.
    flipped = []
    for sample in samples:
        if sample.device_id == 1 and sample.observed_at.hour < 13:
            flipped.append(DeviceDaySample(sample.device_id, sample.observed_at, sample.active_power_kw, "unavailable"))
        else:
            flipped.append(sample)
    result = compute_asset_day_availability(expected_devices=DEVICES_2, samples=flipped)
    assert result.coverage_status == COVERAGE_COMPLETE
    device_1 = next(d for d in result.devices if d.device_id == 1)
    assert device_1.availability_pct is not None and device_1.availability_pct < 100.0
    assert result.availability_pct is not None and result.availability_pct < 100.0


def _absolute_span(start: datetime, end: datetime) -> timedelta:
    # `end - start` directly is NOT safe here: both share the identical
    # `ZoneInfo("Europe/Lisbon")` object (see `lisbon_day_bounds`'s own
    # docstring), and CPython's aware-datetime subtraction takes a fast path
    # that skips per-instant UTC-offset resolution whenever both operands'
    # `tzinfo` compare equal -- silently returning a naive 24h span across a
    # DST transition instead of the real 23h/25h one. Converting to a
    # fixed-offset zone first (`timezone.utc`, a different, non-equal tzinfo
    # object) forces the correct per-instant offset resolution. A database
    # comparing `observed_at` against these bounds independently is not
    # affected by this -- only subtracting the two bounds from each other in
    # Python is.
    return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


def test_lisbon_day_bounds_are_23_hours_on_the_spring_forward_transition() -> None:
    # Portugal 2026: clocks jump 01:00 -> 02:00 on the last Sunday of March (29th).
    start, end = lisbon_day_bounds(date(2026, 3, 29))
    assert _absolute_span(start, end) == timedelta(hours=23)


def test_lisbon_day_bounds_are_25_hours_on_the_fall_back_transition() -> None:
    # Portugal 2026: clocks fall back 02:00 -> 01:00 on the last Sunday of October (25th).
    start, end = lisbon_day_bounds(date(2026, 10, 25))
    assert _absolute_span(start, end) == timedelta(hours=25)


def test_lisbon_day_bounds_is_24_hours_on_an_ordinary_day() -> None:
    start, end = lisbon_day_bounds(date(2026, 7, 15))
    assert _absolute_span(start, end) == timedelta(hours=24)


def test_rollup_none_when_any_member_incomplete() -> None:
    rows = [
        {"availability_pct": 100.0, "installed_dc_power_kw": 500.0},
        {"availability_pct": None, "installed_dc_power_kw": 300.0},
    ]
    assert rollup_availability(rows, weight_key="installed_dc_power_kw") is None


def test_rollup_weights_by_the_given_key() -> None:
    rows = [
        {"availability_pct": 100.0, "installed_dc_power_kw": 90.0},
        {"availability_pct": 0.0, "installed_dc_power_kw": 10.0},
    ]
    assert rollup_availability(rows, weight_key="installed_dc_power_kw") == 90.0
