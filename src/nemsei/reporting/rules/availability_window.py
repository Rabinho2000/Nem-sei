"""Per-day inverter/plant availability windows, ported from V1's *sampled* engine.

Ported from `monitoring_board/services/sampled_availability.py`'s
`materialize_sampled_availability_day` / `_store_sampled_result` --
deliberately the *sampled* engine, not V1's other, older *slot* engine
(`monitoring_board/reporting/availability.py`'s
`inverter_availability_slot`/`apply_inverter_edge_tolerance`, which feeds
V1's actual Excel/monthly-close numbers today). See
`docs/v2/AVAILABILITY_MIGRATION_PLAN.md` §1 for why: the slot engine needs a
second, separate FusionSolar device-history pull V2 has no evidence for and
no reason to add; the sampled engine already matches the raw data V2 already
collects (`device_status_facts`, structurally the same shape as V1's
`device_realtime_snapshots`) and this task's own vocabulary (gap/tolerance/
missing/partial/complete).

Pure calculation only -- no session, no I/O, exactly like `rules/availability.py`
next to it. Callers assemble `expected_devices`/`samples` from the database
(see `diagnostics/availability_service.py`) and hand them here so this module
stays golden-testable against V1's frozen checkout without a database at all.

Coverage vocabulary is translated to V2's own `QUALITY_STATES`
(`monitoring/models.py`) instead of V1's ad hoc `sampled_complete`/
`sampled_partial` strings, plus one V1 did not need to distinguish from
`missing` at device level but plant level: `indeterminate` means "expected
devices exist, evaluable samples may exist, but no positive-power reading was
ever observed" -- a materially different fact than `missing` ("no expected
configuration at all"), kept as V1 kept it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nemsei.reporting.rules.availability import positive_float, weighted_sampled_availability

LISBON = ZoneInfo("Europe/Lisbon")

# Ported verbatim from V1 -- same names, same values, so a diff against
# `sampled_availability.py` stays legible.
OPERATING_EDGE_MINUTES = 30
MAX_SAMPLE_GAP_MINUTES = 90

COVERAGE_MISSING = "missing"
COVERAGE_INDETERMINATE = "indeterminate"
COVERAGE_PARTIAL = "partial"
COVERAGE_COMPLETE = "complete"
COVERAGE_STATES = (COVERAGE_MISSING, COVERAGE_INDETERMINATE, COVERAGE_PARTIAL, COVERAGE_COMPLETE)

WARNING_MISSING_CONFIGURATION = "missing_expected_inverter_configuration"
WARNING_NO_OPERATING_WINDOW = "no_observed_operating_window"
WARNING_MISSING_INVERTER = "missing_expected_inverter"
WARNING_LATE_FIRST_SAMPLE = "late_first_sample"
WARNING_EARLY_LAST_SAMPLE = "early_last_sample"
WARNING_SAMPLE_GAP = "sample_gap_over_90_minutes"
WARNING_INSUFFICIENT_SAMPLES = "insufficient_sample_count"
WARNING_INCOMPLETE_COVERAGE = "incomplete_inverter_sampling_coverage"


def lisbon_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    """The `[start, end)` UTC-comparable instants for one Lisbon calendar day.

    Ported intent from V1's `collected_at.astimezone(LISBON).date() == target_date`
    filter, expressed as a half-open range a caller can push into a SQL
    `WHERE observed_at >= ? AND observed_at < ?` -- correct across the
    Europe/Lisbon DST transition because the bounds themselves are computed
    in Lisbon wall-clock time and only then carry their own (varying) UTC
    offset; a fixed `timedelta(hours=24)` band anchored on UTC midnight would
    not be.

    A caller pushing these into `observed_at >= start AND observed_at < end`
    gets the correct absolute instants either way. Subtracting the two
    returned values *directly in Python*, however, is not safe: both share
    the identical `ZoneInfo` object, and CPython's aware-datetime
    subtraction takes a fast path that skips per-instant UTC-offset
    resolution whenever both operands' `tzinfo` compare equal -- silently
    returning a naive 24h span even across a DST transition. Convert to a
    fixed-offset zone (e.g. `.astimezone(timezone.utc)`) first if the span
    itself is ever needed.
    """
    start = datetime.combine(target_date, time.min, tzinfo=LISBON)
    end = start + timedelta(days=1)
    return start, end


@dataclass(frozen=True)
class DeviceDaySample:
    """One evaluable reading for one device, already filtered to one asset/day.

    Mirrors what V1's `_snapshots_for_lisbon_day` selected: a parsed instant,
    the availability classification, and the power reading `positive_float`
    checks. Only samples whose `availability_status` is a real observation
    (not a `quality='missing'`/'invalid' row with nothing to evaluate) should
    be passed in -- the caller's job, not this module's, exactly as
    `materialize_sampled_availability_day` pre-filtered before calling in.
    """

    device_id: int
    observed_at: datetime
    active_power_kw: Any
    availability_status: str


@dataclass(frozen=True)
class DeviceDayResult:
    device_id: int
    coverage_status: str
    warning_codes: tuple[str, ...]
    valid_sample_count: int
    availability_pct: float | None


@dataclass(frozen=True)
class AssetDayResult:
    coverage_status: str
    warning_codes: tuple[str, ...]
    availability_pct: float | None
    valid_sample_count: int
    expected_device_count: int
    observed_device_count: int
    operational_window_start: datetime | None
    operational_window_end: datetime | None
    minimum_required_samples: int
    devices: tuple[DeviceDayResult, ...]


def compute_asset_day_availability(
    *,
    expected_devices: list[dict[str, Any]],
    samples: list[DeviceDaySample],
) -> AssetDayResult:
    """One asset's full daily availability, ported 1:1 from
    `materialize_sampled_availability_day`.

    `expected_devices`: one dict per device expected that day, each carrying
    at least `device_id` and `rated_power_kw` -- the temporal-mapping
    equivalent of V1's `expected_devices_for_date` query result. Computing
    that list from the database (`asset_provider_mappings` +
    `devices`, both already temporal in V2) is the caller's job; this
    function never queries anything.

    `samples`: every evaluable `DeviceDaySample` for the asset's Lisbon day,
    already restricted to the expected devices or not -- this function
    itself drops anything outside `expected_devices`, exactly like V1's
    `evaluable` filter did.
    """
    expected_ids = {int(device["device_id"]) for device in expected_devices}
    evaluable = [sample for sample in samples if sample.device_id in expected_ids]

    if not expected_devices:
        return AssetDayResult(
            coverage_status=COVERAGE_MISSING,
            warning_codes=(WARNING_MISSING_CONFIGURATION,),
            availability_pct=None,
            valid_sample_count=0,
            expected_device_count=0,
            observed_device_count=0,
            operational_window_start=None,
            operational_window_end=None,
            minimum_required_samples=0,
            devices=(),
        )

    # Positive production defines the observed operating window. The
    # 30-minute value is a coverage tolerance for each inverter at the two
    # edges; shrinking the window here would silently weaken that rule.
    positive_times = sorted({sample.observed_at for sample in evaluable if positive_float(sample.active_power_kw)})
    observed_device_ids = {sample.device_id for sample in evaluable}
    if not positive_times:
        status = COVERAGE_INDETERMINATE if evaluable else COVERAGE_MISSING
        return AssetDayResult(
            coverage_status=status,
            warning_codes=(WARNING_NO_OPERATING_WINDOW,),
            availability_pct=None,
            valid_sample_count=len(evaluable),
            expected_device_count=len(expected_devices),
            observed_device_count=len(observed_device_ids),
            operational_window_start=None,
            operational_window_end=None,
            minimum_required_samples=0,
            devices=(),
        )

    window_start = positive_times[0]
    window_end = positive_times[-1]
    if window_end <= window_start:
        return AssetDayResult(
            coverage_status=COVERAGE_INDETERMINATE,
            warning_codes=(WARNING_NO_OPERATING_WINDOW,),
            availability_pct=None,
            valid_sample_count=len(evaluable),
            expected_device_count=len(expected_devices),
            observed_device_count=len(observed_device_ids),
            operational_window_start=window_start,
            operational_window_end=window_end,
            minimum_required_samples=0,
            devices=(),
        )

    duration_minutes = (window_end - window_start).total_seconds() / 60
    minimum_samples = max(4, math.ceil(duration_minutes / MAX_SAMPLE_GAP_MINUTES) + 1)

    by_device: dict[int, list[DeviceDaySample]] = {device_id: [] for device_id in expected_ids}
    for sample in evaluable:
        if window_start <= sample.observed_at <= window_end:
            by_device[sample.device_id].append(sample)

    device_results: list[DeviceDayResult] = []
    for device in expected_devices:
        device_id = int(device["device_id"])
        rows = sorted(by_device[device_id], key=lambda item: item.observed_at)
        times = [row.observed_at for row in rows]
        warnings: list[str] = []
        if not rows:
            warnings.append(WARNING_MISSING_INVERTER)
        else:
            if times[0] - window_start > timedelta(minutes=OPERATING_EDGE_MINUTES):
                warnings.append(WARNING_LATE_FIRST_SAMPLE)
            if window_end - times[-1] > timedelta(minutes=OPERATING_EDGE_MINUTES):
                warnings.append(WARNING_EARLY_LAST_SAMPLE)
            if any(current - previous > timedelta(minutes=MAX_SAMPLE_GAP_MINUTES) for previous, current in zip(times, times[1:])):
                warnings.append(WARNING_SAMPLE_GAP)
            if len(rows) < minimum_samples:
                warnings.append(WARNING_INSUFFICIENT_SAMPLES)
        complete = not warnings
        availability_pct = (
            round(sum(row.availability_status == "available" for row in rows) / len(rows) * 100, 2)
            if complete and rows
            else None
        )
        device_results.append(
            DeviceDayResult(
                device_id=device_id,
                coverage_status=COVERAGE_COMPLETE if complete else COVERAGE_PARTIAL,
                warning_codes=tuple(warnings),
                valid_sample_count=len(rows),
                availability_pct=availability_pct,
            )
        )

    complete_asset = all(result.coverage_status == COVERAGE_COMPLETE for result in device_results)
    rated_power_by_device = {int(device["device_id"]): device.get("rated_power_kw") for device in expected_devices}
    weighted_rows = [
        {"availability_pct": result.availability_pct, "rated_power_kw": rated_power_by_device[result.device_id]}
        for result in device_results
    ]
    availability_pct = weighted_sampled_availability(weighted_rows) if complete_asset else None
    observed_count = sum(bool(by_device[int(device["device_id"])]) for device in expected_devices)

    return AssetDayResult(
        coverage_status=COVERAGE_COMPLETE if complete_asset else COVERAGE_PARTIAL,
        warning_codes=() if complete_asset else (WARNING_INCOMPLETE_COVERAGE,),
        availability_pct=availability_pct,
        valid_sample_count=sum(result.valid_sample_count for result in device_results),
        expected_device_count=len(expected_devices),
        observed_device_count=observed_count,
        operational_window_start=window_start,
        operational_window_end=window_end,
        minimum_required_samples=minimum_samples,
        devices=tuple(device_results),
    )


def rollup_availability(rows: list[dict[str, Any]], *, weight_key: str = "rated_power_kw") -> float | None:
    """Weight member availability by `weight_key`, reusing V1's own rule.

    Used one level above `compute_asset_day_availability`'s own output:
    installation-over-assets, or portfolio-over-installations. Deliberately
    the *same* function as inverter-over-plant
    (`weighted_sampled_availability`) rather than a re-derived one -- V1 drew
    this distinction only because its two call sites
    (`calculate_weighted_plant_availability`/`calculate_weighted_portfolio_availability`)
    happened to read differently-named dict keys for the same weight, not
    because the arithmetic differs. Any row with `availability_pct is None`
    (an incomplete member) makes the whole rollup `None` -- an installation
    or portfolio is never reported as available from an average that quietly
    dropped an incomplete member, matching the same conservatism V1 already
    applied one level down.
    """
    normalized = [{"availability_pct": row.get("availability_pct"), "rated_power_kw": row.get(weight_key)} for row in rows]
    return weighted_sampled_availability(normalized)
