"""Allow an operator-audit row for closing a reporting month.

Closing a month approves every portfolio run it still owes an approval to
(`bulk_close.close_month`), optionally over an explicitly-acknowledged
financial gap. `operator_audit_events.action` is guarded by a CHECK listing
the actions that existed before it -- the same constraint 0021, 0024, 0028
and 0030 each had to widen.

Revision ID: 0037_report_close_audit_actions
Revises: 0036_digest_kind
"""
from __future__ import annotations

from alembic import op

revision = "0037_report_close_audit_actions"
down_revision = "0036_digest_kind"
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
    "automation_scope_changed",
)
ADDED = ("month_closed",)
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
