from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from monitoring_board.reporting.templates import DEFAULT_TEMPLATE_NAMES, ReportTemplate, default_template, template_from_config, template_to_config, validate_template


def ensure_report_template_schema(conn: sqlite3.Connection) -> None:
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
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES report_generation_runs(id) ON DELETE CASCADE
        );
        """
    )
    seed_default_templates(conn)


def seed_default_templates(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("SELECT name FROM report_templates WHERE portfolio_id IS NULL").fetchall()}
    for name in DEFAULT_TEMPLATE_NAMES:
        if name not in existing:
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
        WHERE active = 1 AND report_type = ? AND is_default = 1 AND (portfolio_id IS NULL OR portfolio_id = ?)
        ORDER BY portfolio_id IS NOT NULL DESC, id
        LIMIT 1
        """,
        (report_type, portfolio_id),
    ).fetchone()
    return template_from_row(row) if row else default_template("Portfolio executivo" if report_type == "portfolio" else "Individual padrao", portfolio_id=portfolio_id)


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


def create_generation_run(conn: sqlite3.Connection, *, template_id: int | None, template_version: int, report_type: str, portfolio_id: int | None = None, asset_id: int | None = None, snapshot_id: int | None = None, period_type: str = "", period_start: str = "", period_end: str = "", requested_count: int = 0) -> int:
    cursor = conn.execute(
        """
        INSERT INTO report_generation_runs (
            template_id, template_version, report_type, portfolio_id, asset_id, snapshot_id,
            period_type, period_start, period_end, status, requested_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (template_id, template_version, report_type, portfolio_id, asset_id, snapshot_id, period_type, period_start, period_end, requested_count, datetime.now().isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def finish_generation_run(conn: sqlite3.Connection, run_id: int, *, status: str, completed_count: int, failed_count: int, warnings: list[str] | None = None, error_message: str = "") -> None:
    conn.execute(
        """
        UPDATE report_generation_runs
        SET status = ?, completed_count = ?, failed_count = ?, warnings_json = ?, error_message = ?, completed_at = ?
        WHERE id = ?
        """,
        (status, completed_count, failed_count, json.dumps(warnings or [], ensure_ascii=True), error_message, datetime.now().isoformat(timespec="seconds"), run_id),
    )


def add_generated_file(conn: sqlite3.Connection, *, run_id: int, fmt: str, filename: str, relative_path: str, sha256: str, size_bytes: int, portfolio_id: int | None = None, asset_id: int | None = None, snapshot_id: int | None = None, status: str = "completed", error_message: str = "") -> int:
    cursor = conn.execute(
        """
        INSERT INTO report_generated_files (
            run_id, asset_id, portfolio_id, snapshot_id, format, filename, relative_path,
            sha256, size_bytes, status, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, asset_id, portfolio_id, snapshot_id, fmt, filename, relative_path, sha256, size_bytes, status, error_message, datetime.now().isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def list_generation_runs(conn: sqlite3.Connection, *, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, t.name AS template_name
        FROM report_generation_runs r
        LEFT JOIN report_templates t ON t.id = r.template_id
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_generated_files(conn: sqlite3.Connection, run_id: int | None = None) -> list[sqlite3.Row]:
    if run_id:
        return conn.execute("SELECT * FROM report_generated_files WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return conn.execute("SELECT * FROM report_generated_files ORDER BY created_at DESC LIMIT 50").fetchall()


def get_generated_file(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report_generated_files WHERE id = ?", (file_id,)).fetchone()
