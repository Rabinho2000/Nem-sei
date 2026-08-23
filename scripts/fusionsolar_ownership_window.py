#!/usr/bin/env python3
"""FusionSolar single-account ownership window broker (V1 <-> V2).

Purpose
-------
V1 and V2 share ONE FusionSolar account (one login, one provider-side rate
budget). This tool lets V2 borrow that account for a short, bounded,
auditable window without touching V1's code and without ever running two
concurrent FusionSolar clients against the same account.

It does this by speaking V1's OWN persisted per-account lease protocol
directly against V1's SQLite database file -- the exact same
`provider_api_account_state` table and `reserve_account_lease` /
`release_account_lease` algorithm that
`Nem-sei/monitoring_board/services/production_api_queue.py` already uses to
serialize V1's own FusionSolar background jobs against each other. Every
V1 FusionSolar API call issued from a background job (state sync, production
sync, backfill, month cycle/close, report requests, wat requests -- see
`FUSIONSOLAR_BACKGROUND_JOB_TYPES` in `Nem-sei/monitoring_board/app_factory.py`)
already reserves this lease before the HTTP call and releases it after. If
this broker holds the lease, those V1 calls get `ApiSlotUnavailableError`,
which V1 already treats as a normal `waiting_api_slot` backoff -- no V1 code
path changes, no crash, no user-visible error.

Known gap (documented, not silently fixed): V1's manual "Testar ligacao"
admin button calls the API outside a background-job context and does NOT
check this lease. This tool cannot close that gap without changing V1 code,
so it is a documented operational rule instead: don't click that button
during an active ownership window.

Restart-safety / stuck-lease protection comes for free from V1's own
mechanism: the lease row carries `lease_until`; an expired lease is treated
as free by any future `reserve_account_lease` call (V1's or ours), and V1's
own `recover_expired_leases()` sweeps stale leases at scheduler startup. If
this broker crashes mid-window without releasing, the worst case is V1
waits out one lease period (default 90s here, renewed continuously) before
resuming on its own.

Fail-closed: any schema mismatch, any inability to prove nobody else holds
the lease, or any exception during acquire aborts before doing anything
V1-observable. Credentials are never read or logged by this tool; it only
ever touches `lease_until` / `lease_owner` columns, keyed by the same
one-way `account_key` hash V1 already computes and stores (not a secret).

Usage
-----
    # Read-only, never writes:
    python3 fusionsolar_ownership_window.py status

    # Manual operation ("dar ownership a V2" / "devolver ownership a V1"):
    python3 fusionsolar_ownership_window.py acquire --owner nemsei-v2-manual
    python3 fusionsolar_ownership_window.py release --owner nemsei-v2-manual

    # Scripted window ("automatic equivalent"): acquire, run a command with
    # the lease held and continuously renewed, always release on exit.
    python3 fusionsolar_ownership_window.py run-window \
        --lease-seconds 90 --max-window-seconds 300 -- \
        python3 some_v2_canary_script.py

    # First test pass -- touches NOTHING real, exercises the whole state
    # machine (including "V1 already holds it -> denied") against a throwaway
    # SQLite file:
    python3 fusionsolar_ownership_window.py selftest
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


LISBON = ZoneInfo("Europe/Lisbon")
PROVIDER = "fusionsolar"

# Default location of V1's live SQLite database. Overridable via
# --v1-db / NEMSEI_V1_DB_PATH so this never has a hidden hardcoded
# production path that surprises anyone reading a `--help`.
DEFAULT_V1_DB_PATH = "/opt/server/apps/Nem-sei/data/monitoring_board.db"

# Where this tool's own audit trail and default owner id live. Kept inside
# the V2 repo (git-ignored) rather than Nem-sei-v2-data/, which this
# session has no write access to.
RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime" / "fusionsolar_ownership"
AUDIT_LOG_PATH = RUNTIME_DIR / "audit.jsonl"

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


# --------------------------------------------------------------------------
# account_key -- byte-for-byte the same algorithm as
# Nem-sei/monitoring_board/services/production_api_queue.py::account_key
# (copied, not imported, so this tool has zero import-time coupling to V1's
# process/venv; the two are kept in lockstep only by this docstring pointer
# and by `selftest`/`status` cross-checking the live row when possible).
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Connection + schema verification (fail closed on anything unexpected)
# --------------------------------------------------------------------------
def connect(db_path: str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise OwnershipBrokerError(f"V1 database not found at {db_path!r}; refusing to proceed.")
    uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
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


# --------------------------------------------------------------------------
# The lease protocol itself -- ported verbatim (same BEGIN IMMEDIATE
# discipline, same denial conditions) from V1's reserve_account_lease /
# release_account_lease, restricted to the account-level table since that is
# what gates every FusionSolar background job type in V1.
# --------------------------------------------------------------------------
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
    """Read-only visibility into V1 jobs that touch FusionSolar. Never used
    to gate the decision (the atomic lease call is what gates it) -- only to
    make the audit log honest about what V1 was doing around the window."""
    try:
        rows = conn.execute(
            """
            SELECT id, job_type, status, updated_at
            FROM background_jobs
            WHERE job_type LIKE 'fusionsolar_%'
              AND status IN ('pending', 'running', 'waiting_api_slot', 'waiting_rate_limit')
            ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


