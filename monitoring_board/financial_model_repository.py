from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from monitoring_board.db import query_all


MODEL_STATUSES = {"preview", "confirmed", "archived", "cancelled", "failed"}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_financial_model_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            base_year INTEGER NOT NULL,
            version INTEGER,
            status TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            detected_name TEXT,
            detected_nif TEXT,
            detected_kwp REAL,
            parser_name TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            warnings_json TEXT,
            validation_json TEXT,
            details_json TEXT,
            override_reason TEXT,
            confirmed_at TEXT,
            archived_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE,
            CHECK (status IN ('preview', 'confirmed', 'archived', 'cancelled', 'failed')),
            CHECK (active IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS financial_model_monthly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            financial_model_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            base_year INTEGER NOT NULL,
            month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
            expected_production_kwh REAL,
            expected_consumption_kwh REAL,
            expected_self_use_kwh REAL,
            expected_export_kwh REAL,
            expected_grid_import_kwh REAL,
            expected_self_consumption_rate_pct REAL,
            expected_self_sufficiency_rate_pct REAL,
            source_fields_json TEXT,
            calculated_fields_json TEXT,
            warnings_json TEXT,
            UNIQUE(financial_model_id, month),
            FOREIGN KEY (financial_model_id) REFERENCES financial_models(id) ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_financial_models_asset_year
            ON financial_models(asset_id, base_year, status, active);
        CREATE INDEX IF NOT EXISTS idx_financial_models_source
            ON financial_models(source_file_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_models_asset_hash
            ON financial_models(asset_id, file_sha256);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_models_asset_year_version
            ON financial_models(asset_id, base_year, version)
            WHERE version IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_models_one_active
            ON financial_models(asset_id, base_year)
            WHERE active = 1 AND status = 'confirmed' AND archived_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_financial_model_monthly_asset_year_month
            ON financial_model_monthly(asset_id, base_year, month);
        """
    )
    _ensure_column(conn, "financial_models", "details_json TEXT")


def _ensure_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    existing = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def json_value(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def get_asset(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()


def get_model(conn: sqlite3.Connection, model_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM financial_models WHERE id = ?", (model_id,)).fetchone()


def get_asset_model(conn: sqlite3.Connection, *, asset_id: int, model_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM financial_models WHERE id = ? AND asset_id = ?",
        (model_id, asset_id),
    ).fetchone()


def get_model_source(conn: sqlite3.Connection, model_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT sf.*
        FROM source_files sf
        JOIN financial_models fm ON fm.source_file_id = sf.id
        WHERE fm.id = ?
        """,
        (model_id,),
    ).fetchone()


def find_model_by_hash(conn: sqlite3.Connection, *, asset_id: int, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM financial_models WHERE asset_id = ? AND file_sha256 = ? ORDER BY id DESC LIMIT 1",
        (asset_id, sha256),
    ).fetchone()


def next_version(conn: sqlite3.Connection, *, asset_id: int, base_year: int) -> int:
    value = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM financial_models WHERE asset_id = ? AND base_year = ?",
        (asset_id, base_year),
    ).fetchone()[0]
    return int(value or 1)


