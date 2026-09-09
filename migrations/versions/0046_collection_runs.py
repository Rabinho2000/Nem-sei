"""One attempt to satisfy one collection obligation, and what it actually did.

`jobs` records that a handler ran and returned. `sync_runs` records that a
provider was contacted. Neither answers "was the data we were supposed to
collect actually collected", and the difference is not academic: Phase 0 found
a Sigenergy run that finished `success`, advanced its cursor, and wrote a
day's total from a day still in progress. Every counter said the work was
done.

`collection_runs` is where that question gets an answer backed by evidence.
`fulfilled` is not "the handler returned" -- it is a state the database itself
refuses to accept unless the run can show it was owned at the end, knew how
many scopes it had to write, and wrote all of them.

The constraints below are deliberate duplication of what the service layer
already checks. The service is where a good error message comes from; the
constraint is what still holds when someone adds a second write path in a
year and forgets the service exists.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_collection_runs"
down_revision = "0045_job_lease_generation"
branch_labels = None
depends_on = None


COLLECTION_RUN_STATUSES = (
    "pending",
    "running",
    "fulfilled",
    "partial",
    "failed",
    "lost_ownership",
    "cancelled",
    "superseded",
)


def upgrade() -> None:
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "provider_connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("scope_kind", sa.String(length=24), nullable=False),
        sa.Column("scope_key", sa.String(length=120), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(length=24), nullable=False),
        # A run outlives the job that made it: the job row can be pruned and
        # the collection history must survive that, so SET NULL rather than
        # CASCADE.
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        # Which ownership closed this run. Required for `fulfilled` by the
        # constraint below: a run that cannot name the generation that
        # finished it has not proved it was owned at the end.
        sa.Column("lease_generation", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("facts_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # NULL means "nobody counted", which can never satisfy the fulfilled
        # constraint. An unknown denominator is not a full one.
        sa.Column("scopes_required", sa.Integer(), nullable=True),
        sa.Column("scopes_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cursor_advanced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN {COLLECTION_RUN_STATUSES!r}", name="ck_collection_runs_status"),
        sa.CheckConstraint("period_start <= period_end", name="ck_collection_runs_period"),
        sa.CheckConstraint("attempt >= 1", name="ck_collection_runs_attempt"),
        sa.CheckConstraint("facts_written >= 0", name="ck_collection_runs_facts_written"),
        sa.CheckConstraint("scopes_written >= 0", name="ck_collection_runs_scopes_written"),
        sa.CheckConstraint(
            "scopes_required IS NULL OR scopes_required >= 0",
            name="ck_collection_runs_scopes_required",
        ),
        sa.CheckConstraint(
            "scopes_required IS NULL OR scopes_written <= scopes_required",
            name="ck_collection_runs_scopes_within_required",
        ),
        # The predicate, in the schema. `fulfilled` is unreachable without the
        # evidence that makes it true, whatever the calling code believes.
        sa.CheckConstraint(
            "status <> 'fulfilled' OR ("
            " lease_generation IS NOT NULL"
            " AND scopes_required IS NOT NULL"
            " AND scopes_written = scopes_required"
            " AND finished_at IS NOT NULL"
            ")",
            name="ck_collection_runs_fulfilled_evidence",
        ),
    )
    # Two workers cannot both close the same logical scope. Partial, so the
    # many non-fulfilled attempts of a scope stay unconstrained and its
    # history is preserved.
    op.create_index(
        "uq_collection_runs_fulfilled_scope",
        "collection_runs",
        ["provider_connection_id", "capability", "scope_kind", "scope_key", "period_start", "period_end"],
        unique=True,
        postgresql_where=sa.text("status = 'fulfilled'"),
    )
    # "What happened to this scope lately", the question the coverage screens
    # and the future obligation engine both ask.
    op.create_index(
        "ix_collection_runs_scope_period",
        "collection_runs",
        ["provider_connection_id", "capability", "scope_kind", "scope_key", "period_start"],
    )
    op.create_index("ix_collection_runs_job", "collection_runs", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_runs_job", table_name="collection_runs")
    op.drop_index("ix_collection_runs_scope_period", table_name="collection_runs")
    op.drop_index("uq_collection_runs_fulfilled_scope", table_name="collection_runs")
    op.drop_table("collection_runs")
