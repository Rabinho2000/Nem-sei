"""Persisted financial models and the source files they came from.

Every number a report shows must be explainable back to a cell in a customer's
workbook, so the schema keeps the source file and its hash, the cell behind each
monthly value, the named rule behind each derived value, the parser that read it
and the warnings it raised. It also records whether a base year came from the
workbook or from an operator, because V1 conflated the two.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.db.base import Base


SOURCE_FILE_KINDS = ("financial_model",)
FINANCIAL_MODEL_STATUSES = ("draft", "confirmed", "superseded", "rejected")
BASE_YEAR_SOURCES = ("workbook", "operator", "unknown")
# The families V1 supports. Only `financial_automatic_as_sold` has a real
# workbook behind it; see docs/v2/FINANCIAL_MODEL_SOURCES.md.
WORKBOOK_FORMATS = (
    "financial_automatic_as_sold",
    "financial_automatic_upac",
    "monthly_metric_rows",
    "monthly_month_rows",
    "unknown",
)


class ReportSourceFile(Base):
    """One uploaded customer artefact, identified by its content hash."""

    __tablename__ = "report_source_files"
    __table_args__ = (
        CheckConstraint(f"file_kind IN {SOURCE_FILE_KINDS!r}", name="ck_report_source_files_kind"),
        CheckConstraint("size_bytes > 0", name="ck_report_source_files_size"),
        UniqueConstraint("sha256", name="uq_report_source_files_sha256"),
        Index("ix_report_source_files_asset", "asset_id", "file_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    file_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FinancialModel(Base):
    """One parse of one workbook, versioned per asset."""

    __tablename__ = "financial_models"
    __table_args__ = (
        CheckConstraint(f"status IN {FINANCIAL_MODEL_STATUSES!r}", name="ck_financial_models_status"),
        CheckConstraint(f"base_year_source IN {BASE_YEAR_SOURCES!r}", name="ck_financial_models_base_year_source"),
        CheckConstraint(f"workbook_format IN {WORKBOOK_FORMATS!r}", name="ck_financial_models_format"),
        CheckConstraint("base_year IS NULL OR base_year BETWEEN 2000 AND 2100", name="ck_financial_models_base_year"),
        CheckConstraint("detected_kwp IS NULL OR detected_kwp >= 0", name="ck_financial_models_kwp"),
        # An operator-chosen base year must say who chose it.
        CheckConstraint(
            "base_year_source <> 'operator' OR confirmed_by IS NOT NULL",
            name="ck_financial_models_operator_year_actor",
        ),
        UniqueConstraint("asset_id", "version", name="uq_financial_models_asset_version"),
        Index("ix_financial_models_asset_status", "asset_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("report_source_files.id", ondelete="RESTRICT"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    supersedes_model_id: Mapped[int | None] = mapped_column(ForeignKey("financial_models.id", ondelete="SET NULL"))

    base_year: Mapped[int | None] = mapped_column(Integer)
    base_year_source: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    base_year_cell: Mapped[str | None] = mapped_column(String(120))

    workbook_format: Mapped[str] = mapped_column(String(48), nullable=False, default="unknown")
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    detected_name: Mapped[str | None] = mapped_column(String(255))
    detected_nif: Mapped[str | None] = mapped_column(String(64))
    detected_kwp: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    parser_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_cells_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    months: Mapped[list["FinancialModelMonth"]] = relationship(back_populates="model", cascade="all, delete-orphan")


class FinancialModelMonth(Base):
    """One expected month of a financial model, with its provenance.

    Values are Numeric rather than float so that sums are exact. The scale is
    deliberately generous: the workbook holds IEEE-754 values with a dozen
    decimals and rounding them away would break numerical parity with V1.
    """

    __tablename__ = "financial_model_months"
    __table_args__ = (
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_financial_model_months_month"),
        UniqueConstraint("financial_model_id", "month", name="uq_financial_model_months_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_model_id: Mapped[int] = mapped_column(ForeignKey("financial_models.id", ondelete="CASCADE"), nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    expected_production_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_consumption_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_self_use_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_export_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_grid_import_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_self_consumption_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_self_sufficiency_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))

    source_fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    calculated_fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)

    model: Mapped[FinancialModel] = relationship(back_populates="months")

    def period_start(self, base_year: int) -> date:
        return date(base_year, self.month, 1)
