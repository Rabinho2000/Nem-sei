from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


LISBON = ZoneInfo("Europe/Lisbon")
PRODUCTION_KPI_AREA = "production_kpi"
CRITICAL_PRIORITIES = {1, 2}


@dataclass(frozen=True)
class ApiQueuePolicy:
    min_interval_seconds: int | None
    daily_budget: int | None
    reserved_calls_by_priority: tuple[tuple[int, int], ...] = ()
    lease_seconds: int = 300

    @property
    def reserved_critical_calls(self) -> int:
        return sum(max(int(calls), 0) for _priority, calls in self.reserved_calls_by_priority)


@dataclass(frozen=True)
class ApiSlotReservation:
    granted: bool
    next_attempt_at: datetime
    wait_reason: str
    lease_owner: str
    daily_call_count: int
    daily_budget: int | None


class ApiSlotUnavailableError(ValueError):
    def __init__(
        self,
        *,
        provider: str,
        account_key: str,
        api_area: str,
        next_attempt_at: datetime,
        wait_reason: str,
        message: str = "",
    ) -> None:
        super().__init__(message or f"API slot unavailable until {next_attempt_at.isoformat()}")
        self.provider = provider
        self.account_key = account_key
        self.api_area = api_area
        self.next_attempt_at = next_attempt_at
        self.wait_reason = wait_reason
        self.message = str(self)


