"""Materializes device/asset daily availability from already-persisted facts.

Ported from V1's `materialize_sampled_availability_day` /
`materialize_existing_sampled_availability`
(`monitoring_board/services/sampled_availability.py`), adapted to read
`device_status_facts` instead of SQLite's `device_realtime_snapshots`. See
`docs/v2/AVAILABILITY_MIGRATION_PLAN.md`.

**This module makes zero provider API calls.** It only reads what
`FusionSolarDeviceStatusService` already wrote for other reasons (current
monitoring, diagnostics) and (re)writes `DeviceAvailabilityDaily`/
`AssetAvailabilityDaily` idempotently. Nothing here can trigger a
FusionSolar request, by construction: no client, no credentials, no
provider import anywhere in this file.

Scoped to FusionSolar only, structurally: `expected_devices_for_date` joins
through `ProviderConnection.provider_code`. Sigenergy assets simply never
have a matching row and materialize as `coverage_status='missing'` --
exactly the same "not silently a zero" outcome every other missing-contract
case in this codebase already produces, not a special-cased skip (item 9,
`docs/v2/AVAILABILITY_MIGRATION_PLAN.md` §8).

A real, open caveat, not silently assumed away: `device_status_facts` only
gains a new row when a poll's *value* actually changed
(`record_device_status`'s `deduplicate_observed_at`, since FusionSolar's
`collectTime` is always absent for this account and freshness is always
`"unknown"` -- `DEVICE_TELEMETRY.md` §1.3/§5). A device whose power is
genuinely fluctuating (real daylight production) gets a new row on nearly
every poll; a device stuck reporting one *unchanging* nonzero value for a
long stretch would not, and could be misread as a `sample_gap_over_90_minutes`
gap that never happened. This is exactly the kind of thing the read-only
compare run against real data (item 7) is for -- not solved by this module,
flagged for that comparison to actually observe.
"""
from __future__ import annotations

import logging

from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device
from nemsei.diagnostics.models import AssetAvailabilityDaily, DeviceAvailabilityDaily, DeviceStatusFact
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCode
from nemsei.reporting.rules.availability_slots import (
    ContractualAssetDay,
    DeviceHistorySample as HistorySample,
    EDGE_TOLERANCE_MINUTES,
    SLOT_MINUTES,
    compute_asset_contractual_day,
)
from nemsei.reporting.rules.availability_source import (
    KIND_CONTRACTUAL,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_FUSIONSOLAR_SAMPLED,
    select_availability,
    source_kind,
)
from nemsei.reporting.rules.availability_window import (
    AssetDayResult,
    COVERAGE_COMPLETE,
    COVERAGE_INDETERMINATE,
    COVERAGE_MISSING,
    LISBON,
    DeviceDaySample,
    compute_asset_day_availability,
    lisbon_day_bounds,
    rollup_availability,
)
from nemsei.shared.clock import utc_now

_LOGGER = logging.getLogger(__name__)

# Ported from V1's `evaluable` filter in `materialize_sampled_availability_day`
# (`in {"available", "unavailable", "no_communication"}`). V2's vocabulary
# (`diagnostics/models.py::AVAILABILITY_STATES`) has no `no_communication`
# state -- `unknown` is its structural equivalent (a poll that could not
# classify the device at all) and is kept out for the same reason V1 kept
# `no_communication` in: it is evidence a poll happened, not evidence of
# operating state, but unlike `available`/`unavailable`/`standby` it says
# nothing usable about whether the device was on -- V1 treated
# `no_communication` as evaluable only because it could still anchor a gap
# check, which `unknown` here cannot honestly do without a resolved
# provider timestamp. Kept deliberately narrower than V1 rather than
# guessed wider.
_EVALUABLE_STATUSES = frozenset({"available", "standby", "unavailable"})


def expected_devices_for_date(session: Session, *, asset_id: int, target_date: date) -> list[dict[str, Any]]:
    """FusionSolar inverters mapped to this asset and valid on this date.

    Reads `asset_provider_mappings` (already temporal: `valid_from`/
    `valid_to`/`mapping_status`) joined to `devices`
    (`device_kind='inverter'`, itself temporal) -- V2's native equivalent of
    V1's bespoke `provider_device_configuration_history` seeding. No new
    table, no new seeding step.
    """
    statement = (
        select(Device.id, Device.rated_power_kw)
        .join(AssetProviderMapping, AssetProviderMapping.device_id == Device.id)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(
            AssetProviderMapping.asset_id == asset_id,
            AssetProviderMapping.resource_kind == "device",
            AssetProviderMapping.mapping_status == "active",
            AssetProviderMapping.valid_from <= target_date,
            (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= target_date),
            ProviderConnection.provider_code == ProviderCode.FUSIONSOLAR.value,
            Device.device_kind == "inverter",
            Device.valid_from <= target_date,
            (Device.valid_to.is_(None)) | (Device.valid_to >= target_date),
        )
        .distinct()
    )
    return [{"device_id": row.id, "rated_power_kw": row.rated_power_kw} for row in session.execute(statement).all()]


