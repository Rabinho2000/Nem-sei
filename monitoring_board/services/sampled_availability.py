from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
LISBON = ZoneInfo("Europe/Lisbon")
SOURCE_REALTIME_SAMPLED = "realtime_sampled"
FINAL_SAMPLED_STATUS = "sampled_complete"
OPERATING_EDGE_MINUTES = 30
MAX_SAMPLE_GAP_MINUTES = 90


def ensure_sampled_availability_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_device_configuration_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_device_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            external_device_id TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            expected INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider_device_id, valid_from),
            FOREIGN KEY (provider_device_id) REFERENCES provider_devices(id)
                ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_device_configuration_history_lookup
        ON provider_device_configuration_history(
            provider, asset_id, valid_from, valid_to, expected
        );

        CREATE TABLE IF NOT EXISTS inverter_availability_sampled_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            availability_date TEXT NOT NULL,
            provider_device_id INTEGER NOT NULL,
            inverter_id TEXT NOT NULL,
            availability_pct REAL,
            valid_snapshot_count INTEGER NOT NULL DEFAULT 0,
            minimum_required_snapshots INTEGER NOT NULL DEFAULT 0,
            coverage_status TEXT NOT NULL,
            warning_code TEXT,
            source TEXT NOT NULL DEFAULT 'realtime_sampled',
            operational_window_start TEXT,
            operational_window_end TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, provider_device_id, availability_date),
            FOREIGN KEY (provider_device_id) REFERENCES provider_devices(id)
                ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_inverter_sampled_daily_asset_date
        ON inverter_availability_sampled_daily(asset_id, availability_date);

        CREATE TABLE IF NOT EXISTS plant_availability_sampled_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            availability_date TEXT NOT NULL,
            availability_pct REAL,
            valid_snapshot_count INTEGER NOT NULL DEFAULT 0,
            expected_inverters INTEGER NOT NULL DEFAULT 0,
            observed_inverters INTEGER NOT NULL DEFAULT 0,
            coverage_status TEXT NOT NULL,
            warning_code TEXT,
            source TEXT NOT NULL DEFAULT 'realtime_sampled',
            operational_window_start TEXT,
            operational_window_end TEXT,
            minimum_required_snapshots INTEGER NOT NULL DEFAULT 0,
            calculation_details_json TEXT,
            calculated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, asset_id, availability_date),
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_plant_sampled_daily_date_asset
        ON plant_availability_sampled_daily(availability_date, asset_id);
        """
    )
    _seed_device_configuration_history(conn)


def record_device_configuration(
    conn: sqlite3.Connection,
    *,
    provider_device_id: int,
    active: bool,
    effective_date: date,
    now: datetime | None = None,
) -> None:
    ensure_sampled_availability_schema(conn)
    timestamp = _utc_iso(now)
    device = conn.execute(
        """
        SELECT id, asset_id, provider, external_device_id
        FROM provider_devices
        WHERE id = ?
        """,
        (provider_device_id,),
    ).fetchone()
    if device is None or not str(device["external_device_id"] or ""):
        return
    open_row = conn.execute(
        """
        SELECT id, asset_id, external_device_id, valid_from
        FROM provider_device_configuration_history
        WHERE provider_device_id = ? AND expected = 1 AND valid_to IS NULL
        ORDER BY valid_from DESC
        LIMIT 1
        """,
        (provider_device_id,),
    ).fetchone()
    if active:
        if open_row is not None:
            same_configuration = (
                int(open_row["asset_id"]) == int(device["asset_id"])
                and str(open_row["external_device_id"])
                == str(device["external_device_id"])
            )
            if same_configuration:
                return
            if str(open_row["valid_from"]) == effective_date.isoformat():
                conn.execute(
                    """
                    UPDATE provider_device_configuration_history
                    SET asset_id = ?, external_device_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(device["asset_id"]),
                        str(device["external_device_id"]),
                        timestamp,
                        int(open_row["id"]),
                    ),
                )
                return
            conn.execute(
                """
                UPDATE provider_device_configuration_history
                SET valid_to = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    (effective_date - timedelta(days=1)).isoformat(),
                    timestamp,
                    int(open_row["id"]),
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO provider_device_configuration_history (
                provider_device_id, asset_id, provider, external_device_id,
                valid_from, valid_to, expected, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)
            """,
            (
                provider_device_id,
                int(device["asset_id"]),
                str(device["provider"]),
                str(device["external_device_id"]),
                effective_date.isoformat(),
                timestamp,
                timestamp,
            ),
        )
        return
    if open_row is not None:
        conn.execute(
            """
            UPDATE provider_device_configuration_history
            SET valid_to = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                (effective_date - timedelta(days=1)).isoformat(),
                timestamp,
                int(open_row["id"]),
            ),
        )


def expected_devices_for_date(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    target_date: date,
) -> list[dict[str, Any]]:
    ensure_sampled_availability_schema(conn)
    rows = conn.execute(
        """
        SELECT
            h.provider_device_id,
            h.external_device_id,
            pd.device_name,
            pd.rated_power_kw,
            pd.model
        FROM provider_device_configuration_history h
        JOIN provider_devices pd ON pd.id = h.provider_device_id
        WHERE h.asset_id = ?
          AND h.provider = ?
          AND h.expected = 1
          AND h.valid_from <= ?
          AND (h.valid_to IS NULL OR h.valid_to >= ?)
          AND pd.dev_type_id IN (1, 38)
        ORDER BY h.provider_device_id
        """,
        (
            asset_id,
            provider,
            target_date.isoformat(),
            target_date.isoformat(),
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def materialize_sampled_availability_day(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    target_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    ensure_sampled_availability_schema(conn)
    calculated_at = _utc_iso(now)
    expected = expected_devices_for_date(
        conn,
        asset_id=asset_id,
        provider=provider,
        target_date=target_date,
    )
    expected_ids = {int(row["provider_device_id"]) for row in expected}
    snapshots = _snapshots_for_lisbon_day(
        conn,
        asset_id=asset_id,
        provider=provider,
        target_date=target_date,
    )
    evaluable = [
        row
        for row in snapshots
        if int(row["provider_device_id"]) in expected_ids
        and row["collected_at_dt"] is not None
        and str(row["availability_status"] or "")
        in {"available", "unavailable", "no_communication"}
    ]
    positive_times = sorted(
        {
            row["collected_at_dt"]
            for row in evaluable
            if _positive_float(row["active_power_kw"])
        }
    )
    if not expected:
        return _store_sampled_result(
            conn,
            asset_id=asset_id,
            provider=provider,
            target_date=target_date,
            coverage_status="missing",
            warning_code="missing_expected_inverter_configuration",
            expected=expected,
            observed_count=0,
            valid_snapshot_count=0,
            calculated_at=calculated_at,
            details={"expected_inverters": []},
        )
    if not positive_times:
        status = "indeterminate" if evaluable else "missing"
        return _store_sampled_result(
            conn,
            asset_id=asset_id,
            provider=provider,
            target_date=target_date,
            coverage_status=status,
            warning_code="no_observed_operating_window",
            expected=expected,
            observed_count=len(
                {int(row["provider_device_id"]) for row in evaluable}
            ),
            valid_snapshot_count=len(evaluable),
            calculated_at=calculated_at,
            details={"expected_inverters": sorted(expected_ids)},
        )

    # Positive production defines the observed operating window.  The
    # 30-minute value is a coverage tolerance for each inverter at the two
    # edges; shrinking the window here would silently weaken that rule.
    window_start = min(positive_times)
    window_end = max(positive_times)
    if window_end <= window_start:
        return _store_sampled_result(
            conn,
            asset_id=asset_id,
            provider=provider,
            target_date=target_date,
            coverage_status="indeterminate",
            warning_code="no_observed_operating_window",
            expected=expected,
            observed_count=len(
                {int(row["provider_device_id"]) for row in evaluable}
            ),
            valid_snapshot_count=len(evaluable),
            calculated_at=calculated_at,
            window_start=window_start,
            window_end=window_end,
            details={"expected_inverters": sorted(expected_ids)},
        )

    duration_minutes = (window_end - window_start).total_seconds() / 60
    minimum_samples = max(
        4,
        math.ceil(duration_minutes / MAX_SAMPLE_GAP_MINUTES) + 1,
    )
    by_device: dict[int, list[dict[str, Any]]] = {
        device_id: [] for device_id in expected_ids
    }
    for row in evaluable:
        collected_at = row["collected_at_dt"]
        if window_start <= collected_at <= window_end:
            by_device[int(row["provider_device_id"])].append(row)

    inverter_results: list[dict[str, Any]] = []
    for device in expected:
        device_id = int(device["provider_device_id"])
        rows = sorted(
            by_device[device_id],
            key=lambda item: item["collected_at_dt"],
        )
        times = [row["collected_at_dt"] for row in rows]
        warning_codes: list[str] = []
        if not rows:
            warning_codes.append("missing_expected_inverter")
        else:
            if times[0] - window_start > timedelta(
                minutes=OPERATING_EDGE_MINUTES
            ):
                warning_codes.append("late_first_sample")
            if window_end - times[-1] > timedelta(
                minutes=OPERATING_EDGE_MINUTES
            ):
                warning_codes.append("early_last_sample")
            if any(
                current - previous
                > timedelta(minutes=MAX_SAMPLE_GAP_MINUTES)
                for previous, current in zip(times, times[1:])
            ):
                warning_codes.append("sample_gap_over_90_minutes")
            if len(rows) < minimum_samples:
                warning_codes.append("insufficient_sample_count")
        complete = not warning_codes
        inverter_results.append(
            {
                **device,
                "coverage_status": (
                    FINAL_SAMPLED_STATUS if complete else "sampled_partial"
                ),
                "warning_code": ",".join(warning_codes),
                "valid_snapshot_count": len(rows),
                "availability_pct": (
                    round(
                        sum(
                            str(row["availability_status"]) == "available"
                            for row in rows
                        )
                        / len(rows)
                        * 100,
                        2,
                    )
                    if complete and rows
                    else None
                ),
            }
        )

    complete = all(
        row["coverage_status"] == FINAL_SAMPLED_STATUS
        for row in inverter_results
    )
    availability_pct = (
        _weighted_sampled_availability(inverter_results) if complete else None
    )
    warning_code = (
        ""
        if complete
        else "incomplete_inverter_sampling_coverage"
    )
    observed_count = sum(
        bool(by_device[int(device["provider_device_id"])])
        for device in expected
    )
    details = {
        "duration_minutes": round(duration_minutes, 2),
        "minimum_required_snapshots": minimum_samples,
        "expected_inverters": sorted(expected_ids),
        "inverters": [
            {
                "provider_device_id": row["provider_device_id"],
                "coverage_status": row["coverage_status"],
                "warning_code": row["warning_code"],
                "valid_snapshot_count": row["valid_snapshot_count"],
            }
            for row in inverter_results
        ],
    }
    return _store_sampled_result(
        conn,
        asset_id=asset_id,
        provider=provider,
        target_date=target_date,
        coverage_status=(
            FINAL_SAMPLED_STATUS if complete else "sampled_partial"
        ),
        warning_code=warning_code,
        expected=expected,
        observed_count=observed_count,
        valid_snapshot_count=sum(
            int(row["valid_snapshot_count"]) for row in inverter_results
        ),
        calculated_at=calculated_at,
        availability_pct=availability_pct,
        window_start=window_start,
        window_end=window_end,
        minimum_samples=minimum_samples,
        inverter_results=inverter_results,
        details=details,
    )


def materialize_existing_sampled_availability(
    conn: sqlite3.Connection,
    *,
    provider: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict[str, int]:
    ensure_sampled_availability_schema(conn)
    pairs: set[tuple[int, date]] = set()
    rows = conn.execute(
        """
        SELECT asset_id, collected_at
        FROM device_realtime_snapshots
        WHERE provider = ?
        """,
        (provider,),
    ).fetchall()
    for row in rows:
        collected_at = _parse_snapshot_timestamp(row["collected_at"])
        if collected_at is None:
            continue
        target_date = collected_at.astimezone(LISBON).date()
        if from_date and target_date < from_date:
            continue
        if to_date and target_date > to_date:
            continue
        pairs.add((int(row["asset_id"]), target_date))
    states: dict[str, int] = {}
    for asset_id, target_date in sorted(pairs, key=lambda item: (item[1], item[0])):
        result = materialize_sampled_availability_day(
            conn,
            asset_id=asset_id,
            provider=provider,
            target_date=target_date,
        )
        state = str(result["coverage_status"])
        states[state] = states.get(state, 0) + 1
    conn.commit()
    return {"days_recalculated_locally": len(pairs), **states}


def cleanup_realtime_snapshot_payloads(
    conn: sqlite3.Connection,
    *,
    provider: str,
    retention_days: int,
    reference_date: date,
) -> dict[str, int]:
    ensure_sampled_availability_schema(conn)
    cutoff_date = reference_date - timedelta(days=max(retention_days, 1))
    materialized = materialize_existing_sampled_availability(
        conn,
        provider=provider,
        to_date=cutoff_date,
    )
    rows = conn.execute(
        """
        SELECT id, asset_id, collected_at
        FROM device_realtime_snapshots
        WHERE provider = ?
          AND (
            payload_json IS NOT NULL
            OR pv_current_json IS NOT NULL
            OR pv_voltage_json IS NOT NULL
          )
        """,
        (provider,),
    ).fetchall()
    clear_ids: list[int] = []
    for row in rows:
        collected_at = _parse_snapshot_timestamp(row["collected_at"])
        if collected_at is None:
            continue
        target_date = collected_at.astimezone(LISBON).date()
        if target_date > cutoff_date:
            continue
        aggregate = conn.execute(
            """
            SELECT 1
            FROM plant_availability_sampled_daily
            WHERE provider = ? AND asset_id = ? AND availability_date = ?
            """,
            (provider, int(row["asset_id"]), target_date.isoformat()),
        ).fetchone()
        if aggregate is not None:
            clear_ids.append(int(row["id"]))
    if clear_ids:
        conn.executemany(
            """
            UPDATE device_realtime_snapshots
            SET payload_json = NULL, pv_current_json = NULL, pv_voltage_json = NULL
            WHERE id = ?
            """,
            [(row_id,) for row_id in clear_ids],
        )
    conn.commit()
    return {
        **materialized,
        "payloads_cleared": len(clear_ids),
        "snapshots_deleted": 0,
    }


def sampled_month_quality(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    month_start: date,
    month_end: date,
) -> dict[str, Any]:
    ensure_sampled_availability_schema(conn)
    rows = conn.execute(
        """
        SELECT availability_date, availability_pct, coverage_status, warning_code
        FROM plant_availability_sampled_daily
        WHERE asset_id = ? AND provider = ?
          AND availability_date BETWEEN ? AND ?
        ORDER BY availability_date
        """,
        (
            asset_id,
            provider,
            month_start.isoformat(),
            month_end.isoformat(),
        ),
    ).fetchall()
    expected_days = (month_end - month_start).days + 1
    final = (
        len(rows) == expected_days
        and all(
            str(row["coverage_status"]) == FINAL_SAMPLED_STATUS
            for row in rows
        )
    )
    values = [
        float(row["availability_pct"])
        for row in rows
        if row["availability_pct"] is not None
    ]
    return {
        "coverage_status": (
            FINAL_SAMPLED_STATUS if final else "sampled_partial"
        ),
        "availability_pct": (
            round(sum(values) / len(values), 2) if final and values else None
        ),
        "covered_days": len(rows),
        "expected_days": expected_days,
        "warnings": sorted(
            {
                str(row["warning_code"])
                for row in rows
                if row["warning_code"]
            }
        ),
    }


def _store_sampled_result(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    target_date: date,
    coverage_status: str,
    warning_code: str,
    expected: list[dict[str, Any]],
    observed_count: int,
    valid_snapshot_count: int,
    calculated_at: str,
    availability_pct: float | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    minimum_samples: int = 0,
    inverter_results: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if coverage_status != FINAL_SAMPLED_STATUS:
        availability_pct = None
    window_start_iso = _utc_iso(window_start) if window_start else None
    window_end_iso = _utc_iso(window_end) if window_end else None
    conn.execute(
        """
        DELETE FROM inverter_availability_sampled_daily
        WHERE asset_id = ? AND provider = ? AND availability_date = ?
        """,
        (asset_id, provider, target_date.isoformat()),
    )
    for row in inverter_results or []:
        row_availability = (
            row["availability_pct"]
            if row["coverage_status"] == FINAL_SAMPLED_STATUS
            else None
        )
        conn.execute(
            """
            INSERT INTO inverter_availability_sampled_daily (
                asset_id, provider, availability_date, provider_device_id,
                inverter_id, availability_pct, valid_snapshot_count,
                minimum_required_snapshots, coverage_status, warning_code,
                source, operational_window_start, operational_window_end,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                provider,
                target_date.isoformat(),
                int(row["provider_device_id"]),
                str(row["external_device_id"]),
                row_availability,
                int(row["valid_snapshot_count"]),
                minimum_samples,
                str(row["coverage_status"]),
                str(row["warning_code"] or ""),
                SOURCE_REALTIME_SAMPLED,
                window_start_iso,
                window_end_iso,
                calculated_at,
                calculated_at,
            ),
        )
    conn.execute(
        """
        INSERT INTO plant_availability_sampled_daily (
            asset_id, provider, availability_date, availability_pct,
            valid_snapshot_count, expected_inverters, observed_inverters,
            coverage_status, warning_code, source, operational_window_start,
            operational_window_end, minimum_required_snapshots,
            calculation_details_json, calculated_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, asset_id, availability_date) DO UPDATE SET
            availability_pct = excluded.availability_pct,
            valid_snapshot_count = excluded.valid_snapshot_count,
            expected_inverters = excluded.expected_inverters,
            observed_inverters = excluded.observed_inverters,
            coverage_status = excluded.coverage_status,
            warning_code = excluded.warning_code,
            source = excluded.source,
            operational_window_start = excluded.operational_window_start,
            operational_window_end = excluded.operational_window_end,
            minimum_required_snapshots = excluded.minimum_required_snapshots,
            calculation_details_json = excluded.calculation_details_json,
            calculated_at = excluded.calculated_at,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            provider,
            target_date.isoformat(),
            availability_pct,
            valid_snapshot_count,
            len(expected),
            observed_count,
            coverage_status,
            warning_code,
            SOURCE_REALTIME_SAMPLED,
            window_start_iso,
            window_end_iso,
            minimum_samples,
            json.dumps(details or {}, ensure_ascii=True, sort_keys=True),
            calculated_at,
            calculated_at,
            calculated_at,
        ),
    )
    return {
        "asset_id": asset_id,
        "availability_date": target_date.isoformat(),
        "availability_pct": availability_pct,
        "valid_snapshot_count": valid_snapshot_count,
        "expected_inverters": len(expected),
        "observed_inverters": observed_count,
        "coverage_status": coverage_status,
        "warning_code": warning_code,
        "source": SOURCE_REALTIME_SAMPLED,
        "minimum_required_snapshots": minimum_samples,
    }


def _snapshots_for_lisbon_day(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    target_date: date,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            provider_device_id, collected_at, active_power_kw,
            availability_status, communication_status
        FROM device_realtime_snapshots
        WHERE asset_id = ? AND provider = ?
        ORDER BY collected_at, id
        """,
        (asset_id, provider),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for source in rows:
        collected_at = _parse_snapshot_timestamp(source["collected_at"])
        if (
            collected_at is None
            or collected_at.astimezone(LISBON).date() != target_date
        ):
            continue
        item = dict(source)
        item["collected_at_dt"] = collected_at
        result.append(item)
    return result


def _seed_device_configuration_history(conn: sqlite3.Connection) -> None:
    table_exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'provider_devices'
        """
    ).fetchone()
    if table_exists is None:
        return
    now = _utc_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO provider_device_configuration_history (
            provider_device_id, asset_id, provider, external_device_id,
            valid_from, valid_to, expected, created_at, updated_at
        )
        SELECT
            id, asset_id, provider, external_device_id,
            '1970-01-01',
            CASE
                WHEN COALESCE(enabled, 1) = 0
                THEN date(COALESCE(updated_at, created_at), '-1 day')
                ELSE NULL
            END,
            1, ?, ?
        FROM provider_devices
        WHERE COALESCE(external_device_id, '') != ''
          AND dev_type_id IN (1, 38)
        """,
        (now, now),
    )


def _weighted_sampled_availability(rows: list[dict[str, Any]]) -> float | None:
    if not rows or any(row.get("availability_pct") is None for row in rows):
        return None
    weighted: list[tuple[float, float]] = []
    for row in rows:
        power = _float_or_none(row.get("rated_power_kw"))
        if power is None or power <= 0:
            return round(
                sum(float(item["availability_pct"]) for item in rows)
                / len(rows),
                2,
            )
        weighted.append((float(row["availability_pct"]), power))
    total_power = sum(power for _value, power in weighted)
    return round(
        sum(value * power for value, power in weighted) / total_power,
        2,
    )


def _parse_snapshot_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _positive_float(value: Any) -> bool:
    parsed = _float_or_none(value)
    return parsed is not None and parsed > 0


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
