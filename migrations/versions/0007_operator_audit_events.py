"""Add sanitized operator audit events.

Revision ID: 0007_operator_audit_events
Revises: 0006_monitoring_current_state
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_operator_audit_events"
down_revision = "0006_monitoring_current_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_username", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Integer()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('mapping_approved', 'mapping_rejected', 'source_policy_created', 'source_policy_changed', 'connection_configured', 'connection_enabled', 'connection_disabled', 'validation_requested')",
            name="ck_operator_audit_events_action",
        ),
    )
    op.create_index(
        "ix_operator_audit_events_entity",
        "operator_audit_events",
        ["entity_type", "entity_id", "occurred_at"],
    )
    op.execute(
        """
        CREATE FUNCTION operator_audit_events_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'operator_audit_events are append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER operator_audit_events_no_update BEFORE UPDATE ON operator_audit_events FOR EACH ROW EXECUTE FUNCTION operator_audit_events_immutable()"
    )
    op.execute(
        "CREATE TRIGGER operator_audit_events_no_delete BEFORE DELETE ON operator_audit_events FOR EACH ROW EXECUTE FUNCTION operator_audit_events_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER operator_audit_events_no_delete ON operator_audit_events")
    op.execute("DROP TRIGGER operator_audit_events_no_update ON operator_audit_events")
    op.execute("DROP FUNCTION operator_audit_events_immutable()")
    op.drop_index("ix_operator_audit_events_entity", table_name="operator_audit_events")
    op.drop_table("operator_audit_events")
