"""Add availability_pct/availability_state to reporting_dataset_rows.

Wires `docs/v2/AVAILABILITY_MIGRATION_PLAN.md`'s materialized
`asset_availability_daily` into the customer-facing reporting dataset, the
same additive-column shape every other energy metric on this table already
uses (`self_use_kwh`/`self_use_state`, etc.) -- a month with no materialized
availability coverage reports an absent percentage, never a zero.

Revision ID: 0040_reporting_availability
Revises: 0039_availability_daily
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_reporting_availability"
down_revision = "0039_availability_daily"
branch_labels = None
depends_on = None

VALUE_STATES = ("measured", "missing", "partial")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.add_column("reporting_dataset_rows", sa.Column("availability_pct", sa.Numeric(5, 2)))
    op.add_column(
        "reporting_dataset_rows",
        sa.Column("availability_state", sa.String(length=16), nullable=False, server_default="missing"),
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_state",
        "reporting_dataset_rows",
        f"availability_state IN ({_values(VALUE_STATES)})",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_missing_availability",
        "reporting_dataset_rows",
        "availability_state <> 'missing' OR availability_pct IS NULL",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_pct_range",
        "reporting_dataset_rows",
        "availability_pct IS NULL OR (availability_pct >= 0 AND availability_pct <= 100)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_reporting_dataset_rows_availability_pct_range", "reporting_dataset_rows", type_="check")
    op.drop_constraint("ck_reporting_dataset_rows_missing_availability", "reporting_dataset_rows", type_="check")
    op.drop_constraint("ck_reporting_dataset_rows_availability_state", "reporting_dataset_rows", type_="check")
    op.drop_column("reporting_dataset_rows", "availability_state")
    op.drop_column("reporting_dataset_rows", "availability_pct")
