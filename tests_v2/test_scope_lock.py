"""The lock key has to be the same number everywhere, or it locks nothing.

These are pure unit tests on purpose: the canonicalisation is the part that
can silently break (a timezone that renders differently, a delimiter that a
value can imitate), and none of it needs a database to prove.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nemsei.sync.scope_lock import (
    SCOPE_KIND_ASSET,
    SCOPE_KIND_CONNECTION,
    collection_scope_canonical_form,
    collection_scope_lock_key,
)


BASE = {
    "connection_id": 5,
    "capability": "production_history",
    "scope_kind": SCOPE_KIND_CONNECTION,
    "scope_key": "5",
    "period_start": datetime(2026, 9, 1, tzinfo=timezone.utc),
    "period_end": datetime(2026, 9, 8, tzinfo=timezone.utc),
}


def key(**overrides):
    return collection_scope_lock_key(**{**BASE, **overrides})


def test_the_same_scope_always_produces_the_same_key():
    assert key() == key()


def test_the_same_instant_in_another_timezone_is_the_same_scope():
    """09:00Z and 10:00+01:00 are one moment; two keys would be two locks."""
    lisbon = ZoneInfo("Europe/Lisbon")
    start_utc = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    start_local = start_utc.astimezone(lisbon)
    assert start_local.utcoffset() != timedelta(0), "the test needs a real offset to be meaningful"

    assert key(period_start=start_utc) == key(period_start=start_local)


def test_a_different_connection_is_a_different_scope():
    assert key(connection_id=6) != key()


def test_a_different_capability_is_a_different_scope():
    assert key(capability="device_monitoring") != key()


def test_a_different_scope_kind_is_a_different_scope():
    assert key(scope_kind=SCOPE_KIND_ASSET) != key()


def test_a_different_scope_key_is_a_different_scope():
    assert key(scope_key="6") != key()


@pytest.mark.parametrize(
    "field, value",
    [
        ("period_start", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("period_end", datetime(2026, 9, 9, tzinfo=timezone.utc)),
    ],
)
def test_a_different_period_is_a_different_scope(field, value):
    assert key(**{field: value}) != key()


def test_capability_and_scope_kind_are_case_insensitive():
    """Normalised, so a caller shouting the capability still takes one lock."""
    assert key(capability="PRODUCTION_HISTORY") == key()
    assert key(scope_kind=SCOPE_KIND_CONNECTION.upper()) == key()


def test_the_key_fits_a_signed_bigint():
    """`pg_advisory_xact_lock(bigint)` rejects anything outside this range."""
    for connection_id in range(1, 400):
        value = key(connection_id=connection_id)
        assert -(2**63) <= value < 2**63


def test_fields_cannot_impersonate_one_another():
    """The delimiter is not something a value can contain.

    Without a separator the pair ('1', '23') and ('12', '3') would hash the
    same string. The unit separator cannot appear in an id, a capability or
    an ISO timestamp, so no shuffling of the parts can collide.
    """
    left = collection_scope_canonical_form(**{**BASE, "connection_id": 1, "scope_key": "23"})
    right = collection_scope_canonical_form(**{**BASE, "connection_id": 12, "scope_key": "3"})
    assert left != right
    assert "\x1f" in left


def test_an_unknown_scope_kind_is_refused():
    with pytest.raises(ValueError):
        key(scope_kind="galaxy")


def test_the_key_survives_a_fresh_interpreter_with_a_different_hash_seed():
    """The failure this guards against is invisible in-process.

    `hash()` is salted per process, so a key built with it would agree with
    itself all through this suite and disagree between two workers -- a lock
    that never locks and never fails a test. Two subprocesses with different
    PYTHONHASHSEED values is the only way to actually prove the key is
    independent of it.
    """
    program = (
        "from datetime import datetime, timezone;"
        "from nemsei.sync.scope_lock import collection_scope_lock_key as k;"
        "print(k(connection_id=5, capability='production_history', scope_kind='connection',"
        " scope_key='5', period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),"
        " period_end=datetime(2026, 9, 8, tzinfo=timezone.utc)))"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        )
        outputs.add(completed.stdout.strip())
    assert len(outputs) == 1, f"the key moved with the hash seed: {outputs}"
    assert outputs == {str(key())}
