"""Portfolios: flat collections, temporal membership, frozen snapshots.

The model is derived from what V1 actually holds, surveyed read-only and
recorded in `docs/v2/PORTFOLIOS.md`. Three of its properties drove the schema:

- A member is identified by its **sub-account within the portfolio**, not by an
  asset. 23 of V1's 80 members have no installation linked at all, and they are
  still members with a name and a NIF. `asset_id` is therefore nullable.
- Membership **overlaps**: two assets and four NIFs belong to both portfolios.
  So it is many-to-many, and uniqueness is per portfolio, not global.
- There is no nesting in V1, and the requirement forbids introducing it. This
  schema has no parent column, which is the strongest way to say so.

Revision ID: 0013_portfolios
Revises: 0012_reporting_inputs
"""
from alembic import op
import sqlalchemy as sa


revision = "0013_portfolios"
down_revision = "0012_reporting_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        # "Portfolio por cliente" is this column and nothing more elaborate.
        sa.Column("owner_id", sa.Integer()),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["owner_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("public_id", name="uq_portfolios_public_id"),
        sa.UniqueConstraint("slug", name="uq_portfolios_slug"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_portfolios_status"),
        sa.CheckConstraint("source_kind IN ('operator', 'v1_import')", name="ck_portfolios_source_kind"),
        sa.CheckConstraint("(status = 'archived') = (archived_at IS NOT NULL)", name="ck_portfolios_archived"),
    )

    op.create_table(
        "portfolio_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        # Null while a member is known but its installation is not. V1 has 23 of
        # these and they carry real evidence; dropping them would lose it.
        sa.Column("asset_id", sa.Integer()),
        sa.Column("sub_account", sa.String(length=64)),
        sa.Column("external_name", sa.String(length=255)),
        sa.Column("tax_id", sa.String(length=64)),
        sa.Column("resolution_state", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "resolution_state IN ('resolved', 'unresolved', 'placeholder')",
            name="ck_portfolio_memberships_resolution",
        ),
        sa.CheckConstraint(
            "(resolution_state = 'resolved') = (asset_id IS NOT NULL)",
            name="ck_portfolio_memberships_resolved_asset",
        ),
        sa.CheckConstraint("source_kind IN ('operator', 'v1_import', 'rule')", name="ck_portfolio_memberships_source"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_portfolio_memberships_validity"),
        sa.CheckConstraint(
            "asset_id IS NOT NULL OR sub_account IS NOT NULL OR external_name IS NOT NULL",
            name="ck_portfolio_memberships_identity",
        ),
    )
    op.create_index(
        "ix_portfolio_memberships_portfolio", "portfolio_memberships", ["portfolio_id", "valid_from", "valid_to"]
    )
    op.create_index("ix_portfolio_memberships_asset", "portfolio_memberships", ["asset_id"])
    # One asset may not be counted twice in the same portfolio at the same time.
    # Without this a plant is silently double-weighted in every portfolio total.
    op.execute(
        """
        ALTER TABLE portfolio_memberships ADD CONSTRAINT ex_portfolio_memberships_no_overlap
        EXCLUDE USING gist (
            portfolio_id WITH =,
            asset_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        ) WHERE (asset_id IS NOT NULL)
        """
    )

    op.create_table(
        "portfolio_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("attribute", sa.String(length=64), nullable=False),
        sa.Column("operator", sa.String(length=16), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        # The attributes a rule may filter on. Country and provider are here as
        # filters precisely so they never become sub-portfolios.
        sa.CheckConstraint(
            "attribute IN ('country_code', 'lifecycle_status', 'provider_code',"
            " 'contract_type', 'owner_id', 'locality')",
            name="ck_portfolio_rules_attribute",
        ),
        sa.CheckConstraint("operator IN ('in', 'not_in')", name="ck_portfolio_rules_operator"),
    )
    op.create_index("ix_portfolio_rules_portfolio", "portfolio_rules", ["portfolio_id"])

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        # The exact list, frozen. A report issued in March keeps naming the
        # plants it covered even after the portfolio changes in April.
        sa.Column("asset_ids_json", sa.JSON(), nullable=False),
        sa.Column("members_json", sa.JSON(), nullable=False),
        sa.Column("membership_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("period_end > period_start", name="ck_portfolio_snapshots_period"),
        sa.UniqueConstraint(
            "portfolio_id", "period_start", "period_end", "membership_digest",
            name="uq_portfolio_snapshots_period_digest",
        ),
    )
    op.create_index("ix_portfolio_snapshots_portfolio", "portfolio_snapshots", ["portfolio_id", "period_start"])

    op.create_table(
        "portfolio_datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("totals_json", sa.JSON(), nullable=False),
        sa.Column("coverage_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_by", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["portfolio_snapshots.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('building', 'ready', 'failed')", name="ck_portfolio_datasets_status"),
        sa.CheckConstraint("period_end > period_start", name="ck_portfolio_datasets_period"),
    )
    op.create_index("ix_portfolio_datasets_portfolio", "portfolio_datasets", ["portfolio_id", "period_start"])

    op.create_table(
        "portfolio_dataset_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_dataset_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        # The per-asset dataset this row came from. This column is what makes
        # the aggregate a reuse of the individual reporting rather than a second
        # implementation of it.
        sa.Column("reporting_dataset_id", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("states_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_dataset_id"], ["portfolio_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reporting_dataset_id"], ["reporting_datasets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("portfolio_dataset_id", "asset_id", name="uq_portfolio_dataset_members_asset"),
    )

    # A snapshot is what a portfolio report says it covered; it never changes.
    op.execute(
        """
        CREATE FUNCTION portfolio_snapshots_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'portfolio snapshots are append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER portfolio_snapshots_no_update BEFORE UPDATE ON portfolio_snapshots"
        " FOR EACH ROW EXECUTE FUNCTION portfolio_snapshots_immutable()"
    )
    op.execute(
        "CREATE TRIGGER portfolio_snapshots_no_delete BEFORE DELETE ON portfolio_snapshots"
        " FOR EACH ROW EXECUTE FUNCTION portfolio_snapshots_immutable()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT count(*) FROM portfolio_snapshots")).scalar_one()
    if existing:
        raise RuntimeError(
            f"Refusing to downgrade: {existing} portfolio snapshots record what reports covered."
        )
    op.execute("DROP TRIGGER portfolio_snapshots_no_delete ON portfolio_snapshots")
    op.execute("DROP TRIGGER portfolio_snapshots_no_update ON portfolio_snapshots")
    op.execute("DROP FUNCTION portfolio_snapshots_immutable()")
    op.drop_table("portfolio_dataset_members")
    op.drop_index("ix_portfolio_datasets_portfolio", table_name="portfolio_datasets")
    op.drop_table("portfolio_datasets")
    op.drop_index("ix_portfolio_snapshots_portfolio", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
    op.drop_index("ix_portfolio_rules_portfolio", table_name="portfolio_rules")
    op.drop_table("portfolio_rules")
    op.execute("ALTER TABLE portfolio_memberships DROP CONSTRAINT ex_portfolio_memberships_no_overlap")
    op.drop_index("ix_portfolio_memberships_asset", table_name="portfolio_memberships")
    op.drop_index("ix_portfolio_memberships_portfolio", table_name="portfolio_memberships")
    op.drop_table("portfolio_memberships")
    op.drop_table("portfolios")
