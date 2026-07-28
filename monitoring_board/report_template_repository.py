from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from monitoring_board.db import ensure_column
from monitoring_board.reporting.templates import DEFAULT_TEMPLATE_NAMES, ReportTemplate, default_template, template_from_config, template_to_config, validate_template


def ensure_report_template_schema(conn: sqlite3.Connection) -> None:
    automation_columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info(report_automations)").fetchall()
    }
    migrate_legacy_automations = bool(
        automation_columns
        and "enabled" in automation_columns
        and "template_id" not in automation_columns
    )
    if migrate_legacy_automations:
        conn.execute("ALTER TABLE report_automations RENAME TO report_automations_legacy")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            report_type TEXT NOT NULL,
            portfolio_id INTEGER,
            client_key TEXT DEFAULT '',
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            is_default INTEGER DEFAULT 0,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (portfolio_id) REFERENCES portfolio_groups(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS report_template_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(template_id, version),
            FOREIGN KEY (template_id) REFERENCES report_templates(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS report_generation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER,
            template_version INTEGER,
            report_type TEXT NOT NULL,
            portfolio_id INTEGER,
            asset_id INTEGER,
            snapshot_id INTEGER,
            period_type TEXT,
            period_start TEXT,
            period_end TEXT,
            status TEXT NOT NULL,
            requested_count INTEGER DEFAULT 0,
            completed_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            warnings_json TEXT DEFAULT '[]',
            error_message TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (template_id) REFERENCES report_templates(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS report_generated_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            asset_id INTEGER,
            portfolio_id INTEGER,
            snapshot_id INTEGER,
            format TEXT NOT NULL,
            filename TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            period_type TEXT,
            period_start TEXT,
            period_end TEXT,
            is_auxiliary INTEGER DEFAULT 0,
            warnings_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES report_generation_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_report_templates_type_scope
            ON report_templates(report_type, portfolio_id, active, is_default);
        CREATE INDEX IF NOT EXISTS idx_report_generation_runs_created
            ON report_generation_runs(created_at DESC, status);
        CREATE INDEX IF NOT EXISTS idx_report_generated_files_run
            ON report_generated_files(run_id, status, is_auxiliary);
        CREATE INDEX IF NOT EXISTS idx_report_generated_files_asset_period
            ON report_generated_files(asset_id, period_start, format);

        CREATE TABLE IF NOT EXISTS report_automations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            report_type TEXT NOT NULL,
            asset_id INTEGER,
            portfolio_id INTEGER,
            template_id INTEGER NOT NULL,
            profile_id INTEGER,
            schedule_day INTEGER NOT NULL,
            schedule_time TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Europe/Lisbon',
            target_period TEXT NOT NULL DEFAULT 'previous_closed_month',
            formats_json TEXT NOT NULL DEFAULT '["pdf"]',
            include_availability INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE,
            FOREIGN KEY (portfolio_id) REFERENCES portfolio_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (template_id) REFERENCES report_templates(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_report_automations_active
            ON report_automations(active, schedule_day, schedule_time);
        """
    )
    ensure_column(conn, "report_generation_runs", "skipped_count INTEGER DEFAULT 0")
    ensure_column(conn, "report_generated_files", "period_type TEXT")
    ensure_column(conn, "report_generated_files", "period_start TEXT")
    ensure_column(conn, "report_generated_files", "period_end TEXT")
    ensure_column(conn, "report_generated_files", "is_auxiliary INTEGER DEFAULT 0")
    ensure_column(conn, "report_generated_files", "warnings_json TEXT DEFAULT '[]'")
    ensure_column(conn, "report_generation_runs", "automation_id INTEGER")
    ensure_column(conn, "report_automations", "name TEXT DEFAULT ''")
    ensure_column(conn, "report_automations", "active INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "report_automations", "report_type TEXT DEFAULT 'individual'")
    ensure_column(conn, "report_automations", "portfolio_id INTEGER")
    ensure_column(conn, "report_automations", "template_id INTEGER")
    ensure_column(conn, "report_automations", "profile_id INTEGER")
    ensure_column(conn, "report_automations", "schedule_day INTEGER NOT NULL DEFAULT 2")
    ensure_column(conn, "report_automations", "schedule_time TEXT NOT NULL DEFAULT '09:00'")
    ensure_column(conn, "report_automations", "timezone TEXT NOT NULL DEFAULT 'Europe/Lisbon'")
    ensure_column(conn, "report_automations", "target_period TEXT NOT NULL DEFAULT 'previous_closed_month'")
    ensure_column(conn, "report_automations", "formats_json TEXT NOT NULL DEFAULT '[\"pdf\"]'")
    ensure_column(conn, "report_automations", "include_availability INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_report_generation_automation_period_active
        ON report_generation_runs(automation_id, period_start)
        WHERE automation_id IS NOT NULL AND status IN ('running', 'completed')
        """
    )
    seed_default_templates(conn)
    if migrate_legacy_automations:
        default_row = conn.execute(
            """
            SELECT id FROM report_templates
            WHERE report_type = 'individual' AND active = 1
            ORDER BY is_default DESC, id LIMIT 1
            """
        ).fetchone()
        if default_row:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO report_automations (
                    name, active, report_type, asset_id, template_id, schedule_day,
                    schedule_time, timezone, target_period, formats_json,
                    include_availability, created_at, updated_at
                )
                SELECT COALESCE(a.project_name, 'Relatório mensal ' || legacy.asset_id),
                       legacy.enabled, 'individual', legacy.asset_id, ?, 2, '09:00',
                       'Europe/Lisbon', 'previous_closed_month', '["pdf"]', 0,
                       legacy.created_at, COALESCE(legacy.updated_at, ?)
                FROM report_automations_legacy legacy
                LEFT JOIN assets a ON a.id = legacy.asset_id
                """,
                (int(default_row["id"]), now),
            )
        conn.execute("DROP TABLE report_automations_legacy")


def seed_default_templates(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM report_templates LIMIT 1").fetchone() is not None:
        return
    for name in DEFAULT_TEMPLATE_NAMES:
        template = default_template(name)
        save_template(conn, template, is_default=1 if template.is_default else 0)


def list_templates(conn: sqlite3.Connection, report_type: str | None = None, portfolio_id: int | None = None, *, include_inactive: bool = False) -> list[sqlite3.Row]:
    where = ["1 = 1"]
    params: list[Any] = []
    if report_type:
        where.append("report_type = ?")
        params.append(report_type)
    if portfolio_id:
        where.append("(portfolio_id IS NULL OR portfolio_id = ?)")
        params.append(portfolio_id)
    if not include_inactive:
        where.append("active = 1")
    return conn.execute(
        f"SELECT * FROM report_templates WHERE {' AND '.join(where)} ORDER BY is_default DESC, report_type, name COLLATE NOCASE",
        params,
    ).fetchall()


def get_template(conn: sqlite3.Connection, template_id: int) -> ReportTemplate | None:
    row = conn.execute("SELECT * FROM report_templates WHERE id = ?", (template_id,)).fetchone()
    return template_from_row(row) if row else None


def get_default_template(conn: sqlite3.Connection, report_type: str, portfolio_id: int | None = None) -> ReportTemplate:
    row = conn.execute(
        """
        SELECT *
        FROM report_templates
        WHERE active = 1 AND report_type = ? AND (portfolio_id IS NULL OR portfolio_id = ?)
        ORDER BY is_default DESC, portfolio_id IS NOT NULL DESC, id
        LIMIT 1
        """,
        (report_type, portfolio_id),
    ).fetchone()
    if row is None:
        raise ValueError("active_report_template_required")
    return template_from_row(row)


def save_template(conn: sqlite3.Connection, template: ReportTemplate, *, active: int = 1, is_default: int = 0) -> int:
    template = validate_template(template)
    now = datetime.now().isoformat(timespec="seconds")
    config_json = json.dumps(template_to_config(template), ensure_ascii=True, sort_keys=True)
    if len(config_json) > 30000:
        raise ValueError("template_config_too_large")
    if template.id:
        existing = conn.execute("SELECT portfolio_id FROM report_templates WHERE id = ?", (template.id,)).fetchone()
        if existing is None:
            raise ValueError("template_not_found")
        if existing["portfolio_id"] != template.portfolio_id:
            raise ValueError("template_scope_change_forbidden")
        conn.execute(
            """
            UPDATE report_templates
            SET name = ?, report_type = ?, client_key = ?, description = ?, active = ?,
                is_default = ?, config_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (template.name, template.report_type, template.client_key, template.description, active, is_default, config_json, now, template.id),
        )
        template_id = int(template.id)
    else:
        cursor = conn.execute(
            """
            INSERT INTO report_templates
                (name, report_type, portfolio_id, client_key, description, active, is_default, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (template.name, template.report_type, template.portfolio_id, template.client_key, template.description, active, is_default, config_json, now, now),
        )
        template_id = int(cursor.lastrowid)
    if is_default:
        set_default_template(conn, template_id)
    create_template_version(conn, template_id, template)
    return template_id


def duplicate_template(conn: sqlite3.Connection, template_id: int, name: str) -> int:
    template = get_template(conn, template_id)
    if template is None:
        raise ValueError("template_not_found")
    return save_template(conn, replace(template, id=None, name=name or f"Copia {template.name}"), is_default=0)


def archive_template(conn: sqlite3.Connection, template_id: int) -> None:
    row = conn.execute("SELECT is_default FROM report_templates WHERE id = ?", (template_id,)).fetchone()
    if row and row["is_default"]:
        raise ValueError("cannot_archive_default_template")
    conn.execute("UPDATE report_templates SET active = 0, updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), template_id))


def set_default_template(conn: sqlite3.Connection, template_id: int) -> None:
    row = conn.execute("SELECT report_type, portfolio_id FROM report_templates WHERE id = ? AND active = 1", (template_id,)).fetchone()
    if row is None:
        raise ValueError("template_not_found")
    if row["portfolio_id"] is None:
        conn.execute("UPDATE report_templates SET is_default = 0 WHERE report_type = ? AND portfolio_id IS NULL", (row["report_type"],))
    else:
        conn.execute("UPDATE report_templates SET is_default = 0 WHERE report_type = ? AND portfolio_id = ?", (row["report_type"], row["portfolio_id"]))
    conn.execute("UPDATE report_templates SET is_default = 1, updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), template_id))


def template_from_row(row: sqlite3.Row) -> ReportTemplate:
    template = template_from_config(json.loads(row["config_json"] or "{}"), template_id=int(row["id"]), portfolio_id=row["portfolio_id"])
    return replace(template, active=bool(row["active"]), is_default=bool(row["is_default"]))


def create_template_version(conn: sqlite3.Connection, template_id: int, template: ReportTemplate) -> int:
    version = latest_template_version(conn, template_id) + 1
    conn.execute(
        "INSERT OR IGNORE INTO report_template_versions (template_id, version, config_json, created_at) VALUES (?, ?, ?, ?)",
        (template_id, version, json.dumps(template_to_config(template), ensure_ascii=True, sort_keys=True), datetime.now().isoformat(timespec="seconds")),
    )
    return version


def latest_template_version(conn: sqlite3.Connection, template_id: int | None) -> int:
    if not template_id:
        return 1
    row = conn.execute("SELECT MAX(version) AS version FROM report_template_versions WHERE template_id = ?", (template_id,)).fetchone()
    return int(row["version"] or 0) if row else 0


def create_generation_run(conn: sqlite3.Connection, *, template_id: int | None, template_version: int, report_type: str, portfolio_id: int | None = None, asset_id: int | None = None, snapshot_id: int | None = None, automation_id: int | None = None, period_type: str = "", period_start: str = "", period_end: str = "", requested_count: int = 0) -> int:
    cursor = conn.execute(
        """
        INSERT INTO report_generation_runs (
            template_id, template_version, report_type, portfolio_id, asset_id, snapshot_id,
            automation_id, period_type, period_start, period_end, status, requested_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (template_id, template_version, report_type, portfolio_id, asset_id, snapshot_id, automation_id, period_type, period_start, period_end, requested_count, datetime.now().isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def finish_generation_run(conn: sqlite3.Connection, run_id: int, *, status: str, completed_count: int, failed_count: int, skipped_count: int = 0, warnings: list[str] | None = None, error_message: str = "") -> None:
    if status not in {"running", "completed", "partial", "failed"}:
        raise ValueError("invalid_generation_run_status")
    conn.execute(
        """
        UPDATE report_generation_runs
        SET status = ?, completed_count = ?, failed_count = ?, skipped_count = ?,
            warnings_json = ?, error_message = ?, completed_at = ?
        WHERE id = ?
        """,
        (status, completed_count, failed_count, skipped_count, json.dumps(warnings or [], ensure_ascii=True), error_message, datetime.now().isoformat(timespec="seconds"), run_id),
    )


def add_generated_file(conn: sqlite3.Connection, *, run_id: int, fmt: str, filename: str, relative_path: str, sha256: str, size_bytes: int, portfolio_id: int | None = None, asset_id: int | None = None, snapshot_id: int | None = None, period_type: str = "", period_start: str = "", period_end: str = "", is_auxiliary: int = 0, warnings: list[str] | None = None, status: str = "completed", error_message: str = "") -> int:
    if status not in {"completed", "failed", "skipped"}:
        raise ValueError("invalid_generated_file_status")
    cursor = conn.execute(
        """
        INSERT INTO report_generated_files (
            run_id, asset_id, portfolio_id, snapshot_id, format, filename, relative_path,
            sha256, size_bytes, status, error_message, period_type, period_start, period_end,
            is_auxiliary, warnings_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            asset_id,
            portfolio_id,
            snapshot_id,
            fmt,
            filename,
            relative_path,
            sha256,
            size_bytes,
            status,
            error_message,
            period_type,
            period_start,
            period_end,
            is_auxiliary,
            json.dumps(warnings or [], ensure_ascii=True),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return int(cursor.lastrowid)


def list_generation_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    offset: int = 0,
    automation_id: int | None = None,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, t.name AS template_name
        FROM report_generation_runs r
        LEFT JOIN report_templates t ON t.id = r.template_id
        WHERE (? IS NULL OR r.automation_id = ?)
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT ? OFFSET ?
        """,
        (automation_id, automation_id, limit, offset),
    ).fetchall()


def list_generated_files(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    automation_id: int | None = None,
) -> list[sqlite3.Row]:
    if run_id:
        return conn.execute("SELECT * FROM report_generated_files WHERE run_id = ? ORDER BY id LIMIT ? OFFSET ?", (run_id, limit, offset)).fetchall()
    return conn.execute(
        """
        SELECT f.*
        FROM report_generated_files f
        JOIN report_generation_runs r ON r.id = f.run_id
        WHERE (? IS NULL OR r.automation_id = ?)
        ORDER BY f.created_at DESC, f.id DESC
        LIMIT ? OFFSET ?
        """,
        (automation_id, automation_id, limit, offset),
    ).fetchall()


def get_generated_file(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report_generated_files WHERE id = ?", (file_id,)).fetchone()


def list_report_automations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ra.*, a.project_name AS asset_name, pg.name AS portfolio_name,
               rt.name AS template_name, last_run.id AS last_run_id,
               last_run.status AS last_status, last_run.created_at AS last_run_at,
               last_file.id AS last_file_id
        FROM report_automations ra
        LEFT JOIN assets a ON a.id = ra.asset_id
        LEFT JOIN portfolio_groups pg ON pg.id = ra.portfolio_id
        LEFT JOIN report_templates rt ON rt.id = ra.template_id
        LEFT JOIN report_generation_runs last_run ON last_run.id = (
            SELECT r.id FROM report_generation_runs r
            WHERE r.automation_id = ra.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1
        )
        LEFT JOIN report_generated_files last_file ON last_file.id = (
            SELECT f.id FROM report_generated_files f
            JOIN report_generation_runs r ON r.id = f.run_id
            WHERE r.automation_id = ra.id AND f.status = 'completed' AND f.format = 'pdf'
            ORDER BY f.created_at DESC, f.id DESC LIMIT 1
        )
        ORDER BY ra.active DESC, ra.name COLLATE NOCASE, ra.id
        """
    ).fetchall()


def list_enabled_report_automations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM report_automations WHERE active = 1 ORDER BY id").fetchall()


def get_report_automation(conn: sqlite3.Connection, automation_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report_automations WHERE id = ?", (automation_id,)).fetchone()


def save_report_automation(
    conn: sqlite3.Connection,
    *,
    automation_id: int | None,
    name: str,
    active: int,
    report_type: str,
    asset_id: int | None,
    portfolio_id: int | None,
    template_id: int,
    profile_id: int | None,
    schedule_day: int,
    schedule_time: str,
    formats: list[str],
    include_availability: int,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    values = (
        name,
        active,
        report_type,
        asset_id,
        portfolio_id,
        template_id,
        profile_id,
        schedule_day,
        schedule_time,
        "Europe/Lisbon",
        "previous_closed_month",
        json.dumps(formats, ensure_ascii=True),
        include_availability,
        now,
    )
    if automation_id:
        conn.execute(
            """
            UPDATE report_automations
            SET name = ?, active = ?, report_type = ?, asset_id = ?, portfolio_id = ?,
                template_id = ?, profile_id = ?, schedule_day = ?, schedule_time = ?,
                timezone = ?, target_period = ?, formats_json = ?,
                include_availability = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, automation_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("automation_not_found")
        return automation_id
    cursor = conn.execute(
        """
        INSERT INTO report_automations (
            name, active, report_type, asset_id, portfolio_id, template_id, profile_id,
            schedule_day, schedule_time, timezone, target_period, formats_json,
            include_availability, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values[:-1], now, now),
    )
    return int(cursor.lastrowid)


def set_report_automation_active(conn: sqlite3.Connection, automation_id: int, active: int) -> None:
    cursor = conn.execute(
        "UPDATE report_automations SET active = ?, updated_at = ? WHERE id = ?",
        (active, datetime.now().isoformat(timespec="seconds"), automation_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("automation_not_found")


def delete_report_automation(conn: sqlite3.Connection, automation_id: int) -> None:
    cursor = conn.execute("DELETE FROM report_automations WHERE id = ?", (automation_id,))
    if cursor.rowcount != 1:
        raise ValueError("automation_not_found")
