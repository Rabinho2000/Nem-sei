from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


SOURCE_USES = ("monitoring", "production")


class AssetSourcePolicy(Base):
    __tablename__ = "asset_source_policies"
    __table_args__ = (
        CheckConstraint(f"source_use IN {SOURCE_USES!r}", name="ck_asset_source_policies_use"),
        CheckConstraint("priority > 0", name="ck_asset_source_policies_priority"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_asset_source_policies_valid_range"),
        Index("ix_asset_source_policies_selection", "asset_id", "source_use", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    provider_mapping_id: Mapped[int] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False)
    source_use: Mapped[str] = mapped_column(String(24), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