def _samples_for_asset_day(session: Session, *, asset_id: int, target_date: date) -> list[DeviceDaySample]:
    start, end = lisbon_day_bounds(target_date)
    statement = (
        select(
            DeviceStatusFact.device_id,
            DeviceStatusFact.observed_at,
            DeviceStatusFact.active_power_kw,
            DeviceStatusFact.availability_status,
        )
        .where(
            DeviceStatusFact.asset_id == asset_id,
            DeviceStatusFact.source_kind == "live_read",
            DeviceStatusFact.availability_status.in_(_EVALUABLE_STATUSES),
            DeviceStatusFact.observed_at >= start,
            DeviceStatusFact.observed_at < end,
        )
        .order_by(DeviceStatusFact.observed_at)
    )
    return [
        DeviceDaySample(
            device_id=row.device_id,
            observed_at=row.observed_at,
            active_power_kw=row.active_power_kw,
            availability_status=row.availability_status,
        )
        for row in session.execute(statement).all()
    ]


def materialize_asset_availability_day(
    session: Session, *, asset_id: int, target_date: date, now: datetime | None = None
) -> AssetDayResult:
    """Compute and persist one asset's availability for one day.

    Pure read of `device_status_facts` plus `asset_provider_mappings`/
    `devices`; no provider call. Idempotent: re-running for the same
    `(asset_id, target_date)` deletes and reinserts, exactly like V1's own
    `_store_sampled_result` -- safe to call again after a late-arriving
    correction to the underlying facts.
    """
    calculated_at = now or utc_now()
    expected = expected_devices_for_date(session, asset_id=asset_id, target_date=target_date)
    samples = _samples_for_asset_day(session, asset_id=asset_id, target_date=target_date)
    result = compute_asset_day_availability(expected_devices=expected, samples=samples)
    _store_result(session, asset_id=asset_id, target_date=target_date, result=result, calculated_at=calculated_at)
    return result


def _store_result(
    session: Session, *, asset_id: int, target_date: date, result: AssetDayResult, calculated_at: datetime
) -> None:
    # Scoped to this source. An unscoped delete would make re-materializing
    # the sampled engine silently destroy a contractual row for the same day
    # -- the exact substitution the source split exists to prevent.
    session.query(DeviceAvailabilityDaily).filter_by(
        asset_id=asset_id, availability_date=target_date, source=SOURCE_FUSIONSOLAR_SAMPLED
    ).delete()
    session.query(AssetAvailabilityDaily).filter_by(
        asset_id=asset_id, availability_date=target_date, source=SOURCE_FUSIONSOLAR_SAMPLED
    ).delete()

    for device in result.devices:
        session.add(
            DeviceAvailabilityDaily(
                device_id=device.device_id,
                asset_id=asset_id,
                availability_date=target_date,
                availability_pct=device.availability_pct,
                valid_sample_count=device.valid_sample_count,
                minimum_required_samples=result.minimum_required_samples,
                coverage_status=device.coverage_status,
                warning_codes_json=list(device.warning_codes),
                operational_window_start=result.operational_window_start,
                operational_window_end=result.operational_window_end,
                source=SOURCE_FUSIONSOLAR_SAMPLED,
                source_kind=source_kind(SOURCE_FUSIONSOLAR_SAMPLED),
                calculated_at=calculated_at,
                created_at=calculated_at,
                updated_at=calculated_at,
            )
        )

    session.add(
        AssetAvailabilityDaily(
            asset_id=asset_id,
            availability_date=target_date,
            availability_pct=result.availability_pct,
            valid_sample_count=result.valid_sample_count,
            expected_device_count=result.expected_device_count,
            observed_device_count=result.observed_device_count,
            minimum_required_samples=result.minimum_required_samples,
            coverage_status=result.coverage_status,
            warning_codes_json=list(result.warning_codes),
            operational_window_start=result.operational_window_start,
            operational_window_end=result.operational_window_end,
            source=SOURCE_FUSIONSOLAR_SAMPLED,
            source_kind=source_kind(SOURCE_FUSIONSOLAR_SAMPLED),
            calculation_details_json={
                "device_count": len(result.devices),
                "devices": [
                    {
                        "device_id": device.device_id,
                        "coverage_status": device.coverage_status,
                        "warning_codes": list(device.warning_codes),
                        "valid_sample_count": device.valid_sample_count,
                    }
                    for device in result.devices
                ],
            },
            calculated_at=calculated_at,
            created_at=calculated_at,
            updated_at=calculated_at,
        )
    )
    session.flush()


