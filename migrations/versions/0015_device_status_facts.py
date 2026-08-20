"""Device status facts: the diagnostic foundation, imported from V1 evidence.

Studied and scoped in `docs/v2/DIAGNOSTICS.md` before any code was written. Two
findings from that study shape this migration.

No device-scoped `asset_provider_mappings` row is active — all 325 imported by
M1 sit at `pending_review` on disabled, credential-free legacy connections. A
table anchored on `provider_mapping_id`, the way `production_facts` and
`monitoring_observations` are, could not accept a single row today. This one is
anchored on `device_id` instead, which the M1 identity import already resolved
for every device, independent of whether any provider connection is usable.

`availability_status` is V1's own vocabulary (`available`, `standby`,
`unavailable`, `unknown`), not V2's `OBSERVATION_CONDITIONS`
(`operational`/`warning`/`fault`/`offline`/`unknown`). Forcing V1's four values
onto that five-value enum would mean guessing which of "warning" or "fault"
"unavailable" was supposed to mean, which is exactly what the ported
classification function in `diagnostics/rules.py` proves is unnecessary: V1's
real logic is available, so V1's real vocabulary is kept rather than
approximated.

Revision ID: 0015_device_status_facts
Revises: 0014_portfolio_report_runs
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_device_status_facts"
down_revision = "0014_portfolio_report_runs"
branch_labels = None
depends_on = None


AVAILABILITY_STATES = ("available", "standby", "unavailable", "unknown")
SOURCE_KINDS = ("v1_import", "live_read")


def upgrade() -> None:
    op.create_table(
        "device_status_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.Integer(), nullable=False),
        # Denormalized, matching production_facts: every query that wants
        # "this asset's device statuses" would otherwise need a join for
        # something the fact already knows at write time.
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("source_fact_key", sa.String(length=255), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supersedes_fact_id", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_status", sa.String(length=24), nullable=False),
        # A real instantaneous reading, distinct from production_facts'
        # period-summed energy. NULL is a missing reading; 0 is a real zero.
        sa.Column("active_power_kw", sa.Numeric(12, 3)),
        sa.Column("day_energy_kwh", sa.Numeric(14, 3)),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["device_status_facts.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"availability_status IN {AVAILABILITY_STATES!r}", name="ck_device_status_facts_availability"),
        sa.CheckConstraint(f"source_kind IN {SOURCE_KINDS!r}", name="ck_device_status_facts_source_kind"),
        sa.CheckConstraint("active_power_kw IS NULL OR active_power_kw >= 0", name="ck_device_status_facts_power"),
        sa.CheckConstraint("day_energy_kwh IS NULL OR day_energy_kwh >= 0", name="ck_device_status_facts_energy"),
        sa.UniqueConstraint(
            "device_id", "source_fact_key", "source_revision", name="uq_device_status_facts_revision"
        ),
    )
    op.create_index("ix_device_status_facts_device_time", "device_status_facts", ["device_id", "observed_at"])
    op.create_index("ix_device_status_facts_asset_time", "device_status_facts", ["asset_id", "observed_at"])


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM device_status_facts")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} device status facts are diagnostic history, not disposable.")
    op.drop_index("ix_device_status_facts_asset_time", table_name="device_status_facts")
    op.drop_index("ix_device_status_facts_device_time", table_name="device_status_facts")
    op.drop_table("device_status_facts")
