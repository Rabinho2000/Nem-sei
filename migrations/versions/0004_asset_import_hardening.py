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
    with op.batch_alter_table("assets", recreate="always") as batch:
        batch.alter_column("timezone", existing_type=sa.String(64), nullable=True)
    with op.batch_alter_table("asset_provider_mappings", recreate="always") as batch:
        batch.create_check_constraint("ck_asset_provider_mappings_valid_range", "valid_to IS NULL OR valid_to >= valid_from")
    with op.batch_alter_table("legacy_import_runs", recreate="always") as batch:
        batch.add_column(sa.Column("importer_version", sa.String(64), nullable=False, server_default="assets-v1-importer/2.0"))
        batch.add_column(sa.Column("source_locator_sha256", sa.String(64), nullable=False, server_default="unknown"))
    with op.batch_alter_table("legacy_import_records", recreate="always") as batch:
        batch.add_column(sa.Column("source_locator_sha256", sa.String(64), nullable=False, server_default="unknown"))
        batch.add_column(sa.Column("evidence_json", sa.JSON(), nullable=False, server_default="{}"))
        batch.drop_constraint("ck_legacy_import_records_outcome", type_="check")
        batch.create_check_constraint("ck_legacy_import_records_outcome", "outcome IN ('created', 'reused', 'quarantined', 'changed_source', 'conflict', 'excluded', 'unresolved')")


def downgrade() -> None:
    with op.batch_alter_table("legacy_import_records", recreate="always") as batch:
        batch.drop_constraint("ck_legacy_import_records_outcome", type_="check")
        batch.create_check_constraint("ck_legacy_import_records_outcome", "outcome IN ('created', 'reused', 'quarantined', 'changed_source', 'conflict', 'excluded')")
        batch.drop_column("evidence_json")
        batch.drop_column("source_locator_sha256")
    with op.batch_alter_table("legacy_import_runs", recreate="always") as batch:
        batch.drop_column("source_locator_sha256")
        batch.drop_column("importer_version")
    with op.batch_alter_table("asset_provider_mappings", recreate="always") as batch:
        batch.drop_constraint("ck_asset_provider_mappings_valid_range", type_="check")
    with op.batch_alter_table("assets", recreate="always") as batch:
        batch.alter_column("timezone", existing_type=sa.String(64), nullable=False)
