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

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint
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
    # The workbook itself. `stored_path` says where it came from; this is the
    # only copy the platform controls, since the web container has no writable
    # storage -- see migration 0022 for the trade-off.
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
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


DATASET_SCOPES = ("asset", "portfolio")
DATASET_STATUSES = ("building", "ready", "failed")
# What a row's actual production is backed by. `missing` is a first-class
# outcome: a period with no fact is never reported as zero.
VALUE_STATES = ("measured", "missing", "partial")


class ReportingDataset(Base):
    """The resolved inputs for one reporting period, built from persisted facts."""

    __tablename__ = "reporting_datasets"
    __table_args__ = (
        CheckConstraint(f"scope IN {DATASET_SCOPES!r}", name="ck_reporting_datasets_scope"),
        CheckConstraint(f"status IN {DATASET_STATUSES!r}", name="ck_reporting_datasets_status"),
        CheckConstraint("period_end > period_start", name="ck_reporting_datasets_period"),
        CheckConstraint(
            "(scope = 'asset') = (asset_id IS NOT NULL)",
            name="ck_reporting_datasets_scope_target",
        ),
        Index("ix_reporting_datasets_target", "scope", "asset_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="building")

    # The digest covers every input value and its provenance, so two datasets
    # built from the same facts are recognisably the same dataset.
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    financial_model_id: Mapped[int | None] = mapped_column(ForeignKey("financial_models.id", ondelete="RESTRICT"))
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    built_by: Mapped[str] = mapped_column(String(120), nullable=False)

    rows: Mapped[list["ReportingDatasetRow"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class ReportingDatasetRow(Base):
    """One asset-month of a dataset, with where each number came from."""

    __tablename__ = "reporting_dataset_rows"
    __table_args__ = (
        CheckConstraint(f"actual_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_actual_state"),
        CheckConstraint(f"expected_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_expected_state"),
        CheckConstraint("actual_state <> 'missing' OR actual_production_kwh IS NULL", name="ck_reporting_dataset_rows_missing_actual"),
        CheckConstraint("expected_state <> 'missing' OR expected_production_kwh IS NULL", name="ck_reporting_dataset_rows_missing_expected"),
        CheckConstraint(f"self_use_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_self_use_state"),
        CheckConstraint("self_use_state <> 'missing' OR self_use_kwh IS NULL", name="ck_reporting_dataset_rows_missing_self_use"),
        CheckConstraint(f"export_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_export_state"),
        CheckConstraint("export_state <> 'missing' OR export_kwh IS NULL", name="ck_reporting_dataset_rows_missing_export"),
        CheckConstraint(f"consumption_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_consumption_state"),
        CheckConstraint("consumption_state <> 'missing' OR consumption_kwh IS NULL", name="ck_reporting_dataset_rows_missing_consumption"),
        CheckConstraint(f"grid_import_state IN {VALUE_STATES!r}", name="ck_reporting_dataset_rows_grid_import_state"),
        CheckConstraint("grid_import_state <> 'missing' OR grid_import_kwh IS NULL", name="ck_reporting_dataset_rows_missing_grid_import"),
        UniqueConstraint("dataset_id", "asset_id", "period_start", name="uq_reporting_dataset_rows_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("reporting_datasets.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    actual_production_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    actual_state: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_production_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    expected_state: Mapped[str] = mapped_column(String(16), nullable=False)

    # The other energy signals a report reads. Each keeps its own state, so a
    # month that measured production but not consumption says exactly that.
    self_use_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    self_use_state: Mapped[str] = mapped_column(String(16), nullable=False, default="missing")
    export_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    export_state: Mapped[str] = mapped_column(String(16), nullable=False, default="missing")
    consumption_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    consumption_state: Mapped[str] = mapped_column(String(16), nullable=False, default="missing")
    grid_import_kwh: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    grid_import_state: Mapped[str] = mapped_column(String(16), nullable=False, default="missing")

    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    dataset: Mapped[ReportingDataset] = relationship(back_populates="rows")


class ReportSnapshot(Base):
    """An immutable capture of a dataset and the payload computed from it."""

    __tablename__ = "report_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_digest", name="uq_report_snapshots_digest"),
        Index("ix_report_snapshots_dataset", "dataset_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("reporting_datasets.id", ondelete="RESTRICT"), nullable=False)
    dataset_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
