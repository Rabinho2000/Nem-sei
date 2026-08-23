"""Core, dependency-free FusionSolar V1 account-lease protocol.

Extracted from `fusionsolar_ownership_window.py` so the exact same
acquire/release logic can be imported both by the host-side CLI
(`fusionsolar_ownership_window.py`) and by the small broker daemon
(`fusionsolar_ownership_broker_daemon.py`) that runs in its own container
with the only mount of V1's data directory anywhere in this deployment.

Deliberately stdlib-only: no SQLAlchemy, no requests, nothing that assumes
V1's or V2's application code is importable. See
`Nem-sei-v2/docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md` for the full design and
audit this ports from `Nem-sei/monitoring_board/services/production_api_queue.py`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LISBON = ZoneInfo("Europe/Lisbon")
PROVIDER = "fusionsolar"

REQUIRED_COLUMNS = {
    "provider",
    "account_key",
    "lease_until",
    "lease_owner",
    "cooldown_until",
    "last_407_at",
    "updated_at",
}


class OwnershipBrokerError(RuntimeError):
    """Raised whenever the broker must fail closed."""


class SchemaMismatchError(OwnershipBrokerError):
    """V1's database does not look like what this tool was built against."""


@dataclasses.dataclass(frozen=True)
class LeaseResult:
    granted: bool
    lease_until: datetime | None
    holder: str
    wait_reason: str
    cooldown_until: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "lease_until": self.lease_until.isoformat() if self.lease_until else None,
            "holder": self.holder,
            "wait_reason": self.wait_reason,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
        }


def account_key(*, provider: str, username: str = "", base_url: str = "", endpoint: str = "") -> str:
    identity = "|".join(
        (
            provider.strip().lower(),
            username.strip().lower(),
            base_url.strip().lower().rstrip("/"),
            endpoint.strip().lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def lisbon_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(LISBON)
    if value.tzinfo is None:
        return value.replace(tzinfo=LISBON)
    return value.astimezone(LISBON)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return lisbon_now(parsed)


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise OwnershipBrokerError(f"V1 database not found at {db_path!r}; refusing to proceed.")
    conn = sqlite3.connect(f"file:{path}", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def verify_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_api_account_state'"
    ).fetchone()
    if row is None:
        raise SchemaMismatchError(
            "provider_api_account_state table not found -- V1's lease schema may have changed. "
            "Refusing to guess; update this tool against the current "
            "Nem-sei/monitoring_board/services/production_api_queue.py before retrying."
        )
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(provider_api_account_state)").fetchall()}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise SchemaMismatchError(f"provider_api_account_state is missing expected columns: {sorted(missing)}")


def read_lease(conn: sqlite3.Connection, *, account_key_value: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM provider_api_account_state WHERE provider = ? AND account_key = ?",
        (PROVIDER, account_key_value),
    ).fetchone()
    return dict(row) if row else {}


def reserve_account_lease(
    conn: sqlite3.Connection,
    *,
    account_key_value: str,
    lease_owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> LeaseResult:
    now = lisbon_now(now)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO provider_api_account_state (provider, account_key, updated_at) VALUES (?, ?, ?)",
            (PROVIDER, account_key_value, now.isoformat(timespec="seconds")),
        )
        state = conn.execute(
            "SELECT * FROM provider_api_account_state WHERE provider = ? AND account_key = ?",
            (PROVIDER, account_key_value),
        ).fetchone()

        cooldown_until = _parse_dt(state["cooldown_until"])
        if cooldown_until and cooldown_until > now:
            conn.commit()
            return LeaseResult(False, None, str(state["lease_owner"] or ""), "account_cooldown_407", cooldown_until)

        lease_until = _parse_dt(state["lease_until"])
        active_owner = str(state["lease_owner"] or "")
        if lease_until and lease_until > now and active_owner != lease_owner:
            conn.commit()
            return LeaseResult(False, lease_until, active_owner, "active_lease")

        new_lease_until = now + timedelta(seconds=max(lease_seconds, 1))
        conn.execute(
            """
            UPDATE provider_api_account_state
            SET lease_until = ?, lease_owner = ?, updated_at = ?
            WHERE provider = ? AND account_key = ?
            """,
            (new_lease_until.isoformat(timespec="seconds"), lease_owner, now.isoformat(timespec="seconds"), PROVIDER, account_key_value),
        )
        conn.commit()
        return LeaseResult(True, new_lease_until, lease_owner, "")
    except Exception:
        conn.rollback()
        raise


def release_account_lease(conn: sqlite3.Connection, *, account_key_value: str, lease_owner: str, now: datetime | None = None) -> bool:
    now = lisbon_now(now)
    cursor = conn.execute(
        """
        UPDATE provider_api_account_state
        SET lease_until = NULL, lease_owner = NULL, updated_at = ?
        WHERE provider = ? AND account_key = ? AND lease_owner = ?
        """,
        (now.isoformat(timespec="seconds"), PROVIDER, account_key_value, lease_owner),
    )
    conn.commit()
    return cursor.rowcount > 0


def observe_v1_fusionsolar_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read-only visibility only -- never used to gate the decision."""
    try:
        rows = conn.execute(
            """
            SELECT id, job_type, status, created_at
            FROM background_jobs
            WHERE job_type LIKE 'fusionsolar_%'
              AND status IN ('pending', 'running', 'waiting_api_slot', 'waiting_rate_limit')
            ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def read_v1_daily_budget_state(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read-only: V1's own per-area daily call counters and configured budgets."""
    try:
        rows = conn.execute(
            """
            SELECT account_key, api_area, daily_call_count, daily_budget, daily_count_date, cooldown_until
            FROM production_api_queue_state
            WHERE provider = ?
            """,
            (PROVIDER,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def append_audit(audit_log_path: Path, event: str, **fields: Any) -> None:
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(LISBON).isoformat(timespec="seconds"), "event": event, **fields}
    with audit_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")