def create_preview_model(
    conn: sqlite3.Connection,
    *,
    source_file_id: int,
    asset_id: int,
    base_year: int,
    detected_name: str,
    detected_nif: str,
    detected_kwp: float | None,
    parser_name: str,
    parser_version: str,
    file_sha256: str,
    warnings: list[str],
    validation: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> int:
    current = now_text()
    cursor = conn.execute(
        """
        INSERT INTO financial_models (
            source_file_id, asset_id, base_year, version, status, active,
            detected_name, detected_nif, detected_kwp, parser_name, parser_version,
            file_sha256, warnings_json, validation_json, details_json, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, 'preview', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_file_id,
            asset_id,
            base_year,
            detected_name,
            detected_nif,
            detected_kwp,
            parser_name,
            parser_version,
            file_sha256,
            json_text(warnings),
            json_text(validation),
            json_text(details or {}),
            current,
            current,
        ),
    )
    return int(cursor.lastrowid)


def update_model_parse_details(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    warnings: list[str],
    validation: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE financial_models
        SET warnings_json = ?, validation_json = ?, details_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (json_text(warnings), json_text(validation), json_text(details or {}), now_text(), model_id),
    )


def replace_monthly_rows(conn: sqlite3.Connection, *, model_id: int, asset_id: int, base_year: int, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM financial_model_monthly WHERE financial_model_id = ?", (model_id,))
    for row in rows:
        conn.execute(
            """
            INSERT INTO financial_model_monthly (
                financial_model_id, asset_id, base_year, month,
                expected_production_kwh, expected_consumption_kwh, expected_self_use_kwh,
                expected_export_kwh, expected_grid_import_kwh,
                expected_self_consumption_rate_pct, expected_self_sufficiency_rate_pct,
                source_fields_json, calculated_fields_json, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                asset_id,
                base_year,
                row["month"],
                row.get("expected_production_kwh"),
                row.get("expected_consumption_kwh"),
                row.get("expected_self_use_kwh"),
                row.get("expected_export_kwh"),
                row.get("expected_grid_import_kwh"),
                row.get("expected_self_consumption_rate_pct"),
                row.get("expected_self_sufficiency_rate_pct"),
                json_text(row.get("source_fields") or {}),
                json_text(row.get("calculated_fields") or {}),
                json_text(row.get("warnings") or []),
            ),
        )


def confirm_model(conn: sqlite3.Connection, *, model_id: int, override_reason: str = "") -> int:
    model = get_model(conn, model_id)
    if model is None:
        raise ValueError("financial_model_not_found")
    if model["status"] != "preview":
        raise ValueError("financial_model_not_preview")
    version = next_version(conn, asset_id=int(model["asset_id"]), base_year=int(model["base_year"]))
    current = now_text()
    conn.execute(
        """
        UPDATE financial_models
        SET active = 0, updated_at = ?
        WHERE asset_id = ? AND base_year = ? AND active = 1
        """,
        (current, model["asset_id"], model["base_year"]),
    )
    conn.execute(
        """
        UPDATE financial_models
        SET status = 'confirmed', active = 1, version = ?, override_reason = ?,
            confirmed_at = ?, updated_at = ?
        WHERE id = ? AND status = 'preview'
        """,
        (version, override_reason.strip(), current, current, model_id),
    )
    return version


def cancel_model(conn: sqlite3.Connection, *, model_id: int) -> None:
    conn.execute(
        "UPDATE financial_models SET status = 'cancelled', active = 0, updated_at = ? WHERE id = ? AND status = 'preview'",
        (now_text(), model_id),
    )


def archive_model(conn: sqlite3.Connection, *, model_id: int) -> None:
    current = now_text()
    conn.execute(
        """
        UPDATE financial_models
        SET status = 'archived', active = 0, archived_at = ?, updated_at = ?
        WHERE id = ? AND status IN ('preview', 'confirmed')
        """,
        (current, current, model_id),
    )


def activate_model(conn: sqlite3.Connection, *, model_id: int) -> None:
    model = get_model(conn, model_id)
    if model is None:
        raise ValueError("financial_model_not_found")
    if model["status"] != "confirmed" or model["archived_at"]:
        raise ValueError("financial_model_not_activatable")
    current = now_text()
    conn.execute(
        "UPDATE financial_models SET active = 0, updated_at = ? WHERE asset_id = ? AND base_year = ?",
        (current, model["asset_id"], model["base_year"]),
    )
    conn.execute("UPDATE financial_models SET active = 1, updated_at = ? WHERE id = ?", (current, model_id))


def active_model_for_month(conn: sqlite3.Connection, *, asset_id: int | None, year: int, month: int) -> sqlite3.Row | None:
    if asset_id is None:
        return None
    return conn.execute(
        """
        SELECT fm.*, fmm.*
        FROM financial_models fm
        JOIN financial_model_monthly fmm ON fmm.financial_model_id = fm.id
        WHERE fm.asset_id = ?
          AND fm.base_year = ?
          AND fmm.month = ?
          AND fm.status = 'confirmed'
          AND fm.active = 1
          AND fm.archived_at IS NULL
        LIMIT 1
        """,
        (asset_id, year, month),
    ).fetchone()


def list_asset_models(conn: sqlite3.Connection, *, asset_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT fm.*, sf.original_filename, sf.uploaded_at
        FROM financial_models fm
        JOIN source_files sf ON sf.id = fm.source_file_id
        WHERE fm.asset_id = ?
        ORDER BY fm.base_year DESC, COALESCE(fm.version, 0) DESC, fm.id DESC
        """,
        (asset_id,),
    )


def get_active_model(conn: sqlite3.Connection, *, asset_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT fm.*, sf.original_filename, sf.uploaded_at
        FROM financial_models fm
        JOIN source_files sf ON sf.id = fm.source_file_id
        WHERE fm.asset_id = ? AND fm.status = 'confirmed' AND fm.active = 1 AND fm.archived_at IS NULL
        ORDER BY fm.base_year DESC, fm.id DESC
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()


def list_model_monthly(conn: sqlite3.Connection, *, model_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        "SELECT * FROM financial_model_monthly WHERE financial_model_id = ? ORDER BY month",
        (model_id,),
    )


def model_warnings(model: sqlite3.Row | dict[str, Any] | None) -> list[str]:
    if model is None:
        return []
    return list(json_value(model["warnings_json"], []))


def model_validation(model: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if model is None:
        return {}
    return dict(json_value(model["validation_json"], {}))


def model_details(model: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if model is None:
        return {}
    if "details_json" not in model.keys():
        return {}
    return dict(json_value(model["details_json"], {}))
