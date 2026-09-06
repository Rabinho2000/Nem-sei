"""Golden parity for plant availability, the number customers argue about."""
from __future__ import annotations

import importlib
import importlib.util as _importlib_util
import sqlite3
import sys
from datetime import date as _date, datetime as _datetime, timedelta
from pathlib import Path

import pytest

from nemsei.reporting.rules.availability import float_or_none, weighted_sampled_availability
from nemsei.reporting.rules.availability_window import DeviceDaySample, compute_asset_day_availability


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1():
    if not (V1_ROOT / "monitoring_board" / "services" / "sampled_availability.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        return importlib.import_module("monitoring_board.services.sampled_availability")
    except Exception:  # pragma: no cover - a broken checkout is missing evidence
        return None


V1 = load_v1()
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


CASES = {
    "empty": [],
    "single full": [{"availability_pct": 100.0, "rated_power_kw": 20.0}],
    "equal weights": [
        {"availability_pct": 100.0, "rated_power_kw": 10.0},
        {"availability_pct": 50.0, "rated_power_kw": 10.0},
    ],
    "weighting actually matters": [
        {"availability_pct": 100.0, "rated_power_kw": 90.0},
        {"availability_pct": 0.0, "rated_power_kw": 10.0},
    ],
    "one device unknown availability": [
        {"availability_pct": 100.0, "rated_power_kw": 10.0},
        {"availability_pct": None, "rated_power_kw": 10.0},
    ],
    "one device unrated falls back to mean": [
        {"availability_pct": 100.0, "rated_power_kw": 90.0},
        {"availability_pct": 0.0, "rated_power_kw": None},
    ],
    "zero rating is not a weight": [
        {"availability_pct": 100.0, "rated_power_kw": 0.0},
        {"availability_pct": 40.0, "rated_power_kw": 10.0},
    ],
    "negative rating": [
        {"availability_pct": 80.0, "rated_power_kw": -5.0},
        {"availability_pct": 40.0, "rated_power_kw": 10.0},
    ],
    "rounding boundary": [
        {"availability_pct": 99.995, "rated_power_kw": 1.0},
        {"availability_pct": 99.994, "rated_power_kw": 1.0},
    ],
    "string values": [
        {"availability_pct": 90.0, "rated_power_kw": "30"},
        {"availability_pct": 60.0, "rated_power_kw": "10"},
    ],
    "unparseable rating": [
        {"availability_pct": 90.0, "rated_power_kw": "n/a"},
        {"availability_pct": 60.0, "rated_power_kw": "10"},
    ],
}


@requires_v1
@pytest.mark.parametrize("label", sorted(CASES))
def test_weighted_availability_matches_v1(label: str) -> None:
    rows = [dict(row) for row in CASES[label]]
    assert weighted_sampled_availability(rows) == V1._weighted_sampled_availability([dict(row) for row in CASES[label]]), label


@requires_v1
@pytest.mark.parametrize("value", [None, "", "10", "10.5", "abc", 0, -1, True])
def test_float_coercion_matches_v1(value) -> None:
    assert float_or_none(value) == V1._float_or_none(value)


def test_an_unknown_device_makes_the_plant_unknown_not_zero() -> None:
    """The rule that matters commercially, pinned without needing V1."""
    assert weighted_sampled_availability([{"availability_pct": None, "rated_power_kw": 10.0}]) is None
    assert weighted_sampled_availability([]) is None


# ---------------------------------------------------------------------------
# Window/gap/coverage parity: `availability_window.compute_asset_day_availability`
# against V1's real `materialize_sampled_availability_day`, run through V1's
# actual SQLite schema (not re-implemented), for the same synthetic inputs.
# See docs/v2/AVAILABILITY_MIGRATION_PLAN.md §7/§9.
# ---------------------------------------------------------------------------
_EVALUABLE_STATUSES = {"available", "unavailable", "no_communication"}
_V1_COVERAGE_TO_V2 = {
    "sampled_complete": "complete",
    "sampled_partial": "partial",
    "missing": "missing",
    "indeterminate": "indeterminate",
}


def _run_v1(devices: list[dict], snapshots: list[dict], target_date: _date) -> dict:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE provider_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL, provider TEXT NOT NULL,
            station_code TEXT NOT NULL, external_device_id TEXT, dev_dn TEXT, sn TEXT, device_name TEXT,
            dev_type_id INTEGER, model TEXT, rated_power_kw REAL, enabled INTEGER DEFAULT 1,
            last_seen_at TEXT, payload_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE device_realtime_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider_device_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
            provider TEXT NOT NULL, station_code TEXT NOT NULL, collected_at TEXT NOT NULL,
            inverter_state INTEGER, active_power_kw REAL, day_energy_kwh REAL,
            availability_status TEXT NOT NULL, communication_status TEXT NOT NULL,
            string_available_count INTEGER, string_total_count INTEGER,
            pv_current_json TEXT, pv_voltage_json TEXT, payload_json TEXT, created_at TEXT NOT NULL
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    for device in devices:
        conn.execute(
            """
            INSERT INTO provider_devices (id, asset_id, provider, station_code, external_device_id,
                device_name, dev_type_id, model, rated_power_kw, created_at, updated_at)
            VALUES (?, 1, 'FusionSolar', 'ST1', ?, ?, 1, 'model', ?, ?, ?)
            """,
            (device["provider_device_id"], device["external_device_id"], device["external_device_id"],
             device["rated_power_kw"], now, now),
        )
    for snapshot in snapshots:
        conn.execute(
            """
            INSERT INTO device_realtime_snapshots (provider_device_id, asset_id, provider, station_code,
                collected_at, active_power_kw, availability_status, communication_status, created_at)
            VALUES (?, 1, 'FusionSolar', 'ST1', ?, ?, ?, 'online', ?)
            """,
            (snapshot["provider_device_id"], snapshot["collected_at"], snapshot["active_power_kw"],
             snapshot["availability_status"], now),
        )
    conn.commit()
    result = V1.materialize_sampled_availability_day(conn, asset_id=1, provider="FusionSolar", target_date=target_date)
    device_rows = conn.execute(
        "SELECT provider_device_id, coverage_status, warning_code, valid_snapshot_count, availability_pct "
        "FROM inverter_availability_sampled_daily ORDER BY provider_device_id"
    ).fetchall()
    conn.close()
    return {"asset": result, "devices": [dict(row) for row in device_rows]}


def _run_v2(devices: list[dict], snapshots: list[dict]) -> "object":
    expected = [{"device_id": d["provider_device_id"], "rated_power_kw": d["rated_power_kw"]} for d in devices]
    samples = [
        DeviceDaySample(
            device_id=s["provider_device_id"],
            observed_at=_datetime.fromisoformat(s["collected_at"]),
            active_power_kw=s["active_power_kw"],
            availability_status=s["availability_status"],
        )
        for s in snapshots
        if s["availability_status"] in _EVALUABLE_STATUSES
    ]
    return compute_asset_day_availability(expected_devices=expected, samples=samples)


def _lisbon(hour: int, minute: int, *, day: int = 15) -> str:
    # Mid-July: Lisbon is UTC+1 (WEST). Expressed directly as a UTC offset so
    # V1's own naive `datetime.fromisoformat` (no zoneinfo dependency in its
    # snapshot parser beyond `.astimezone(LISBON)`) sees exactly what a real
    # FusionSolar poll would have stored.
    return f"2026-07-{day:02d}T{hour:02d}:{minute:02d}:00+01:00"


DEVICES = [
    {"provider_device_id": 1, "external_device_id": "INV-1", "rated_power_kw": 20.0},
    {"provider_device_id": 2, "external_device_id": "INV-2", "rated_power_kw": 30.0},
]


def _clean_snapshots(device_ids: list[int], *, start=(6, 0), end=(20, 0), step_minutes=30) -> list[dict]:
    rows = []
    hour, minute = start
    while (hour, minute) <= end:
        for device_id in device_ids:
            rows.append({"provider_device_id": device_id, "collected_at": _lisbon(hour, minute),
                         "active_power_kw": 5.0, "availability_status": "available"})
        minute += step_minutes
        if minute >= 60:
            hour, minute = hour + minute // 60, minute % 60
    return rows


SCENARIOS = {
    "clean full day, two devices": _clean_snapshots([1, 2]),
    "one device missing entirely": _clean_snapshots([2]),
    "90-minute gap on one device": [
        row for row in _clean_snapshots([1, 2])
        if not (row["provider_device_id"] == 1 and (11, 0) < (int(row["collected_at"][11:13]), int(row["collected_at"][14:16])) < (13, 0))
    ],
    "late first sample on one device": [
        row for row in _clean_snapshots([1, 2])
        if row["provider_device_id"] == 2 or (int(row["collected_at"][11:13]), int(row["collected_at"][14:16])) >= (6, 45)
    ],
    "mixed availability status, no gaps": [
        {**row, "availability_status": "unavailable"} if row["provider_device_id"] == 1 and int(row["collected_at"][11:13]) < 12 else row
        for row in _clean_snapshots([1, 2])
    ],
    "no snapshots at all": [],
    "snapshots present, never positive power": [
        {**row, "active_power_kw": 0.0} for row in _clean_snapshots([1, 2], start=(10, 0), end=(10, 30), step_minutes=30)
    ],
}


@requires_v1
@pytest.mark.parametrize("label", sorted(SCENARIOS))
def test_window_engine_matches_v1_materialize_sampled_availability_day(label: str) -> None:
    snapshots = SCENARIOS[label]
    target_date = _date(2026, 7, 15)
    v1 = _run_v1(DEVICES, snapshots, target_date)
    v2 = _run_v2(DEVICES, snapshots)

    assert _V1_COVERAGE_TO_V2[v1["asset"]["coverage_status"]] == v2.coverage_status, label
    assert v1["asset"]["availability_pct"] == v2.availability_pct, label
    assert v1["asset"]["valid_snapshot_count"] == v2.valid_sample_count, label
    assert v1["asset"]["observed_inverters"] == v2.observed_device_count, label

    v2_by_device = {d.device_id: d for d in v2.devices}
    for row in v1["devices"]:
        device_id = row["provider_device_id"]
        v2_device = v2_by_device[device_id]
        expected_status = "complete" if row["coverage_status"] == "sampled_complete" else "partial"
        assert expected_status == v2_device.coverage_status, (label, device_id)
        assert row["valid_snapshot_count"] == v2_device.valid_sample_count, (label, device_id)
        assert row["availability_pct"] == v2_device.availability_pct, (label, device_id)


def test_empty_expected_devices_matches_v1_missing_configuration() -> None:
    """No V1 mount needed: both sides agree with no configuration at all, by construction."""
    result = compute_asset_day_availability(expected_devices=[], samples=[])
    assert result.coverage_status == "missing"
    assert result.warning_codes == ("missing_expected_inverter_configuration",)


# ---------------------------------------------------------------------------
# Real-data parity: V1's actual frozen `device_realtime_snapshots` for real
# assets, fed through V2's port, compared against V1's own already-stored
# `plant_availability_sampled_daily` result for the same day. Not synthetic --
# this is the item 7 "compare against existing V1 data" check, automated.
# The candidate assets/range were found by scanning V1's own database for its
# ten FusionSolar assets with the most realtime-snapshot rows in one
# contiguous window (`scripts/v2_availability_compare.py`'s own logic,
# exploratory run 2026-09-04): 390 real device-day comparisons across 10
# assets, 390/390 agreement, 0 reaching `complete` in this window (consistent
# with V1's overall 3-of-6720 historical sparsity, `DIAGNOSTICS.md`).
# ---------------------------------------------------------------------------
_COMPARE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "v2_availability_compare.py"


def _load_compare_module():
    spec = _importlib_util.spec_from_file_location("v2_availability_compare", _COMPARE_SCRIPT)
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REAL_DATA_ASSETS = (1888, 1890, 1963, 1924, 1853, 1882, 1894, 1906, 2115, 1857)
_REAL_DATA_FROM = _date(2026, 5, 18)
_REAL_DATA_TO = _date(2026, 7, 13)


@requires_v1
@pytest.mark.parametrize("v1_asset_id", _REAL_DATA_ASSETS)
def test_window_engine_matches_v1_real_historical_data(v1_asset_id: int) -> None:
    if not _COMPARE_SCRIPT.is_file():  # pragma: no cover - defensive, script ships alongside this test
        pytest.skip("v2_availability_compare.py is not present")
    compare = _load_compare_module()
    if not compare.V1_DEFAULT_DB.is_file():
        pytest.skip("V1's live database is not mounted here")
    conn = compare._open_v1_readonly(compare.V1_DEFAULT_DB)
    try:
        compared = 0
        current = _REAL_DATA_FROM
        while current <= _REAL_DATA_TO:
            expected, samples = compare._v1_expected_and_samples(conn, v1_asset_id=v1_asset_id, target_date=current)
            v2 = compute_asset_day_availability(expected_devices=expected, samples=samples)
            v1_stored = compare._v1_stored_result(conn, v1_asset_id=v1_asset_id, target_date=current)
            if v1_stored is not None:
                compared += 1
                assert v1_stored["availability_pct"] == v2.availability_pct, (v1_asset_id, current)
                assert v1_stored["valid_snapshot_count"] == v2.valid_sample_count, (v1_asset_id, current)
            current += timedelta(days=1)
        # A day range with zero comparable V1 rows would make this test
        # vacuously pass -- V1's history changing under us should fail loudly,
        # not silently stop proving anything.
        assert compared > 0, f"no comparable V1 rows found for asset {v1_asset_id} in the fixed range"
    finally:
        conn.close()
