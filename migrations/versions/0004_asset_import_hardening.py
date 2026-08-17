"""Harden legacy asset import provenance and temporal validation.

Revision ID: 0004_asset_import_hardening
Revises: 0003_assets_provider_mappings
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_asset_import_hardening"
down_revision = "0003_assets_provider_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("assets", "timezone", existing_type=sa.String(64), nullable=True)
    op.create_check_constraint(
        "ck_asset_provider_mappings_valid_range",
        "asset_provider_mappings",
        "valid_to IS NULL OR valid_to >= valid_from",
    )
    op.add_column(
        "legacy_import_runs",
        sa.Column("importer_version", sa.String(64), nullable=False, server_default="assets-v1-importer/2.0"),
    )
    op.add_column(
        "legacy_import_runs",
        sa.Column("source_locator_sha256", sa.String(64), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "legacy_import_records",
        sa.Column("source_locator_sha256", sa.String(64), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "legacy_import_records",
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.drop_constraint("ck_legacy_import_records_outcome", "legacy_import_records", type_="check")
    op.create_check_constraint(
        "ck_legacy_import_records_outcome",
        "legacy_import_records",
        "outcome IN ('created', 'reused', 'quarantined', 'changed_source', 'conflict', 'excluded', 'unresolved')",
    )
    for table_name, column_name in (
        ("legacy_import_runs", "importer_version"),
        ("legacy_import_runs", "source_locator_sha256"),
        ("legacy_import_records", "source_locator_sha256"),
        ("legacy_import_records", "evidence_json"),
    ):
        op.alter_column(table_name, column_name, server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_legacy_import_records_outcome", "legacy_import_records", type_="check")
    op.create_check_constraint(
        "ck_legacy_import_records_outcome",
        "legacy_import_records",
        "outcome IN ('created', 'reused', 'quarantined', 'changed_source', 'conflict', 'excluded')",
    )
    op.drop_column("legacy_import_records", "evidence_json")
    op.drop_column("legacy_import_records", "source_locator_sha256")
    op.drop_column("legacy_import_runs", "source_locator_sha256")
    op.drop_column("legacy_import_runs", "importer_version")
    op.drop_constraint("ck_asset_provider_mappings_valid_range", "asset_provider_mappings", type_="check")
    op.alter_column("assets", "timezone", existing_type=sa.String(64), nullable=False)
