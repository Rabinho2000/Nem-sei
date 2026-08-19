"""Reproducible reporting datasets and immutable report snapshots.

A dataset resolves one reporting period from persisted facts; a snapshot freezes
a dataset and its payload so the same report for the same data version yields
the same numbers. Snapshots are append-only at the database level.

Revision ID: 0011_reporting_datasets
Revises: 0010_financial_models
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_reporting_datasets"
down_revision = "0010_financial_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reporting_datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("asset_id", sa.Integer()),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("financial_model_id", sa.Integer()),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_by", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["financial_model_id"], ["financial_models.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("scope IN ('asset', 'portfolio')", name="ck_reporting_datasets_scope"),
        sa.CheckConstraint("status IN ('building', 'ready', 'failed')", name="ck_reporting_datasets_status"),
        sa.CheckConstraint("period_end > period_start", name="ck_reporting_datasets_period"),
        sa.CheckConstraint("(scope = 'asset') = (asset_id IS NOT NULL)", name="ck_reporting_datasets_scope_target"),
    )
    op.create_index("ix_reporting_datasets_target", "reporting_datasets", ["scope", "asset_id", "period_start"])

    op.create_table(
        "reporting_dataset_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("actual_production_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("actual_state", sa.String(length=16), nullable=False),
        sa.Column("expected_production_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_state", sa.String(length=16), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["reporting_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("actual_state IN ('measured', 'missing', 'partial')", name="ck_reporting_dataset_rows_actual_state"),
        sa.CheckConstraint("expected_state IN ('measured', 'missing', 'partial')", name="ck_reporting_dataset_rows_expected_state"),
        sa.CheckConstraint("actual_state <> 'missing' OR actual_production_kwh IS NULL", name="ck_reporting_dataset_rows_missing_actual"),
        sa.CheckConstraint("expected_state <> 'missing' OR expected_production_kwh IS NULL", name="ck_reporting_dataset_rows_missing_expected"),
        sa.UniqueConstraint("dataset_id", "asset_id", "period_start", name="uq_reporting_dataset_rows_period"),
    )

    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("dataset_input_digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["reporting_datasets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("snapshot_digest", name="uq_report_snapshots_digest"),
    )
    op.create_index("ix_report_snapshots_dataset", "report_snapshots", ["dataset_id", "created_at"])

    # A snapshot is the record of what a customer was told; it never changes.
    op.execute(
        """
        CREATE FUNCTION report_snapshots_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'report snapshots are append-only';
        END;
        $$
        """
    )
    op.execute("CREATE TRIGGER report_snapshots_no_update BEFORE UPDATE ON report_snapshots FOR EACH ROW EXECUTE FUNCTION report_snapshots_immutable()")
    op.execute("CREATE TRIGGER report_snapshots_no_delete BEFORE DELETE ON report_snapshots FOR EACH ROW EXECUTE FUNCTION report_snapshots_immutable()")


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM report_snapshots")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} report snapshots exist and are the record of what customers were told.")
    op.execute("DROP TRIGGER report_snapshots_no_delete ON report_snapshots")
    op.execute("DROP TRIGGER report_snapshots_no_update ON report_snapshots")
    op.execute("DROP FUNCTION report_snapshots_immutable()")
    op.drop_index("ix_report_snapshots_dataset", table_name="report_snapshots")
    op.drop_table("report_snapshots")
    op.drop_table("reporting_dataset_rows")
    op.drop_index("ix_reporting_datasets_target", table_name="reporting_datasets")
    op.drop_table("reporting_datasets")
