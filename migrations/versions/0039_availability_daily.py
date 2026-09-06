"""Materialized device/asset availability, ported from V1's sampled engine.

Two tables only: `device_availability_daily` and `asset_availability_daily`,
the direct analogues of V1's `inverter_availability_sampled_daily` /
`plant_availability_sampled_daily`
(`monitoring_board/services/sampled_availability.py`). See
`docs/v2/AVAILABILITY_MIGRATION_PLAN.md` for the full port.

Deliberately does not touch `device_status_facts` (migrations 0015/0016,
already the raw-sample analogue of V1's `device_realtime_snapshots`) or
`asset_provider_mappings` (already temporal, replacing V1's own
`provider_device_configuration_history` entirely -- see the plan's §0/§2).
Nothing else in the schema changes.

Revision ID: 0039_availability_daily
Revises: 0038_work_order_priority
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_availability_daily"
down_revision = "0038_work_order_priority"
branch_labels = None
depends_on = None

COVERAGE_STATES = ("complete", "partial", "missing", "indeterminate")
AVAILABILITY_SOURCES = ("fusionsolar_sampled",)


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.create_table(
        "device_availability_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("availability_date", sa.Date(), nullable=False),
        sa.Column("availability_pct", sa.Numeric(5, 2)),
        sa.Column("valid_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minimum_required_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_status", sa.String(length=24), nullable=False),
        sa.Column("warning_codes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("operational_window_start", sa.DateTime(timezone=True)),
        sa.Column("operational_window_end", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="fusionsolar_sampled"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "availability_date", name="uq_device_availability_daily_day"),
        sa.CheckConstraint(f"coverage_status IN ({_values(COVERAGE_STATES)})", name="ck_device_availability_daily_coverage"),
        sa.CheckConstraint(f"source IN ({_values(AVAILABILITY_SOURCES)})", name="ck_device_availability_daily_source"),
        sa.CheckConstraint(
            "availability_pct IS NULL OR (availability_pct >= 0 AND availability_pct <= 100)",
            name="ck_device_availability_daily_pct_range",
        ),
        sa.CheckConstraint(
            "coverage_status = 'complete' OR availability_pct IS NULL",
            name="ck_device_availability_daily_pct_requires_complete",
        ),
    )
    op.create_index(
        "ix_device_availability_daily_asset_date", "device_availability_daily", ["asset_id", "availability_date"]
    )

    op.create_table(
        "asset_availability_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("availability_date", sa.Date(), nullable=False),
        sa.Column("availability_pct", sa.Numeric(5, 2)),
        sa.Column("valid_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_device_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_device_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minimum_required_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_status", sa.String(length=24), nullable=False),
        sa.Column("warning_codes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("operational_window_start", sa.DateTime(timezone=True)),
        sa.Column("operational_window_end", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="fusionsolar_sampled"),
        sa.Column("calculation_details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("asset_id", "availability_date", name="uq_asset_availability_daily_day"),
        sa.CheckConstraint(f"coverage_status IN ({_values(COVERAGE_STATES)})", name="ck_asset_availability_daily_coverage"),
        sa.CheckConstraint(f"source IN ({_values(AVAILABILITY_SOURCES)})", name="ck_asset_availability_daily_source"),
        sa.CheckConstraint(
            "availability_pct IS NULL OR (availability_pct >= 0 AND availability_pct <= 100)",
            name="ck_asset_availability_daily_pct_range",
        ),
        sa.CheckConstraint(
            "coverage_status = 'complete' OR availability_pct IS NULL",
            name="ck_asset_availability_daily_pct_requires_complete",
        ),
    )
    op.create_index(
        "ix_asset_availability_daily_date", "asset_availability_daily", ["availability_date", "asset_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_asset_availability_daily_date", table_name="asset_availability_daily")
    op.drop_table("asset_availability_daily")
    op.drop_index("ix_device_availability_daily_asset_date", table_name="device_availability_daily")
    op.drop_table("device_availability_daily")
