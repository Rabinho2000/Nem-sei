#!/usr/bin/env python3
"""Tiny HTTP daemon exposing V1's FusionSolar account lease to V2 containers.

Why this exists (see docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md and
docs/v2/FUSIONSOLAR_V2_ROLLOUT.md for the full picture): V2's `worker` and
`scheduler` containers must never make a FusionSolar HTTP call while V1
might be making one too, and `scripts/verify_v2_runtime_isolation.py`
deliberately refuses to deploy `docker-compose.v2.yml` if it references
V1's runtime data at all (`Nem-sei/data`, `monitoring_board.db`) -- V2's own
team built that guardrail on purpose and this tool does not route around it.

So V1's SQLite file is mounted into exactly ONE small, separate container
(this daemon, defined in `docker-compose.v1-ownership-broker.yml`, its own
compose project, never merged into `docker-compose.v2.yml`), and `worker`/
`scheduler` reach it over the network instead of touching V1's filesystem
directly. The only thing that crosses the wire is: an owner id, a lease
duration, and a granted/denied verdict -- never a FusionSolar credential,
never V1 business data.

Endpoints (all JSON, all require `Authorization: Bearer <token>`):
  GET  /status                       -- read-only lease + V1 job/budget state
  POST /acquire  {account_key, owner, lease_seconds}
  POST /release  {account_key, owner}
  GET  /budget                       -- V1's per-area daily call counters

Stdlib only, deliberately: this container's whole job is "open one SQLite
file safely and answer a few JSON questions about it", so it does not need
Flask, SQLAlchemy, or anything from the V2 application.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusionsolar_ownership_core as core  # noqa: E402


DB_PATH = os.environ.get("NEMSEI_V1_DB_PATH", "/v1data/monitoring_board.db")
TOKEN = os.environ.get("NEMSEI_V1_BROKER_TOKEN", "").strip()
BIND_HOST = os.environ.get("NEMSEI_V1_BROKER_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("NEMSEI_V1_BROKER_BIND_PORT", "8765"))
AUDIT_LOG_PATH = Path(os.environ.get("NEMSEI_V1_BROKER_AUDIT_LOG", "/var/log/fusionsolar-ownership-broker/audit.jsonl"))

if not TOKEN:
    raise SystemExit("NEMSEI_V1_BROKER_TOKEN is required -- refusing to start an unauthenticated lease broker.")


def _open_db() -> "core.sqlite3.Connection":
    conn = core.connect(DB_PATH)
    core.verify_schema(conn)
    return conn


class Handler(BaseHTTPRequestHandler):
    server_version = "fusionsolar-ownership-broker/1"

    def log_message(self, fmt: str, *args: object) -> None:  # quieter, structured logging instead
        pass

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {TOKEN}"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/status":
                self._handle_status()
            elif path == "/budget":
                self._handle_budget()
            elif path == "/healthz":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not_found"})
        except core.OwnershipBrokerError as exc:
            core.append_audit(AUDIT_LOG_PATH, "fail_closed", route=path, error=str(exc))
            self._send(503, {"error": "fail_closed", "message": str(exc)})
        except Exception as exc:  # never leak a traceback to a network caller
            core.append_audit(AUDIT_LOG_PATH, "unexpected_error", route=path, error=str(exc))
            self._send(500, {"error": "internal"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/acquire":
                self._handle_acquire(body)
            elif path == "/release":
                self._handle_release(body)
            else:
                self._send(404, {"error": "not_found"})
        except core.OwnershipBrokerError as exc:
            core.append_audit(AUDIT_LOG_PATH, "fail_closed", route=path, error=str(exc))
            self._send(503, {"error": "fail_closed", "message": str(exc)})
        except Exception as exc:
            core.append_audit(AUDIT_LOG_PATH, "unexpected_error", route=path, error=str(exc))
            self._send(500, {"error": "internal"})

    def _handle_status(self) -> None:
        account_key_value = urlparse(self.path).query
        params = dict(p.split("=", 1) for p in account_key_value.split("&") if "=" in p)
        key = params.get("account_key", "")
        with contextlib.closing(_open_db()) as conn:
            state = core.read_lease(conn, account_key_value=key) if key else {}
            jobs = core.observe_v1_fusionsolar_jobs(conn)
        now = core.lisbon_now()
        lease_until = core._parse_dt(state.get("lease_until"))
        cooldown_until = core._parse_dt(state.get("cooldown_until"))
        self._send(200, {
            "account_key": key,
            "lease_active": bool(lease_until and lease_until > now),
            "lease_owner": state.get("lease_owner"),
            "lease_until": state.get("lease_until"),
            "cooldown_active": bool(cooldown_until and cooldown_until > now),
            "cooldown_until": state.get("cooldown_until"),
            "v1_fusionsolar_jobs_in_flight": jobs,
        })

    def _handle_budget(self) -> None:
        with contextlib.closing(_open_db()) as conn:
            rows = core.read_v1_daily_budget_state(conn)
        self._send(200, {"v1_daily_budget_state": rows})

    def _handle_acquire(self, body: dict) -> None:
        key = str(body.get("account_key") or "").strip()
        owner = str(body.get("owner") or "").strip()
        lease_seconds = int(body.get("lease_seconds") or 60)
        if not key or not owner:
            self._send(400, {"error": "account_key and owner are required"})
            return
        with contextlib.closing(_open_db()) as conn:
            result = core.reserve_account_lease(conn, account_key_value=key, lease_owner=owner, lease_seconds=lease_seconds)
        core.append_audit(AUDIT_LOG_PATH, "acquire", owner=owner, account_key=key, granted=result.granted, reason=result.wait_reason)
        self._send(200 if result.granted else 409, result.to_dict())

    def _handle_release(self, body: dict) -> None:
        key = str(body.get("account_key") or "").strip()
        owner = str(body.get("owner") or "").strip()
        if not key or not owner:
            self._send(400, {"error": "account_key and owner are required"})
            return
        with contextlib.closing(_open_db()) as conn:
            released = core.release_account_lease(conn, account_key_value=key, lease_owner=owner)
        core.append_audit(AUDIT_LOG_PATH, "release", owner=owner, account_key=key, released=released)
        self._send(200, {"released": released})


def main() -> int:
    # Fail closed at startup, not on the first request: prove the DB and
    # schema are actually reachable before accepting any traffic.
    with contextlib.closing(_open_db()):
        pass
    core.append_audit(AUDIT_LOG_PATH, "daemon_started", bind=f"{BIND_HOST}:{BIND_PORT}", db_path=DB_PATH)
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    finally:
        core.append_audit(AUDIT_LOG_PATH, "daemon_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
