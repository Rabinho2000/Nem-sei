"""Notification baseline snapshots: close the opened_at-only baseline gap.

Studied and scoped in `docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` s28.
`opened_at < baseline_at` alone cannot distinguish "pre-existing incident,
unchanged" from "pre-existing incident that later worsened into scope" --
both look identical by that comparison alone, forever. This table records,
once, what a pre-existing incident's severity was the first time a policy
evaluated it at/after its own baseline_at -- a fixed, auditable reference
point to compare later evaluations against, not a second finding or a
second incident. `diagnostic_incidents` and `notification_events` are both
unchanged and unaware this exists.

Revision ID: 0020_baseline_snapshots
Revises: 0019_digest_runs
"""
from alembic import op
import sqlalchemy as sa


revision = "0020_baseline_snapshots"
down_revision = "0019_digest_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_baseline_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity_at_capture", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["diagnostic_incidents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["policy_id"], ["notification_policies.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("incident_id", "policy_id", name="uq_notification_baseline_snapshots_identity"),
    )


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM notification_baseline_snapshots")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} baseline snapshots are auditable history, not disposable.")
    op.drop_table("notification_baseline_snapshots")
