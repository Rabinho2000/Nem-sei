"""Record which installations are under O&M, and for how long.

V1 identified its O&M portfolio with `assets.maintenance`,
`assets.active_contract`, `assets.start_contract`, `assets.end_contract`,
`assets.duration` and a `UNIQUE (asset_id)` `om_contracts` table. V2 keeps one
fact -- the engagement period -- and derives the rest.

`active_contract` is not carried over because it was never independent data:
V1's `derive_active_contract` computes it from `end_contract >= today` and a
nightly pass rewrote the stored copy. The uniqueness on `om_contracts.asset_id`
is not carried over either: it is why renewing a contract in V1 destroyed the
terms that were true the year before. Here a renewal is a new row, and the
exclusion constraint is what stops two rows claiming the same day.

Revision ID: 0027_asset_service_contracts
Revises: 0026_huawei_scada
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_asset_service_contracts"
down_revision = "0026_huawei_scada"
branch_labels = None
depends_on = None

SERVICE_KINDS = ("om",)
SOURCE_KINDS = ("v1_import", "operator")
REVIEW_STATUSES = ("clear", "needs_review")
RENEWAL_STATUSES = ("not_started", "in_contact", "renewed", "lost")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    # Already installed by 0012 for the tariff and billing windows; repeated
    # here so this revision stands on its own if applied to a fresh database.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "asset_service_contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("service_kind", sa.String(length=32), nullable=False, server_default="om"),
        # Both bounds are nullable: an unbounded side means "not known", which
        # is the honest state for the two V1 installations that are operated
        # without a recorded start. `valid_to` is exclusive.
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("annual_value_eur", sa.Numeric(14, 2), nullable=True),
        sa.Column("renewal_status", sa.String(length=32), nullable=True),
        sa.Column("last_contact_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(length=24), nullable=False, server_default="clear"),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_asset_service_contracts_public_id"),
        sa.CheckConstraint(f"service_kind IN ({_values(SERVICE_KINDS)})", name="ck_asset_service_contracts_kind"),
        sa.CheckConstraint(f"source_kind IN ({_values(SOURCE_KINDS)})", name="ck_asset_service_contracts_source_kind"),
        sa.CheckConstraint(
            f"review_status IN ({_values(REVIEW_STATUSES)})", name="ck_asset_service_contracts_review_status"
        ),
        sa.CheckConstraint(
            f"renewal_status IS NULL OR renewal_status IN ({_values(RENEWAL_STATUSES)})",
            name="ck_asset_service_contracts_renewal_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="ck_asset_service_contracts_validity",
        ),
        sa.CheckConstraint(
            "annual_value_eur IS NULL OR annual_value_eur >= 0",
            name="ck_asset_service_contracts_annual_value",
        ),
    )
    op.create_index(
        "ix_asset_service_contracts_validity", "asset_service_contracts", ["asset_id", "valid_from", "valid_to"]
    )
    # Two engagements may not claim the same day for the same installation. A
    # NULL bound is unbounded here, so an undated row conflicts with every
    # other row on that installation -- which is the intended reading: we do
    # not know when it ran, so we cannot assert it ran beside something else.
    op.execute(
        """
        ALTER TABLE asset_service_contracts
        ADD CONSTRAINT ex_asset_service_contracts_no_overlap
        EXCLUDE USING gist (
            asset_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_asset_service_contracts_validity", table_name="asset_service_contracts")
    op.drop_table("asset_service_contracts")
