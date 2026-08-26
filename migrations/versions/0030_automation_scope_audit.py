"""Allow an operator-audit row for changing a policy's asset scope.

`notification_policies.asset_scope` arrived in 0029 with no way to change it
from the interface. Giving the automations page that control makes it the
third automation write, and `operator_audit_events.action` is guarded by a
CHECK listing the actions that existed before it -- the same constraint 0021,
0024 and 0028 each had to widen.

Revision ID: 0030_automation_scope_audit
Revises: 0029_notification_asset_scope
"""
from __future__ import annotations

from alembic import op

revision = "0030_automation_scope_audit"
down_revision = "0029_notification_asset_scope"
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
    "service_contract_created",
    "service_contract_closed",
    "service_contract_renewal_updated",
)
ADDED = ("automation_scope_changed",)
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