def materialize_existing_availability(
    session: Session, *, asset_id: int, from_date: date, to_date: date, now: datetime | None = None
) -> dict[str, int]:
    """Backfill/repair over a date range, reading only already-stored facts.

    Ported from V1's `materialize_existing_sampled_availability`: zero
    provider calls, safe to re-run, one day at a time. The caller (a
    script, never a scheduled job in this milestone -- see the migration
    plan §3/§7) decides the range; this function does not discover it from
    what happens to already exist in `device_status_facts`, unlike V1's
    version, because V2's facts are not (yet) scoped to a single provider
    per row the way V1's `device_realtime_snapshots` was -- an explicit
    range keeps this function honest about what it actually recomputed.
    """
    calculated_at = now or utc_now()
    states: dict[str, int] = {}
    current = from_date
    days = 0
    while current <= to_date:
        result = materialize_asset_availability_day(session, asset_id=asset_id, target_date=current, now=calculated_at)
        states[result.coverage_status] = states.get(result.coverage_status, 0) + 1
        days += 1
        current = date.fromordinal(current.toordinal() + 1)
    return {"days_recalculated": days, **states}


def installation_availability_for_date(session: Session, *, installation_id: int, target_date: date) -> float | None:
    """Item 5: roll up an installation's already-materialized member assets.

    Reads `asset_availability_daily` only -- never recomputes, never touches
    `device_status_facts` directly. An installation with zero materialized
    assets for the day (nothing computed yet, or none of its assets are
    FusionSolar-mapped) has nothing to weight and returns `None`, the same
    "absent, not zero" answer an incomplete member produces.

    Genuinely new policy, not a V1 port: V1 had no multi-asset
    installations. One incomplete member asset makes the whole rollup
    `None` (`rollup_availability`'s existing rule) -- confirmed as the
    intended conservatism, see `docs/v2/AVAILABILITY_MIGRATION_PLAN.md` §5.
    """
    asset_ids = list(session.scalars(select(Asset.id).where(Asset.installation_id == installation_id)).all())
    if not asset_ids:
        return None
    rows = session.execute(
        select(AssetAvailabilityDaily.asset_id, AssetAvailabilityDaily.availability_pct, Asset.installed_dc_power_kw)
        .join(Asset, Asset.id == AssetAvailabilityDaily.asset_id)
        .where(AssetAvailabilityDaily.asset_id.in_(asset_ids), AssetAvailabilityDaily.availability_date == target_date)
    ).all()
    if len(rows) != len(asset_ids):
        # Not every member asset has a materialized day yet -- an honest
        # "not computed" is not the same claim as "computed and incomplete",
        # but neither can report a real percentage, so both return None.
        return None
    member_rows = [
        {"availability_pct": (float(pct) if pct is not None else None), "installed_dc_power_kw": (float(power) if power is not None else None)}
        for _asset_id, pct, power in rows
    ]
    return rollup_availability(member_rows, weight_key="installed_dc_power_kw")


def portfolio_availability_for_date(session: Session, *, portfolio_id: int, target_date: date) -> float | None:
    """Item 5: roll up a portfolio's temporal membership for one day.

    Membership is resolved the same way every other portfolio view resolves
    it (`portfolios.service.resolve_members`) -- no separate notion of
    "which assets count" invented here.
    """
    from nemsei.portfolios.service import resolve_members  # local import: avoids a package-boundary cycle at import time

    members = resolve_members(session, portfolio_id=portfolio_id, on=target_date)
    asset_ids = [member.asset_id for member in members if member.asset_id is not None]
    if not asset_ids:
        return None
    rows = session.execute(
        select(AssetAvailabilityDaily.asset_id, AssetAvailabilityDaily.availability_pct, Asset.installed_dc_power_kw)
        .join(Asset, Asset.id == AssetAvailabilityDaily.asset_id)
        .where(AssetAvailabilityDaily.asset_id.in_(asset_ids), AssetAvailabilityDaily.availability_date == target_date)
    ).all()
    if len(rows) != len(asset_ids):
        return None
    member_rows = [
        {"availability_pct": (float(pct) if pct is not None else None), "installed_dc_power_kw": (float(power) if power is not None else None)}
        for _asset_id, pct, power in rows
    ]
    return rollup_availability(member_rows, weight_key="installed_dc_power_kw")


# Monthly/coverage vocabulary for `monthly_availability_for_asset`, kept
# distinct from `COVERAGE_STATES` (`reporting/rules/availability_window.py`
# only knows `complete`/`partial`/`missing`/`indeterminate` for one *day*).
# A month is honestly a three-state question -- more precise than V1's own
# `sampled_month_quality`, which only ever answered `sampled_complete` or
# `sampled_partial`, even for a month with literally zero materialized days
# (see this function's own docstring for why that quirk is not ported).
MONTHLY_COVERAGE_STATES = ("complete", "partial", "missing")