# --------------------------------------------------------------------------
# Audit log -- append-only JSONL, never contains credentials (only the
# one-way account_key hash and owner/job-id strings).
# --------------------------------------------------------------------------
def audit(event: str, **fields: Any) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(LISBON).isoformat(timespec="seconds"), "event": event, **fields}
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")


# --------------------------------------------------------------------------
# High-level window orchestration
# --------------------------------------------------------------------------
class OwnershipWindow:
    def __init__(
        self,
        *,
        db_path: str,
        account_key_value: str,
        owner: str,
        lease_seconds: int,
        acquire_retries: int,
        acquire_retry_delay: float,
    ) -> None:
        self.db_path = db_path
        self.account_key_value = account_key_value
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.acquire_retries = acquire_retries
        self.acquire_retry_delay = acquire_retry_delay
        self._stop_renewal = threading.Event()
        self._renewal_thread: threading.Thread | None = None
        self._granted_at: float | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = connect(self.db_path)
        verify_schema(conn)
        return conn

    def acquire(self) -> LeaseResult:
        with contextlib.closing(self._connect()) as conn:
            observed = observe_v1_fusionsolar_jobs(conn)
            if observed:
                audit("v1_jobs_observed_before_acquire", owner=self.owner, jobs=observed)

            last_result: LeaseResult | None = None
            for attempt in range(1, self.acquire_retries + 1):
                result = reserve_account_lease(
                    conn,
                    account_key_value=self.account_key_value,
                    lease_owner=self.owner,
                    lease_seconds=self.lease_seconds,
                )
                last_result = result
                if result.granted:
                    self._granted_at = time.monotonic()
                    audit(
                        "acquired",
                        owner=self.owner,
                        account_key=self.account_key_value,
                        attempt=attempt,
                        lease_until=result.lease_until.isoformat() if result.lease_until else None,
                    )
                    self._start_renewal_thread()
                    return result
                audit(
                    "acquire_denied",
                    owner=self.owner,
                    account_key=self.account_key_value,
                    attempt=attempt,
                    reason=result.wait_reason,
                    held_by=result.holder,
                )
                if attempt < self.acquire_retries:
                    time.sleep(self.acquire_retry_delay)
            assert last_result is not None
            return last_result

    def _start_renewal_thread(self) -> None:
        interval = max(1.0, self.lease_seconds * 0.6)

        def _loop() -> None:
            while not self._stop_renewal.wait(interval):
                try:
                    with contextlib.closing(self._connect()) as conn:
                        result = reserve_account_lease(
                            conn,
                            account_key_value=self.account_key_value,
                            lease_owner=self.owner,
                            lease_seconds=self.lease_seconds,
                        )
                        audit(
                            "renewed" if result.granted else "renewal_lost",
                            owner=self.owner,
                            lease_until=result.lease_until.isoformat() if result.lease_until else None,
                        )
                        if not result.granted:
                            # Somebody else holds it (should not happen while
                            # our own release hasn't run) -- stop pretending
                            # we own it and let run-window's own check fail.
                            self._stop_renewal.set()
                except Exception as exc:  # fail closed: never crash silently
                    audit("renewal_error", owner=self.owner, error=str(exc))
                    self._stop_renewal.set()

        self._renewal_thread = threading.Thread(target=_loop, name="fusionsolar-lease-renewal", daemon=True)
        self._renewal_thread.start()

    def release(self) -> bool:
        self._stop_renewal.set()
        if self._renewal_thread is not None:
            self._renewal_thread.join(timeout=5)
        with contextlib.closing(self._connect()) as conn:
            released = release_account_lease(conn, account_key_value=self.account_key_value, lease_owner=self.owner)
            held_seconds = (time.monotonic() - self._granted_at) if self._granted_at else None
            audit("released", owner=self.owner, released=released, held_seconds=held_seconds)
            return released

    @contextlib.contextmanager
    def held(self) -> Iterator[LeaseResult]:
        result = self.acquire()
        if not result.granted:
            raise OwnershipBrokerError(
                f"Could not acquire FusionSolar ownership: {result.wait_reason} "
                f"(held by {result.holder!r} until {result.lease_until})."
            )
        try:
            yield result
        finally:
            self.release()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _resolve_account_key(args: argparse.Namespace) -> str:
    if args.account_key:
        return args.account_key
    if not (args.username and args.base_url):
        raise OwnershipBrokerError(
            "Need --account-key, or --username and --base-url to derive it. "
            "Refusing to guess V1's configured FusionSolar account (fail closed)."
        )
    return account_key(provider=PROVIDER, username=args.username, base_url=args.base_url, endpoint="account")


