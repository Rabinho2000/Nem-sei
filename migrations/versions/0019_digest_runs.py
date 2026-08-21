"""Digest runs: D6, one periodic summary of diagnostic incidents.

Studied and scoped in `docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`.
`diagnostic_incidents` (D1) and `notification_events` (D3) are unchanged
and unaware this exists -- a digest is a *summary* over incidents that
already exist, never a second finding or a second incident. One table, not
two: unlike `notification_events` (one row per incident per channel), a
digest is one summary delivered at most once to at most one channel per
run, so decision and delivery share this row.

Revision ID: 0019_digest_runs
Revises: 0018_notifications
"""
from alembic import op
import sqlalchemy as sa


revision = "0019_digest_runs"
down_revision = "0018_notifications"
branch_labels = None
depends_on = None


DIGEST_DELIVERY_STATUSES = ("pending", "delivered", "failed")


def upgrade() -> None:
    op.create_table(
        "digest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Integer()),
        sa.Column("delivery_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("delivery_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"delivery_status IN {DIGEST_DELIVERY_STATUSES!r}", name="ck_digest_runs_delivery_status"),
        sa.CheckConstraint("window_end > window_start", name="ck_digest_runs_window"),
        sa.CheckConstraint("(delivery_status = 'delivered') = (delivered_at IS NOT NULL)", name="ck_digest_runs_delivered_at"),
        sa.CheckConstraint("delivery_attempt_count >= 0", name="ck_digest_runs_attempt_count"),
        sa.UniqueConstraint("window_start", "window_end", name="uq_digest_runs_window"),
    )
    op.create_index("ix_digest_runs_window_end", "digest_runs", ["window_end"])


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM digest_runs")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} digest runs are auditable history, not disposable.")
    op.drop_index("ix_digest_runs_window_end", table_name="digest_runs")
    op.drop_table("digest_runs")