def ensure_api_queue_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_api_queue_state (
            provider TEXT NOT NULL,
            account_key TEXT NOT NULL,
            api_area TEXT NOT NULL,
            next_allowed_at TEXT,
            lease_until TEXT,
            lease_owner TEXT,
            cooldown_until TEXT,
            last_407_at TEXT,
            daily_count_date TEXT,
            daily_call_count INTEGER NOT NULL DEFAULT 0,
            daily_critical_call_count INTEGER NOT NULL DEFAULT 0,
            daily_noncritical_call_count INTEGER NOT NULL DEFAULT 0,
            daily_budget INTEGER,
            reserved_critical_calls INTEGER NOT NULL DEFAULT 0,
            min_interval_seconds INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, account_key, api_area)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_production_api_queue_next
        ON production_api_queue_state(api_area, next_allowed_at, cooldown_until)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_api_queue_daily_usage (
            provider TEXT NOT NULL,
            account_key TEXT NOT NULL,
            api_area TEXT NOT NULL,
            count_date TEXT NOT NULL,
            priority INTEGER NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (
                provider, account_key, api_area, count_date, priority
            )
        )
        """
    )


def account_key(
    *,
    provider: str,
    username: str = "",
    base_url: str = "",
    endpoint: str = "",
) -> str:
    identity = "|".join(
        (
            provider.strip().lower(),
            username.strip().lower(),
            base_url.strip().lower().rstrip("/"),
            endpoint.strip().lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def reserve_api_slot(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    lease_owner: str,
    priority: int,
    policy: ApiQueuePolicy,
    now: datetime | None = None,
) -> ApiSlotReservation:
    now = _lisbon_now(now)
    ensure_api_queue_schema(conn)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _load_or_create_state(
            conn,
            provider=provider,
            account_key_value=account_key_value,
            api_area=api_area,
            policy=policy,
            now=now,
        )
        count_date = now.date().isoformat()
        daily_count = int(state["daily_call_count"] or 0)
        critical_count = int(state["daily_critical_call_count"] or 0)
        noncritical_count = int(state["daily_noncritical_call_count"] or 0)
        if str(state["daily_count_date"] or "") != count_date:
            daily_count = 0
            critical_count = 0
            noncritical_count = 0

        active_lease_until = _parse_datetime(state["lease_until"])
        active_owner = str(state["lease_owner"] or "")
        if active_lease_until and active_lease_until > now and active_owner != lease_owner:
            reservation = _denied(
                active_lease_until,
                "active_lease",
                lease_owner,
                daily_count,
                policy.daily_budget,
            )
            conn.commit()
            return reservation

        cooldown_until = _parse_datetime(state["cooldown_until"])
        if cooldown_until and cooldown_until > now:
            reservation = _denied(
                cooldown_until,
                "cooldown_407",
                lease_owner,
                daily_count,
                policy.daily_budget,
            )
            conn.commit()
            return reservation

        if policy.daily_budget is not None:
            if daily_count >= policy.daily_budget:
                reservation = _denied(
                    _next_lisbon_midnight(now),
                    "daily_budget",
                    lease_owner,
                    daily_count,
                    policy.daily_budget,
                )
                conn.commit()
                return reservation
            usage_by_priority = _load_daily_usage(
                conn,
                provider=provider,
                account_key_value=account_key_value,
                api_area=api_area,
                count_date=count_date,
            )
            protected_for_other_priorities = sum(
                max(reserved_calls - usage_by_priority.get(reserved_priority, 0), 0)
                for reserved_priority, reserved_calls in policy.reserved_calls_by_priority
                if reserved_priority != priority
            )
            if daily_count >= max(
                policy.daily_budget - protected_for_other_priorities,
                0,
            ):
                reservation = _denied(
                    _next_lisbon_midnight(now),
                    "reserved_budget",
                    lease_owner,
                    daily_count,
                    policy.daily_budget,
                )
                conn.commit()
                return reservation

        next_allowed_at = _parse_datetime(state["next_allowed_at"])
        if next_allowed_at and next_allowed_at > now:
            reservation = _denied(
                next_allowed_at,
                "min_interval",
                lease_owner,
                daily_count,
                policy.daily_budget,
            )
            conn.commit()
            return reservation

        lease_until = now + timedelta(seconds=max(policy.lease_seconds, 1))
        next_allowed = now + timedelta(seconds=max(policy.min_interval_seconds or 0, 0))
        daily_count += 1
        if priority in CRITICAL_PRIORITIES:
            critical_count += 1
        else:
            noncritical_count += 1
        conn.execute(
            """
            INSERT INTO production_api_queue_daily_usage (
                provider, account_key, api_area, count_date, priority, call_count
            ) VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT (
                provider, account_key, api_area, count_date, priority
            ) DO UPDATE SET call_count = call_count + 1
            """,
            (
                provider,
                account_key_value,
                api_area,
                count_date,
                priority,
            ),
        )
        conn.execute(
            """
            UPDATE production_api_queue_state
            SET next_allowed_at = ?, lease_until = ?, lease_owner = ?,
                daily_count_date = ?, daily_call_count = ?,
                daily_critical_call_count = ?, daily_noncritical_call_count = ?,
                daily_budget = ?, reserved_critical_calls = ?,
                min_interval_seconds = ?, updated_at = ?
            WHERE provider = ? AND account_key = ? AND api_area = ?
            """,
            (
                next_allowed.isoformat(timespec="seconds"),
                lease_until.isoformat(timespec="seconds"),
                lease_owner,
                count_date,
                daily_count,
                critical_count,
                noncritical_count,
                policy.daily_budget,
                policy.reserved_critical_calls,
                policy.min_interval_seconds,
                now.isoformat(timespec="seconds"),
                provider,
                account_key_value,
                api_area,
            ),
        )
        conn.commit()
        return ApiSlotReservation(
            granted=True,
            next_attempt_at=now,
            wait_reason="",
            lease_owner=lease_owner,
            daily_call_count=daily_count,
            daily_budget=policy.daily_budget,
        )
    except Exception:
        conn.rollback()
        raise


def release_api_lease(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    lease_owner: str,
    now: datetime | None = None,
) -> None:
    now = _lisbon_now(now)
    ensure_api_queue_schema(conn)
    conn.execute(
        """
        UPDATE production_api_queue_state
        SET lease_until = NULL, lease_owner = NULL, updated_at = ?
        WHERE provider = ? AND account_key = ? AND api_area = ? AND lease_owner = ?
        """,
        (
            now.isoformat(timespec="seconds"),
            provider,
            account_key_value,
            api_area,
            lease_owner,
        ),
    )
    conn.commit()


def record_api_407(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    cooldown_until: datetime,
    now: datetime | None = None,
) -> None:
    now = _lisbon_now(now)
    cooldown_until = _lisbon_now(cooldown_until)
    ensure_api_queue_schema(conn)
    _load_or_create_state(
        conn,
        provider=provider,
        account_key_value=account_key_value,
        api_area=api_area,
        policy=ApiQueuePolicy(None, None),
        now=now,
    )
    conn.execute(
        """
        UPDATE production_api_queue_state
        SET cooldown_until = ?, next_allowed_at = ?, lease_until = NULL,
            lease_owner = NULL, last_407_at = ?, updated_at = ?
        WHERE provider = ? AND account_key = ? AND api_area = ?
        """,
        (
            cooldown_until.isoformat(timespec="seconds"),
            cooldown_until.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            provider,
            account_key_value,
            api_area,
        ),
    )
    conn.commit()


def list_api_queue_states(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = _lisbon_now(now)
    ensure_api_queue_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM production_api_queue_state
        ORDER BY provider, account_key, api_area
        """
    ).fetchall()
    states: list[dict[str, Any]] = []
    for row in rows:
        state = dict(row)
        if str(state.get("daily_count_date") or "") != now.date().isoformat():
            state["daily_call_count"] = 0
            state["daily_critical_call_count"] = 0
            state["daily_noncritical_call_count"] = 0
        states.append(state)
    return states


