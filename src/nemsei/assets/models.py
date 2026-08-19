from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.db.base import Base


ASSET_LIFECYCLE_STATUSES = ("unknown", "active", "inactive", "decommissioned")
IMPORT_REVIEW_STATUSES = ("clear", "needs_review")
# Open vocabulary: only `inverter` is populated by the V1 migration. The other
# kinds exist so that a meter or a datalogger is a new row, not a new table.
DEVICE_KINDS = ("inverter", "meter", "datalogger", "string_box")


def public_id() -> str:
    return str(uuid.uuid4())


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(f"review_status IN {IMPORT_REVIEW_STATUSES!r}", name="ck_organizations_review_status"),
        Index(
            "uq_organizations_normalized_tax_id",
            "normalized_tax_id",
            unique=True,
            postgresql_where=text("normalized_tax_id IS NOT NULL AND normalized_tax_id != ''"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_tax_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="clear")
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assets: Mapped[list["Asset"]] = relationship(back_populates="owner")


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(f"lifecycle_status IN {ASSET_LIFECYCLE_STATUSES!r}", name="ck_assets_lifecycle_status"),
        CheckConstraint(f"review_status IN {IMPORT_REVIEW_STATUSES!r}", name="ck_assets_review_status"),
        Index("ix_assets_normalized_name", "normalized_name"),
        Index("ix_assets_owner", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="clear")
    review_note: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    country_code: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str | None] = mapped_column(String(64))
    timezone_source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    installed_dc_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    commissioned_on: Mapped[date | None] = mapped_column(Date)
    address: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    technical_notes: Mapped[str | None] = mapped_column(Text)
    # What the customer contracted. These four decide EPC against ESCO, and
    # without them `detect_report_type` defaults to EPC and sends an ESCO
    # customer the wrong document. They are free text because V1's are, and
    # normalising them would lose the distinctions its operators actually wrote
    # ("EPC (O&M)", "ESCO BUYOUT").
    contract_type: Mapped[str | None] = mapped_column(String(120))
    asset_type: Mapped[str | None] = mapped_column(String(120))
    coverage_type: Mapped[str | None] = mapped_column(String(120))
    sell_to: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    owner: Mapped[Organization | None] = relationship(back_populates="assets")
    aliases: Mapped[list["AssetAlias"]] = relationship(back_populates="asset", cascade="all, delete-orphan")
    devices: Mapped[list["Device"]] = relationship(back_populates="asset")


class AssetAlias(Base):
    __tablename__ = "asset_aliases"
    __table_args__ = (
        UniqueConstraint("asset_id", "normalized_alias", "valid_from", name="uq_asset_aliases_asset_alias_from"),
        Index("ix_asset_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    alias_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    asset: Mapped[Asset] = relationship(back_populates="aliases")


class Device(Base):
    """A physical device belonging to one installation.

    Canonical identity is the hardware itself: serial number, model and rated
    power. Provider identifiers live in `asset_provider_mappings` so that a
    provider account change supersedes evidence without touching the device.
    """

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint(f"device_kind IN {DEVICE_KINDS!r}", name="ck_devices_device_kind"),
        CheckConstraint(f"lifecycle_status IN {ASSET_LIFECYCLE_STATUSES!r}", name="ck_devices_lifecycle_status"),
        CheckConstraint(f"review_status IN {IMPORT_REVIEW_STATUSES!r}", name="ck_devices_review_status"),
        CheckConstraint("rated_power_kw IS NULL OR rated_power_kw >= 0", name="ck_devices_rated_power"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_devices_validity"),
        # Serials are unique per installation, never globally: identical
        # transcriptions across installations are plausible and must stay a
        # review condition rather than an import failure.
        Index(
            "uq_devices_asset_serial",
            "asset_id",
            "normalized_serial_number",
            unique=True,
            postgresql_where=text("normalized_serial_number IS NOT NULL AND valid_to IS NULL"),
        ),
        Index("ix_devices_asset", "asset_id", "device_kind"),
        Index("ix_devices_normalized_serial", "normalized_serial_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    parent_device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    device_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(120))
    normalized_serial_number: Mapped[str | None] = mapped_column(String(120))
    label: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(120))
    rated_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    lifecycle_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="clear")
    review_note: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    asset: Mapped[Asset] = relationship(back_populates="devices")
