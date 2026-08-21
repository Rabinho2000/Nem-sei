"""Build reporting datasets and freeze them into snapshots.

Everything here reads persisted facts only. There is no provider client in this
module and no code path that could acquire one, which is the point: a report
must be reproducible from what the database already holds, not from whatever a
provider answers at render time.

A period with no fact stays missing. It is never coerced to zero, because zero
production and unknown production mean opposite things to a customer.
"""
from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from reportlab.lib.colors import HexColor
from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.monitoring.models import ProductionFact
from nemsei.monitoring.repository import CanonicalFactRepository
from nemsei.reporting.models import (
    FinancialModel,
    FinancialModelMonth,
    ReportSnapshot,
    ReportingDataset,
    ReportingDatasetRow,
)
from nemsei.shared.clock import utc_now
from nemsei.shared.json_safe import json_safe


def month_starts(period_start: date, period_end: date) -> list[date]:
    """Every month covered by a half-open period."""
    if period_end <= period_start:
        raise ValueError("A reporting period must end after it starts.")
    months: list[date] = []
    cursor = period_start.replace(day=1)
    while cursor < period_end:
        months.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


def month_end(month_start: date) -> date:
    """The exclusive end of a month: the first day of the next one."""
    last_day = monthrange(month_start.year, month_start.month)[1]
    return date(month_start.year, month_start.month, last_day) + timedelta(days=1)