def _monthly_figure_for_source(rows: list[AssetAvailabilityDaily], *, expected_days: int) -> dict[str, Any]:
    """One month's figure for one source, using that source's own V1 semantics.

    V1 aggregated its two engines to a month **differently**, and the
    difference is not cosmetic -- so this does not pick one and apply it to
    both:

    - *Operational* (V1's `sampled_month_quality`): plain arithmetic mean over
      the days, reported only when every expected day exists and is
      `complete`. Each day already weighted its own inverters; V1 weighted the
      days themselves equally.
    - *Contractual* (V1's `get_monthly_availability`, the query that actually
      fed V1's reports and monthly close):
      `SUM(availability_pct * valid_slots) / SUM(valid_slots)` -- weighted by
      how much evidence each day carried, so a day with two usable readings
      does not count as much as a full one. `valid_sample_count` is V2's
      column for V1's `valid_slots`.

    Using the arithmetic mean for a contractual source would silently change a
    commercial number, which is why the branch exists before any contractual
    source does.
    """
    final = len(rows) == expected_days and bool(rows) and all(row.coverage_status == COVERAGE_COMPLETE for row in rows)
    if final:
        coverage_status = "complete"
    elif rows:
        coverage_status = "partial"
    else:
        coverage_status = "missing"
    source = rows[0].source if rows else None
    kind = rows[0].source_kind if rows else None
    availability_pct: float | None = None
    if final:
        if kind == KIND_CONTRACTUAL:
            weighted = sum(float(row.availability_pct) * row.valid_sample_count for row in rows if row.availability_pct is not None)
            weight = sum(row.valid_sample_count for row in rows if row.availability_pct is not None)
            availability_pct = round(weighted / weight, 2) if weight else None
        else:
            values = [float(row.availability_pct) for row in rows if row.availability_pct is not None]
            availability_pct = round(sum(values) / len(values), 2) if values else None
    return {
        "coverage_status": coverage_status,
        "availability_pct": availability_pct,
        "covered_days": len(rows),
        "expected_days": expected_days,
        "warnings": sorted({code for row in rows for code in (row.warning_codes_json or [])}),
        "source": source,
        "source_kind": kind,
    }


def monthly_availability_for_asset(
    session: Session, *, asset_id: int, month_start: date, month_end_exclusive: date
) -> dict[str, Any]:
    """One asset's availability for one calendar month, ported from V1's
    `sampled_month_quality` (`services/sampled_availability.py`).

    Reads only already-materialized `asset_availability_daily` rows -- never
    recomputes a day, never touches `device_status_facts` directly. `final`
    (V1's own name for the gate) requires every expected day of the month to
    exist *and* be `complete`; the average is only reported when that gate
    passes, exactly like V1's `availability_pct: round(...) if final and
    values else None`.

    Deliberate improvement over V1, not a silent behavior change: V1's
    `sampled_month_quality` returns `coverage_status='sampled_partial'` even
    for a month with **zero** materialized rows (`len(rows) == expected_days`
    is `0 == N`, always false, so `final` is always false and the `else`
    branch is always `"sampled_partial"` -- there is no third V1 state to
    fall back to). `MONTHLY_COVERAGE_STATES` adds `'missing'` for exactly
    that zero-row case, matching this codebase's own `VALUE_STATES`
    vocabulary (`reporting/models.py`) and this milestone's own item 3
    requirement to distinguish `missing` from `partial`.
    """
    rows = session.execute(
        select(AssetAvailabilityDaily).where(
            AssetAvailabilityDaily.asset_id == asset_id,
            AssetAvailabilityDaily.availability_date >= month_start,
            AssetAvailabilityDaily.availability_date < month_end_exclusive,
        )
    ).scalars().all()
    expected_days = (month_end_exclusive - month_start).days

    # One month's figure per source present, then the selection policy picks
    # which may be reported (`availability_source.select_availability`:
    # contractual over operational, never by recency). With only
    # `fusionsolar_sampled` materializable today this reduces to exactly the
    # previous single-source behavior -- proven by the unchanged tests -- but
    # the shape is what stops a future contractual source from having to be
    # merged in by a caller that might get the precedence wrong.
    by_source: dict[str, list[AssetAvailabilityDaily]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row)
    candidates = [_monthly_figure_for_source(source_rows, expected_days=expected_days) for source_rows in by_source.values()]

    chosen = select_availability(candidates)
    if chosen is not None:
        return chosen
    if candidates:
        # Nothing eligible to report, but there is still coverage evidence.
        # Prefer the contractual source's own account of why it is absent, so
        # the report explains the number it would have used.
        return min(candidates, key=lambda item: (0 if item["source_kind"] == KIND_CONTRACTUAL else 1, str(item["source"])))
    return {
        "coverage_status": "missing",
        "availability_pct": None,
        "covered_days": 0,
        "expected_days": expected_days,
        "warnings": [],
        "source": None,
        "source_kind": None,
    }


