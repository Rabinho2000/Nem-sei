"""Work orders and visits, scoped to Installation; many-to-many with incidents.

New tables only. Nothing in `diagnostics.models` changes -- `DiagnosticIncident`
keeps being what it already is (a deduplicated episode of a rule being true),
and this revision does not touch its columns, its evaluator, or the 447 real
incidents it currently holds.

No V1 data migration: V1's `tickets`/`ticket_visits` hold 13 and 7 rows
respectively with `work_type`/`assigned_to`/`material_status` populated on
essentially none of them, and `field_route_plans` -- the elaborate route/cost
planning schema V1 built around this concept -- holds zero rows ever. There is
nothing worth carrying across; see `work_orders/models.py`.

Revision ID: 0033_work_orders
Revises: 0032_contract_scopes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_work_orders"
down_revision = "0032_contract_scopes"
branch_labels = None
depends_on = None

WORK_TYPES = ("corrective", "preventive", "cleaning")
WORK_ORDER_STATUSES = ("open", "planned", "in_progress", "completed", "cancelled")
MATERIAL_STATUSES = ("not_applicable", "pending", "ordered", "ready")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.create_table(
        "work_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("installation_id", sa.Integer(), sa.ForeignKey("installations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("work_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("planned_date", sa.Date()),
        sa.Column("due_date", sa.Date()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("assigned_to", sa.String(length=120)),
        sa.Column("material_status", sa.String(length=24), nullable=False, server_default="not_applicable"),
        sa.Column("material_notes", sa.Text()),
        sa.Column("estimated_cost_eur", sa.Numeric(12, 2)),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_work_orders_public_id", "work_orders", ["public_id"])
    op.create_check_constraint("ck_work_orders_type", "work_orders", f"work_type IN ({_values(WORK_TYPES)})")
    op.create_check_constraint("ck_work_orders_status", "work_orders", f"status IN ({_values(WORK_ORDER_STATUSES)})")
    op.create_check_constraint(
        "ck_work_orders_material_status", "work_orders", f"material_status IN ({_values(MATERIAL_STATUSES)})"
    )
    op.create_check_constraint(
        "ck_work_orders_dates", "work_orders", "due_date IS NULL OR planned_date IS NULL OR due_date >= planned_date"
    )
    op.create_check_constraint(
        "ck_work_orders_completed_at", "work_orders", "(status = 'completed') = (completed_at IS NOT NULL)"
    )
    op.create_check_constraint(
        "ck_work_orders_cost", "work_orders", "estimated_cost_eur IS NULL OR estimated_cost_eur >= 0"
    )
    op.create_index("ix_work_orders_installation", "work_orders", ["installation_id", "status"])
    op.create_index("ix_work_orders_planned_date", "work_orders", ["planned_date"])

    op.create_table(
        "visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("work_order_id", sa.Integer(), sa.ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visit_date", sa.Date(), nullable=False),
        sa.Column("technician", sa.String(length=120)),
        sa.Column("outcome", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_visits_public_id", "visits", ["public_id"])
    op.create_index("ix_visits_work_order", "visits", ["work_order_id", "visit_date"])

    op.create_table(
        "work_order_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("work_order_id", sa.Integer(), sa.ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "incident_id", sa.Integer(), sa.ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_work_order_incidents_pair", "work_order_incidents", ["work_order_id", "incident_id"]
    )
    op.create_index("ix_work_order_incidents_incident", "work_order_incidents", ["incident_id"])


def downgrade() -> None:
    op.drop_table("work_order_incidents")
    op.drop_table("visits")
    op.drop_table("work_orders")
