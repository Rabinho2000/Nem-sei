"""Starting, finishing and refusing to finish a collection run.

The rule this module exists to enforce, stated once so no handler has to
remember it:

    a handler returning without an exception is *execution success*.
    it is not *collection fulfilment*.

Phase 0 found what happens when those two are the same thing. A Sigenergy run
counted an empty day as accepted, finished `success`, and advanced its cursor
past a day it had never really read. Every signal said the work was done. The
fix is not a better handler -- it is to stop letting the handler be the one
who decides.

So `finalize_collection_run` does not take a status. It takes evidence, and
derives the status from it. A caller cannot ask for `fulfilled`; it can only
supply facts that happen to satisfy the predicate, and if they do not, the run
lands on `partial` or `failed` no matter how confident the caller was.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.jobs.ownership import OwnershipFence, OwnershipLost, assert_ownership
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sync.collection_models import (
    ALLOWED_TRANSITIONS,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_LOST_OWNERSHIP,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    CollectionRun,
)


class CollectionRunStateError(RuntimeError):
    """An illegal transition was attempted, e.g. failed -> fulfilled."""


@dataclass(frozen=True)
class CollectionEvidence:
    """What a run can actually show for itself.

    `scopes_required` is `None` when the caller could not determine how many
    scopes it owed. That is not the same as zero and must never satisfy
    fulfilment: an unknown denominator is not a full one.
    """

    facts_written: int = 0
    scopes_required: int | None = None
    scopes_written: int = 0
    cursor_advanced: bool = False
    # Set when the capability has closed-period semantics and the period is
    # not closed yet. Blocks fulfilment on its own, whatever the counters say.
    period_closed: bool = True
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.facts_written < 0 or self.scopes_written < 0:
            raise ValueError("Collection evidence counters cannot be negative.")
        if self.scopes_required is not None and self.scopes_required < 0:
            raise ValueError("A required scope count cannot be negative.")


def can_fulfill_collection_run(evidence: CollectionEvidence, *, requires_cursor: bool) -> bool:
    """The one definition of fulfilment. Ownership is checked separately.

    Ownership is deliberately not a parameter here: it is proved against the
    live job row inside the transaction, not passed in as a value a caller
    could get wrong. This function answers only "is the *evidence* complete".
    """
    if evidence.error_code is not None:
        return False
    if not evidence.period_closed:
        return False
    if evidence.scopes_required is None:
        return False
    if evidence.scopes_written != evidence.scopes_required:
        return False
    if requires_cursor and not evidence.cursor_advanced:
        return False
    return True


def start_collection_run(
    session: Session,
    *,
    provider_connection_id: int,
    capability: str,
    scope_kind: str,
    scope_key: str,
    period_start: datetime,
    period_end: datetime,
    job_id: int | None = None,
    lease_generation: int | None = None,
    now: datetime | None = None,
) -> CollectionRun:
    """Open a `running` attempt for a scope, numbered after its predecessors.

    The attempt number is derived from what is already recorded for this
    scope rather than passed in, so a retry cannot claim to be attempt 1 and
    hide the ones before it.
    """
    moment = as_utc(now) if now is not None else utc_now()
    previous = session.scalar(
        select(CollectionRun.attempt)
        .where(
            CollectionRun.provider_connection_id == provider_connection_id,
            CollectionRun.capability == capability,
            CollectionRun.scope_kind == scope_kind,
            CollectionRun.scope_key == scope_key,
            CollectionRun.period_start == as_utc(period_start),
            CollectionRun.period_end == as_utc(period_end),
        )
        .order_by(CollectionRun.attempt.desc())
        .limit(1)
    )
    run = CollectionRun(
        provider_connection_id=provider_connection_id,
        capability=capability,
        scope_kind=scope_kind,
        scope_key=scope_key,
        period_start=as_utc(period_start),
        period_end=as_utc(period_end),
        attempt=(int(previous) + 1) if previous is not None else 1,
        status=STATUS_RUNNING,
        job_id=job_id,
        lease_generation=lease_generation,
        started_at=moment,
        created_at=moment,
        updated_at=moment,
    )
    session.add(run)
    session.flush()
    return run


def _transition(run: CollectionRun, target: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(run.status, ())
    if target not in allowed:
        raise CollectionRunStateError(
            f"collection_run {run.id} cannot move from {run.status!r} to {target!r}"
        )
    run.status = target


def finalize_collection_run(
    session: Session,
    run: CollectionRun,
    *,
    fence: OwnershipFence,
    evidence: CollectionEvidence,
    requires_cursor: bool,
    now: datetime | None = None,
) -> CollectionRun:
    """Close a run at the status its evidence earns, not the one asked for.

    Takes no status argument, on purpose. The final ownership assertion
    happens here -- the lease can expire while the transaction is doing
    perfectly legitimate work, so ownership proved before the writes says
    nothing about ownership at the moment of closing.

    Raises `OwnershipLost` without touching the row. The caller must roll the
    transaction back; recording the loss is a separate concern precisely
    because it cannot be written in the transaction being discarded.
    """
    assert_ownership(session, fence, now=now)

    moment = as_utc(now) if now is not None else utc_now()
    if can_fulfill_collection_run(evidence, requires_cursor=requires_cursor):
        target = STATUS_FULFILLED
    elif evidence.error_code is not None:
        target = STATUS_FAILED
    else:
        # Nothing went wrong that anyone noticed, and the work is still not
        # all there. That is the state the old code called success.
        target = STATUS_PARTIAL

    _transition(run, target)
    run.lease_generation = fence.lease_generation
    run.facts_written = evidence.facts_written
    run.scopes_required = evidence.scopes_required
    run.scopes_written = evidence.scopes_written
    run.cursor_advanced = evidence.cursor_advanced
    run.error_code = evidence.error_code
    run.error_detail = evidence.error_detail
    run.finished_at = moment
    run.updated_at = moment
    session.flush()
    return run


def mark_collection_run_lost_ownership(
    session: Session,
    run_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> CollectionRun | None:
    """Record, in a *fresh* transaction, that a run lost its job.

    Takes an id rather than an instance because the instance belongs to a
    transaction that has just been rolled back. Writing this inside that
    transaction would discard it along with everything else, and -- worse --
    would let a reader believe the loss had been recorded when nothing was.

    Returns None when the row is gone or has already reached a terminal
    state: failing to record a diagnostic must never be able to resurrect an
    invalid commit, so this is best-effort by construction.
    """
    run = session.get(CollectionRun, run_id)
    if run is None:
        return None
    if STATUS_LOST_OWNERSHIP not in ALLOWED_TRANSITIONS.get(run.status, ()):
        return None
    moment = as_utc(now) if now is not None else utc_now()
    _transition(run, STATUS_LOST_OWNERSHIP)
    run.error_code = reason
    run.finished_at = moment
    run.updated_at = moment
    session.flush()
    return run


__all__ = [
    "CollectionEvidence",
    "CollectionRunStateError",
    "OwnershipLost",
    "can_fulfill_collection_run",
    "finalize_collection_run",
    "mark_collection_run_lost_ownership",
    "start_collection_run",
]