def digest_of(payload: Any) -> str:
    """A stable digest: same inputs, same string, regardless of dict order."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# The report-facing name of each canonical metric. Production keeps the name it
# already had in this module; the rest join it rather than being read elsewhere,
# so the dataset digest still covers every number a report was built from.
DATASET_METRICS = {
    "self_use": "self_use_energy",
    "export": "export_energy",
    "consumption": "consumption_energy",
    "grid_import": "grid_import_energy",
}


def _sum_actual(facts: Iterable[ProductionFact]) -> tuple[Decimal | None, str, list[str]]:
    """Total one month of daily facts, keeping missing distinguishable from zero."""
    values, sources, incomplete = [], [], False
    for fact in facts:
        sources.append(fact.source_fact_key)
        if fact.value is None or fact.quality == "missing":
            incomplete = True
            continue
        if fact.quality != "complete" or fact.completeness != "complete":
            incomplete = True
        values.append(fact.value)
    if not values:
        return None, "missing", sources
    return sum(values, Decimal("0")), ("partial" if incomplete else "measured"), sources


def build_dataset(
    session: Session,
    *,
    asset_id: int,
    period_start: date,
    period_end: date,
    built_by: str,
    financial_model: FinancialModel | None = None,
) -> ReportingDataset:
    """Resolve one asset's reporting period from persisted facts alone."""
    actor = built_by.strip()
    if not actor:
        raise ValueError("A dataset must record who built it.")
    if session.get(Asset, asset_id) is None:
        raise ValueError("Unknown asset.")
    months = month_starts(period_start, period_end)

    if financial_model is None:
        financial_model = session.scalar(
            select(FinancialModel)
            .where(FinancialModel.asset_id == asset_id, FinancialModel.status == "confirmed")
            .order_by(FinancialModel.version.desc())
        )
    expected_by_month: dict[int, FinancialModelMonth] = {}
    if financial_model is not None:
        expected_by_month = {
            row.month: row
            for row in session.scalars(
                select(FinancialModelMonth).where(FinancialModelMonth.financial_model_id == financial_model.id)
            )
        }

    # Only the current revision of each source fact. `production_facts` is
    # append-only, so a corrected reading sits beside the one it replaced;
    # totalling the raw rows would report a plant's production twice over.
    repository = CanonicalFactRepository(session)
    facts = repository.current_production_facts_for_asset(
        asset_id=asset_id,
        period_start=_as_datetime(period_start),
        period_end=_as_datetime(period_end),
    )
    other_facts = {
        name: repository.current_production_facts_for_asset(
            asset_id=asset_id,
            period_start=_as_datetime(period_start),
            period_end=_as_datetime(period_end),
            metric_kind=metric_kind,
        )
        for name, metric_kind in DATASET_METRICS.items()
    }

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for start in months:
        end = month_end(start)
        monthly_facts = [fact for fact in facts if start <= fact.period_start.date() < end]
        actual, actual_state, sources = _sum_actual(monthly_facts)

        monthly_metrics: dict[str, Any] = {}
        for name in DATASET_METRICS:
            in_month = [fact for fact in other_facts[name] if start <= fact.period_start.date() < end]
            value, state, keys = _sum_actual(in_month)
            monthly_metrics[f"{name}_kwh"] = value
            monthly_metrics[f"{name}_state"] = state
            monthly_metrics[f"{name}_fact_keys"] = keys

        expected_row = expected_by_month.get(start.month)
        expected = expected_row.expected_production_kwh if expected_row else None
        expected_state = "measured" if expected is not None else "missing"
        if expected_state == "missing":
            warnings.append(f"expected_production_missing:{start.isoformat()}")
        if actual_state == "missing":
            warnings.append(f"actual_production_missing:{start.isoformat()}")

        rows.append(
            {
                "asset_id": asset_id,
                "period_start": start,
                "period_end": end,
                "actual_production_kwh": actual,
                "actual_state": actual_state,
                "expected_production_kwh": expected,
                "expected_state": expected_state,
                **{key: value for key, value in monthly_metrics.items() if not key.endswith("_fact_keys")},
                "provenance": {
                    "actual_fact_keys": sources,
                    "actual_fact_count": len(monthly_facts),
                    "metric_fact_keys": {
                        name: monthly_metrics[f"{name}_fact_keys"] for name in DATASET_METRICS
                    },
                    "expected_source": (
                        {
                            "financial_model_id": financial_model.id,
                            "financial_model_version": financial_model.version,
                            "base_year": financial_model.base_year,
                            "base_year_source": financial_model.base_year_source,
                            "cells": expected_row.source_fields_json.get("expected_production_kwh"),
                        }
                        if expected_row is not None and financial_model is not None
                        else None
                    ),
                },
            }
        )

    dataset = ReportingDataset(
        scope="asset",
        asset_id=asset_id,
        period_start=period_start,
        period_end=period_end,
        status="ready",
        input_digest=digest_of([_row_digest_payload(row) for row in rows]),
        financial_model_id=financial_model.id if financial_model is not None else None,
        quality_json={
            "months": len(rows),
            "months_with_actual": sum(1 for row in rows if row["actual_state"] != "missing"),
            "months_with_expected": sum(1 for row in rows if row["expected_state"] != "missing"),
            "months_by_metric": {
                name: sum(1 for row in rows if row[f"{name}_state"] != "missing") for name in DATASET_METRICS
            },
        },
        warnings_json=sorted(set(warnings)),
        built_at=utc_now(),
        built_by=actor,
    )
    session.add(dataset)
    session.flush()
    for row in rows:
        session.add(
            ReportingDatasetRow(
                dataset_id=dataset.id,
                asset_id=row["asset_id"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                actual_production_kwh=row["actual_production_kwh"],
                actual_state=row["actual_state"],
                expected_production_kwh=row["expected_production_kwh"],
                expected_state=row["expected_state"],
                **{
                    field: row[field]
                    for name in DATASET_METRICS
                    for field in (f"{name}_kwh", f"{name}_state")
                },
                provenance_json=row["provenance"],
            )
        )
    session.flush()
    return dataset


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _row_digest_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Only the values and their provenance decide identity, never build time."""
    return {
        "asset_id": row["asset_id"],
        "period_start": row["period_start"].isoformat(),
        "actual": None if row["actual_production_kwh"] is None else format(row["actual_production_kwh"], "f"),
        "actual_state": row["actual_state"],
        "expected": None if row["expected_production_kwh"] is None else format(row["expected_production_kwh"], "f"),
        "expected_state": row["expected_state"],
        **{
            name: (None if row[f"{name}_kwh"] is None else format(row[f"{name}_kwh"], "f"))
            for name in DATASET_METRICS
        },
        **{f"{name}_state": row[f"{name}_state"] for name in DATASET_METRICS},
        "provenance": row["provenance"],
    }


# `json_safe` now lives in `nemsei.shared.json_safe` (imported above) --
# moved out of this module (D1, docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md)
# because `diagnostics/incidents.py` hit the exact same "JSON column raises on
# Decimal/date" problem this module solved first, persisting a plain evidence
# dict that happened to carry a `Decimal`. Everything below still calls
# `json_safe(...)` exactly as before -- only where the function is defined
# changed.


def rehydrate_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reverse the two lossy steps of `json_safe`.

    `daily_rows[].date`: every other date-bearing field is only ever read back
    through `str()` or an f-string in the renderers, and an ISO date string
    sorts and prints exactly like the `date` it replaced. This field is the
    exception — `customer_pdf.py` filters rows with
    `isinstance(row["date"], date)` to draw the daily chart, so a snapshot
    reopened without this step would render with its chart silently empty.

    `tariff_rows[][2]`: stored as a hex string; reopened as the same
    `reportlab.lib.colors.Color` the live payload carried, or the donut and
    the client-outage chart would draw in whatever reportlab's default fill is
    instead of the report's own palette, and `_themed_color`'s
    `color == NAVY`-style comparisons would silently stop matching.
    """
    rehydrated = dict(payload)
    daily_rows = rehydrated.get("daily_rows")
    if isinstance(daily_rows, list):
        rehydrated["daily_rows"] = [
            {**row, "date": date.fromisoformat(row["date"])}
            if isinstance(row, dict) and isinstance(row.get("date"), str)
            else row
            for row in daily_rows
        ]
    tariff_rows = rehydrated.get("tariff_rows")
    if isinstance(tariff_rows, list):
        rehydrated["tariff_rows"] = [
            (row[0], row[1], HexColor(row[2]))
            if isinstance(row, (list, tuple)) and len(row) == 3 and isinstance(row[2], str)
            else row
            for row in tariff_rows
        ]
    return rehydrated


def snapshot_dataset(
    session: Session,
    *,
    dataset: ReportingDataset,
    payload: dict[str, Any],
    created_by: str,
    quality: dict[str, Any] | None = None,
    notes: str | None = None,
) -> ReportSnapshot:
    """Freeze a dataset and its payload. Re-freezing identical input reuses it."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("A snapshot must record who created it.")
    if dataset.status != "ready":
        raise ValueError("Only a ready dataset can be snapshotted.")
    payload = json_safe(payload)
    # `assemble_asset_report` embeds `dataset_id`, the primary key of the
    # `ReportingDataset` row it happened to build. `build_dataset` is never
    # deduplicated — two calls over the same unchanged facts get two different
    # ids — so hashing it would mean "identical input" could never actually
    # reuse a snapshot: every regeneration would look new even when nothing
    # about the facts changed. `dataset.input_digest`, already part of what is
    # hashed below, is the content-stable identity; the row id is bookkeeping,
    # kept in the stored payload for provenance but excluded from the digest.
    digest_payload = {key: value for key, value in payload.items() if key != "dataset_id"}
    snapshot_digest = digest_of({"dataset": dataset.input_digest, "payload": digest_payload})
    existing = session.scalar(select(ReportSnapshot).where(ReportSnapshot.snapshot_digest == snapshot_digest))
    if existing is not None:
        return existing
    snapshot = ReportSnapshot(
        dataset_id=dataset.id,
        dataset_input_digest=dataset.input_digest,
        snapshot_digest=snapshot_digest,
        payload_json=payload,
        quality_json=quality or {},
        notes=notes.strip() if notes else None,
        created_by=actor,
        created_at=utc_now(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot
