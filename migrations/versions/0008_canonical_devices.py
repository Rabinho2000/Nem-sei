"""Add the canonical device level and device-scoped provider claims.

Devices are children of assets. Canonical identity is the hardware serial,
model and rated power; provider identifiers stay in asset_provider_mappings so
a provider account change supersedes evidence without touching the device.

Revision ID: 0008_canonical_devices
Revises: 0007_operator_audit_events
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_canonical_devices"
down_revision = "0007_operator_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("parent_device_id", sa.Integer()),
        sa.Column("device_kind", sa.String(length=32), nullable=False),
        sa.Column("serial_number", sa.String(length=120)),
        sa.Column("normalized_serial_number", sa.String(length=120)),
        sa.Column("label", sa.String(length=255)),
        sa.Column("model", sa.String(length=120)),
        sa.Column("rated_power_kw", sa.Numeric(precision=12, scale=3)),
        sa.Column("lifecycle_status", sa.String(length=24), nullable=False),
        sa.Column("review_status", sa.String(length=24), nullable=False),
        sa.Column("review_note", sa.Text()),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_device_id"], ["devices.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "device_kind IN ('inverter', 'meter', 'datalogger', 'string_box')",
            name="ck_devices_device_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('unknown', 'active', 'inactive', 'decommissioned')",
            name="ck_devices_lifecycle_status",
        ),
        sa.CheckConstraint("review_status IN ('clear', 'needs_review')", name="ck_devices_review_status"),
        sa.CheckConstraint("rated_power_kw IS NULL OR rated_power_kw >= 0", name="ck_devices_rated_power"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_devices_validity"),
    )
    op.create_index(
        "uq_devices_asset_serial",
        "devices",
        ["asset_id", "normalized_serial_number"],
        unique=True,
        postgresql_where=sa.text("normalized_serial_number IS NOT NULL AND valid_to IS NULL"),
    )
    op.create_index("ix_devices_asset", "devices", ["asset_id", "device_kind"])
    op.create_index("ix_devices_normalized_serial", "devices", ["normalized_serial_number"])

    op.add_column("asset_provider_mappings", sa.Column("device_id", sa.Integer()))
    op.add_column("asset_provider_mappings", sa.Column("parent_mapping_id", sa.Integer()))
    op.create_foreign_key(
        "fk_asset_provider_mappings_device",
        "asset_provider_mappings",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_asset_provider_mappings_parent",
        "asset_provider_mappings",
        "asset_provider_mappings",
        ["parent_mapping_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("ck_asset_provider_mappings_resource_kind", "asset_provider_mappings", type_="check")
    op.create_check_constraint(
        "ck_asset_provider_mappings_resource_kind",
        "asset_provider_mappings",
        "resource_kind IN ('plant', 'device')",
    )
    op.create_check_constraint(
        "ck_asset_provider_mappings_device_link",
        "asset_provider_mappings",
        "(resource_kind = 'device') = (device_id IS NOT NULL)",
    )
    op.create_index("ix_asset_provider_mappings_device", "asset_provider_mappings", ["device_id", "mapping_status"])

    op.add_column("legacy_import_records", sa.Column("target_device_id", sa.Integer()))
    op.create_foreign_key(
        "fk_legacy_import_records_device",
        "legacy_import_records",
        "devices",
        ["target_device_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Refuse rather than silently discard canonical device evidence.
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM devices")).scalar_one()
    if existing:
        raise RuntimeError(
            f"Refusing to downgrade: {existing} device rows exist. "
            "Remove or export them explicitly before rolling back 0008."
        )
    op.drop_constraint("fk_legacy_import_records_device", "legacy_import_records", type_="foreignkey")
    op.drop_column("legacy_import_records", "target_device_id")

    op.drop_index("ix_asset_provider_mappings_device", table_name="asset_provider_mappings")
    op.drop_constraint("ck_asset_provider_mappings_device_link", "asset_provider_mappings", type_="check")
    op.drop_constraint("ck_asset_provider_mappings_resource_kind", "asset_provider_mappings", type_="check")
    op.create_check_constraint(
        "ck_asset_provider_mappings_resource_kind",
        "asset_provider_mappings",
        "resource_kind = 'plant'",
    )
    op.drop_constraint("fk_asset_provider_mappings_parent", "asset_provider_mappings", type_="foreignkey")
    op.drop_constraint("fk_asset_provider_mappings_device", "asset_provider_mappings", type_="foreignkey")
    op.drop_column("asset_provider_mappings", "parent_mapping_id")
    op.drop_column("asset_provider_mappings", "device_id")

    op.drop_index("ix_devices_normalized_serial", table_name="devices")
    op.drop_index("ix_devices_asset", table_name="devices")
    op.drop_index("uq_devices_asset_serial", table_name="devices")
    op.drop_table("devices")