def cmd_status(args: argparse.Namespace) -> int:
    with contextlib.closing(connect(args.v1_db, read_only=True)) as conn:
        verify_schema(conn)
        account_key_value = _resolve_account_key(args)
        state = read_lease(conn, account_key_value=account_key_value)
        jobs = observe_v1_fusionsolar_jobs(conn)
    now = lisbon_now()
    lease_until = _parse_dt(state.get("lease_until"))
    cooldown_until = _parse_dt(state.get("cooldown_until"))
    print(json.dumps({
        "account_key": account_key_value,
        "lease_active": bool(lease_until and lease_until > now),
        "lease_owner": state.get("lease_owner"),
        "lease_until": state.get("lease_until"),
        "cooldown_active": bool(cooldown_until and cooldown_until > now),
        "cooldown_until": state.get("cooldown_until"),
        "v1_fusionsolar_jobs_in_flight": jobs,
    }, indent=2, ensure_ascii=True))
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    account_key_value = _resolve_account_key(args)
    window = OwnershipWindow(
        db_path=args.v1_db,
        account_key_value=account_key_value,
        owner=args.owner,
        lease_seconds=args.lease_seconds,
        acquire_retries=args.acquire_retries,
        acquire_retry_delay=args.acquire_retry_delay,
    )
    result = window.acquire()
    if not result.granted:
        print(f"DENIED: {result.wait_reason} (held by {result.holder!r} until {result.lease_until})", file=sys.stderr)
        return 1
    print(f"GRANTED to {args.owner!r} until {result.lease_until.isoformat()}")
    print("Renewal thread is only alive for the life of THIS process. "
          "For a held window that survives process exit, use `run-window`.")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    account_key_value = _resolve_account_key(args)
    with contextlib.closing(connect(args.v1_db)) as conn:
        verify_schema(conn)
        released = release_account_lease(conn, account_key_value=account_key_value, lease_owner=args.owner)
    audit("manual_release", owner=args.owner, released=released)
    print("Released." if released else "Nothing to release (lease was not held by this owner).")
    return 0


