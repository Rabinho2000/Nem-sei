from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


DEFAULT_COOLDOWN_MINUTES = 60
RATE_LIMIT_STATUS = "waiting_rate_limit"


@dataclass(frozen=True)
class ApiArea:
    provider: str
    area: str


class ApiRateLimitError(ValueError):
    def __init__(
        self,
        provider: str,
        area: str,
        cooldown_until: datetime,
        message: str,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.area = area
        self.cooldown_until = cooldown_until
        self.message = message


class ApiTransientError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def ensure_api_call_state_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_call_state (
            provider TEXT NOT NULL,
            api_area TEXT NOT NULL,
            cooldown_until TEXT,
            last_error TEXT,
            last_success_at TEXT,
            last_attempt_at TEXT,
            last_alert_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, api_area)
        )
        """
    )


def get_api_call_state(conn: sqlite3.Connection, provider: str, area: str) -> dict[str, Any]:
    ensure_api_call_state_schema(conn)
    row = conn.execute(
        """
        SELECT provider, api_area, cooldown_until, last_error, last_success_at,
               last_attempt_at, last_alert_at, created_at, updated_at
        FROM api_call_state
        WHERE provider = ? AND api_area = ?
        """,
        (provider, area),
    ).fetchone()
    return dict(row) if row else {
        "provider": provider,
        "api_area": area,
        "cooldown_until": "",
        "last_error": "",
        "last_success_at": "",
        "last_attempt_at": "",
        "last_alert_at": "",
        "created_at": "",
        "updated_at": "",
    }


def record_api_attempt(conn: sqlite3.Connection, provider: str, area: str, now: datetime | None = None) -> None:
    ensure_api_call_state_schema(conn)
    now = now or utcnow()
    conn.execute(
        """
        INSERT INTO api_call_state (provider, api_area, last_attempt_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(provider, api_area) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at,
            updated_at = excluded.updated_at
        """,
        (provider, area, iso(now), iso(now), iso(now)),
    )


def record_api_success(conn: sqlite3.Connection, provider: str, area: str, now: datetime | None = None) -> None:
    ensure_api_call_state_schema(conn)
    now = now or utcnow()
    conn.execute(
        """
        INSERT INTO api_call_state (
            provider, api_area, cooldown_until, last_error, last_success_at,
            last_attempt_at, created_at, updated_at
        )
        VALUES (?, ?, '', '', ?, ?, ?, ?)
        ON CONFLICT(provider, api_area) DO UPDATE SET
            cooldown_until = '',
            last_error = '',
            last_success_at = excluded.last_success_at,
            last_attempt_at = excluded.last_attempt_at,
            updated_at = excluded.updated_at
        """,
        (provider, area, iso(now), iso(now), iso(now), iso(now)),
    )


def mark_api_cooldown(
    conn: sqlite3.Connection,
    provider: str,
    area: str,
    reason: str,
    *,
    cooldown_until: datetime | None = None,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    now: datetime | None = None,
) -> datetime:
    ensure_api_call_state_schema(conn)
    now = now or utcnow()
    until = cooldown_until or (now + timedelta(minutes=max(1, cooldown_minutes)))
    conn.execute(
        """
        INSERT INTO api_call_state (
            provider, api_area, cooldown_until, last_error, last_attempt_at,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, api_area) DO UPDATE SET
            cooldown_until = excluded.cooldown_until,
            last_error = excluded.last_error,
            last_attempt_at = excluded.last_attempt_at,
            updated_at = excluded.updated_at
        """,
        (provider, area, iso(until), reason[:2000], iso(now), iso(now), iso(now)),
    )
    return until


def active_cooldown_until(
    conn: sqlite3.Connection,
    provider: str,
    area: str,
    now: datetime | None = None,
) -> datetime | None:
    state = get_api_call_state(conn, provider, area)
    until = parse_datetime(state.get("cooldown_until"))
    now = now or utcnow()
    return until if until and until > now else None


def require_not_in_cooldown(
    conn: sqlite3.Connection,
    provider: str,
    area: str,
    now: datetime | None = None,
) -> None:
    until = active_cooldown_until(conn, provider, area, now)
    if until is None:
        return
    message = (
        f"{provider} temporariamente limitado pela API. "
        f"Nova tentativa disponivel apos {until.isoformat(timespec='minutes')}."
    )
    raise ApiRateLimitError(provider, area, until, message)
