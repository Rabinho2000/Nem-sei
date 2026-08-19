"""Reporting inputs: energy metrics beyond production, tariffs, billing, contracts.

Three gaps stopped a report being complete, and all three were data rather than
code. This migration closes them.

**Energy metrics.** `production_facts.metric_kind` accepted only
`production_energy`. V1's own reports read self-consumption, export, consumption
and grid import out of the provider payloads it stored, so the vocabulary is
extended to name them. The evidence is real: of 41 503 FusionSolar daily rows in
V1 that carry all three of `PVYield`, `selfUsePower` and `ongrid_power`, 40 303
satisfy `PVYield = selfUsePower + ongrid_power` exactly. Nothing is derived from
that identity — it is used to *check* provider rows, never to invent a value.

**Tariffs and billing.** V1 keeps `asset_tariffs`, `tariff_period_rules` and
`asset_billing_configs`. V2 gains the same concepts with temporal validity and
provenance, because the one real tariff V1 holds was derived from a confirmed
financial model and says so in a free-text note; here that lineage is a column.

**Contract attributes.** `assets` gains the four fields V1 uses to decide EPC
against ESCO. Without them `detect_report_type` silently defaults to EPC, which
sends an ESCO customer the wrong document. V1 populates `contract_type` on 254
of its 267 assets, so this is resolvable rather than aspirational.

Revision ID: 0012_reporting_inputs
Revises: 0011_reporting_datasets
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_reporting_inputs"
down_revision = "0011_reporting_datasets"
branch_labels = None
depends_on = None


PRODUCTION_METRICS = (
    "production_energy",
    "self_use_energy",
    "export_energy",
    "consumption_energy",
    "grid_import_energy",
)


def upgrade() -> None:
    # Temporal exclusion needs `=` on a scalar beside `&&` on a range in one
    # index, which stock GiST cannot do. btree_gist is a standard contrib
    # extension, not new infrastructure, and it is what puts "two tariffs may
    # not price the same day" in the database rather than in a code path
    # somebody can forget to call. The downgrade leaves it installed: dropping a
    # shared extension to undo one revision is a worse failure than leaving it.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # --- energy metrics -----------------------------------------------------
    op.drop_constraint("ck_production_facts_metric", "production_facts", type_="check")
    op.create_check_constraint(
        "ck_production_facts_metric",
        "production_facts",
        sa.text(f"metric_kind IN {PRODUCTION_METRICS!r}"),
    )

    # --- the dataset carries every metric a report reads ---------------------
    # Production already had its own columns; the four signals added above join
    # it here rather than being read around the dataset, so the input digest
    # still covers everything a report was built from. These tables are empty,
    # which is the only reason this is a column addition and not a project.
    op.add_column("reporting_dataset_rows", sa.Column("self_use_kwh", sa.Numeric(20, 10)))
    op.add_column("reporting_dataset_rows", sa.Column("self_use_state", sa.String(length=16), nullable=False, server_default="missing"))
    op.add_column("reporting_dataset_rows", sa.Column("export_kwh", sa.Numeric(20, 10)))
    op.add_column("reporting_dataset_rows", sa.Column("export_state", sa.String(length=16), nullable=False, server_default="missing"))
    op.add_column("reporting_dataset_rows", sa.Column("consumption_kwh", sa.Numeric(20, 10)))
    op.add_column("reporting_dataset_rows", sa.Column("consumption_state", sa.String(length=16), nullable=False, server_default="missing"))
    op.add_column("reporting_dataset_rows", sa.Column("grid_import_kwh", sa.Numeric(20, 10)))
    op.add_column("reporting_dataset_rows", sa.Column("grid_import_state", sa.String(length=16), nullable=False, server_default="missing"))
    op.create_check_constraint(
        "ck_reporting_dataset_rows_self_use_state",
        "reporting_dataset_rows",
        "self_use_state IN ('measured', 'missing', 'partial')",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_missing_self_use",
        "reporting_dataset_rows",
        "self_use_state <> 'missing' OR self_use_kwh IS NULL",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_export_state",
        "reporting_dataset_rows",
        "export_state IN ('measured', 'missing', 'partial')",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_missing_export",
        "reporting_dataset_rows",
        "export_state <> 'missing' OR export_kwh IS NULL",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_consumption_state",
        "reporting_dataset_rows",
        "consumption_state IN ('measured', 'missing', 'partial')",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_missing_consumption",
        "reporting_dataset_rows",
        "consumption_state <> 'missing' OR consumption_kwh IS NULL",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_grid_import_state",
        "reporting_dataset_rows",
        "grid_import_state IN ('measured', 'missing', 'partial')",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_missing_grid_import",
        "reporting_dataset_rows",
        "grid_import_state <> 'missing' OR grid_import_kwh IS NULL",
    )

    # --- contract attributes on the asset -----------------------------------
    for column in ("contract_type", "asset_type", "coverage_type", "sell_to"):
        op.add_column("assets", sa.Column(column, sa.String(length=120)))

    # --- tariffs ------------------------------------------------------------
    op.create_table(
        "asset_tariffs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("tariff_type", sa.String(length=32), nullable=False),
        sa.Column("cycle_type", sa.String(length=32)),
        # Prices keep V1's full precision. V1 stores them as TEXT and parses
        # through Decimal, and its one real row carries seventeen decimals;
        # rounding them here would change a customer's invoice.
        sa.Column("simple_price_eur_kwh", sa.Numeric(28, 18)),
        sa.Column("ponta_price_eur_kwh", sa.Numeric(28, 18)),
        sa.Column("cheia_price_eur_kwh", sa.Numeric(28, 18)),
        sa.Column("vazio_price_eur_kwh", sa.Numeric(28, 18)),
        sa.Column("super_vazio_price_eur_kwh", sa.Numeric(28, 18)),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        # Where this tariff came from. V1 recorded the same lineage in a note.
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_financial_model_id", sa.Integer()),
        sa.Column("source_file_id", sa.Integer()),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_financial_model_id"], ["financial_models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_file_id"], ["report_source_files.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "tariff_type IN ('simple', 'bi-hourly', 'tri-hourly', 'tetra-hourly')",
            name="ck_asset_tariffs_type",
        ),
        sa.CheckConstraint(
            "source_kind IN ('financial_model', 'invoice', 'operator', 'v1_import')",
            name="ck_asset_tariffs_source_kind",
        ),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_asset_tariffs_validity"),
        # A tariff that names a financial model must actually reference one.
        sa.CheckConstraint(
            "source_kind <> 'financial_model' OR source_financial_model_id IS NOT NULL",
            name="ck_asset_tariffs_model_reference",
        ),
        # Every type must price what it claims to price. A tri-hourly tariff
        # without a ponta price is not a tariff, it is a missing import.
        sa.CheckConstraint(
            "tariff_type <> 'simple' OR simple_price_eur_kwh IS NOT NULL",
            name="ck_asset_tariffs_simple_price",
        ),
        sa.CheckConstraint(
            "tariff_type NOT IN ('bi-hourly', 'tri-hourly', 'tetra-hourly')"
            " OR (vazio_price_eur_kwh IS NOT NULL AND cheia_price_eur_kwh IS NOT NULL)",
            name="ck_asset_tariffs_cycle_prices",
        ),
        sa.CheckConstraint(
            "tariff_type <> 'tetra-hourly' OR super_vazio_price_eur_kwh IS NOT NULL",
            name="ck_asset_tariffs_super_vazio_price",
        ),
    )
    op.create_index("ix_asset_tariffs_validity", "asset_tariffs", ["asset_id", "valid_from", "valid_to"])
    # Two tariffs for one asset may not cover the same day. V1 allowed it and
    # resolved the ambiguity by picking the newest row, which is how a customer
    # can be billed at a price nobody chose.
    op.execute(
        """
        ALTER TABLE asset_tariffs ADD CONSTRAINT ex_asset_tariffs_no_overlap
        EXCLUDE USING gist (
            asset_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )

    op.create_table(
        "tariff_period_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("weekday_type", sa.String(length=24), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("period_name", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(["tariff_id"], ["asset_tariffs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "weekday_type IN ('weekday', 'saturday', 'sunday', 'holiday', 'all')",
            name="ck_tariff_period_rules_weekday",
        ),
        sa.CheckConstraint(
            "period_name IN ('ponta', 'cheia', 'vazio', 'super_vazio')",
            name="ck_tariff_period_rules_period",
        ),
    )
    op.create_index("ix_tariff_period_rules_tariff", "tariff_period_rules", ["tariff_id"])

    # --- billing configuration ---------------------------------------------
    op.create_table(
        "asset_billing_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("report_type", sa.String(length=16), nullable=False),
        sa.Column("billing_mode", sa.String(length=32), nullable=False),
        sa.Column("billing_energy_base", sa.String(length=32), nullable=False),
        sa.Column("solcor_price_per_kwh", sa.Numeric(28, 18), nullable=False),
        sa.Column("fixed_monthly_fee_eur", sa.Numeric(28, 18), nullable=False),
        sa.Column("default_electricity_price", sa.Numeric(28, 18), nullable=False),
        sa.Column("default_export_price", sa.Numeric(28, 18), nullable=False),
        sa.Column("export_revenue_enabled", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("report_type IN ('epc', 'esco')", name="ck_asset_billing_configs_report_type"),
        sa.CheckConstraint("billing_mode IN ('energy', 'fixed_monthly_fee')", name="ck_asset_billing_configs_mode"),
        sa.CheckConstraint(
            "billing_energy_base IN ('self_consumption', 'total_production')",
            name="ck_asset_billing_configs_base",
        ),
        sa.CheckConstraint("source_kind IN ('operator', 'v1_import')", name="ck_asset_billing_configs_source_kind"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_asset_billing_configs_validity"),
        # Prices are never negative; a negative price is a parse error, not a discount.
        sa.CheckConstraint(
            "solcor_price_per_kwh >= 0 AND fixed_monthly_fee_eur >= 0"
            " AND default_electricity_price >= 0 AND default_export_price >= 0",
            name="ck_asset_billing_configs_non_negative",
        ),
    )
    op.create_index("ix_asset_billing_configs_validity", "asset_billing_configs", ["asset_id", "valid_from", "valid_to"])
    op.execute(
        """
        ALTER TABLE asset_billing_configs ADD CONSTRAINT ex_asset_billing_configs_no_overlap
        EXCLUDE USING gist (
            asset_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("asset_tariffs", "asset_billing_configs"):
        existing = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if existing:
            raise RuntimeError(
                f"Refusing to downgrade: {existing} rows in {table} price what customers are billed."
            )
    non_production = bind.execute(
        sa.text("SELECT count(*) FROM production_facts WHERE metric_kind <> 'production_energy'")
    ).scalar_one()
    if non_production:
        raise RuntimeError(
            f"Refusing to downgrade: {non_production} production facts use a metric this revision introduced."
        )

    op.execute("ALTER TABLE asset_billing_configs DROP CONSTRAINT ex_asset_billing_configs_no_overlap")
    op.drop_index("ix_asset_billing_configs_validity", table_name="asset_billing_configs")
    op.drop_table("asset_billing_configs")
    op.drop_index("ix_tariff_period_rules_tariff", table_name="tariff_period_rules")
    op.drop_table("tariff_period_rules")
    op.execute("ALTER TABLE asset_tariffs DROP CONSTRAINT ex_asset_tariffs_no_overlap")
    op.drop_index("ix_asset_tariffs_validity", table_name="asset_tariffs")
    op.drop_table("asset_tariffs")
    for metric in ("grid_import", "consumption", "export", "self_use"):
        op.drop_column("reporting_dataset_rows", f"{metric}_state")
        op.drop_column("reporting_dataset_rows", f"{metric}_kwh")
    for column in ("sell_to", "coverage_type", "asset_type", "contract_type"):
        op.drop_column("assets", column)
    op.drop_constraint("ck_production_facts_metric", "production_facts", type_="check")
    op.create_check_constraint(
        "ck_production_facts_metric",
        "production_facts",
        # Written out rather than interpolated from a tuple: a one-element
        # Python tuple renders as ('production_energy',) and the trailing comma
        # is a syntax error in SQL.
        sa.text("metric_kind IN ('production_energy')"),
    )
