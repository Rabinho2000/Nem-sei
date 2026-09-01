"""Record where an installation physically is, and how much to trust it.

`assets.latitude` and `assets.longitude` have existed since `0001` and are
NULL for all 267 rows. Nothing in V2 has ever been able to say when the sun
rises over a plant, so `monitoring/production_window.py` -- and every rule
that must not fire at night -- had no input at all.

V1 holds 119 of them, and it also holds something V2's two columns cannot
carry: where each pair came from. 87 were traced on a Google MyMap, 24 were
geocoded from a postal address by OpenRouteService and V1 itself marked them
`suspect`, and 8 were typed in by an operator. A geocoded address can land in
the middle of a municipality; a traced roof cannot. Importing all 119 as if
they were equally good would be exactly the kind of invented precision this
schema refuses elsewhere, so the provenance comes with them.

The two columns are nullable with no default and no backfill here: the
importer writes them, not the migration, so applying this revision changes no
data at all.

Revision ID: 0031_asset_coordinates
Revises: 0030_automation_scope_audit
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_asset_coordinates"
down_revision = "0030_automation_scope_audit"
branch_labels = None
depends_on = None

# V1's own vocabulary, kept verbatim rather than mapped onto something tidier.
# `suspect` is V1's word for "geocoded from an address, nobody checked it",
# and renaming it would lose the distinction its operators actually recorded.
COORDINATE_SOURCES = ("google_mymaps", "openrouteservice", "manual", "operator", "provider")
COORDINATE_CONFIDENCES = ("ok", "suspect", "manual")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.add_column("assets", sa.Column("coordinates_source", sa.String(length=32), nullable=True))
    op.add_column("assets", sa.Column("coordinates_confidence", sa.String(length=32), nullable=True))
    # Coordinates and their provenance travel together: a pair with no source
    # is a number nobody can argue with later, which is how V1's `suspect`
    # rows became indistinguishable from its traced ones.
    op.create_check_constraint(
        "ck_assets_coordinates_provenance",
        "assets",
        "(latitude IS NULL AND longitude IS NULL) OR coordinates_source IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_assets_coordinates_pair",
        "assets",
        "(latitude IS NULL) = (longitude IS NULL)",
    )
    op.create_check_constraint(
        "ck_assets_coordinates_source",
        "assets",
        f"coordinates_source IS NULL OR coordinates_source IN ({_values(COORDINATE_SOURCES)})",
    )
    op.create_check_constraint(
        "ck_assets_coordinates_confidence",
        "assets",
        f"coordinates_confidence IS NULL OR coordinates_confidence IN ({_values(COORDINATE_CONFIDENCES)})",
    )
    # Every rule that asks "is it daylight there" reads latitude/longitude for
    # a set of installations at once.
    op.create_index(
        "ix_assets_coordinates",
        "assets",
        ["latitude", "longitude"],
        postgresql_where=sa.text("latitude IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_assets_coordinates", table_name="assets")
    op.drop_constraint("ck_assets_coordinates_confidence", "assets", type_="check")
    op.drop_constraint("ck_assets_coordinates_source", "assets", type_="check")
    op.drop_constraint("ck_assets_coordinates_pair", "assets", type_="check")
    op.drop_constraint("ck_assets_coordinates_provenance", "assets", type_="check")
    op.drop_column("assets", "coordinates_confidence")
    op.drop_column("assets", "coordinates_source")
