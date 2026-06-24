from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from monitoring_board.db import ensure_column
from monitoring_board.reporting.models import ReportPeriodType, ReportingPeriod
from monitoring_board.reporting.periods import build_period
from monitoring_board.reporting.portfolio import (
    DEFAULT_PROFILE_COLUMNS,
    ENGINE_VERSION,
    PortfolioComparisonResult,
    PortfolioDataCoverage,
    PortfolioReportColumn,
    PortfolioReportProfile,
    PortfolioReportResult,
    PortfolioReportRow,
    PortfolioReportSummary,
    default_profile,
    profile_from_config,
    profile_to_config,
    result_to_dict,
    validate_profile,
)


def ensure_portfolio_reporting_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_report_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            is_default INTEGER DEFAULT 0,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (portfolio_id) REFERENCES portfolio_groups(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS portfolio_report_profile_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(profile_id, version),
            FOREIGN KEY (profile_id) REFERENCES portfolio_report_profiles(id) ON DELETE CASCADE
        );
        """
    )
    for column in (
        "profile_id INTEGER",
        "profile_version INTEGER",
        "period_type TEXT",
        "period_start TEXT",
        "period_end TEXT",
        "engine_version TEXT",
        "status TEXT",
        "config_snapshot_json TEXT",
        "summary_json TEXT",
        "coverage_json TEXT",
        "warnings_json TEXT",
        "rows_json TEXT",
        "comparison_json TEXT",
        "completed_at TEXT",
    ):
        ensure_column(conn, "portfolio_report_runs", column)
    conn.execute(
        """
        UPDATE portfolio_report_runs
        SET status = COALESCE(status, 'legacy'),
            period_type = COALESCE(period_type, 'monthly'),
            period_start = COALESCE(period_start, report_month || '-01'),
            period_end = COALESCE(period_end, report_month || '-01'),
            engine_version = COALESCE(engine_version, 'legacy')
        WHERE engine_version IS NULL OR status IS NULL OR period_type IS NULL
        """
    )
    seed_default_profiles(conn)


def seed_default_profiles(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM portfolio_report_profiles WHERE portfolio_id IS NULL"
        ).fetchall()
    }
    for name in DEFAULT_PROFILE_COLUMNS:
        if name in existing:
            continue
        profile = default_profile(name)
        save_profile(conn, profile, is_default=1 if name == "Completo" else 0)
    defaults = conn.execute(
        "SELECT id FROM portfolio_report_profiles WHERE portfolio_id IS NULL AND active = 1 AND is_default = 1 ORDER BY name = 'Completo' DESC, id"
    ).fetchall()
    if not defaults:
        row = conn.execute("SELECT id FROM portfolio_report_profiles WHERE portfolio_id IS NULL AND active = 1 AND name = 'Completo' LIMIT 1").fetchone()
        if row:
            set_default_profile(conn, int(row["id"]))
    elif len(defaults) > 1:
        set_default_profile(conn, int(defaults[0]["id"]))


def list_profiles(conn: sqlite3.Connection, portfolio_id: int | None = None, *, include_inactive: bool = False) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ["(portfolio_id IS NULL" + (" OR portfolio_id = ?" if portfolio_id else "") + ")"]
    if portfolio_id:
        params.append(portfolio_id)
    if not include_inactive:
        where.append("active = 1")
    return conn.execute(
        f"""
        SELECT *
        FROM portfolio_report_profiles
        WHERE {' AND '.join(where)}
        ORDER BY is_default DESC, portfolio_id IS NOT NULL DESC, name COLLATE NOCASE
        """,
        params,
    ).fetchall()


def get_profile(conn: sqlite3.Connection, profile_id: int) -> PortfolioReportProfile | None:
    row = conn.execute("SELECT * FROM portfolio_report_profiles WHERE id = ?", (profile_id,)).fetchone()
    return profile_from_row(row) if row else None


def get_default_profile(conn: sqlite3.Connection, portfolio_id: int | None = None) -> PortfolioReportProfile:
    row = conn.execute(
        """
        SELECT *
        FROM portfolio_report_profiles
        WHERE active = 1
          AND is_default = 1
          AND (portfolio_id IS NULL OR portfolio_id = ?)
        ORDER BY portfolio_id IS NOT NULL DESC, is_default DESC, id
        LIMIT 1
        """,
        (portfolio_id,),
    ).fetchone()
    return profile_from_row(row) if row else default_profile("Completo", portfolio_id=portfolio_id)


def save_profile(conn: sqlite3.Connection, profile: PortfolioReportProfile, *, active: int = 1, is_default: int = 0) -> int:
    profile = validate_profile(profile)
    now = datetime.now().isoformat(timespec="seconds")
    config_json = dump_json(profile_to_config(profile))
    if len(config_json) > 20000:
        raise ValueError("profile_config_too_large")
    if profile.id:
        existing = conn.execute("SELECT portfolio_id FROM portfolio_report_profiles WHERE id = ?", (profile.id,)).fetchone()
        if existing is None:
            raise ValueError("profile_not_found")
        if existing["portfolio_id"] != profile.portfolio_id:
            raise ValueError("profile_scope_change_forbidden")
        conn.execute(
            """
            UPDATE portfolio_report_profiles
            SET portfolio_id = ?, name = ?, description = ?, active = ?, is_default = ?,
                config_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (profile.portfolio_id, profile.name, profile.description, active, is_default, config_json, now, profile.id),
        )
        profile_id = profile.id
    else:
        cursor = conn.execute(
            """
            INSERT INTO portfolio_report_profiles
                (portfolio_id, name, description, active, is_default, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile.portfolio_id, profile.name, profile.description, active, is_default, config_json, now, now),
        )
        profile_id = int(cursor.lastrowid)
    if is_default:
        set_default_profile(conn, profile_id)
    create_profile_version(conn, profile_id, profile)
    return profile_id


def set_default_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    row = conn.execute("SELECT portfolio_id FROM portfolio_report_profiles WHERE id = ? AND active = 1", (profile_id,)).fetchone()
    if row is None:
        raise ValueError("profile_not_found")
    portfolio_id = row["portfolio_id"]
    if portfolio_id is None:
        conn.execute("UPDATE portfolio_report_profiles SET is_default = 0 WHERE portfolio_id IS NULL")
    else:
        conn.execute("UPDATE portfolio_report_profiles SET is_default = 0 WHERE portfolio_id = ?", (portfolio_id,))
    conn.execute("UPDATE portfolio_report_profiles SET is_default = 1, updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), profile_id))


def duplicate_profile(conn: sqlite3.Connection, profile_id: int, name: str) -> int:
    profile = get_profile(conn, profile_id)
    if profile is None:
        raise ValueError("profile_not_found")
    return save_profile(conn, replace(profile, id=None, name=name, description=f"Copia de {profile.name}"), is_default=0)


def archive_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("UPDATE portfolio_report_profiles SET active = 0, updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), profile_id))


def profile_from_row(row: sqlite3.Row) -> PortfolioReportProfile:
    return profile_from_config(json.loads(row["config_json"] or "{}"), profile_id=int(row["id"]), portfolio_id=row["portfolio_id"])


def create_profile_version(conn: sqlite3.Connection, profile_id: int, profile: PortfolioReportProfile) -> int:
    version = latest_profile_version(conn, profile_id) + 1
    conn.execute(
        """
        INSERT OR IGNORE INTO portfolio_report_profile_versions (profile_id, version, config_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (profile_id, version, dump_json(profile_to_config(profile)), datetime.now().isoformat(timespec="seconds")),
    )
    return version


def latest_profile_version(conn: sqlite3.Connection, profile_id: int | None) -> int:
    if not profile_id:
        return 1
    row = conn.execute("SELECT MAX(version) AS version FROM portfolio_report_profile_versions WHERE profile_id = ?", (profile_id,)).fetchone()
    return int(row["version"] or 0) if row else 0


def snapshot_portfolio_result(conn: sqlite3.Connection, result: PortfolioReportResult, notes: str = "") -> int:
    payload = result_to_dict(result)
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO portfolio_report_runs (
            portfolio_id, report_month, created_at, notes, profile_id, profile_version,
            period_type, period_start, period_end, engine_version, status,
            config_snapshot_json, summary_json, coverage_json, warnings_json, rows_json, comparison_json, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.portfolio_id,
            result.period.start.strftime("%Y-%m"),
            now,
            notes,
            result.profile.id,
            result.profile_version,
            result.period.period_type.value,
            result.period.start.isoformat(),
            result.period.end.isoformat(),
            result.engine_version,
            dump_json(payload["profile"]),
            dump_json(payload["summary"]),
            dump_json(payload["coverage"]),
            dump_json(payload["warnings"]),
            dump_json(payload["rows"]),
            dump_json(payload["comparison"]),
            now,
        ),
    )
    return int(cursor.lastrowid)


