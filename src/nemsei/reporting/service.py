"""Import a parsed financial model workbook into V2, preserving its evidence.

Importing is deliberately explicit: the caller supplies the file, the operator
and, if they are overriding it, the base year. Nothing here reads a provider,
invents a value, or turns a missing value into a zero.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.reporting.commercial import confirmed_financial_model
from nemsei.reporting.financial_workbook import ParsedFinancialModel, parse_financial_model_workbook
from nemsei.reporting.models import (
    BASE_YEAR_SOURCES,
    WORKBOOK_FORMATS,
    FinancialModel,
    FinancialModelMonth,
    ReportSourceFile,
)
from nemsei.shared.clock import utc_now


MONTH_VALUE_FIELDS = (
    "expected_production_kwh",
    "expected_consumption_kwh",
    "expected_self_use_kwh",
    "expected_export_kwh",
    "expected_grid_import_kwh",
    "expected_self_consumption_rate_pct",
    "expected_self_sufficiency_rate_pct",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_or_none(value: Any) -> Decimal | None:
    """Keep a missing value missing. Only a real number becomes a number."""
    if value is None:
        return None
    return Decimal(str(value))


def register_source_file(
    session: Session,
    *,
    asset_id: int,
    path: Path,
    original_filename: str,
    stored_path: str,
    uploaded_by: str,
    mime_type: str | None = None,
    notes: str | None = None,
    content: bytes | None = None,
) -> ReportSourceFile:
    """Record an uploaded artefact, or return the one that already has this hash."""
    if session.get(Asset, asset_id) is None:
        raise ValueError("Unknown asset.")
    digest = file_sha256(path)
    existing = session.scalar(select(ReportSourceFile).where(ReportSourceFile.sha256 == digest))
    if existing is not None:
        if existing.asset_id != asset_id:
            raise ValueError("This file is already registered against another asset.")
        return existing
    source = ReportSourceFile(
        asset_id=asset_id,
        file_kind="financial_model",
        original_filename=original_filename.strip(),
        stored_path=stored_path.strip(),
        sha256=digest,
        mime_type=mime_type,
        size_bytes=path.stat().st_size,
        uploaded_by=uploaded_by.strip() or None,
        uploaded_at=utc_now(),
        notes=notes.strip() if notes else None,
        content=content,
    )
    session.add(source)
    session.flush()
    return source


def import_financial_model(
    session: Session,
    *,
    source_file: ReportSourceFile,
    workbook_path: Path,
    operator: str,
    base_year_override: int | None = None,
    confirm: bool = False,
    parsed: ParsedFinancialModel | None = None,
) -> FinancialModel:
    """Parse a workbook and persist it as the next version for its asset."""
    actor = operator.strip()
    if not actor:
        raise ValueError("An importing operator is required.")
    parsed = parsed if parsed is not None else parse_financial_model_workbook(workbook_path)

    # Where the base year came from is evidence in its own right: V1 stored an
    # operator's choice and the workbook's own value in the same column.
    if base_year_override is not None:
        base_year, base_year_source, base_year_cell = base_year_override, "operator", None
    elif parsed.base_year is not None:
        base_year = parsed.base_year
        base_year_source = "workbook"
        base_year_cell = parsed.source_cells.get("base_year")
    else:
        base_year, base_year_source, base_year_cell = None, "unknown", None
    if base_year_source not in BASE_YEAR_SOURCES:  # pragma: no cover - defensive
        raise ValueError("Invalid base year source.")

    workbook_format = str(parsed.details.get("format") or "unknown")
    if workbook_format not in WORKBOOK_FORMATS:
        workbook_format = "unknown"

    previous = confirmed_financial_model(session, asset_id=source_file.asset_id)
    next_version = (
        session.scalar(select(func.max(FinancialModel.version)).where(FinancialModel.asset_id == source_file.asset_id)) or 0
    ) + 1

    now = utc_now()
    model = FinancialModel(
        source_file_id=source_file.id,
        asset_id=source_file.asset_id,
        version=next_version,
        status="confirmed" if confirm else "draft",
        supersedes_model_id=previous.id if (confirm and previous is not None) else None,
        base_year=base_year,
        base_year_source=base_year_source,
        base_year_cell=base_year_cell,
        workbook_format=workbook_format,
        sheet_name=parsed.sheet_name,
        detected_name=parsed.detected_name or None,
        detected_nif=parsed.detected_nif or None,
        detected_kwp=decimal_or_none(parsed.detected_kwp),
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        source_file_sha256=source_file.sha256,
        warnings_json=list(parsed.warnings),
        details_json=dict(parsed.details),
        source_cells_json=dict(parsed.source_cells),
        confirmed_by=actor if (confirm or base_year_source == "operator") else None,
        confirmed_at=now if confirm else None,
        created_at=now,
        updated_at=now,
    )
    session.add(model)
    session.flush()

    for entry in parsed.monthly:
        session.add(
            FinancialModelMonth(
                financial_model_id=model.id,
                month=int(entry["month"]),
                source_fields_json=dict(entry.get("source_fields") or {}),
                calculated_fields_json=dict(entry.get("calculated_fields") or {}),
                warnings_json=list(entry.get("warnings") or []),
                **{field: decimal_or_none(entry.get(field)) for field in MONTH_VALUE_FIELDS},
            )
        )
    if confirm and previous is not None:
        previous.status = "superseded"
        previous.updated_at = now
    session.flush()
    return model
