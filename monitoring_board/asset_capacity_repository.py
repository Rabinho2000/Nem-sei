from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class CapacityResolution:
    installed_power_kwp: Decimal | None
    source: str
    period_id: int | None = None
    ambiguous: bool = False


def ensure_asset_capacity_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS asset_capacity_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            installed_power_kwp TEXT NOT NULL,
            reason TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE,
            CHECK (valid_to IS NULL OR valid_to >= valid_from)
        );
        CREATE INDEX IF NOT EXISTS idx_asset_capacity_periods_lookup
            ON asset_capacity_periods(asset_id, valid_from, valid_to);
        """
    )
    backfill_initial_capacity_periods(conn)


def backfill_initial_capacity_periods(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT id, kwp, mounting_date, start_contract
        FROM assets
        WHERE NOT EXISTS (
            SELECT 1 FROM asset_capacity_periods cp WHERE cp.asset_id = assets.id
        )
        """
    ).fetchall()
    created = 0
    for row in rows:
        power = parse_positive_power(row["kwp"])
        valid_from = parse_date(row["mounting_date"]) or parse_date(row["start_contract"])
        if power is None or valid_from is None:
            continue
        add_capacity_period(
            conn,
            asset_id=int(row["id"]),
            valid_from=valid_from,
            valid_to=None,
            installed_power_kwp=power,
            reason="Período inicial criado a partir da potência atual.",
            source="asset_backfill",
        )
        created += 1
    return created


def list_capacity_periods(conn: sqlite3.Connection, asset_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM asset_capacity_periods
        WHERE asset_id = ?
        ORDER BY valid_from, id
        """,
        (asset_id,),
    ).fetchall()


def add_capacity_period(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    valid_from: date | str,
    valid_to: date | str | None,
    installed_power_kwp: Decimal | str | float,
    reason: str = "",
    source: str = "manual",
) -> int:
    start = require_date(valid_from)
    end = require_date(valid_to) if valid_to not in (None, "") else None
    power = require_positive_power(installed_power_kwp)
    if end is not None and end < start:
        raise ValueError("capacity_invalid_period")
    if capacity_period_overlaps(conn, asset_id=asset_id, start=start, end=end):
        raise ValueError("capacity_period_overlap")
    now = datetime.now().isoformat(timespec="seconds")
    return int(
        conn.execute(
            """
            INSERT INTO asset_capacity_periods (
                asset_id, valid_from, valid_to, installed_power_kwp,
                reason, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                start.isoformat(),
                end.isoformat() if end else None,
                decimal_text(power),
                reason.strip(),
                source.strip() or "manual",
                now,
                now,
            ),
        ).lastrowid
    )


