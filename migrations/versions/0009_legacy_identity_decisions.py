"""Record operator rulings that resolve ambiguous V1 identity evidence.

Duplicate V1 names are never merged automatically. A human designates the
canonical row and discards the rest, and the importer replays that decision so
the migration stays repeatable instead of becoming a manual database edit.

Revision ID: 0009_legacy_identity_decisions
Revises: 0008_canonical_devices
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_legacy_identity_decisions"
down_revision = "0008_canonical_devices"
branch_labels = None
depends_on = None


AUDIT_ACTIONS_BEFORE = (
    "mapping_approved, mapping_rejected, source_policy_created, source_policy_changed, "
    "connection_configured, connection_enabled, connection_disabled, validation_requested"
)
AUDIT_ACTIONS_AFTER = f"identity_decision_recorded, {AUDIT_ACTIONS_BEFORE}"


def quoted(actions: str) -> str:
    return ", ".join(f"'{action.strip()}'" for action in actions.split(","))


def upgrade() -> None:
    op.create_table(
        "legacy_identity_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legacy_table", sa.String(length=120), nullable=False),
        sa.Column("legacy_id", sa.String(length=120), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("actor_username", sa.String(length=120), nullable=False),
        sa.Column("source_database_sha256", sa.String(length=64)),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('canonical', 'discard')", name="ck_legacy_identity_decisions_decision"),
        sa.UniqueConstraint("legacy_table", "legacy_id", name="uq_legacy_identity_decisions_row"),
    )
    op.drop_constraint("ck_operator_audit_events_action", "operator_audit_events", type_="check")
    op.create_check_constraint(
        "ck_operator_audit_events_action",
        "operator_audit_events",
        f"action IN ({quoted(AUDIT_ACTIONS_AFTER)})",
    )
    # Let a decided source row move from quarantined to created without erasing
    # the earlier evidence. A rerun reaching the same outcome is still blocked.
    op.drop_constraint("uq_legacy_import_records_source_version", "legacy_import_records", type_="unique")
    op.create_unique_constraint(
        "uq_legacy_import_records_source_version",
        "legacy_import_records",
        ["source_database_sha256", "legacy_table", "legacy_id", "source_hash", "outcome"],
    )


def downgrade() -> None:
    duplicated = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM legacy_import_records "
            "GROUP BY source_database_sha256, legacy_table, legacy_id, source_hash "
            "HAVING count(*) > 1) AS collisions"
        )
    ).scalar_one()
    if duplicated:
        raise RuntimeError(
            f"Refusing to downgrade: {duplicated} source rows carry more than one outcome, "
            "which the narrower unique constraint would reject."
        )
    op.drop_constraint("uq_legacy_import_records_source_version", "legacy_import_records", type_="unique")
    op.create_unique_constraint(
        "uq_legacy_import_records_source_version",
        "legacy_import_records",
        ["source_database_sha256", "legacy_table", "legacy_id", "source_hash"],
    )
    existing = op.get_bind().execute(
        sa.text("SELECT count(*) FROM operator_audit_events WHERE action = 'identity_decision_recorded'")
    ).scalar_one()
    if existing:
        raise RuntimeError(
            f"Refusing to downgrade: {existing} identity decision audit events exist and the "
            "narrowed action constraint would reject them."
        )
    op.drop_constraint("ck_operator_audit_events_action", "operator_audit_events", type_="check")
    op.create_check_constraint(
        "ck_operator_audit_events_action",
        "operator_audit_events",
        f"action IN ({quoted(AUDIT_ACTIONS_BEFORE)})",
    )
    op.drop_table("legacy_identity_decisions")
