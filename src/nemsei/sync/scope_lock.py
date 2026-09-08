"""Serialise the writers of one logical collection scope.

The queue already prevents two *jobs* for the same work: a partial unique
index on `(job_type, dedupe_key)` refuses a duplicate while one is queued,
running or waiting. That is dedupe, and dedupe is not locking. It says
nothing once a job leaves those states, nothing about a job recovered from an
expired lease while its handler is still alive, and nothing at all about the
rows the handler writes. Two writers can still meet inside
`sync_cursors`, or inside a revision chain, and the queue will not have
noticed.

This module gives them somewhere to queue up. `pg_advisory_xact_lock` is
transaction-scoped: it is taken as the first statement of the authoritative
transaction and released by COMMIT or ROLLBACK, with nothing to unlock by
hand and nothing left held by a process that died.

**Why advisory and not `SELECT ... FOR UPDATE`.** A row lock needs a row, and
the first collection for a scope has none -- the run that would be locked is
the run about to be created. Locking the *connection* row instead would
serialise unrelated capabilities against each other. An advisory key locks
the idea of the scope, which is the thing that must not have two writers,
whether or not anything has been written for it yet.

**Granularity: at least as coarse as the cursor.** `sync_cursors` is unique
per `(provider_connection_id, capability, cursor_key)`, so every asset of a
connection shares one production cursor. A per-asset lock would let two
holders advance that same cursor concurrently -- the exact race the lock
exists to prevent. So production history locks at connection level, and
`scope_kind` stays in the key so a capability that genuinely owns per-asset
state can be finer later without changing this function's shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import blake2b

from sqlalchemy import text
from sqlalchemy.orm import Session

from nemsei.shared.clock import as_utc


SCOPE_KIND_CONNECTION = "connection"
SCOPE_KIND_ASSET = "asset"
SCOPE_KIND_DEVICE = "device"

SCOPE_KINDS = (SCOPE_KIND_CONNECTION, SCOPE_KIND_ASSET, SCOPE_KIND_DEVICE)

# ASCII unit separator. Chosen because it cannot occur in any of the fields
# being joined -- capability and scope_kind are lowercase identifiers, scope
# keys are ids, timestamps are ISO-8601 -- so no combination of values can
# imitate a different combination by containing the delimiter itself.
_DELIMITER = "\x1f"


def _canonical_instant(value: datetime) -> str:
    """One instant, one string, whatever timezone it arrived in.

    `as_utc` normalises the offset, so 10:00+01:00 and 09:00Z are the same
    moment and must produce the same key. `isoformat()` on a UTC-normalised
    aware datetime is stable and always carries an explicit `+00:00`, which
    keeps a naive value from ever colliding with an aware one.
    """
    normalized = as_utc(value)
    if normalized.tzinfo is None:  # pragma: no cover - as_utc always attaches one
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def collection_scope_canonical_form(
    *,
    connection_id: int,
    capability: str,
    scope_kind: str,
    scope_key: str,
    period_start: datetime,
    period_end: datetime,
) -> str:
    """The exact string that gets hashed, exposed so a test can pin it."""
    # Normalise first, then validate: the normalised form is what gets
    # hashed, so it is also the form the vocabulary check has to agree with.
    normalized_kind = str(scope_kind).strip().lower()
    if normalized_kind not in SCOPE_KINDS:
        raise ValueError(f"Unknown collection scope kind: {scope_kind!r}")
    return _DELIMITER.join(
        (
            str(int(connection_id)),
            str(capability).strip().lower(),
            normalized_kind,
            str(scope_key).strip(),
            _canonical_instant(period_start),
            _canonical_instant(period_end),
        )
    )


def collection_scope_lock_key(
    *,
    connection_id: int,
    capability: str,
    scope_kind: str,
    scope_key: str,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """A stable signed 64-bit advisory key for one logical collection scope.

    BLAKE2b rather than Python's `hash()`, which is randomised per process by
    `PYTHONHASHSEED` and would hand two workers different keys for the same
    scope -- a lock that silently fails to lock. Rather than
    `hashtextextended()` too: hashing here keeps the key identical across
    PostgreSQL versions and lets the canonicalisation be unit-tested without
    a database.
    """
    canonical = collection_scope_canonical_form(
        connection_id=connection_id,
        capability=capability,
        scope_kind=scope_kind,
        scope_key=scope_key,
        period_start=period_start,
        period_end=period_end,
    )
    digest = blake2b(canonical.encode("utf-8"), digest_size=8).digest()
    unsigned = int.from_bytes(digest, "big", signed=False)
    # `pg_advisory_xact_lock(bigint)` takes a signed value; fold the top half
    # of the range down rather than truncating, so no two keys collapse.
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def acquire_collection_scope_lock(
    session: Session,
    *,
    connection_id: int,
    capability: str,
    scope_kind: str,
    scope_key: str,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """Block until this transaction owns the scope. Released by COMMIT/ROLLBACK.

    Transaction-scoped on purpose: a session-level lock would outlive the
    work, and a worker that died holding one would keep the scope shut until
    its connection was reaped.
    """
    key = collection_scope_lock_key(
        connection_id=connection_id,
        capability=capability,
        scope_kind=scope_kind,
        scope_key=scope_key,
        period_start=period_start,
        period_end=period_end,
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return key
