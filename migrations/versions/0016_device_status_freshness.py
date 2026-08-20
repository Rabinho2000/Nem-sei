"""Device status facts: add freshness/quality/completeness and sync provenance.

M7 Fatia 2 (`docs/v2/DEVICE_TELEMETRY.md`) adds a live device-level read on top
of Fatia 1's V1-imported history. `device_status_facts` (migration `0015`) was
built for point-in-time import evidence only, so it carries no first-class
signal for "how much do we trust this reading" -- `monitoring_observations`
and `production_facts` both do, as `freshness`/`quality`/`completeness`
columns rather than buried JSON, and a live device read needs exactly the
same guarantee: a device that stopped communicating must be distinguishable
from one that is genuinely available, not silently indistinguishable because
the last known status was simply carried forward.

`sync_run_id` mirrors `monitoring_observations.sync_run_id`: a live reading
traces back to the run that fetched it (and, transitively, to its
`ProviderRequestAttempt` rows), which no V1-imported row can, since V1 kept no
run identity of its own. Nullable, `SET NULL`, exactly like its counterpart.

Every existing row is `v1_import` evidence with no reliable freshness/quality
signal of its own -- V1 never recorded either, so a computed default here
would be invented, not imported. All three new columns default to
`unknown` rather than guessing a value for 51 289 rows retroactively; only
future `live_read` facts populate them meaningfully.

Revision ID: 0016_device_status_freshness
Revises: 0015_device_status_facts
"""
from alembic import op
import sqlalchemy as sa


revision = "0016_device_status_freshness"
down_revision = "0015_device_status_facts"
branch_labels = None
depends_on = None


# Same vocabulary as monitoring_observations (monitoring/models.py), reused
# rather than re-derived: "how fresh/complete/trustworthy is this reading" is
# the same question at plant and device granularity.
FRESHNESS_STATES = ("fresh", "stale", "unknown")
QUALITY_STATES = ("complete", "partial", "missing", "invalid", "unknown")


def upgrade() -> None:
    op.add_column("device_status_facts", sa.Column("freshness", sa.String(length=24), nullable=False, server_default="unknown"))
    op.add_column("device_status_facts", sa.Column("quality", sa.String(length=24), nullable=False, server_default="unknown"))
    op.add_column("device_status_facts", sa.Column("completeness", sa.String(length=24), nullable=False, server_default="unknown"))
    op.add_column("device_status_facts", sa.Column("sync_run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_device_status_facts_sync_run", "device_status_facts", "sync_runs", ["sync_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint("ck_device_status_facts_freshness", "device_status_facts", f"freshness IN {FRESHNESS_STATES!r}")
    op.create_check_constraint("ck_device_status_facts_quality", "device_status_facts", f"quality IN {QUALITY_STATES!r}")
    op.create_check_constraint("ck_device_status_facts_completeness", "device_status_facts", f"completeness IN {QUALITY_STATES!r}")
    # The server defaults exist only to backfill existing rows safely; new
    # rows are always written with an explicit value from here on.
    op.alter_column("device_status_facts", "freshness", server_default=None)
    op.alter_column("device_status_facts", "quality", server_default=None)
    op.alter_column("device_status_facts", "completeness", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_device_status_facts_completeness", "device_status_facts", type_="check")
    op.drop_constraint("ck_device_status_facts_quality", "device_status_facts", type_="check")
    op.drop_constraint("ck_device_status_facts_freshness", "device_status_facts", type_="check")
    op.drop_constraint("fk_device_status_facts_sync_run", "device_status_facts", type_="foreignkey")
    op.drop_column("device_status_facts", "sync_run_id")
    op.drop_column("device_status_facts", "completeness")
    op.drop_column("device_status_facts", "quality")
    op.drop_column("device_status_facts", "freshness")