def add_capacity_expansion(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    valid_from: date | str,
    installed_power_kwp: Decimal | str | float,
    reason: str = "",
    source: str = "manual",
) -> int:
    start = require_date(valid_from)
    previous = conn.execute(
        """
        SELECT *
        FROM asset_capacity_periods
        WHERE asset_id = ? AND valid_from < ?
          AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY valid_from DESC, id DESC
        LIMIT 1
        """,
        (asset_id, start.isoformat(), start.isoformat()),
    ).fetchone()
    if previous is not None:
        conn.execute(
            """
            UPDATE asset_capacity_periods
            SET valid_to = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                (start - timedelta(days=1)).isoformat(),
                datetime.now().isoformat(timespec="seconds"),
                previous["id"],
            ),
        )
    try:
        return add_capacity_period(
            conn,
            asset_id=asset_id,
            valid_from=start,
            valid_to=None,
            installed_power_kwp=installed_power_kwp,
            reason=reason,
            source=source,
        )
    except Exception:
        if previous is not None:
            conn.execute(
                "UPDATE asset_capacity_periods SET valid_to = ? WHERE id = ?",
                (previous["valid_to"], previous["id"]),
            )
        raise


def update_capacity_period(
    conn: sqlite3.Connection,
    *,
    period_id: int,
    asset_id: int,
    valid_from: date | str,
    valid_to: date | str | None,
    installed_power_kwp: Decimal | str | float,
    reason: str = "",
) -> None:
    start = require_date(valid_from)
    end = require_date(valid_to) if valid_to not in (None, "") else None
    power = require_positive_power(installed_power_kwp)
    if end is not None and end < start:
        raise ValueError("capacity_invalid_period")
    if capacity_period_overlaps(
        conn,
        asset_id=asset_id,
        start=start,
        end=end,
        exclude_period_id=period_id,
    ):
        raise ValueError("capacity_period_overlap")
    cursor = conn.execute(
        """
        UPDATE asset_capacity_periods
        SET valid_from = ?, valid_to = ?, installed_power_kwp = ?,
            reason = ?, updated_at = ?
        WHERE id = ? AND asset_id = ?
        """,
        (
            start.isoformat(),
            end.isoformat() if end else None,
            decimal_text(power),
            reason.strip(),
            datetime.now().isoformat(timespec="seconds"),
            period_id,
            asset_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("capacity_period_not_found")


def resolve_capacity_at(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    target_date: date | str,
    fallback_kwp: Any = None,
) -> CapacityResolution:
    target = require_date(target_date)
    rows = conn.execute(
        """
        SELECT *
        FROM asset_capacity_periods
        WHERE asset_id = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY valid_from DESC, id DESC
        """,
        (asset_id, target.isoformat(), target.isoformat()),
    ).fetchall()
    if len(rows) > 1:
        return CapacityResolution(None, "capacity_history", ambiguous=True)
    if rows:
        power = parse_positive_power(rows[0]["installed_power_kwp"])
        return CapacityResolution(power, "capacity_history", int(rows[0]["id"]))
    fallback = parse_positive_power(fallback_kwp)
    return CapacityResolution(fallback, "asset_kwp" if fallback is not None else "missing")


def resolve_capacity_for_period(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    period_start: date,
    period_end: date,
    fallback_kwp: Any = None,
) -> CapacityResolution:
    rows = conn.execute(
        """
        SELECT *
        FROM asset_capacity_periods
        WHERE asset_id = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY valid_from, id
        """,
        (asset_id, period_end.isoformat(), period_start.isoformat()),
    ).fetchall()
    if len(rows) > 1:
        return CapacityResolution(None, "capacity_history", ambiguous=True)
    if rows:
        row = rows[0]
        if row["valid_from"] <= period_start.isoformat() and (
            row["valid_to"] is None or row["valid_to"] >= period_end.isoformat()
        ):
            return CapacityResolution(
                parse_positive_power(row["installed_power_kwp"]),
                "capacity_history",
                int(row["id"]),
            )
        return CapacityResolution(None, "capacity_history", ambiguous=True)
    fallback = parse_positive_power(fallback_kwp)
    return CapacityResolution(fallback, "asset_kwp" if fallback is not None else "missing")


def capacity_period_overlaps(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    start: date,
    end: date | None,
    exclude_period_id: int | None = None,
) -> bool:
    end_text = end.isoformat() if end else "9999-12-31"
    params: list[Any] = [asset_id, end_text, start.isoformat()]
    exclude = ""
    if exclude_period_id is not None:
        exclude = "AND id != ?"
        params.append(exclude_period_id)
    return (
        conn.execute(
            f"""
            SELECT 1
            FROM asset_capacity_periods
            WHERE asset_id = ?
              AND valid_from <= ?
              AND COALESCE(valid_to, '9999-12-31') >= ?
              {exclude}
            LIMIT 1
            """,
            params,
        ).fetchone()
        is not None
    )


def parse_positive_power(value: Any) -> Decimal | None:
    try:
        power = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not power.is_finite() or power <= 0:
        return None
    return power


def require_positive_power(value: Any) -> Decimal:
    power = parse_positive_power(value)
    if power is None:
        raise ValueError("capacity_power_must_be_positive")
    return power


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def require_date(value: Any) -> date:
    result = parse_date(value)
    if result is None:
        raise ValueError("capacity_invalid_date")
    return result


def decimal_text(value: Decimal) -> str:
    if not math.isfinite(float(value)):
        raise ValueError("capacity_power_must_be_positive")
    return format(value.normalize(), "f")
