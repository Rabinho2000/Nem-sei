"""Audit the one automation switch the interface can actually flip.

Bloco E shows every automation, but only the notification channel and its
policies live in the database -- the schedulers are environment variables read
at process start, so the screen can report them and must not pretend to change
them. Enabling a channel is the step every prior session deliberately left to a
human (D4 in DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md), so when a human finally
takes it, it leaves a record.

Revision ID: 0024_automation_audit_actions
Revises: 0023_incident_handling
"""
from __future__ import annotations

from alembic import op

revision = "0024_automation_audit_actions"
down_revision = "0023_incident_handling"
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
)
AFTER = BEFORE + ("automation_enabled", "automation_disabled")


def _values(actions: tuple[str, ...]) -> str:
    return ", ".join(f"'{action}'" for action in actions)


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(AFTER)}))")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE action IN ('automation_enabled', 'automation_disabled')")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (action IN ({_values(BEFORE)}))")
