"""Let a notification policy target part of the fleet, not all of it.

V1 had this and V2 did not. `ALERT_SCOPE` defaulted to `only_o&m`, and
`is_asset_in_oem_scope` gated every alert on it; V2's `NotificationPolicy`
narrows only by severity and rule code, so a policy switched on today would
alert for all 267 installations, including the 175 Solcor does not operate.

`all` is the server default because it is what the existing rows already mean
-- an unscoped policy is not silently narrowed by a migration. New policies
choose deliberately.

Revision ID: 0029_notification_asset_scope
Revises: 0028_om_contract_audit_actions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_notification_asset_scope"
down_revision = "0028_om_contract_audit_actions"
branch_labels = None
depends_on = None

TABLE = "notification_policies"
COLUMN = "asset_scope"
CONSTRAINT = "ck_notification_policies_asset_scope"
SCOPES = ("all", "om", "om_active")


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=24), nullable=False, server_default="all"))
    values = ", ".join(f"'{scope}'" for scope in SCOPES)
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK ({COLUMN} IN ({values}))")


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