def cmd_run_window(args: argparse.Namespace) -> int:
    import subprocess

    account_key_value = _resolve_account_key(args)
    owner = args.owner or f"nemsei-v2-window-{uuid.uuid4().hex[:12]}"
    window = OwnershipWindow(
        db_path=args.v1_db,
        account_key_value=account_key_value,
        owner=owner,
        lease_seconds=args.lease_seconds,
        acquire_retries=args.acquire_retries,
        acquire_retry_delay=args.acquire_retry_delay,
    )

    started = time.monotonic()
    takeover_seconds: float | None = None
    exit_code = 1
    try:
        with window.held() as result:
            takeover_seconds = time.monotonic() - started
            audit("takeover_complete", owner=owner, takeover_seconds=round(takeover_seconds, 3))
            print(f"Ownership granted to {owner!r} in {takeover_seconds:.2f}s (until {result.lease_until.isoformat()}).")
            print(f"Running: {' '.join(args.command)}")
            proc = subprocess.run(
                args.command,
                timeout=args.max_window_seconds,
                cwd=args.cwd,
            )
            exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        audit("command_timeout", owner=owner, max_window_seconds=args.max_window_seconds)
        print(f"Command exceeded --max-window-seconds={args.max_window_seconds}; releasing ownership and aborting.", file=sys.stderr)
        exit_code = 124
    except OwnershipBrokerError as exc:
        audit("acquire_failed", owner=owner, error=str(exc))
        print(f"FAILED CLOSED: {exc}", file=sys.stderr)
        return 1

    with contextlib.closing(connect(args.v1_db, read_only=True)) as conn:
        state = read_lease(conn, account_key_value=account_key_value)
    now = lisbon_now()
    resumed_ok = state.get("lease_owner") is None or _parse_dt(state.get("lease_until")) is None or _parse_dt(state.get("lease_until")) <= now
    audit(
        "window_summary",
        owner=owner,
        takeover_seconds=round(takeover_seconds, 3) if takeover_seconds is not None else None,
        command_exit_code=exit_code,
        v1_resumed_cleanly=resumed_ok,
    )
    print(f"V1 lease clear after handback: {resumed_ok}")
    return exit_code


