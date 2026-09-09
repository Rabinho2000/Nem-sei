"""Monotonic ownership fencing for the job queue.

`jobs.lease_token` identifies a claim; it cannot order two of them. It is
`secrets.token_urlsafe(24)` -- a fresh random string on every claim -- so when
a lease expires and the job is claimed again, the old token and the new one
are simply *different*, with no way to say which came first. That is enough to
guard the queue row (`finish` and `fail` already match on it) and not enough
to guard anything else: a worker whose lease expired mid-handler can still
write facts, advance a cursor, and close a run, because none of those writes
ever look at the token.

`lease_generation` is the missing order. It comes from a sequence, so a later
ownership always carries a strictly greater number, and a write can be
rejected by comparing rather than by guessing.

Deliberately **not** a column default. A `DEFAULT nextval(...)` would burn a
generation on any INSERT and, worse, invite an `UPDATE` somewhere else to
quietly advance it. The sequence is read in exactly one place --
`JobRepository.claim_next`, when an ownership is actually acquired -- and
`recover_expired` clears the column instead of allocating a new value, so
there is no second bump and no second source of truth.

Nullable, with no backfill. Rows that predate this migration keep `NULL`, and
`NULL` never equals a fence's generation, so every fenced write against an
old row fails closed. That is the intended reading: a job claimed before
fencing existed cannot prove it still owns anything.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_job_lease_generation"
down_revision = "0044_production_scheduling"
branch_labels = None
depends_on = None


SEQUENCE_NAME = "jobs_lease_generation_seq"


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME} AS BIGINT START WITH 1 INCREMENT BY 1"))
    op.add_column("jobs", sa.Column("lease_generation", sa.BigInteger(), nullable=True))
    # The fence reads one job row by primary key and compares the generation.
    # The index is on the pair so that read is index-only rather than a heap
    # fetch on the queue's hottest table.
    op.create_index("ix_jobs_lease_generation", "jobs", ["id", "lease_generation"])


def downgrade() -> None:
    op.drop_index("ix_jobs_lease_generation", table_name="jobs")
    op.drop_column("jobs", "lease_generation")
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}"))
