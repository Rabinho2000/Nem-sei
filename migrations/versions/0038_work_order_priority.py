"""Work orders: priority, two more workflow states, and who changed one last.

Three additive changes to `work_orders`, none touching `visits` or
`work_order_incidents`:

- **`priority`** (`critical`/`high`/`normal`/`low`, default `normal`). Did not
  exist at all before this revision -- the operator picks it when creating a
  trabalho, seeded from the linked incident's severity but always overridable
  (`work_orders/service.py`).
- **`waiting_material`/`waiting_customer`** added to the status vocabulary,
  next to the five that already existed. Distinct from `material_status`
  (`not_applicable`/`pending`/`ordered`/`ready`), which stays exactly what it
  was: `material_status` says how ready the material is, `status` says where
  the job itself sits in the workflow. A job can be `status='open'` with
  `material_status='pending'` (not blocked on it yet) or explicitly
  `status='waiting_material'` (the whole job is stalled on it) -- two
  different facts, never merged into one column.
- **`updated_by`**. `update_work_order_status` already took an `actor`
  argument; nothing stored it. Every other write in this table records who
  made it (`created_by` on `WorkOrder`/`Visit`); a status change silently did
  not, which is the actual gap the O&M workflow's auditability requirement
  named -- not a new subsystem, a column that should have been there since
  0033.

Revision ID: 0038_work_order_priority
Revises: 0037_report_close_audit_actions

The obvious longer id (matching this file's own title) does not fit:
`alembic_version.version_num` is `VARCHAR(32)`, and a revision id that
exceeds it fails at upgrade time with a raw `StringDataRightTruncation`,
not an Alembic error -- the same class of deployment bug `DECISIONS.md`
already records being caught once before. Keeping ids short is cheaper
than teaching every future revision to check.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_work_order_priority"
down_revision = "0037_report_close_audit_actions"
branch_labels = None
depends_on = None

OLD_STATUSES = ("open", "planned", "in_progress", "completed", "cancelled")
NEW_STATUSES = ("open", "planned", "in_progress", "waiting_material", "waiting_customer", "completed", "cancelled")
PRIORITIES = ("critical", "high", "normal", "low")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.add_column("work_orders", sa.Column("priority", sa.String(length=24), nullable=False, server_default="normal"))
    op.add_column("work_orders", sa.Column("updated_by", sa.String(length=120)))
    op.create_check_constraint("ck_work_orders_priority", "work_orders", f"priority IN ({_values(PRIORITIES)})")
    op.create_index("ix_work_orders_priority", "work_orders", ["priority", "status"])

    op.drop_constraint("ck_work_orders_status", "work_orders", type_="check")
    op.create_check_constraint("ck_work_orders_status", "work_orders", f"status IN ({_values(NEW_STATUSES)})")


def downgrade() -> None:
    # Any row already sitting in one of the two new states has no honest old
    # equivalent to fall back to -- `open` is the closest "still needs work"
    # state the old vocabulary had, so a downgrade normalises to that rather
    # than refusing outright or inventing data the row never had.
    op.execute("UPDATE work_orders SET status = 'open' WHERE status IN ('waiting_material', 'waiting_customer')")
    op.drop_constraint("ck_work_orders_status", "work_orders", type_="check")
    op.create_check_constraint("ck_work_orders_status", "work_orders", f"status IN ({_values(OLD_STATUSES)})")

    op.drop_index("ix_work_orders_priority", table_name="work_orders")
    op.drop_constraint("ck_work_orders_priority", "work_orders", type_="check")
    op.drop_column("work_orders", "updated_by")
    op.drop_column("work_orders", "priority")
