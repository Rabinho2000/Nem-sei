"""Tariffs and billing configuration: what a customer is actually charged.

These are the inputs a report needs that no provider can answer. They come from
an invoice, from a confirmed financial model, or from an operator, so every row
records which — V1 kept the same lineage in a free-text note, and a note cannot
be joined against the model it names.

Both tables are temporal. A tariff or a billing arrangement is true for a range
of dates, not forever, and a database exclusion constraint refuses two rows that
would price the same day for the same asset. V1 permitted the overlap and
resolved it by taking the newest row, which is how a customer gets billed at a
price nobody chose.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.db.base import Base
# These tables carry foreign keys into the reporting schema, so its mappers
# must be registered before SQLAlchemy can resolve them. Importing a module
# that only defines tables is how that ordering is stated rather than hoped for.
import nemsei.reporting.models  # noqa: E402,F401 - register the referenced tables


TARIFF_TYPES = ("simple", "bi-hourly", "tri-hourly", "tetra-hourly")
TARIFF_SOURCE_KINDS = ("financial_model", "invoice", "operator", "v1_import")
BILLING_SOURCE_KINDS = ("operator", "v1_import")
TARIFF_PERIOD_NAMES = ("ponta", "cheia", "vazio", "super_vazio")
WEEKDAY_TYPES = ("weekday", "saturday", "sunday", "holiday", "all")
REPORT_TYPES = ("epc", "esco")
BILLING_MODES = ("energy", "fixed_monthly_fee")
BILLING_ENERGY_BASES = ("self_consumption", "total_production")

# Prices keep V1's full precision. V1 stores them as TEXT and parses through
# Decimal, and its one real billing row carries seventeen decimal places.
PRICE = Numeric(28, 18)


class AssetTariff(Base):
    """One priced tariff for one asset over one date range."""

    __tablename__ = "asset_tariffs"
    __table_args__ = (
        CheckConstraint(f"tariff_type IN {TARIFF_TYPES!r}", name="ck_asset_tariffs_type"),
        CheckConstraint(f"source_kind IN {TARIFF_SOURCE_KINDS!r}", name="ck_asset_tariffs_source_kind"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_asset_tariffs_validity"),
        CheckConstraint(
            "source_kind <> 'financial_model' OR source_financial_model_id IS NOT NULL",
            name="ck_asset_tariffs_model_reference",
        ),
        CheckConstraint(
            "tariff_type <> 'simple' OR simple_price_eur_kwh IS NOT NULL",
            name="ck_asset_tariffs_simple_price",
        ),
        CheckConstraint(
            "tariff_type NOT IN ('bi-hourly', 'tri-hourly', 'tetra-hourly')"
            " OR (vazio_price_eur_kwh IS NOT NULL AND cheia_price_eur_kwh IS NOT NULL)",
            name="ck_asset_tariffs_cycle_prices",
        ),
        CheckConstraint(
            "tariff_type <> 'tetra-hourly' OR super_vazio_price_eur_kwh IS NOT NULL",
            name="ck_asset_tariffs_super_vazio_price",
        ),
        Index("ix_asset_tariffs_validity", "asset_id", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    tariff_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cycle_type: Mapped[str | None] = mapped_column(String(32))

    simple_price_eur_kwh: Mapped[Decimal | None] = mapped_column(PRICE)
    ponta_price_eur_kwh: Mapped[Decimal | None] = mapped_column(PRICE)
    cheia_price_eur_kwh: Mapped[Decimal | None] = mapped_column(PRICE)
    vazio_price_eur_kwh: Mapped[Decimal | None] = mapped_column(PRICE)
    super_vazio_price_eur_kwh: Mapped[Decimal | None] = mapped_column(PRICE)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_financial_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_models.id", ondelete="RESTRICT")
    )
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("report_source_files.id", ondelete="RESTRICT"))
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    period_rules: Mapped[list["TariffPeriodRule"]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan"
    )


class TariffPeriodRule(Base):
    """When a tariff's named period applies, in the plant's own local time."""

    __tablename__ = "tariff_period_rules"
    __table_args__ = (
        CheckConstraint(f"weekday_type IN {WEEKDAY_TYPES!r}", name="ck_tariff_period_rules_weekday"),
        CheckConstraint(f"period_name IN {TARIFF_PERIOD_NAMES!r}", name="ck_tariff_period_rules_period"),
        Index("ix_tariff_period_rules_tariff", "tariff_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("asset_tariffs.id", ondelete="CASCADE"), nullable=False)
    weekday_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # A window may cross midnight, so end_time can be earlier than start_time.
    # The ported `tariffs.py` already handles that case and is pinned by a
    # golden test against V1.
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    period_name: Mapped[str] = mapped_column(String(24), nullable=False)

    tariff: Mapped[AssetTariff] = relationship(back_populates="period_rules")


class AssetBillingConfig(Base):
    """How an asset is billed, over one date range."""

    __tablename__ = "asset_billing_configs"
    __table_args__ = (
        CheckConstraint(f"report_type IN {REPORT_TYPES!r}", name="ck_asset_billing_configs_report_type"),
        CheckConstraint(f"billing_mode IN {BILLING_MODES!r}", name="ck_asset_billing_configs_mode"),
        CheckConstraint(f"billing_energy_base IN {BILLING_ENERGY_BASES!r}", name="ck_asset_billing_configs_base"),
        CheckConstraint(f"source_kind IN {BILLING_SOURCE_KINDS!r}", name="ck_asset_billing_configs_source_kind"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_asset_billing_configs_validity"),
        CheckConstraint(
            "solcor_price_per_kwh >= 0 AND fixed_monthly_fee_eur >= 0"
            " AND default_electricity_price >= 0 AND default_export_price >= 0",
            name="ck_asset_billing_configs_non_negative",
        ),
        Index("ix_asset_billing_configs_validity", "asset_id", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="energy")
    billing_energy_base: Mapped[str] = mapped_column(String(32), nullable=False, default="self_consumption")
    solcor_price_per_kwh: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    fixed_monthly_fee_eur: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    default_electricity_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    default_export_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    export_revenue_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
