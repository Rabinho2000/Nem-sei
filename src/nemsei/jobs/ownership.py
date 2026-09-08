"""Who is allowed to write, proved at the moment of writing.

The queue has always known who holds a job: `claim_next` writes
`lease_owner`/`lease_token` and `finish`/`fail` refuse to act on a row whose
token has changed underneath them. What it never knew is whether the *handler*
still holds it. `lease_token` appears nowhere in `integrations/`,
`monitoring/` or `sync/` -- the token reaches `execute()` and is dropped, so
every fact, every revision and every cursor advance is written with no check
at all. A worker whose 30-second lease expired mid-handler goes on writing,
and a second worker recovering the same job writes the same rows from the
other side.

This module is the missing half. `OwnershipFence` is the proof a worker
carries out of `claim_next`, and `assert_ownership` is where that proof is
checked -- inside the same transaction as the writes it authorises, against
the job row locked `FOR UPDATE` so it cannot change between the check and the
commit.

**Expiry alone revokes.** `assert_ownership` fails on an expired lease even
when nobody has reclaimed the job and the generation is still the newest one
in the table. This matters more than the reclaim case and is easier to get
wrong: the common failure is not a second worker stealing the job, it is one
worker going quiet for longer than its lease and then finishing as if nothing
happened. There is no second party to compare against there -- only the clock.

**It raises.** A boolean would be ignored by the one caller that forgets to
check it, and that caller is exactly the bug this module exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.jobs.models import Job
from nemsei.shared.clock import as_utc, utc_now


class OwnershipLost(RuntimeError):
    """This worker may no longer write on behalf of its job.

    Raised, never returned. Carries the reason so the caller can record a
    structured `error_code` on the collection run rather than a stringified
    exception.
    """

    def __init__(self, reason: str, *, job_id: int) -> None:
        super().__init__(f"Ownership lost for job {job_id}: {reason}")
        self.reason = reason
        self.job_id = job_id


# The reasons, as codes, so `collection_runs.error_code` stays a small closed
# vocabulary instead of free text.
REASON_JOB_MISSING = "job_missing"
REASON_NOT_RUNNING = "job_not_running"
REASON_TOKEN_MISMATCH = "lease_token_mismatch"
REASON_GENERATION_MISMATCH = "lease_generation_mismatch"
REASON_GENERATION_ABSENT = "lease_generation_absent"
REASON_LEASE_EXPIRED = "lease_expired"

OWNERSHIP_LOST_REASONS = (
    REASON_JOB_MISSING,
    REASON_NOT_RUNNING,
    REASON_TOKEN_MISMATCH,
    REASON_GENERATION_MISMATCH,
    REASON_GENERATION_ABSENT,
    REASON_LEASE_EXPIRED,
)


@dataclass(frozen=True)
class OwnershipFence:
    """The claim a worker holds, in the three facts a write has to re-prove."""

    job_id: int
    lease_token: str
    lease_generation: int


def assert_ownership(session: Session, fence: OwnershipFence, *, now: datetime | None = None) -> None:
    """Refuse to continue unless this worker still owns its job.

    Locks the job row for the remainder of the caller's transaction, so a
    concurrent `claim_next` or `recover_expired` either happened before this
    check (and fails it) or waits until this transaction ends.

    Every condition is checked, not just the generation: a job that was
    cancelled, or finished by another path, or whose lease quietly ran out,
    is no more entitled to write than one that was stolen.
    """
    reference = as_utc(now) if now is not None else utc_now()
    row = session.execute(
        select(Job.id, Job.status, Job.lease_token, Job.lease_generation, Job.lease_expires_at)
        .where(Job.id == fence.job_id)
        .with_for_update()
    ).mappings().first()

    if row is None:
        raise OwnershipLost(REASON_JOB_MISSING, job_id=fence.job_id)
    if row["status"] != "running":
        raise OwnershipLost(REASON_NOT_RUNNING, job_id=fence.job_id)
    if row["lease_token"] != fence.lease_token:
        raise OwnershipLost(REASON_TOKEN_MISMATCH, job_id=fence.job_id)
    if row["lease_generation"] is None:
        # A job claimed before fencing existed. Failing closed is the point:
        # it cannot prove anything, so it is allowed nothing.
        raise OwnershipLost(REASON_GENERATION_ABSENT, job_id=fence.job_id)
    if int(row["lease_generation"]) != fence.lease_generation:
        raise OwnershipLost(REASON_GENERATION_MISMATCH, job_id=fence.job_id)
    expires_at = row["lease_expires_at"]
    if expires_at is None or as_utc(expires_at) <= reference:
        # Deliberately independent of whether anyone else took the job. An
        # expired lease authorises nothing on its own.
        raise OwnershipLost(REASON_LEASE_EXPIRED, job_id=fence.job_id)
