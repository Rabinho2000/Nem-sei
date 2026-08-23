"""Process-local, credential-keyed FusionSolar session reuse.

Mirrors what V1 already does (`monitoring_board/app_factory.py`:
`FUSIONSOLAR_SESSION_CACHE`, a module-level dict + `threading.Lock`, TTL 55
minutes, keyed by `f"{base_url}|{username}"`) -- independently implemented
here because V2's `FusionSolarClient` is a small dataclass with an
injectable transport rather than a `requests.Session`, but the shape of the
problem and the fix are the same one V1 already solved.

Every V2 sync (`discover`, `sync_current_monitoring`, `sync_incremental`,
device status polling) built a brand new `FusionSolarClient` and called
`.authenticate()` on it, with no cache anywhere -- so N syncs cost N logins.
V1 never does this: it reuses one session for up to 55 minutes. Discovered
live 2026-08-23 when a handful of test syncs against the shared account
tripped FusionSolar's own login rate limit (`authentication` endpoint
family, not a data-call budget) after 13 cumulative logins -- see
docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's rollout write-up.

Design constraints this satisfies (session-reuse rollout priority):
  - a valid cached session skips the network call entirely -- no login, no
    ownership-broker acquire, no request-control evidence row, exactly like
    V1 skipping its own cached-session path;
  - TTL is conservative (`DEFAULT_SESSION_TTL_MINUTES`, 45 < V1's 55) so V2
    never plausibly outlives a session V1 itself would already have
    refreshed, and re-authenticates a little early rather than risk relying
    on a session near its unknown real expiry;
  - a single lock serializes the whole "check cache, else authenticate and
    populate it" critical section per process, so N concurrent callers for
    the same account produce at most one login, not N (the retry-storm
    case) -- this only prevents duplicate logins *within one process*;
    cross-process/cross-app duplicate logins are what the ownership broker
    already exists to prevent, and remain gated by it here (the real network
    call inside the lock still goes through `FusionSolarRequestController`,
    which acquires V1's lease first);
  - `invalidate()` drops a stale entry so the *next* call re-authenticates;
    callers must call it whenever a later request signals the session
    itself is dead (see `client.py`'s widened session-expiry detection,
    fixed alongside this module -- previously only checked on the login
    call itself, so a session dying *between* logins during ordinary use
    was misclassified and never triggered a fresh login);
  - nothing here ever logs or exposes the token, cookies, username, or
    password -- only the non-secret cache key (a hash-free `base_url|username`
    string, already only ever handled in-process, never persisted).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from nemsei.integrations.fusionsolar.client import (
    FusionSolarClient,
    FusionSolarCredentials,
    FusionSolarTransport,
)
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.providers.errors import ProviderError, ProviderErrorCode


DEFAULT_SESSION_TTL_MINUTES = 45


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _CacheEntry:
    transport: FusionSolarTransport
    token: str
    expires_at: datetime


class FusionSolarSessionCache:
    """One instance per process is the point -- see `default_session_cache()`."""

    def __init__(
        self,
        *,
        ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._ttl = timedelta(minutes=max(1, ttl_minutes))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}

    @staticmethod
    def _key(credentials: FusionSolarCredentials) -> str:
        return f"{credentials.base_url.strip().lower().rstrip('/')}|{credentials.username.strip().lower()}"

    def get_or_authenticate(
        self,
        credentials: FusionSolarCredentials,
        *,
        client_factory: Callable[[FusionSolarCredentials], FusionSolarClient],
        authenticate: Callable[[FusionSolarClient], tuple[object, ProviderError | None]],
    ) -> tuple[FusionSolarClient | None, ProviderError | None, bool]:
        """Returns (client, error, reused). `authenticate` is the caller's
        `FusionSolarRequestController.call(..., operation=client.authenticate)`
        closure -- kept as an injected callable so the real HTTP call (and
        its ownership-broker lease, its request-control evidence row) only
        ever happens for a genuine cache miss, never for a reuse.

        `client_factory` is called with exactly one positional argument
        (`credentials`), matching the `Callable[[FusionSolarCredentials],
        FusionSolarClient]` contract every existing caller and test fake
        already relies on (`lambda credentials: FusionSolarClient(credentials,
        transport=...)`); this never asks it to also accept a transport
        override. Reuse instead patches the freshly built client's own
        `transport`/`_token` fields after construction -- both are plain,
        unfrozen dataclass attributes, so this is a supported mutation, not
        a hack around immutability."""
        key = self._key(credentials)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                client = client_factory(credentials)
                client.transport = entry.transport
                client._token = entry.token
                return client, None, True

            client = client_factory(credentials)
            _value, error = authenticate(client)
            if error is not None:
                return None, error, False
            if client._token is None:
                # authenticate() "succeeded" but left no usable token --
                # never cache a session we could not actually confirm.
                return None, ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar login produced no usable session token."), False
            self._entries[key] = _CacheEntry(transport=client.transport, token=client._token, expires_at=now + self._ttl)
            return client, None, False

    def invalidate(self, credentials: FusionSolarCredentials) -> None:
        with self._lock:
            self._entries.pop(self._key(credentials), None)


_default_cache: FusionSolarSessionCache | None = None
_default_cache_lock = threading.Lock()


def default_session_cache() -> FusionSolarSessionCache:
    """Process-wide singleton -- every service instantiation in this worker
    shares the same cache, the way V1's module-level global does."""
    global _default_cache
    if _default_cache is None:
        with _default_cache_lock:
            if _default_cache is None:
                _default_cache = FusionSolarSessionCache()
    return _default_cache


def authenticated_client(
    *,
    calls: FusionSolarRequestController,
    connection_id: int,
    sync_run_id: int,
    purpose: str,
    credentials: FusionSolarCredentials,
    client_factory: Callable[..., FusionSolarClient],
    cache: FusionSolarSessionCache | None = None,
) -> tuple[FusionSolarClient | None, ProviderError | None]:
    """Drop-in replacement for the `client = client_factory(credentials);
    calls.call(..., operation=client.authenticate)` pattern repeated in
    service.py / monitoring.py / production.py. A cache hit makes zero
    network calls and is invisible to `calls` (no ownership lease acquired,
    no request-control evidence row) -- exactly mirroring what V1 does when
    it reuses its own cached session."""
    cache = cache or default_session_cache()

    def _do_authenticate(client: FusionSolarClient) -> tuple[object, ProviderError | None]:
        return calls.call(
            connection_id=connection_id,
            sync_run_id=sync_run_id,
            endpoint_family="authentication",
            purpose=purpose,
            operation=client.authenticate,
        )

    client, error, _reused = cache.get_or_authenticate(credentials, client_factory=client_factory, authenticate=_do_authenticate)
    return client, error


def invalidate_session(credentials: FusionSolarCredentials, *, cache: FusionSolarSessionCache | None = None) -> None:
    (cache or default_session_cache()).invalidate(credentials)


def is_session_expiry(error: ProviderError | None) -> bool:
    """True for both a rejected login and a session that died mid-use on a
    later data call -- see client.py's widened detection. Callers use this
    to decide whether to invalidate the cache so the next call re-logs in,
    rather than silently retrying with a session FusionSolar has already
    discarded."""
    return error is not None and error.code is ProviderErrorCode.AUTHENTICATION
