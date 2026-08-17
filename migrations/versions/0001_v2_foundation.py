"""Create V2 foundation persistence.

Revision ID: 0001_v2_foundation
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_v2_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255)),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=120)),
        sa.Column("lease_token", sa.String(length=64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", sa.JSON()),
        sa.Column("error_type", sa.String(length=120)),
        sa.Column("error_message", sa.Text()),
        sa.CheckConstraint("status IN ('queued', 'running', 'waiting', 'success', 'partial', 'failed', 'cancelled')", name="ck_jobs_status"),
    )
    op.create_index("ix_jobs_due", "jobs", ["status", "available_at", "priority", "id"])
    op.create_index("uq_jobs_active_dedupe", "jobs", ["job_type", "dedupe_key"], unique=True, postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'waiting')"))
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=24)),
        sa.Column("to_status", sa.String(length=24)),
        sa.Column("actor_source", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("actor_source IN ('web', 'scheduler', 'worker', 'recovery', 'system')", name="ck_job_events_actor_source"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_table(
        "scheduler_leases",
        sa.Column("name", sa.String(length=120), primary_key=True),
        sa.Column("owner_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "schedule_state",
        sa.Column("schedule_key", sa.String(length=120), primary_key=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedule_state")
    op.drop_table("scheduler_leases")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("uq_jobs_active_dedupe", table_name="jobs")
    op.drop_index("ix_jobs_due", table_name="jobs")
    op.drop_table("jobs")