def _expected_devices_by_asset_day(
    session: Session, *, asset_ids: list[int], from_date: date, to_date: date
) -> dict[tuple[int, date], list[dict[str, Any]]]:
    """Expected inverters for every (asset, day) in the window, in one query.

    The temporal predicate `expected_devices_for_date` applies per date is
    applied here in memory instead, over rows whose validity *overlaps the
    window at all*. Same rule, same result -- a device is expected on a day
    iff its mapping and its own device record are both valid that day -- but
    one query for the whole fleet-window instead of one per asset per day.

    That difference is the whole point: the scheduled job covers every
    FusionSolar-mapped asset over a multi-day lookback, so a per-day query
    would be hundreds of round trips per tick.
    """
    if not asset_ids:
        return {}
    statement = (
        select(
            AssetProviderMapping.asset_id,
            Device.id,
            Device.rated_power_kw,
            AssetProviderMapping.valid_from,
            AssetProviderMapping.valid_to,
            Device.valid_from,
            Device.valid_to,
        )
        .join(AssetProviderMapping, AssetProviderMapping.device_id == Device.id)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(
            AssetProviderMapping.asset_id.in_(asset_ids),
            AssetProviderMapping.resource_kind == "device",
            AssetProviderMapping.mapping_status == "active",
            AssetProviderMapping.valid_from <= to_date,
            (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= from_date),
            ProviderConnection.provider_code == ProviderCode.FUSIONSOLAR.value,
            Device.device_kind == "inverter",
            Device.valid_from <= to_date,
            (Device.valid_to.is_(None)) | (Device.valid_to >= from_date),
        )
        .distinct()
    )
    expected: dict[tuple[int, date], list[dict[str, Any]]] = {}
    rows = session.execute(statement).all()
    for asset_id, device_id, rated_power_kw, map_from, map_to, dev_from, dev_to in rows:
        current = from_date
        while current <= to_date:
            if map_from <= current and (map_to is None or map_to >= current) and dev_from <= current and (dev_to is None or dev_to >= current):
                expected.setdefault((asset_id, current), []).append(
                    {"device_id": device_id, "rated_power_kw": rated_power_kw}
                )
            current = date.fromordinal(current.toordinal() + 1)
    return expected


def _samples_by_asset_day(
    session: Session, *, asset_ids: list[int], from_date: date, to_date: date
) -> dict[tuple[int, date], list[DeviceDaySample]]:
    """Every evaluable sample for the window, in one query, bucketed by Lisbon day.

    Bucketing happens here rather than in SQL so the day boundary stays the
    one `lisbon_day_bounds` defines (DST-correct wall-clock), not a database
    timezone setting this code does not control.
    """
    if not asset_ids:
        return {}
    window_start, _ = lisbon_day_bounds(from_date)
    _, window_end = lisbon_day_bounds(to_date)
    statement = (
        select(
            DeviceStatusFact.asset_id,
            DeviceStatusFact.device_id,
            DeviceStatusFact.observed_at,
            DeviceStatusFact.active_power_kw,
            DeviceStatusFact.availability_status,
        )
        .where(
            DeviceStatusFact.asset_id.in_(asset_ids),
            DeviceStatusFact.source_kind == "live_read",
            DeviceStatusFact.availability_status.in_(_EVALUABLE_STATUSES),
            DeviceStatusFact.observed_at >= window_start,
            DeviceStatusFact.observed_at < window_end,
        )
        .order_by(DeviceStatusFact.observed_at)
    )
    buckets: dict[tuple[int, date], list[DeviceDaySample]] = {}
    for asset_id, device_id, observed_at, power, status in session.execute(statement).all():
        day = observed_at.astimezone(LISBON).date()
        buckets.setdefault((asset_id, day), []).append(
            DeviceDaySample(device_id=device_id, observed_at=observed_at, active_power_kw=power, availability_status=status)
        )
    return buckets


def fusionsolar_mapped_asset_ids(session: Session, *, on_or_after: date) -> list[int]:
    """Assets with at least one active FusionSolar device mapping in scope.

    The scheduled job's unit of work. Deliberately not "every asset": an
    asset with no device mapping can only ever materialize as `missing`, and
    writing a `missing` row for it every hour would be churn, not evidence.
    """
    return list(
        session.scalars(
            select(AssetProviderMapping.asset_id)
            .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
            .where(
                AssetProviderMapping.resource_kind == "device",
                AssetProviderMapping.mapping_status == "active",
                (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= on_or_after),
                ProviderConnection.provider_code == ProviderCode.FUSIONSOLAR.value,
            )
            .distinct()
        ).all()
    )


