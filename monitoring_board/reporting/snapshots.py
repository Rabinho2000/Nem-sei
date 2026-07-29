from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from monitoring_board.reporting.quality_gate import QualityGateResult


SNAPSHOT_STATES = {"draft", "validated", "approved", "rejected", "superseded"}


@dataclass(frozen=True)
class ReportSnapshot:
    id: int
    scope_type: str
    asset_id: int | None
    portfolio_id: int | None
    period_start: str
    period_end: str
    payload: dict[str, Any]
    data_hash: str
    validation_status: str
    approval_status: str
    quality_status: str
    blockers: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    template_id: int | None
    template_version: int | None
    profile_id: int | None
    profile_version: int | None
    engine_version: str


def ensure_report_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL CHECK (scope_type IN ('individual', 'portfolio')),
            asset_id INTEGER,
            portfolio_id INTEGER,
            period_type TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            profile_id INTEGER,
            profile_version INTEGER,
            profile_snapshot_json TEXT NOT NULL DEFAULT '{}',
            template_id INTEGER,
            template_version INTEGER,
            template_snapshot_json TEXT NOT NULL DEFAULT '{}',
            billing_snapshot_json TEXT NOT NULL DEFAULT '{}',
            energy_sources_json TEXT NOT NULL DEFAULT '[]',
            source_versions_json TEXT NOT NULL DEFAULT '{}',
            coverage_json TEXT NOT NULL DEFAULT '{}',
            quality_status TEXT NOT NULL DEFAULT 'draft',
            blockers_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            data_hash TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            validation_status TEXT NOT NULL DEFAULT 'draft',
            validated_at TEXT,
            validated_by TEXT,
            approval_status TEXT NOT NULL DEFAULT 'draft',
            approved_at TEXT,
            approved_by TEXT,
            rejection_reason TEXT,
            supersedes_snapshot_id INTEGER,
            superseded_at TEXT,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE RESTRICT,
            FOREIGN KEY (portfolio_id) REFERENCES portfolio_groups(id) ON DELETE RESTRICT,
            FOREIGN KEY (supersedes_snapshot_id) REFERENCES report_snapshots(id) ON DELETE SET NULL,
            CHECK (
                (scope_type = 'individual' AND asset_id IS NOT NULL AND portfolio_id IS NULL)
                OR
                (scope_type = 'portfolio' AND portfolio_id IS NOT NULL AND asset_id IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_report_snapshots_scope_period
            ON report_snapshots(scope_type, asset_id, portfolio_id, period_start, approval_status);
        CREATE TABLE IF NOT EXISTS report_snapshot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT,
            reason TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES report_snapshots(id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_report_snapshot_events_snapshot
            ON report_snapshot_events(snapshot_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS trg_report_snapshot_approved_payload_immutable
        BEFORE UPDATE OF
            payload_json, profile_snapshot_json, template_snapshot_json,
            billing_snapshot_json, energy_sources_json, source_versions_json,
            coverage_json, data_hash, engine_version, period_start, period_end
        ON report_snapshots
        WHEN OLD.approval_status = 'approved'
        BEGIN
            SELECT RAISE(ABORT, 'approved_snapshot_immutable');
        END;
        """
    )


def create_snapshot(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    period_type: str,
    period_start: str,
    period_end: str,
    payload: dict[str, Any],
    engine_version: str,
    asset_id: int | None = None,
    portfolio_id: int | None = None,
    profile_id: int | None = None,
    profile_version: int | None = None,
    profile_snapshot: dict[str, Any] | None = None,
    template_id: int | None = None,
    template_version: int | None = None,
    template_snapshot: dict[str, Any] | None = None,
    billing_snapshot: dict[str, Any] | None = None,
    energy_sources: list[dict[str, Any]] | None = None,
    source_versions: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    created_by: str = "",
    supersedes_snapshot_id: int | None = None,
    now: datetime | None = None,
) -> int:
    ensure_report_snapshot_schema(conn)
    if scope_type not in {"individual", "portfolio"}:
        raise ValueError("invalid_snapshot_scope")
    if (scope_type == "individual") != bool(asset_id):
        raise ValueError("invalid_snapshot_target")
    if (scope_type == "portfolio") != bool(portfolio_id):
        raise ValueError("invalid_snapshot_target")
    envelope = immutable_envelope(
        payload=payload,
        profile_snapshot=profile_snapshot or {},
        template_snapshot=template_snapshot or {},
        billing_snapshot=billing_snapshot or {},
        energy_sources=energy_sources or [],
        source_versions=source_versions or {},
        coverage=coverage or {},
        engine_version=engine_version,
        period_type=period_type,
        period_start=period_start,
        period_end=period_end,
    )
    data_hash = snapshot_hash(envelope)
    current = (now or datetime.now()).isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO report_snapshots (
            scope_type, asset_id, portfolio_id, period_type, period_start,
            period_end, payload_json, profile_id, profile_version,
            profile_snapshot_json, template_id, template_version,
            template_snapshot_json, billing_snapshot_json, energy_sources_json,
            source_versions_json, coverage_json, data_hash, engine_version,
            created_at, created_by, supersedes_snapshot_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scope_type,
            asset_id,
            portfolio_id,
            period_type,
            period_start,
            period_end,
            canonical_json(payload),
            profile_id,
            profile_version,
            canonical_json(profile_snapshot or {}),
            template_id,
            template_version,
            canonical_json(template_snapshot or {}),
            canonical_json(billing_snapshot or {}),
            canonical_json(energy_sources or []),
            canonical_json(source_versions or {}),
            canonical_json(coverage or {}),
            data_hash,
            engine_version,
            current,
            created_by,
            supersedes_snapshot_id,
        ),
    )
    snapshot_id = int(cursor.lastrowid)
    record_event(conn, snapshot_id, "created", created_by, details={"data_hash": data_hash}, now=now)
    return snapshot_id


def validate_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    quality: QualityGateResult,
    *,
    actor: str = "",
    now: datetime | None = None,
) -> str:
    row = require_snapshot_row(conn, snapshot_id)
    assert_snapshot_hash(row)
    if row["approval_status"] == "approved":
        raise ValueError("approved_snapshot_immutable")
    current = (now or datetime.now()).isoformat(timespec="seconds")
    validation_status = "blocked" if quality.blockers else "validated"
    approval_status = "validated" if validation_status == "validated" else "draft"
    conn.execute(
        """
        UPDATE report_snapshots
        SET validation_status = ?, validated_at = ?, validated_by = ?,
            approval_status = ?, quality_status = ?, blockers_json = ?,
            warnings_json = ?
        WHERE id = ?
        """,
        (
            validation_status,
            current,
            actor,
            approval_status,
            quality.status,
            canonical_json([item.as_dict() for item in quality.blockers]),
            canonical_json([item.as_dict() for item in quality.warnings]),
            snapshot_id,
        ),
    )
    record_event(
        conn,
        snapshot_id,
        validation_status,
        actor,
        details={"quality_status": quality.status},
        now=now,
    )
    return validation_status


def approve_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    actor: str,
    now: datetime | None = None,
) -> None:
    row = require_snapshot_row(conn, snapshot_id)
    assert_snapshot_hash(row)
    if row["validation_status"] != "validated" or json.loads(row["blockers_json"] or "[]"):
        raise ValueError("snapshot_approval_blocked")
    current = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE report_snapshots
        SET approval_status = 'approved', approved_at = ?, approved_by = ?,
            rejection_reason = NULL
        WHERE id = ?
        """,
        (current, actor, snapshot_id),
    )
    record_event(conn, snapshot_id, "approved", actor, now=now)


def reject_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    if not reason.strip():
        raise ValueError("snapshot_rejection_reason_required")
    row = require_snapshot_row(conn, snapshot_id)
    if row["approval_status"] == "approved":
        raise ValueError("approved_snapshot_immutable")
    conn.execute(
        """
        UPDATE report_snapshots
        SET approval_status = 'rejected', rejection_reason = ?
        WHERE id = ?
        """,
        (reason.strip(), snapshot_id),
    )
    record_event(conn, snapshot_id, "rejected", actor, reason=reason, now=now)


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> ReportSnapshot | None:
    row = conn.execute("SELECT * FROM report_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return snapshot_from_row(row) if row else None


def list_snapshots(
    conn: sqlite3.Connection,
    *,
    scope_type: str | None = None,
    asset_id: int | None = None,
    portfolio_id: int | None = None,
    period_start: str | None = None,
) -> list[sqlite3.Row]:
    conditions: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("scope_type", scope_type),
        ("asset_id", asset_id),
        ("portfolio_id", portfolio_id),
        ("period_start", period_start),
    ):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return conn.execute(
        f"SELECT * FROM report_snapshots {where} ORDER BY created_at DESC, id DESC",
        params,
    ).fetchall()


def approved_snapshot_for_period(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    period_start: str,
    asset_id: int | None = None,
    portfolio_id: int | None = None,
    template_id: int | None = None,
    profile_id: int | None = None,
) -> ReportSnapshot | None:
    conditions = ["scope_type = ?", "period_start = ?", "approval_status = 'approved'"]
    params: list[Any] = [scope_type, period_start]
    for column, value in (
        ("asset_id", asset_id),
        ("portfolio_id", portfolio_id),
        ("template_id", template_id),
        ("profile_id", profile_id),
    ):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value)
    row = conn.execute(
        f"""
        SELECT * FROM report_snapshots
        WHERE {' AND '.join(conditions)}
        ORDER BY approved_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    assert_snapshot_hash(row)
    return snapshot_from_row(row)


def assert_snapshot_hash(row: sqlite3.Row | dict[str, Any]) -> None:
    envelope = immutable_envelope_from_row(row)
    if snapshot_hash(envelope) != str(row["data_hash"]):
        raise ValueError("snapshot_hash_invalid")


def immutable_envelope_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return immutable_envelope(
        payload=json.loads(row["payload_json"] or "{}"),
        profile_snapshot=json.loads(row["profile_snapshot_json"] or "{}"),
        template_snapshot=json.loads(row["template_snapshot_json"] or "{}"),
        billing_snapshot=json.loads(row["billing_snapshot_json"] or "{}"),
        energy_sources=json.loads(row["energy_sources_json"] or "[]"),
        source_versions=json.loads(row["source_versions_json"] or "{}"),
        coverage=json.loads(row["coverage_json"] or "{}"),
        engine_version=str(row["engine_version"]),
        period_type=str(row["period_type"]),
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
    )


def immutable_envelope(**values: Any) -> dict[str, Any]:
    return values


def snapshot_hash(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    )


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


def snapshot_from_row(row: sqlite3.Row) -> ReportSnapshot:
    assert_snapshot_hash(row)
    return ReportSnapshot(
        id=int(row["id"]),
        scope_type=str(row["scope_type"]),
        asset_id=int(row["asset_id"]) if row["asset_id"] is not None else None,
        portfolio_id=int(row["portfolio_id"]) if row["portfolio_id"] is not None else None,
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        payload=json.loads(row["payload_json"] or "{}"),
        data_hash=str(row["data_hash"]),
        validation_status=str(row["validation_status"]),
        approval_status=str(row["approval_status"]),
        quality_status=str(row["quality_status"]),
        blockers=tuple(json.loads(row["blockers_json"] or "[]")),
        warnings=tuple(json.loads(row["warnings_json"] or "[]")),
        template_id=int(row["template_id"]) if row["template_id"] is not None else None,
        template_version=int(row["template_version"]) if row["template_version"] is not None else None,
        profile_id=int(row["profile_id"]) if row["profile_id"] is not None else None,
        profile_version=int(row["profile_version"]) if row["profile_version"] is not None else None,
        engine_version=str(row["engine_version"]),
    )


def require_snapshot_row(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM report_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError("snapshot_not_found")
    return row


def record_event(
    conn: sqlite3.Connection,
    snapshot_id: int,
    event_type: str,
    actor: str = "",
    *,
    reason: str = "",
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO report_snapshot_events (
            snapshot_id, event_type, actor, reason, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            event_type,
            actor,
            reason,
            canonical_json(details or {}),
            (now or datetime.now()).isoformat(timespec="seconds"),
        ),
    )