def ensure_api_queue_state(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    policy: ApiQueuePolicy,
    now: datetime | None = None,
) -> None:
    now = _lisbon_now(now)
    ensure_api_queue_schema(conn)
    _load_or_create_state(
        conn,
        provider=provider,
        account_key_value=account_key_value,
        api_area=api_area,
        policy=policy,
        now=now,
    )
    conn.execute(
        """
        UPDATE production_api_queue_state
        SET daily_budget = ?, reserved_critical_calls = ?,
            min_interval_seconds = ?, updated_at = ?
        WHERE provider = ? AND account_key = ? AND api_area = ?
        """,
        (
            policy.daily_budget,
            policy.reserved_critical_calls,
            policy.min_interval_seconds,
            now.isoformat(timespec="seconds"),
            provider,
            account_key_value,
            api_area,
        ),
    )
    conn.commit()


def recover_expired_leases(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> int:
    now = _lisbon_now(now)
    ensure_api_queue_schema(conn)
    cursor = conn.execute(
        """
        UPDATE production_api_queue_state
        SET lease_until = NULL, lease_owner = NULL, updated_at = ?
        WHERE lease_until IS NOT NULL AND lease_until <= ?
        """,
        (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
    conn.commit()
    return cursor.rowcount


def _load_or_create_state(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    policy: ApiQueuePolicy,
    now: datetime,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT OR IGNORE INTO production_api_queue_state (
            provider, account_key, api_area, daily_budget,
            reserved_critical_calls, min_interval_seconds, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            account_key_value,
            api_area,
            policy.daily_budget,
            policy.reserved_critical_calls,
            policy.min_interval_seconds,
            now.isoformat(timespec="seconds"),
        ),
    )
    return conn.execute(
        """
        SELECT *
        FROM production_api_queue_state
        WHERE provider = ? AND account_key = ? AND api_area = ?
        """,
        (provider, account_key_value, api_area),
    ).fetchone()


def _denied(
    next_attempt_at: datetime,
    reason: str,
    lease_owner: str,
    daily_count: int,
    daily_budget: int | None,
) -> ApiSlotReservation:
    return ApiSlotReservation(
        granted=False,
        next_attempt_at=next_attempt_at,
        wait_reason=reason,
        lease_owner=lease_owner,
        daily_call_count=daily_count,
        daily_budget=daily_budget,
    )


def _load_daily_usage(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key_value: str,
    api_area: str,
    count_date: str,
) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT priority, call_count
        FROM production_api_queue_daily_usage
        WHERE provider = ? AND account_key = ? AND api_area = ?
          AND count_date = ?
        """,
        (provider, account_key_value, api_area, count_date),
    ).fetchall()
    return {
        int(row["priority"]): int(row["call_count"] or 0)
        for row in rows
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _lisbon_now(parsed)


def _lisbon_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(LISBON)
    if value.tzinfo is None:
        return value.replace(tzinfo=LISBON)
    return value.astimezone(LISBON)


def _next_lisbon_midnight(now: datetime) -> datetime:
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=LISBON)
