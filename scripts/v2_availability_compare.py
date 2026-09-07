#!/usr/bin/env python3
"""Read-only comparison: V2's ported availability engine against V1's real,
frozen historical data -- item 7 of `docs/v2/AVAILABILITY_MIGRATION_PLAN.md`.

**No blind import.** This script writes nothing to any V2 reporting table
and nothing to V1's database (opened `mode=ro`); it only prints a report.

For each day in the range, for the given V1 asset id:

1. Reads V1's real `device_realtime_snapshots` for that asset/day (its own
   historical evidence, whatever density it actually has -- V1's own history
   is known to be sparse: only 3 of 6 720 device-days ever reached
   `sampled_complete`, `DIAGNOSTICS.md`).
2. Feeds those exact snapshots through V2's ported
   `compute_asset_day_availability` (`reporting/rules/availability_window.py`).
3. Reads what V1's own `materialize_sampled_availability_day` already
   computed and stored for that same day
   (`plant_availability_sampled_daily`/`inverter_availability_sampled_daily`)
   -- V1's real answer, not recomputed here.
4. Prints both side by side. A mismatch here is a real port bug, not a data
   difference: both are working from the identical V1 snapshot rows.

Separately, if V2 already has its own live-collected
`asset_availability_daily` rows for `--asset-id` in the range (only possible
from 2026-08-25 onward, when FusionSolar device-status polling for asset 153
went live -- `DEVICE_TELEMETRY.md` §10.1), those are printed too, clearly
labelled as *not* a V1 comparison: V1 was stopped before that collection
window began, so there is no V1 data from the same days to diff against.
That is a real, structural limitation of this validation step, not an
oversight -- see the migration plan's §7.

Run inside a V2 container, with the frozen V1 checkout mounted read-only.
**V1 and V2 asset ids are separate id spaces and are not guaranteed to
match numerically** -- confirm the V1 id for the same physical plant by
matching `provider_devices.station_code`/`external_device_id` against V2's
`asset_provider_mappings.external_id` before running this, rather than
assuming the two ids happen to coincide:

    docker exec nemsei-v2-worker-1 python /app/scripts/v2_availability_compare.py \
        --asset-id <v2-asset-id> --v1-asset-id <confirmed-v1-asset-id> \
        --from 2026-07-01 --to 2026-09-03
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.reporting.rules.availability_window import DeviceDaySample, compute_asset_day_availability

LISBON = ZoneInfo("Europe/Lisbon")
UTC = datetime_timezone.utc
V1_DEFAULT_DB = Path("/opt/server/apps/Nem-sei/data/monitoring_board.db")
_EVALUABLE_STATUSES = {"available", "unavailable", "no_communication"}


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _open_v1_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise SystemExit(f"V1 database not found (read-only mount required): {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_v1_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _v1_expected_and_samples(conn: sqlite3.Connection, *, v1_asset_id: int, target_date: date):
    devices = conn.execute(
        """
        SELECT id AS provider_device_id, rated_power_kw FROM provider_devices
        WHERE asset_id = ? AND provider = 'FusionSolar' AND dev_type_id IN (1, 38)
        """,
        (v1_asset_id,),
    ).fetchall()
    expected = [{"device_id": row["provider_device_id"], "rated_power_kw": row["rated_power_kw"]} for row in devices]

    rows = conn.execute(
        """
        SELECT provider_device_id, collected_at, active_power_kw, availability_status
        FROM device_realtime_snapshots WHERE asset_id = ? AND provider = 'FusionSolar'
        ORDER BY collected_at
        """,
        (v1_asset_id,),
    ).fetchall()
    samples = []
    for row in rows:
        when = _parse_v1_timestamp(row["collected_at"])
        if when is None or when.astimezone(LISBON).date() != target_date:
            continue
        if row["availability_status"] not in _EVALUABLE_STATUSES:
            continue
        samples.append(
            DeviceDaySample(
                device_id=row["provider_device_id"], observed_at=when,
                active_power_kw=row["active_power_kw"], availability_status=row["availability_status"],
            )
        )
    return expected, samples


def _v1_stored_result(conn: sqlite3.Connection, *, v1_asset_id: int, target_date: date) -> dict | None:
    row = conn.execute(
        """
        SELECT availability_pct, coverage_status, valid_snapshot_count, observed_inverters
        FROM plant_availability_sampled_daily
        WHERE asset_id = ? AND provider = 'FusionSolar' AND availability_date = ?
        """,
        (v1_asset_id, target_date.isoformat()),
    ).fetchone()
    return dict(row) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset-id", type=int, required=True, help="V2 asset id")
    parser.add_argument("--v1-asset-id", type=int, required=True, help="The same physical plant's V1 asset id")
    parser.add_argument("--from", dest="from_date", type=_date, required=True)
    parser.add_argument("--to", dest="to_date", type=_date, required=True)
    parser.add_argument("--v1-db", type=Path, default=V1_DEFAULT_DB)
    args = parser.parse_args()
    if args.to_date < args.from_date:
        parser.error("--to must not be before --from")

    v1_conn = _open_v1_readonly(args.v1_db)
    settings = Settings.from_environment()
    sessions = build_session_factory(build_engine(settings))

    report: list[dict] = []
    current = args.from_date
    while current <= args.to_date:
        expected, samples = _v1_expected_and_samples(v1_conn, v1_asset_id=args.v1_asset_id, target_date=current)
        v2_from_v1_data = compute_asset_day_availability(expected_devices=expected, samples=samples)
        v1_stored = _v1_stored_result(v1_conn, v1_asset_id=args.v1_asset_id, target_date=current)

        with sessions() as session:
            v2_live = session.query(AssetAvailabilityDaily).filter_by(asset_id=args.asset_id, availability_date=current).one_or_none()

        row = {
            "date": current.isoformat(),
            "v1_stored": v1_stored,
            "v2_recomputed_from_v1_snapshots": {
                "coverage_status": v2_from_v1_data.coverage_status,
                "availability_pct": v2_from_v1_data.availability_pct,
                "valid_sample_count": v2_from_v1_data.valid_sample_count,
                "observed_device_count": v2_from_v1_data.observed_device_count,
            },
            "engine_agrees_with_v1": (
                v1_stored is not None
                and v1_stored["availability_pct"] == v2_from_v1_data.availability_pct
                and v1_stored["valid_snapshot_count"] == v2_from_v1_data.valid_sample_count
            ) if v1_stored else None,
            "v2_live_collection (not a V1 comparison -- V1 was stopped before this window)": (
                {
                    "coverage_status": v2_live.coverage_status,
                    "availability_pct": float(v2_live.availability_pct) if v2_live.availability_pct is not None else None,
                }
                if v2_live else None
            ),
        }
        report.append(row)
        current += timedelta(days=1)

    v1_conn.close()
    print(json.dumps(report, indent=2, default=str))

    mismatches = [row for row in report if row["engine_agrees_with_v1"] is False]
    if mismatches:
        print(f"\n{len(mismatches)} day(s) where V2's port disagrees with V1's own stored result -- investigate before any go-live.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