def materialize_availability_window(
    session: Session,
    *,
    from_date: date,
    to_date: date,
    asset_ids: list[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute a whole (assets x days) window with a bounded number of queries.

    Zero provider calls, like everything else in this module -- it reads
    `device_status_facts` and rewrites the two derived tables. Idempotent per
    `(asset, day)` (delete+insert), so re-running a window that has not
    changed produces the same rows and no duplicates.

    Query cost is independent of the number of asset-days: two batched reads
    for the whole window (expected devices, samples) plus the per-key writes.
    """
    calculated_at = now or utc_now()
    targets = asset_ids if asset_ids is not None else fusionsolar_mapped_asset_ids(session, on_or_after=from_date)
    if not targets:
        return {"assets": 0, "days_recalculated": 0}
    expected_map = _expected_devices_by_asset_day(session, asset_ids=targets, from_date=from_date, to_date=to_date)
    sample_map = _samples_by_asset_day(session, asset_ids=targets, from_date=from_date, to_date=to_date)

    states: dict[str, int] = {}
    days = 0
    for asset_id in sorted(targets):
        current = from_date
        while current <= to_date:
            result = compute_asset_day_availability(
                expected_devices=expected_map.get((asset_id, current), []),
                samples=sample_map.get((asset_id, current), []),
            )
            _store_result(session, asset_id=asset_id, target_date=current, result=result, calculated_at=calculated_at)
            states[result.coverage_status] = states.get(result.coverage_status, 0) + 1
            days += 1
            _LOGGER.info(
                "availability_materialized asset_id=%s date=%s source=%s availability=%s quality=%s "
                "expected_devices=%s observed_devices=%s",
                asset_id,
                current.isoformat(),
                SOURCE_FUSIONSOLAR_SAMPLED,
                result.availability_pct,
                result.coverage_status,
                result.expected_device_count,
                result.observed_device_count,
            )
            if result.availability_pct is None:
                _LOGGER.info(
                    "availability_indeterminate asset_id=%s date=%s reason=%s",
                    asset_id,
                    current.isoformat(),
                    ",".join(result.warning_codes) or "none",
                )
            current = date.fromordinal(current.toordinal() + 1)
    return {"assets": len(targets), "days_recalculated": days, **states}


def days_with_facts_but_no_availability(
    session: Session, *, before: date, asset_ids: list[int] | None = None
) -> list[tuple[int, date]]:
    """(asset, day) pairs holding raw device facts that were never materialized.

    The retention precondition. `device_status_facts` is the only evidence
    daily availability is computed from, so purging a day's raw rows before
    its `asset_availability_daily` row exists destroys the ability to ever
    produce that day's figure -- not just the payload, the fact itself.

    There is **no purge of `device_status_facts` in this codebase today**
    (only `huawei_scada_power_samples` has retention, `jobs/handlers.py`),
    so this function guards nothing yet by itself. It exists so that whoever
    adds that purge has the check ready and does not have to rediscover the
    ordering requirement: materialize first, delete second, and only delete
    days this returns nothing for.

    Returns the offending pairs rather than a bool so a caller can report
    exactly which days would have been lost.
    """
    fact_days = select(
        DeviceStatusFact.asset_id.label("asset_id"),
        func.date(func.timezone("Europe/Lisbon", DeviceStatusFact.observed_at)).label("day"),
    ).where(DeviceStatusFact.source_kind == "live_read", DeviceStatusFact.observed_at < lisbon_day_bounds(before)[0])
    if asset_ids:
        fact_days = fact_days.where(DeviceStatusFact.asset_id.in_(asset_ids))
    fact_days = fact_days.distinct().subquery()

    materialized = select(AssetAvailabilityDaily.asset_id, AssetAvailabilityDaily.availability_date).subquery()
    statement = (
        select(fact_days.c.asset_id, fact_days.c.day)
        .outerjoin(
            materialized,
            (materialized.c.asset_id == fact_days.c.asset_id) & (materialized.c.availability_date == fact_days.c.day),
        )
        .where(materialized.c.asset_id.is_(None))
        .order_by(fact_days.c.asset_id, fact_days.c.day)
    )
    return [(row[0], row[1]) for row in session.execute(statement).all()]


# ---------------------------------------------------------------------------
# Contractual availability (V1's slot engine, fed by device history facts).
# ---------------------------------------------------------------------------
# Everything above computes the *operational* figure from the realtime poll.
# Everything below computes the *contractual* one from `history_read` facts.
# They share this module because they share their inputs' shape and their
# temporal device configuration, but they never share a row: the two write
# different `source`/`source_kind` values into the same tables, which is what
# lets both exist for one day and lets the selection policy choose.


def _history_samples_for_asset_day(
    session: Session, *, asset_id: int, target_date: date, tz: Any
) -> list[HistorySample]:
    """The *current* `history_read` reading for each instant of one asset's day.

    Superseded revisions are excluded, and that exclusion is load-bearing
    rather than tidiness: a history fact key carries its own instant, so when
    the provider corrects a reading the new row supersedes the old one for
    the *same* instant. Reading both would leave the original value still
    voting -- a corrected 0 kW would sit beside the original 10 kW and the
    slot would still count as producing, so a correction could never lower a
    number. The audit trail keeps the old row; the calculation must not see
    it.

    This is deliberately *not* applied to `live_read` facts by the sampled
    engine beside it. There, one key per device accumulates a revision per
    poll, so the revision chain *is* the time series and dropping earlier
    revisions would delete most of the day.
    """
    superseded = select(DeviceStatusFact.supersedes_fact_id).where(DeviceStatusFact.supersedes_fact_id.is_not(None))
    start = datetime.combine(target_date, dt_time.min, tzinfo=tz)
    end = datetime.combine(target_date + timedelta(days=1), dt_time.min, tzinfo=tz)
    statement = (
        select(DeviceStatusFact.device_id, DeviceStatusFact.observed_at, DeviceStatusFact.active_power_kw)
        .where(
            DeviceStatusFact.asset_id == asset_id,
            DeviceStatusFact.source_kind == "history_read",
            DeviceStatusFact.observed_at >= start,
            DeviceStatusFact.observed_at < end,
            DeviceStatusFact.id.not_in(superseded),
        )
        .order_by(DeviceStatusFact.observed_at)
    )
    return [
        HistorySample(device_id=row.device_id, sample_time=row.observed_at.astimezone(tz), active_power_kw=row.active_power_kw)
        for row in session.execute(statement).all()
    ]


def materialize_asset_contractual_day(
    session: Session, *, asset_id: int, target_date: date, tz: Any, now: datetime | None = None
) -> ContractualAssetDay:
    """Compute and persist one asset's **contractual** availability for one day.

    Reads only already-persisted `history_read` facts -- zero provider calls,
    exactly like the sampled materializer beside it. Ingestion
    (`integrations/fusionsolar/device_history.py`) is a separate step on
    purpose, so recomputing a historical figure never costs an API call.

    Writes with `source='fusionsolar_device_history'` /
    `source_kind='contractual'`, scoped so it can never disturb the sampled
    rows for the same day.
    """
    calculated_at = now or utc_now()
    expected = expected_devices_for_date(session, asset_id=asset_id, target_date=target_date)
    samples = _history_samples_for_asset_day(session, asset_id=asset_id, target_date=target_date, tz=tz)
    result = compute_asset_contractual_day(expected_devices=expected, samples=samples)
    _store_contractual_result(session, asset_id=asset_id, target_date=target_date, result=result, calculated_at=calculated_at)
    return result


def _store_contractual_result(
    session: Session, *, asset_id: int, target_date: date, result: ContractualAssetDay, calculated_at: datetime
) -> None:
    # Scoped to this source: the sampled rows for the same day are a
    # different fact and must survive untouched.
    session.query(DeviceAvailabilityDaily).filter_by(
        asset_id=asset_id, availability_date=target_date, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    ).delete()
    session.query(AssetAvailabilityDaily).filter_by(
        asset_id=asset_id, availability_date=target_date, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    ).delete()

    # `coverage_status` here answers a different question than the sampled
    # engine's: there is no gap/edge/minimum-sample test, because a closed-day
    # history pull either produced a measurable window or it did not.
    asset_status = COVERAGE_COMPLETE if result.availability_pct is not None else (
        COVERAGE_MISSING if not result.expected_device_count else COVERAGE_INDETERMINATE
    )
    for device in result.devices:
        session.add(
            DeviceAvailabilityDaily(
                device_id=device.device_id,
                asset_id=asset_id,
                availability_date=target_date,
                availability_pct=device.availability_pct,
                # V1's `inverter_availability_daily.valid_slots`.
                valid_sample_count=device.valid_slots,
                minimum_required_samples=0,
                coverage_status=COVERAGE_COMPLETE if device.availability_pct is not None else COVERAGE_INDETERMINATE,
                warning_codes_json=[],
                operational_window_start=result.window_start,
                operational_window_end=result.window_end,
                source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
                source_kind=source_kind(SOURCE_FUSIONSOLAR_DEVICE_HISTORY),
                calculated_at=calculated_at,
                created_at=calculated_at,
                updated_at=calculated_at,
            )
        )
    session.add(
        AssetAvailabilityDaily(
            asset_id=asset_id,
            availability_date=target_date,
            availability_pct=result.availability_pct,
            # The monthly weight. V1's `plant_availability_daily.valid_slots`
            # is the *tolerated* plant slot count, and its monthly query
            # weights by exactly this column -- storing anything else here
            # would silently change every contractual month.
            valid_sample_count=result.valid_slots,
            expected_device_count=result.expected_device_count,
            observed_device_count=result.observed_device_count,
            minimum_required_samples=0,
            coverage_status=asset_status,
            warning_codes_json=[],
            operational_window_start=result.window_start,
            operational_window_end=result.window_end,
            source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
            source_kind=source_kind(SOURCE_FUSIONSOLAR_DEVICE_HISTORY),
            calculation_details_json={
                "engine": "slot",
                "slot_minutes": SLOT_MINUTES,
                "edge_tolerance_minutes": EDGE_TOLERANCE_MINUTES,
                # Provenance the milestone asks for explicitly: the plant
                # figure is a weighted aggregation of device figures, not a
                # number the provider stated.
                "calculation": "weighted_device_aggregation",
                "device_source": SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
                "devices": [
                    {
                        "device_id": device.device_id,
                        "availability_pct": device.availability_pct,
                        "valid_slots": device.valid_slots,
                        "available_slots": device.available_slots,
                        "unavailable_slots": device.unavailable_slots,
                    }
                    for device in result.devices
                ],
            },
            calculated_at=calculated_at,
            created_at=calculated_at,
            updated_at=calculated_at,
        )
    )
    session.flush()


def materialize_contractual_window(
    session: Session,
    *,
    from_date: date,
    to_date: date,
    tz: Any,
    asset_ids: list[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute contractual availability for a window, from stored facts only.

    Same zero-API-call guarantee and the same idempotence as the sampled
    window materializer. Kept as a separate function rather than a flag on
    that one because the two read different facts and write different
    sources; a shared function with a mode switch would make it far easier to
    accidentally let one overwrite the other.
    """
    calculated_at = now or utc_now()
    targets = asset_ids if asset_ids is not None else fusionsolar_mapped_asset_ids(session, on_or_after=from_date)
    if not targets:
        return {"assets": 0, "days_recalculated": 0}
    states: dict[str, int] = {}
    days = 0
    for asset_id in sorted(targets):
        current = from_date
        while current <= to_date:
            result = materialize_asset_contractual_day(
                session, asset_id=asset_id, target_date=current, tz=tz, now=calculated_at
            )
            status = COVERAGE_COMPLETE if result.availability_pct is not None else COVERAGE_INDETERMINATE
            states[status] = states.get(status, 0) + 1
            days += 1
            _LOGGER.info(
                "availability_materialized asset_id=%s date=%s source=%s availability=%s quality=%s "
                "expected_devices=%s observed_devices=%s valid_slots=%s",
                asset_id, current.isoformat(), SOURCE_FUSIONSOLAR_DEVICE_HISTORY, result.availability_pct,
                status, result.expected_device_count, result.observed_device_count, result.valid_slots,
            )
            current = date.fromordinal(current.toordinal() + 1)
    return {"assets": len(targets), "days_recalculated": days, **states}


def assets_missing_history_for_date(
    session: Session, *, connection_id: int, target_date: date, tz: Any = LISBON
) -> list[int]:
    """Assets expected that day whose `history_read` facts are not in yet.

    The scheduler's zero-call guard. A closed day's history never changes, so
    once every expected device has facts for it there is nothing to fetch --
    and asking anyway would spend budget on a shared account to receive
    identical rows. Returns the assets still worth a call, so an empty list
    means the tick is free.
    """
    start = datetime.combine(target_date, dt_time.min, tzinfo=tz)
    end = datetime.combine(target_date + timedelta(days=1), dt_time.min, tzinfo=tz)
    expected = session.execute(
        select(AssetProviderMapping.asset_id, AssetProviderMapping.device_id)
        .join(Device, Device.id == AssetProviderMapping.device_id)
        .where(
            AssetProviderMapping.provider_connection_id == connection_id,
            AssetProviderMapping.resource_kind == "device",
            AssetProviderMapping.mapping_status == "active",
            AssetProviderMapping.valid_from <= target_date,
            (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= target_date),
            Device.device_kind == "inverter",
            Device.valid_from <= target_date,
            (Device.valid_to.is_(None)) | (Device.valid_to >= target_date),
        )
    ).all()
    if not expected:
        return []
    covered = {
        row[0]
        for row in session.execute(
            select(DeviceStatusFact.device_id).where(
                DeviceStatusFact.source_kind == "history_read",
                DeviceStatusFact.observed_at >= start,
                DeviceStatusFact.observed_at < end,
                DeviceStatusFact.device_id.in_([device_id for _asset_id, device_id in expected]),
            ).distinct()
        ).all()
    }
    return sorted({asset_id for asset_id, device_id in expected if device_id not in covered})


def expected_device_mappings_for_date(
    session: Session, *, connection_id: int, target_date: date
) -> tuple[list[AssetProviderMapping], dict[int, str]]:
    """Device mappings valid on a date, plus each asset's station code.

    Lives here rather than in the FusionSolar adapter because resolving *which
    inverters a plant had on a given day* is a domain question -- it reads
    `devices` (canonical identity) as well as the provider mapping, and the
    adapter package is deliberately barred from depending on the asset domain
    (`test_architecture_boundaries`). The adapter asks this, then fetches.

    Same temporal predicate as `expected_devices_for_date`, so a backfill of a
    past day fetches the inverters that plant actually had that day --
    including one since replaced.
    """
    statement = (
        select(AssetProviderMapping)
        .join(Device, Device.id == AssetProviderMapping.device_id)
        .where(
            AssetProviderMapping.provider_connection_id == connection_id,
            AssetProviderMapping.resource_kind == "device",
            AssetProviderMapping.mapping_status == "active",
            AssetProviderMapping.valid_from <= target_date,
            (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= target_date),
            Device.device_kind == "inverter",
            Device.valid_from <= target_date,
            (Device.valid_to.is_(None)) | (Device.valid_to >= target_date),
        )
    )
    mappings = list(session.scalars(statement).all())
    asset_ids = {mapping.asset_id for mapping in mappings}
    stations: dict[int, str] = {}
    if asset_ids:
        for asset_id, external_id in session.execute(
            select(AssetProviderMapping.asset_id, AssetProviderMapping.external_id).where(
                AssetProviderMapping.provider_connection_id == connection_id,
                AssetProviderMapping.resource_kind == "plant",
                AssetProviderMapping.mapping_status == "active",
                AssetProviderMapping.asset_id.in_(asset_ids),
            )
        ).all():
            stations.setdefault(asset_id, external_id)
    for mapping in mappings:
        session.expunge(mapping)
    return mappings, stations