def cmd_selftest(args: argparse.Namespace) -> int:
    """Exercise the whole state machine against a throwaway SQLite file.
    Makes zero connections to V1's real database and zero network calls."""
    tmp_dir = tempfile.mkdtemp(prefix="fusionsolar-ownership-selftest-")
    db_path = os.path.join(tmp_dir, "fake_monitoring_board.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE provider_api_account_state (
            provider TEXT NOT NULL,
            account_key TEXT NOT NULL,
            lease_until TEXT,
            lease_owner TEXT,
            cooldown_until TEXT,
            last_407_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, account_key)
        );
        CREATE TABLE background_jobs (
            id INTEGER PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    key = account_key(provider=PROVIDER, username="selftest-user", base_url="https://example.invalid", endpoint="account")
    failures: list[str] = []

    def check(label: str, condition: bool) -> None:
        status = "ok" if condition else "FAIL"
        print(f"[{status}] {label}")
        if not condition:
            failures.append(label)

    with contextlib.closing(connect(db_path)) as conn:
        verify_schema(conn)
    check("schema verifies on a freshly created V1-shaped database", True)

    # 1. Simulate V1 already holding the lease -> our acquire must be denied.
    with contextlib.closing(connect(db_path)) as conn:
        v1_result = reserve_account_lease(conn, account_key_value=key, lease_owner="background-job-999", lease_seconds=5)
    check("V1 (simulated) acquires its own lease first", v1_result.granted)

    v2_window = OwnershipWindow(db_path=db_path, account_key_value=key, owner="nemsei-v2-window-selftest", lease_seconds=2, acquire_retries=1, acquire_retry_delay=0.1)
    denied = v2_window.acquire()
    check("V2 is DENIED while V1's lease is active (no concurrent calls)", not denied.granted)

    # 2. Let V1's lease expire, confirm V2 can now acquire (auto-recovery of stuck lease).
    time.sleep(5.2)
    granted = v2_window.acquire()
    check("V2 acquires once V1's lease naturally expires (no stuck ownership)", granted.granted)

    # 3. While V2 holds it, a simulated V1 job must be denied (no concurrent calls, both directions).
    with contextlib.closing(connect(db_path)) as conn:
        v1_retry = reserve_account_lease(conn, account_key_value=key, lease_owner="background-job-1000", lease_seconds=5)
    check("V1 is DENIED while V2 holds the window", not v1_retry.granted)

    # 4. Release and confirm V1 can immediately reacquire (clean handback).
    v2_window.release()
    with contextlib.closing(connect(db_path)) as conn:
        v1_after_release = reserve_account_lease(conn, account_key_value=key, lease_owner="background-job-1001", lease_seconds=5)
    check("V1 resumes immediately after V2 releases (clean handback)", v1_after_release.granted)
    with contextlib.closing(connect(db_path)) as conn:
        release_account_lease(conn, account_key_value=key, lease_owner="background-job-1001")

    # 5. A cooldown (simulated 407) must fail-closed even with nobody else holding the lease.
    with contextlib.closing(connect(db_path)) as conn:
        conn.execute(
            "UPDATE provider_api_account_state SET cooldown_until = ? WHERE provider = ? AND account_key = ?",
            ((lisbon_now() + timedelta(minutes=5)).isoformat(timespec="seconds"), PROVIDER, key),
        )
        conn.commit()
    with contextlib.closing(connect(db_path)) as conn:
        during_cooldown = reserve_account_lease(conn, account_key_value=key, lease_owner="nemsei-v2-window-selftest-2", lease_seconds=5)
    check("Acquire is denied during a rate-limit cooldown (respects shared cooldown)", not during_cooldown.granted)

    print(f"\nSelftest database: {db_path} (left in place for inspection)")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}", file=sys.stderr)
        return 1
    print("\nAll selftest checks passed. No V1 database and no network were touched.")
    return 0


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--v1-db", default=os.environ.get("NEMSEI_V1_DB_PATH", DEFAULT_V1_DB_PATH), help="Path to V1's monitoring_board.db")
    p.add_argument("--account-key", default=os.environ.get("NEMSEI_FUSIONSOLAR_ACCOUNT_KEY", ""), help="Precomputed account_key (preferred: avoids passing username/base_url on the command line)")
    p.add_argument("--username", default=os.environ.get("NEMSEI_FUSIONSOLAR_USERNAME", ""), help="Only used to derive --account-key if not given directly; never logged")
    p.add_argument("--base-url", default=os.environ.get("NEMSEI_FUSIONSOLAR_BASE_URL", ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Read-only: show current lease/cooldown state and V1 jobs in flight")
    _add_common_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_acquire = sub.add_parser("acquire", help='"Dar ownership a V2" -- one-shot acquire, no auto-renewal after this process exits')
    _add_common_args(p_acquire)
    p_acquire.add_argument("--owner", default=f"nemsei-v2-manual-{uuid.uuid4().hex[:8]}")
    p_acquire.add_argument("--lease-seconds", type=int, default=90)
    p_acquire.add_argument("--acquire-retries", type=int, default=1)
    p_acquire.add_argument("--acquire-retry-delay", type=float, default=2.0)
    p_acquire.set_defaults(func=cmd_acquire)

    p_release = sub.add_parser("release", help='"Devolver ownership a V1"')
    _add_common_args(p_release)
    p_release.add_argument("--owner", required=True)
    p_release.set_defaults(func=cmd_release)

    p_run = sub.add_parser("run-window", help="Acquire, keep renewed, run a command, always release")
    _add_common_args(p_run)
    p_run.add_argument("--owner", default="")
    p_run.add_argument("--lease-seconds", type=int, default=90)
    p_run.add_argument("--max-window-seconds", type=int, default=300)
    p_run.add_argument("--acquire-retries", type=int, default=5)
    p_run.add_argument("--acquire-retry-delay", type=float, default=3.0)
    p_run.add_argument("--cwd", default=None)
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="Command to run while ownership is held (prefix with --)")
    p_run.set_defaults(func=cmd_run_window)

    p_selftest = sub.add_parser("selftest", help="Simulated, no-network, no-real-DB test of the whole state machine")
    p_selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cmd", None) == "run-window":
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run-window requires a command after --")
        args.command = command
    try:
        return args.func(args)
    except OwnershipBrokerError as exc:
        print(f"FAILED CLOSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
