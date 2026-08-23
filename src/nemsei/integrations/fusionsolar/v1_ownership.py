"""Mandatory V1 ownership check for every real FusionSolar HTTP call V2 makes.

V1 and V2 share ONE FusionSolar account. `FusionSolarRequestController.call()`
is the single choke point every FusionSolar HTTP call in V2 goes through
(discovery, monitoring, production, diagnostics alike) -- so this module
hooks in exactly there, not in any one service, so nothing can accidentally
call the provider without going through it.

The actual lease lives in V1's own SQLite `provider_api_account_state`
table and is read/written by a small broker daemon
(`scripts/fusionsolar_ownership_broker_daemon.py`) running in its own
container with the only mount of V1's data directory anywhere in this
deployment -- see docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md for why it is a
separate container rather than a mount on `worker`/`scheduler` themselves
(`scripts/verify_v2_runtime_isolation.py` deliberately forbids the latter).

Fail-closed by construction: if the broker URL/token are not configured, if
the broker cannot be reached, or if it denies the lease, this raises
`V1LeaseUnavailable`. The caller (`FusionSolarRequestController.call`) turns
that into the exact same `ProviderError(RATE_LIMITED, transient=True)`
shape already used for V2's own internal contention -- every existing
caller already knows how to defer and retry that, so wiring this in changes
no calling code's error handling.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Iterator

from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import read_secret_value
from nemsei.providers.models import ProviderConnection


class V1LeaseUnavailable(Exception):
    pass


def _account_key(*, provider: str, username: str, base_url: str, endpoint: str = "account") -> str:
    """Byte-for-byte the same algorithm as
    Nem-sei/monitoring_board/services/production_api_queue.py::account_key
    and scripts/fusionsolar_ownership_core.py::account_key. Keep all three
    in lockstep; a mismatch here means V2 computes a *different* lease row
    than V1 does, which would silently stop coordinating anything."""
    identity = "|".join(
        (
            provider.strip().lower(),
            username.strip().lower(),
            base_url.strip().lower().rstrip("/"),
            endpoint.strip().lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _connection_account_key(connection: ProviderConnection) -> str:
    """Username + base_url only -- no password needed for a hash, and this
    intentionally never touches the password env var, unlike
    `nemsei.integrations.fusionsolar.service.credentials_for`. Not imported
    from there: `service.py` imports `FusionSolarRequestController` from
    this call site's module, so importing back would be circular."""
    reference = (connection.credential_reference or "").strip()
    if not reference:
        raise V1LeaseUnavailable("Connection has no credential_reference; cannot derive its V1 account_key.")
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    username = read_secret_value(value_name=f"{prefix}_USERNAME", file_name=f"{prefix}_USERNAME_FILE")
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    if not username or not base_url:
        raise V1LeaseUnavailable("FusionSolar username/base_url are not configured; cannot derive account_key.")
    return _account_key(provider=connection.provider_code, username=username, base_url=base_url)


def _broker_token() -> str:
    token = read_secret_value(
        value_name="NEMSEI_V1_OWNERSHIP_BROKER_TOKEN",
        file_name="NEMSEI_V1_OWNERSHIP_BROKER_TOKEN_FILE",
    )
    if not token:
        raise V1LeaseUnavailable("NEMSEI_V1_OWNERSHIP_BROKER_TOKEN(_FILE) is not configured.")
    return token


def _broker_url() -> str:
    url = os.environ.get("NEMSEI_V1_OWNERSHIP_BROKER_URL", "").strip()
    if not url:
        raise V1LeaseUnavailable("NEMSEI_V1_OWNERSHIP_BROKER_URL is not configured.")
    return url.rstrip("/")


def _request(method: str, path: str, *, body: dict | None = None, timeout: float = 5.0) -> dict:
    url = f"{_broker_url()}{path}"
    token = _broker_token()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        if exc.code == 409:
            return payload  # a well-formed denial, not a transport failure
        raise V1LeaseUnavailable(f"Ownership broker returned HTTP {exc.code}: {payload}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise V1LeaseUnavailable(f"Ownership broker unreachable: {exc}") from exc


@contextlib.contextmanager
def lease_for(
    *,
    session_factory: sessionmaker[Session],
    connection_id: int,
    owner: str,
    lease_seconds: int = 45,
) -> Iterator[None]:
    """Acquire V1's account lease for exactly the duration of one HTTP call,
    release it immediately after (success or failure) -- mirrors how V1
    itself holds this lease per background job, not per process.

    Distinguishes two very different situations, both surfaced identically
    as "no NEMSEI_V1_OWNERSHIP_BROKER_URL configured" would otherwise look:
    a deployment that has never heard of V1 coordination at all (unit tests
    with fake transports and fake credentials that were never going to make
    a real call regardless; a future point where V1 no longer runs
    FusionSolar) skips this check entirely -- there is no lease to hold
    because there is no shared account being coordinated. A deployment that
    *does* have the URL configured (docker-compose.v2.yml always sets it for
    `worker`/`scheduler`) and the broker is merely unreachable, denying, or
    erroring still fails closed exactly as before -- "broker indisponível
    => zero chamadas provider" is about that second case, not the first.
    Nobody bypasses this in the real deployment without deliberately
    removing the env var from docker-compose.v2.yml."""
    if not os.environ.get("NEMSEI_V1_OWNERSHIP_BROKER_URL", "").strip():
        yield
        return
    with session_factory() as session:
        connection = session.get(ProviderConnection, connection_id)
        if connection is None:
            raise V1LeaseUnavailable(f"Unknown provider connection {connection_id}.")
        account_key = _connection_account_key(connection)

    result = _request(
        "POST",
        "/acquire",
        body={"account_key": account_key, "owner": owner, "lease_seconds": lease_seconds},
    )
    if not result.get("granted"):
        raise V1LeaseUnavailable(
            f"V1 lease denied: {result.get('wait_reason', 'unknown')} "
            f"(held by {result.get('holder')!r} until {result.get('lease_until')})."
        )
    try:
        yield
    finally:
        with contextlib.suppress(V1LeaseUnavailable):
            _request("POST", "/release", body={"account_key": account_key, "owner": owner})
