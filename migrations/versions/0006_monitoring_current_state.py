"""Add mutable current-monitoring confirmation evidence.

Revision ID: 0006_monitoring_current_state
Revises: 0005_provider_sync_foundation
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_monitoring_current_state"
down_revision = "0005_provider_sync_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitoring_current_states",
        sa.Column(
            "provider_mapping_id",
            sa.Integer(),
            sa.ForeignKey("asset_provider_mappings.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "latest_observation_id",
            sa.Integer(),
            sa.ForeignKey("monitoring_observations.id", ondelete="RESTRICT"),
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_successful_sync_run_id",
            sa.Integer(),
            sa.ForeignKey("sync_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monitoring_current_states")
