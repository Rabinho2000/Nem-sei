"""Add V2 assets, owners, provider mappings, and import audit tables.

Revision ID: 0003_assets_provider_mappings
Revises: 0002_job_hardening
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_assets_provider_mappings"
down_revision = "0002_job_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("organizations", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("public_id", sa.String(36), nullable=False, unique=True), sa.Column("display_name", sa.String(255), nullable=False), sa.Column("normalized_tax_id", sa.String(64)), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("review_status", sa.String(24), nullable=False), sa.Column("review_note", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("review_status IN ('clear', 'needs_review')", name="ck_organizations_review_status"))
    op.create_index("uq_organizations_normalized_tax_id", "organizations", ["normalized_tax_id"], unique=True, postgresql_where=sa.text("normalized_tax_id IS NOT NULL AND normalized_tax_id != ''"))
    op.create_table("assets", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("public_id", sa.String(36), nullable=False, unique=True), sa.Column("canonical_name", sa.String(255), nullable=False), sa.Column("normalized_name", sa.String(255), nullable=False), sa.Column("lifecycle_status", sa.String(24), nullable=False), sa.Column("review_status", sa.String(24), nullable=False), sa.Column("review_note", sa.Text()), sa.Column("owner_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="SET NULL")), sa.Column("country_code", sa.String(2)), sa.Column("timezone", sa.String(64), nullable=False), sa.Column("timezone_source", sa.String(32), nullable=False), sa.Column("installed_dc_power_kw", sa.Numeric(12, 3)), sa.Column("commissioned_on", sa.Date()), sa.Column("address", sa.Text()), sa.Column("locality", sa.String(255)), sa.Column("latitude", sa.Numeric(10, 7)), sa.Column("longitude", sa.Numeric(10, 7)), sa.Column("technical_notes", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("lifecycle_status IN ('unknown', 'active', 'inactive', 'decommissioned')", name="ck_assets_lifecycle_status"), sa.CheckConstraint("review_status IN ('clear', 'needs_review')", name="ck_assets_review_status"))
    op.create_index("ix_assets_normalized_name", "assets", ["normalized_name"])
    op.create_index("ix_assets_owner", "assets", ["owner_id"])
    op.create_table("asset_aliases", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False), sa.Column("alias", sa.String(255), nullable=False), sa.Column("normalized_alias", sa.String(255), nullable=False), sa.Column("alias_kind", sa.String(32), nullable=False), sa.Column("source", sa.String(64), nullable=False), sa.Column("valid_from", sa.Date(), nullable=False), sa.Column("valid_to", sa.Date()), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("asset_id", "normalized_alias", "valid_from", name="uq_asset_aliases_asset_alias_from"))
    op.create_index("ix_asset_aliases_normalized_alias", "asset_aliases", ["normalized_alias"])
    op.create_table("provider_connections", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("provider_code", sa.String(32), nullable=False), sa.Column("connection_key", sa.String(120), nullable=False), sa.Column("display_name", sa.String(255), nullable=False), sa.Column("account_reference", sa.String(255)), sa.Column("region", sa.String(64)), sa.Column("credential_reference", sa.String(255)), sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("configuration_status", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("provider_code IN ('fusionsolar', 'sigenergy', 'sma')", name="ck_provider_connections_provider_code"), sa.CheckConstraint("configuration_status IN ('not_configured', 'configured', 'disabled')", name="ck_provider_connections_configuration_status"), sa.UniqueConstraint("provider_code", "connection_key", name="uq_provider_connections_provider_key"))
    op.create_index("ix_provider_connections_provider", "provider_connections", ["provider_code", "enabled"])
    op.create_table("asset_provider_mappings", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False), sa.Column("provider_connection_id", sa.Integer(), sa.ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False), sa.Column("resource_kind", sa.String(32), nullable=False), sa.Column("external_id", sa.String(255), nullable=False), sa.Column("normalized_external_id", sa.String(255), nullable=False), sa.Column("external_name", sa.String(255)), sa.Column("mapping_status", sa.String(32), nullable=False), sa.Column("valid_from", sa.Date(), nullable=False), sa.Column("valid_to", sa.Date()), sa.Column("monitoring_priority", sa.Integer()), sa.Column("production_priority", sa.Integer()), sa.Column("replaced_by_mapping_id", sa.Integer(), sa.ForeignKey("asset_provider_mappings.id", ondelete="SET NULL")), sa.Column("notes", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("resource_kind = 'plant'", name="ck_asset_provider_mappings_resource_kind"), sa.CheckConstraint("mapping_status IN ('active', 'superseded', 'invalid', 'pending_review')", name="ck_asset_provider_mappings_status"), sa.UniqueConstraint("asset_id", "provider_connection_id", "resource_kind", "normalized_external_id", "valid_from", name="uq_asset_provider_mappings_history"))
    op.create_index("uq_asset_provider_mappings_active_external", "asset_provider_mappings", ["provider_connection_id", "resource_kind", "normalized_external_id"], unique=True, postgresql_where=sa.text("mapping_status = 'active' AND valid_to IS NULL"))
    op.create_index("ix_asset_provider_mappings_asset", "asset_provider_mappings", ["asset_id", "mapping_status"])
    op.create_table("legacy_import_runs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_database_sha256", sa.String(64), nullable=False), sa.Column("dry_run", sa.Boolean(), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("manifest_json", sa.JSON(), nullable=False))
    op.create_table("legacy_import_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("import_run_id", sa.Integer(), sa.ForeignKey("legacy_import_runs.id", ondelete="RESTRICT"), nullable=False), sa.Column("source_database_sha256", sa.String(64), nullable=False), sa.Column("legacy_table", sa.String(120), nullable=False), sa.Column("legacy_id", sa.String(120), nullable=False), sa.Column("source_hash", sa.String(64), nullable=False), sa.Column("outcome", sa.String(32), nullable=False), sa.Column("reason", sa.Text()), sa.Column("target_organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="SET NULL")), sa.Column("target_asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="SET NULL")), sa.Column("target_mapping_id", sa.Integer(), sa.ForeignKey("asset_provider_mappings.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("outcome IN ('created', 'reused', 'quarantined', 'changed_source', 'conflict', 'excluded')", name="ck_legacy_import_records_outcome"), sa.UniqueConstraint("source_database_sha256", "legacy_table", "legacy_id", "source_hash", name="uq_legacy_import_records_source_version"))
    op.create_index("ix_legacy_import_records_outcome", "legacy_import_records", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_legacy_import_records_outcome", table_name="legacy_import_records")
    op.drop_table("legacy_import_records")
    op.drop_table("legacy_import_runs")
    op.drop_index("ix_asset_provider_mappings_asset", table_name="asset_provider_mappings")
    op.drop_index("uq_asset_provider_mappings_active_external", table_name="asset_provider_mappings")
    op.drop_table("asset_provider_mappings")
    op.drop_index("ix_provider_connections_provider", table_name="provider_connections")
    op.drop_table("provider_connections")
    op.drop_index("ix_asset_aliases_normalized_alias", table_name="asset_aliases")
    op.drop_table("asset_aliases")
    op.drop_index("ix_assets_owner", table_name="assets")
    op.drop_index("ix_assets_normalized_name", table_name="assets")
    op.drop_table("assets")
    op.drop_index("uq_organizations_normalized_tax_id", table_name="organizations")
    op.drop_table("organizations")
