"""Diagnostic incidents: D1, deduplicated persisted episodes of a finding rule.

Studied and scoped in `docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`
(Option A). `diagnostics/findings.py` is unchanged by this migration --
still recomputed per request, still not persisted. This table exists only
for what a recomputed-every-time module cannot answer on its own: has this
exact problem already been seen (so it does not notify/count as new every
single re-evaluation), when did it start, and when did it stop.

Two things worth calling out about the schema itself, not just the table:

`device_id` is nullable (asset-level findings like `partial_device_coverage`
have no single device), so identity uniqueness cannot be a plain
`UniqueConstraint` -- Postgres treats every NULL as distinct from every
other NULL, which would silently let two "open" asset-level incidents for
the same asset coexist. The partial unique index below uses
`COALESCE(device_id, -1)` to close that gap; no real device has id -1.

No foreign key to `device_status_facts`: an incident's evidence is a JSON
snapshot of the triggering `DiagnosticFinding.evidence` (already a plain
dict, not a fact row reference) refreshed on every re-observation, not a
pointer to one row that would go stale the moment a newer fact arrives.

Revision ID: 0017_diagnostic_incidents
Revises: 0016_device_status_freshness
"""
from alembic import op
import sqlalchemy as sa


revision = "0017_diagnostic_incidents"
down_revision = "0016_device_status_freshness"
branch_labels = None
depends_on = None


INCIDENT_STATUSES = ("open", "resolved")


def upgrade() -> None:
    op.create_table(
        "diagnostic_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_code", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer()),
        sa.Column("severity", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("detector_version", sa.String(length=16), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"status IN {INCIDENT_STATUSES!r}", name="ck_diagnostic_incidents_status"),
        sa.CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL)", name="ck_diagnostic_incidents_resolved_at"),
        sa.CheckConstraint("occurrence_count >= 1", name="ck_diagnostic_incidents_occurrence_count"),
        sa.CheckConstraint("last_observed_at >= opened_at", name="ck_diagnostic_incidents_observed_after_open"),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= last_observed_at", name="ck_diagnostic_incidents_resolved_after_observed"
        ),
    )
    op.create_index("ix_diagnostic_incidents_status", "diagnostic_incidents", ["status", "last_observed_at"])
    op.create_index("ix_diagnostic_incidents_asset", "diagnostic_incidents", ["asset_id", "status"])
    op.execute(
        """
        CREATE UNIQUE INDEX uq_diagnostic_incidents_open_identity
        ON diagnostic_incidents (rule_code, asset_id, COALESCE(device_id, -1))
        WHERE status = 'open'
        """
    )


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM diagnostic_incidents")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} diagnostic incidents are operational history, not disposable.")
    op.execute("DROP INDEX IF EXISTS uq_diagnostic_incidents_open_identity")
    op.drop_index("ix_diagnostic_incidents_asset", table_name="diagnostic_incidents")
    op.drop_index("ix_diagnostic_incidents_status", table_name="diagnostic_incidents")
    op.drop_table("diagnostic_incidents")
