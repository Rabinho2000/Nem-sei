"""Split the physical site from the canonical technical plant.

`Asset` has been carrying two questions at once: "what is the canonical
electrical plant" and "where is this, physically, and who operates it". The
second question is `Installation`. See `installations/models.py` for the
full reasoning.

This revision is schema only: `installations`, and a nullable
`assets.installation_id`. No row moves. The backfill that creates one
Installation per existing Asset and links it lives in
`installations/service.py::backfill_installations_from_assets`, run
separately and idempotently, the same way `contracts/v1_import.py` and every
other data-carrying step in this codebase is a script an operator runs and
reviews, not something `alembic upgrade head` does unattended.

Nothing here touches `production_facts`, `device_status_facts`,
`asset_provider_mappings`, `devices`, or any ingestion path -- they keep
pointing at `Asset`, unchanged.

Revision ID: 0031_installations
Revises: 0030_automation_scope_audit
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_installations"
down_revision = "0030_automation_scope_audit"
branch_labels = None
depends_on = None

# Same vocabulary as the coordinate provenance everywhere else in this
# schema (see `installations/models.py`).
COORDINATE_SOURCES = ("google_mymaps", "openrouteservice", "manual", "operator", "provider")
COORDINATE_CONFIDENCES = ("ok", "suspect", "manual")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.create_table(
        "installations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="SET NULL")),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("country_code", sa.String(length=2)),
        sa.Column("locality", sa.String(length=255)),
        sa.Column("address", sa.Text()),
        sa.Column("timezone", sa.String(length=64)),
        sa.Column("timezone_source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("coordinates_source", sa.String(length=32)),
        sa.Column("coordinates_confidence", sa.String(length=32)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_installations_public_id", "installations", ["public_id"])
    op.create_index("ix_installations_organization", "installations", ["organization_id"])
    op.create_index(
        "ix_installations_coordinates",
        "installations",
        ["latitude", "longitude"],
        postgresql_where=sa.text("latitude IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_installations_coordinates_provenance",
        "installations",
        "(latitude IS NULL AND longitude IS NULL) OR coordinates_source IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_installations_coordinates_pair", "installations", "(latitude IS NULL) = (longitude IS NULL)"
    )
    op.create_check_constraint(
        "ck_installations_coordinates_source",
        "installations",
        f"coordinates_source IS NULL OR coordinates_source IN ({_values(COORDINATE_SOURCES)})",
    )
    op.create_check_constraint(
        "ck_installations_coordinates_confidence",
        "installations",
        f"coordinates_confidence IS NULL OR coordinates_confidence IN ({_values(COORDINATE_CONFIDENCES)})",
    )

    op.add_column(
        "assets",
        sa.Column("installation_id", sa.Integer(), sa.ForeignKey("installations.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_assets_installation", "assets", ["installation_id"])


def downgrade() -> None:
    op.drop_index("ix_assets_installation", table_name="assets")
    op.drop_column("assets", "installation_id")
    op.drop_table("installations")
