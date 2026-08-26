"""Allow operator-audit rows for O&M contract edits.

`operator_audit_events.action` is guarded by a CHECK listing the actions that
existed when it was last widened. The contract panel and the renewals screen
are the first writes to the new `asset_service_contracts` table, and without
widening this the first save fails on the constraint -- the same failure 0021
and 0024 were written to prevent.

Revision ID: 0028_om_contract_audit_actions
Revises: 0027_asset_service_contracts
"""
from __future__ import annotations

from alembic import op

revision = "0028_om_contract_audit_actions"
down_revision = "0027_asset_service_contracts"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_operator_audit_events_action"
TABLE = "operator_audit_events"

BEFORE = (
    "identity_decision_recorded",
    "mapping_approved",
    "mapping_rejected",
    "source_policy_created",
    "source_policy_changed",
    "connection_configured",
    "connection_enabled",
    "connection_disabled",
    "validation_requested",
    "asset_updated",
    "asset_reviewed",
    "automation_enabled",
    "automation_disabled",
)
ADDED = ("service_contract_created", "service_contract_closed", "service_contract_renewal_updated")
AFTER = BEFORE + ADDED


def _values(actions: tuple[str, ...]) -> str:
    return ", ".join(f"'{action}'" for action in actions)


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(AFTER)}))")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE action IN ({_values(ADDED)})")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(BEFORE)}))")
