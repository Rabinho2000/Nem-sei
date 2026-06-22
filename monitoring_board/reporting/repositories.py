from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from monitoring_board.db import query_all


def list_portfolio_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(conn, "SELECT * FROM portfolio_groups ORDER BY name COLLATE NOCASE")


def get_latest_tariff(conn: sqlite3.Connection, asset_id: int, report_start: date) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM asset_tariffs
        WHERE asset_id = ?
          AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
        ORDER BY COALESCE(valid_from, '') DESC, id DESC
        LIMIT 1
        """,
        (asset_id, report_start.isoformat(), report_start.isoformat()),
    ).fetchone()


def has_expired_tariff(conn: sqlite3.Connection, asset_id: int, report_start: date) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM asset_tariffs
        WHERE asset_id = ?
          AND valid_to IS NOT NULL
          AND valid_to != ''
          AND valid_to < ?
        LIMIT 1
        """,
        (asset_id, report_start.isoformat()),
    ).fetchone()
    return row is not None


def get_monthly_availability(conn: sqlite3.Connection, asset_id: int, start: date, end: date) -> float | None:
    row = conn.execute(
        """
        SELECT SUM(weighted_availability_pct * valid_slots) AS weighted_sum, SUM(valid_slots) AS slots
        FROM plant_availability_daily
        WHERE asset_id = ? AND provider = 'FusionSolar' AND availability_date BETWEEN ? AND ?
        """,
        (asset_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    if row and row["slots"]:
        return round(float(row["weighted_sum"]) / float(row["slots"]), 2)
    return None


def get_monthly_production_record(conn: sqlite3.Connection, asset_id: int | None, period_start: date) -> sqlite3.Row | None:
    if asset_id is None:
        return None
    return conn.execute(
        """
        SELECT production_kwh
        FROM production_records
        WHERE asset_id = ? AND provider = 'FusionSolar' AND period_type = 'month' AND period_date = ?
        LIMIT 1
        """,
        (asset_id, period_start.isoformat()),
    ).fetchone()


def get_latest_helioscope_expected(conn: sqlite3.Connection, asset_id: int | None, month: int) -> sqlite3.Row | None:
    if asset_id is None:
        return None
    return conn.execute(
        """
        SELECT expected_kwh
        FROM helioscope_expected_production
        WHERE asset_id = ? AND month = ?
        ORDER BY imported_at DESC, id DESC
        LIMIT 1
        """,
        (asset_id, month),
    ).fetchone()


def list_tariff_period_rules(conn: sqlite3.Connection, tariff_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        "SELECT * FROM tariff_period_rules WHERE tariff_id = ? ORDER BY weekday_type, start_time",
        (tariff_id,),
    )


def list_hourly_production_records(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    start_iso: str,
    end_iso: str,
) -> list[sqlite3.Row]:
    if asset_id is None:
        return []
    return query_all(
        conn,
        """
        SELECT *
        FROM production_hourly_records
        WHERE asset_id = ? AND provider = 'FusionSolar' AND period_start >= ? AND period_start < ?
        ORDER BY period_start
        """,
        (asset_id, start_iso, end_iso),
    )


def list_portfolio_report_assets(conn: sqlite3.Connection, portfolio_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT pa.*, a.project_name, a.nif AS asset_nif, a.start_contract, a.mounting_date, a.kwp
        FROM portfolio_assets pa
        LEFT JOIN assets a ON a.id = pa.asset_id
        WHERE pa.portfolio_id = ? AND pa.active = 1
        ORDER BY COALESCE(pa.external_name, a.project_name) COLLATE NOCASE
        """,
        (portfolio_id,),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
