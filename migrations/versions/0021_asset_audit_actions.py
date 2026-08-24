"""Allow operator-audit rows for asset identity edits.

Bloco A gives the asset detail page a form for the first time. Every edit made
through it records who did it and which fields moved, but
`operator_audit_events.action` is guarded by a CHECK constraint listing the
actions that existed when only provider work was auditable. Without widening
it, the first save through the new form fails on the constraint.

Revision ID: 0021_asset_audit_actions
Revises: 0020_baseline_snapshots
"""
from __future__ import annotations

from alembic import op

revision = "0021_asset_audit_actions"
down_revision = "0020_baseline_snapshots"
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
)
AFTER = BEFORE + ("asset_updated", "asset_reviewed")


def _values(actions: tuple[str, ...]) -> str:
    return ", ".join(f"'{action}'" for action in actions)


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(AFTER)}))")


def downgrade() -> None:
    # Rows written under the new vocabulary would violate the old constraint,
    # so drop them first -- they are an audit trail of edits that the earlier
    # schema had no way to represent at all.
    op.execute(f"DELETE FROM {TABLE} WHERE action IN ('asset_updated', 'asset_reviewed')")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(BEFORE)}))")
