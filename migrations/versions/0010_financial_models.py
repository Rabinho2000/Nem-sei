"""Persist and version customer financial models with their provenance.

Every reported number must be explainable back to a cell in a customer's
workbook, so the source file, its hash, the cell behind each monthly value and
the rule behind each derived value are all first-class columns.

Revision ID: 0010_financial_models
Revises: 0009_legacy_identity_decisions
"""
from alembic import op
import sqlalchemy as sa


revision = "0010_financial_models"
down_revision = "0009_legacy_identity_decisions"
branch_labels = None
depends_on = None


FORMATS = "'financial_automatic_as_sold', 'financial_automatic_upac', 'monthly_metric_rows', 'monthly_month_rows', 'unknown'"


def upgrade() -> None:
    op.create_table(
        "report_source_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("file_kind", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=255)),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=120)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("file_kind IN ('financial_model')", name="ck_report_source_files_kind"),
        sa.CheckConstraint("size_bytes > 0", name="ck_report_source_files_size"),
        sa.UniqueConstraint("sha256", name="uq_report_source_files_sha256"),
    )
    op.create_index("ix_report_source_files_asset", "report_source_files", ["asset_id", "file_kind"])

    op.create_table(
        "financial_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_file_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("supersedes_model_id", sa.Integer()),
        sa.Column("base_year", sa.Integer()),
        sa.Column("base_year_source", sa.String(length=24), nullable=False),
        sa.Column("base_year_cell", sa.String(length=120)),
        sa.Column("workbook_format", sa.String(length=48), nullable=False),
        sa.Column("sheet_name", sa.String(length=255)),
        sa.Column("detected_name", sa.String(length=255)),
        sa.Column("detected_nif", sa.String(length=64)),
        sa.Column("detected_kwp", sa.Numeric(precision=12, scale=3)),
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=16), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("source_cells_json", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=120)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_file_id"], ["report_source_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_model_id"], ["financial_models.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('draft', 'confirmed', 'superseded', 'rejected')", name="ck_financial_models_status"),
        sa.CheckConstraint("base_year_source IN ('workbook', 'operator', 'unknown')", name="ck_financial_models_base_year_source"),
        sa.CheckConstraint(f"workbook_format IN ({FORMATS})", name="ck_financial_models_format"),
        sa.CheckConstraint("base_year IS NULL OR base_year BETWEEN 2000 AND 2100", name="ck_financial_models_base_year"),
        sa.CheckConstraint("detected_kwp IS NULL OR detected_kwp >= 0", name="ck_financial_models_kwp"),
        sa.CheckConstraint(
            "base_year_source <> 'operator' OR confirmed_by IS NOT NULL",
            name="ck_financial_models_operator_year_actor",
        ),
        sa.UniqueConstraint("asset_id", "version", name="uq_financial_models_asset_version"),
    )
    op.create_index("ix_financial_models_asset_status", "financial_models", ["asset_id", "status"])

    op.create_table(
        "financial_model_months",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("financial_model_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("expected_production_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_consumption_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_self_use_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_export_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_grid_import_kwh", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_self_consumption_rate_pct", sa.Numeric(precision=20, scale=10)),
        sa.Column("expected_self_sufficiency_rate_pct", sa.Numeric(precision=20, scale=10)),
        sa.Column("source_fields_json", sa.JSON(), nullable=False),
        sa.Column("calculated_fields_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["financial_model_id"], ["financial_models.id"], ondelete="CASCADE"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="ck_financial_model_months_month"),
        sa.UniqueConstraint("financial_model_id", "month", name="uq_financial_model_months_month"),
    )


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM financial_models")).scalar_one()
    if existing:
        raise RuntimeError(
            f"Refusing to downgrade: {existing} financial models exist and their imported "
            "provenance would be discarded. Export or remove them explicitly first."
        )
    op.drop_table("financial_model_months")
    op.drop_index("ix_financial_models_asset_status", table_name="financial_models")
    op.drop_table("financial_models")
    op.drop_index("ix_report_source_files_asset", table_name="report_source_files")
    op.drop_table("report_source_files")
