from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from monitoring_board.reporting_storage import reconcile_generated_reports
from monitoring_board.runtime import UPLOAD_DIR

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")
DISTRIBUTION_STATES = {"draft", "ready_to_send", "approved_to_send", "queued", "sent", "failed", "cancelled"}


def ensure_distribution_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER, asset_id INTEGER, portfolio_id INTEGER,
            name TEXT NOT NULL, email TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE RESTRICT,
            FOREIGN KEY (portfolio_id) REFERENCES portfolio_groups(id) ON DELETE RESTRICT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_report_recipients_email_scope
            ON report_recipients(lower(email), ifnull(customer_id, 0), ifnull(asset_id, 0), ifnull(portfolio_id, 0));
        CREATE TABLE IF NOT EXISTS report_distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_file_id INTEGER NOT NULL, snapshot_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL, channel TEXT NOT NULL DEFAULT 'email',
            status TEXT NOT NULL DEFAULT 'draft',
            approved_by TEXT, approved_at TEXT, queued_at TEXT, sent_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY (generated_file_id) REFERENCES report_generated_files(id) ON DELETE RESTRICT,
            FOREIGN KEY (snapshot_id) REFERENCES report_snapshots(id) ON DELETE RESTRICT,
            FOREIGN KEY (recipient_id) REFERENCES report_recipients(id) ON DELETE RESTRICT,
            UNIQUE(generated_file_id, recipient_id, channel)
        );
        CREATE INDEX IF NOT EXISTS idx_report_distributions_status ON report_distributions(status, created_at);
        CREATE TABLE IF NOT EXISTS report_distribution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL, event_type TEXT NOT NULL,
            actor TEXT, details TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY (distribution_id) REFERENCES report_distributions(id) ON DELETE RESTRICT
        );
        """
    )


def create_recipient(conn: sqlite3.Connection, *, name: str, email: str, customer_id: int | None = None, asset_id: int | None = None, portfolio_id: int | None = None) -> int:
    name, email = name.strip(), email.strip().casefold()
    if not name:
        raise ValueError("recipient_name_required")
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("invalid_recipient_email")
    if sum(value is not None for value in (customer_id, asset_id, portfolio_id)) != 1:
        raise ValueError("recipient_scope_required")
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """INSERT INTO report_recipients
           (customer_id, asset_id, portfolio_id, name, email, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, asset_id, portfolio_id, name, email, now, now),
    )
    return int(cursor.lastrowid)


def create_distribution(conn: sqlite3.Connection, *, generated_file_id: int, recipient_id: int, actor: str = "", storage_root: Path | None = None) -> int:
    row = conn.execute(
        """SELECT f.*, s.approval_status, r.active AS recipient_active
           FROM report_generated_files f
           JOIN report_snapshots s ON s.id = f.snapshot_id
           JOIN report_recipients r ON r.id = ?
           WHERE f.id = ?""",
        (recipient_id, generated_file_id),
    ).fetchone()
    if row is None or row["status"] != "completed":
        raise ValueError("generated_file_not_available")
    if row["approval_status"] != "approved":
        raise ValueError("snapshot_not_approved")
    if not row["recipient_active"]:
        raise ValueError("recipient_inactive")
    findings = reconcile_generated_reports(conn, root=storage_root or (UPLOAD_DIR / "generated_reports"))
    if not any(item.file_id == generated_file_id and item.status == "ok" for item in findings):
        raise ValueError("generated_file_integrity_failed")
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """INSERT INTO report_distributions
           (generated_file_id, snapshot_id, recipient_id, channel, status, created_at, updated_at)
           VALUES (?, ?, ?, 'email', 'ready_to_send', ?, ?)
           ON CONFLICT(generated_file_id, recipient_id, channel) DO NOTHING""",
        (generated_file_id, int(row["snapshot_id"]), recipient_id, now, now),
    )
    if cursor.rowcount:
        distribution_id = int(cursor.lastrowid)
        record_distribution_event(conn, distribution_id, "prepared", actor)
        return distribution_id
    existing = conn.execute(
        "SELECT id FROM report_distributions WHERE generated_file_id = ? AND recipient_id = ? AND channel = 'email'",
        (generated_file_id, recipient_id),
    ).fetchone()
    return int(existing["id"])


def transition_distribution(conn: sqlite3.Connection, distribution_id: int, target: str, *, actor: str = "", error_message: str = "") -> None:
    row = conn.execute("SELECT * FROM report_distributions WHERE id = ?", (distribution_id,)).fetchone()
    if row is None:
        raise ValueError("distribution_not_found")
    allowed = {
        "draft": {"ready_to_send", "cancelled"},
        "ready_to_send": {"approved_to_send", "cancelled"},
        "approved_to_send": {"cancelled"},
        "failed": {"ready_to_send", "cancelled"},
    }
    if target not in DISTRIBUTION_STATES or target not in allowed.get(row["status"], set()):
        raise ValueError("invalid_distribution_transition")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """UPDATE report_distributions
           SET status = ?, approved_at = ?, approved_by = ?, attempt_count = ?,
               error_message = ?, updated_at = ? WHERE id = ?""",
        (
            target,
            now if target == "approved_to_send" else row["approved_at"],
            actor if target == "approved_to_send" else row["approved_by"],
            int(row["attempt_count"] or 0) + (1 if row["status"] == "failed" else 0),
            error_message,
            now,
            distribution_id,
        ),
    )
    record_distribution_event(conn, distribution_id, target, actor, error_message)


def record_distribution_event(conn: sqlite3.Connection, distribution_id: int, event_type: str, actor: str = "", details: str = "") -> None:
    conn.execute(
        """INSERT INTO report_distribution_events
           (distribution_id, event_type, actor, details, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (distribution_id, event_type, actor, details[:500], datetime.now().isoformat(timespec="seconds")),
    )