def list_report_history(conn: sqlite3.Connection, portfolio_id: int | None = None, *, limit: int = 30) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    if portfolio_id:
        where = "WHERE prr.portfolio_id = ?"
        params.append(portfolio_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT prr.*, pg.name AS portfolio_name, prp.name AS profile_name
        FROM portfolio_report_runs prr
        JOIN portfolio_groups pg ON pg.id = prr.portfolio_id
        LEFT JOIN portfolio_report_profiles prp ON prp.id = prr.profile_id
        {where}
        ORDER BY prr.created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_snapshot_result(conn: sqlite3.Connection, snapshot_id: int) -> PortfolioReportResult | None:
    row = conn.execute(
        """
        SELECT prr.*, pg.name AS portfolio_name
        FROM portfolio_report_runs prr
        JOIN portfolio_groups pg ON pg.id = prr.portfolio_id
        WHERE prr.id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    return snapshot_result_from_row(row) if row and row["rows_json"] else None


def snapshot_result_from_row(row: sqlite3.Row) -> PortfolioReportResult:
    config = json.loads(row["config_snapshot_json"] or "{}")
    profile = profile_from_config(config, profile_id=row["profile_id"], portfolio_id=row["portfolio_id"])
    period = build_snapshot_period(row)
    rows = tuple(
        PortfolioReportRow(
            asset_id=item.get("asset_id"),
            values=dict(item.get("values") or {}),
            warnings=tuple(item.get("warnings") or ()),
            severity=str(item.get("severity") or "ok"),
        )
        for item in json.loads(row["rows_json"] or "[]")
    )
    summary_payload = json.loads(row["summary_json"] or "{}")
    coverage_payload = json.loads(row["coverage_json"] or "{}")
    comparison_payload = json.loads(row["comparison_json"] or "null") if "comparison_json" in row.keys() else None
    comparison = (
        None
        if comparison_payload is None
        else PortfolioComparisonResult(
            mode=str(comparison_payload.get("mode") or ""),
            values=dict(comparison_payload.get("values") or {}),
            warnings=tuple(comparison_payload.get("warnings") or ()),
        )
    )
    return PortfolioReportResult(
        portfolio_id=int(row["portfolio_id"]),
        portfolio_name=row["portfolio_name"],
        profile=profile,
        profile_version=int(row["profile_version"] or 1),
        period=period,
        columns=tuple(column for column in profile.columns if column.visible),
        rows=rows,
        summary=PortfolioReportSummary(values=dict(summary_payload.get("values") or {}), warnings=tuple(summary_payload.get("warnings") or ())),
        comparison=comparison,
        coverage=PortfolioDataCoverage(
            global_pct=Decimal(str(coverage_payload.get("global_pct") or 0)),
            by_source={key: Decimal(str(value)) for key, value in (coverage_payload.get("by_source") or {}).items()},
            complete_installations=int(coverage_payload.get("complete_installations") or 0),
            incomplete_installations=int(coverage_payload.get("incomplete_installations") or 0),
            missing_months=tuple(coverage_payload.get("missing_months") or ()),
        ),
        warnings=tuple(json.loads(row["warnings_json"] or "[]")),
        metadata={"snapshot_id": row["id"], "status": row["status"]},
        engine_version=row["engine_version"] or ENGINE_VERSION,
        generated_at=datetime.fromisoformat(row["completed_at"] or row["created_at"]),
    )


def build_snapshot_period(row: sqlite3.Row) -> ReportingPeriod:
    period_type = row["period_type"] or "monthly"
    if period_type == ReportPeriodType.MONTHLY.value:
        return build_period(period_type, report_month=row["report_month"])
    start = date.fromisoformat(row["period_start"])
    if period_type == ReportPeriodType.QUARTERLY.value:
        return build_period(period_type, year=start.year, quarter=((start.month - 1) // 3) + 1)
    if period_type == ReportPeriodType.SEMIANNUAL.value:
        return build_period(period_type, year=start.year, semester=1 if start.month == 1 else 2)
    return build_period(period_type, year=start.year)


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=json_default)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, PortfolioReportColumn):
        return value.__dict__
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
