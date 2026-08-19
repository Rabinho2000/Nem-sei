"""Assemble a customer report payload from V2's persisted data alone.

This is the layer between the database and the renderers. V1 keeps it in
`app_factory.build_local_customer_production_report`; the payload shape is
copied from V1's `_report_payload_for_period` so that `customer_pdf` renders a
V2-assembled report exactly as it renders a V1-assembled one.

Reads nothing but the database. No provider client is imported here and no code
path could acquire one, which is what makes a report reproducible.

## What V2 cannot source yet, and why it is `None` rather than `0`

V2's `production_facts` carries a single metric, `production_energy`, because
the FusionSolar adapter accepts only the explicitly verified `PVYield` signal.
Self-consumption, export, consumption and grid import have no persisted source,
and neither do tariffs, billing configuration or the commercial attributes that
decide EPC against ESCO. Every such field is emitted as `None` and named in
`unavailable_fields`, so a reader of the payload — or of the report — can see
which numbers are absent instead of reading a zero that looks like a
measurement. `datasets.py` makes the same distinction at the row level.

Note that `prepare_customer_report` still computes the *derived* monetary fields
from those absent inputs, and will therefore produce `0.00 €` for savings and
export revenue. That is V1's own behaviour given the same payload and it is left
alone: parity is the requirement, and diverging here would break the golden
tests that prove the renderers agree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.monitoring.repository import CanonicalFactRepository
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.reporting.customer_pdf import prepare_customer_report
from nemsei.reporting.datasets import build_dataset
from nemsei.reporting.models import ReportingDataset, ReportingDatasetRow
from nemsei.reporting.periods import exclusive_end
from nemsei.reporting.rules.types import BillingConfig, ReportPeriodType, ReportingPeriod


# Fields V1's payload carries that V2 has no persisted source for. Each one is
# emitted as None; this tuple is what turns "absent" into something a caller can
# assert on rather than discover in a rendered document.
ENERGY_FIELDS_WITHOUT_SOURCE = (
    "self_use_kwh",
    "export_kwh",
    "consumption_kwh",
    "grid_import_kwh",
    "self_use_cheia_kwh",
    "self_use_ponta_kwh",
    "self_use_vazio_kwh",
    "self_use_super_vazio_kwh",
)
COMMERCIAL_FIELDS_WITHOUT_SOURCE = (
    "tariff_value_eur",
    "tariff_type",
    "tariff_period_breakdown",
    "tariff_coverage_pct",
    "electricity_price",
    "sell_price",
    "solcor_price_per_kwh",
    "fixed_monthly_fee_eur",
)
ASSET_FIELDS_WITHOUT_SOURCE = ("contract_type", "asset_type", "coverage_type", "sell_to")
AVAILABILITY_FIELDS_WITHOUT_SOURCE = ("availability_pct",)


@dataclass(frozen=True)
class AssembledReport:
    """A rendered-ready payload plus the dataset and the honest gap list."""

    payload: dict[str, Any]
    dataset: ReportingDataset
    unavailable_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def asset_descriptor(session: Session, asset: Asset) -> dict[str, Any]:
    """The asset fields V1's payload names, from V2's canonical identity.

    `contract_type`, `asset_type`, `coverage_type` and `sell_to` are V1 columns
    with no V2 equivalent. They stay None rather than being guessed, which means
    `detect_report_type` falls back to EPC. A customer on an ESCO contract would
    then receive an EPC report, so the caller is expected to pass an explicit
    `billing_config` until those attributes are persisted.
    """
    owner = session.get(Organization, asset.owner_id) if asset.owner_id else None
    # The plant claim that identifies this asset to its provider. Device claims
    # are deliberately excluded: a report is about the installation.
    mapping = session.scalar(
        select(AssetProviderMapping)
        .where(
            AssetProviderMapping.asset_id == asset.id,
            AssetProviderMapping.resource_kind == "plant",
            AssetProviderMapping.mapping_status == "active",
        )
        .order_by(AssetProviderMapping.id)
    )
    connection = session.get(ProviderConnection, mapping.provider_connection_id) if mapping else None
    return {
        "asset_id": asset.id,
        "project_name": asset.canonical_name,
        "location": asset.locality or asset.address,
        "kwp": _float_or_none(asset.installed_dc_power_kw),
        "owner_name": owner.display_name if owner else None,
        "nif": owner.normalized_tax_id if owner else None,
        "external_id": mapping.external_id if mapping else None,
        "external_name": mapping.external_name if mapping else None,
        "energy_provider": connection.provider_code if connection else None,
        "contract_type": None,
        "asset_type": None,
        "coverage_type": None,
        "sell_to": None,
    }


def daily_rows_for(session: Session, *, asset_id: int, period: ReportingPeriod) -> list[dict[str, Any]]:
    """V1-shaped daily rows from the current revision of each daily fact.

    V1's rows carry production, self-use, export and consumption together. V2
    persists only production, so the remaining series are None and the PDF's
    daily chart plots the one series that exists — which is precisely the axis
    difference recorded in REPORTING_PARITY.md, now visible in the data rather
    than only in a document.
    """
    facts = CanonicalFactRepository(session).current_production_facts_for_asset(
        asset_id=asset_id,
        period_start=_as_datetime(period.start),
        period_end=_as_datetime(exclusive_end(period)),
    )
    rows: list[dict[str, Any]] = []
    for fact in facts:
        if fact.granularity != "day":
            continue
        rows.append(
            {
                "date": fact.period_start.date(),
                "production_kwh": _float_or_none(fact.value),
                "self_use_kwh": None,
                "export_kwh": None,
                "consumption_kwh": None,
                "grid_import_kwh": None,
                "quality": fact.quality,
                "source_fact_key": fact.source_fact_key,
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def monthly_rows_for(dataset_rows: list[ReportingDatasetRow]) -> list[dict[str, Any]]:
    """V1-shaped monthly rows, carrying V2's expected production alongside."""
    return [
        {
            "date": row.period_start,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "production_kwh": _float_or_none(row.actual_production_kwh),
            "production_state": row.actual_state,
            "expected_production_kwh": _float_or_none(row.expected_production_kwh),
            "expected_state": row.expected_state,
            "self_use_kwh": None,
            "export_kwh": None,
            "consumption_kwh": None,
            "grid_import_kwh": None,
        }
        for row in dataset_rows
    ]


def aggregate_rows(dataset_rows: list[ReportingDatasetRow], daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Total a period without ever turning an absent month into a zero."""
    measured = [row for row in dataset_rows if row.actual_state != "missing" and row.actual_production_kwh is not None]
    months_with_data = [row.period_start.strftime("%Y-%m") for row in measured]
    missing_months = [row.period_start.strftime("%Y-%m") for row in dataset_rows if row.actual_state == "missing"]
    total = sum((row.actual_production_kwh for row in measured), Decimal("0")) if measured else None
    partial = any(row.actual_state == "partial" for row in dataset_rows)

    expected_rows = [row for row in dataset_rows if row.expected_production_kwh is not None]
    expected_total = sum((row.expected_production_kwh for row in expected_rows), Decimal("0")) if expected_rows else None

    daily_values = [row["production_kwh"] for row in daily_rows if row["production_kwh"] is not None]
    coverage_pct = (len(months_with_data) / len(dataset_rows) * 100.0) if dataset_rows else 0.0

    if not measured:
        production_status = "missing"
    elif partial or missing_months:
        production_status = "partial"
    else:
        production_status = "complete"

    return {
        "production_kwh": _float_or_none(total),
        "expected_production_kwh": _float_or_none(expected_total),
        "raw_daily_total_kwh": sum(daily_values) if daily_values else None,
        "months_with_data": months_with_data,
        "missing_months": missing_months,
        "months_requiring_fallback": [],
        "coverage_pct": coverage_pct,
        "production_status": production_status,
        # A period is final only when every month it covers is measured.
        "production_is_final": bool(measured) and not missing_months and not partial,
        "monthly_production_quality": {
            row.period_start.strftime("%Y-%m"): row.actual_state for row in dataset_rows
        },
    }


def assemble_asset_report(
    session: Session,
    *,
    asset_id: int,
    period: ReportingPeriod,
    built_by: str,
    billing_config: BillingConfig | None = None,
    dataset: ReportingDataset | None = None,
) -> AssembledReport:
    """Build one asset's report payload from persisted facts alone."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ValueError("Unknown asset.")

    if dataset is None:
        dataset = build_dataset(
            session,
            asset_id=asset_id,
            period_start=period.start,
            # V1's period end is the last day; the dataset's is exclusive.
            period_end=exclusive_end(period),
            built_by=built_by,
        )
    dataset_rows = sorted(dataset.rows, key=lambda row: row.period_start)
    daily_rows = daily_rows_for(session, asset_id=asset_id, period=period)
    aggregate = aggregate_rows(dataset_rows, daily_rows)

    unavailable = (
        ENERGY_FIELDS_WITHOUT_SOURCE
        + COMMERCIAL_FIELDS_WITHOUT_SOURCE
        + AVAILABILITY_FIELDS_WITHOUT_SOURCE
        + tuple(f"asset.{name}" for name in ASSET_FIELDS_WITHOUT_SOURCE)
    )

    descriptor = asset_descriptor(session, asset)
    report: dict[str, Any] = {
        "asset": descriptor,
        "station_code": descriptor["external_id"],
        "month_start": period.start,
        "month_end": period.end,
        "month_label": period.label,
        "period_type": period.period_type.value,
        "period_start": period.start,
        "period_end": period.end,
        "period_label": period.label,
        "months_count": period.month_count,
        "included_months": [month.strftime("%Y-%m") for month in period.included_months],
        "months_with_data": aggregate["months_with_data"],
        "missing_months": aggregate["missing_months"],
        "months_requiring_fallback": aggregate["months_requiring_fallback"],
        "coverage_pct": aggregate["coverage_pct"],
        "daily_rows": daily_rows,
        "monthly_rows": monthly_rows_for(dataset_rows),
        "chart_granularity": "daily" if period.period_type == ReportPeriodType.MONTHLY else "monthly",
        "production_kwh": aggregate["production_kwh"],
        "expected_production_kwh": aggregate["expected_production_kwh"],
        "raw_daily_total_kwh": aggregate["raw_daily_total_kwh"],
        "production_quality_status": aggregate["production_status"],
        "production_is_final": aggregate["production_is_final"],
        "monthly_production_quality": aggregate["monthly_production_quality"],
        "warnings": list(dataset.warnings_json or []),
        "data_source": "V2 canonical facts",
        "data_origin": "production_facts",
        "covered_period": f"{period.start.isoformat()}/{exclusive_end(period).isoformat()}",
        "data_quality": aggregate["production_status"],
        "report_notes": [],
        "tariff_types_used": [],
        "tariff_source": "unavailable",
        "tariff_warnings": ["tariff_configuration_not_persisted_in_v2"],
        "include_availability_kpi": False,
        # Provenance a reader can follow back to the rows it came from.
        "dataset_id": dataset.id,
        "dataset_input_digest": dataset.input_digest,
        "financial_model_id": dataset.financial_model_id,
        "unavailable_fields": list(unavailable),
    }
    # Everything V2 cannot source is explicitly absent, never zero.
    for name in ENERGY_FIELDS_WITHOUT_SOURCE + COMMERCIAL_FIELDS_WITHOUT_SOURCE + AVAILABILITY_FIELDS_WITHOUT_SOURCE:
        report[name] = None

    prepared = prepare_customer_report(report, billing_config=billing_config, months_count=period.month_count)
    prepared["unavailable_fields"] = list(unavailable)
    return AssembledReport(
        payload=prepared,
        dataset=dataset,
        unavailable_fields=unavailable,
        notes=tuple(dataset.warnings_json or []),
    )


def excel_payload_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """Project the flat V1 payload into the sections the Excel writer reads.

    The two renderers do not share a payload shape: `customer_pdf` takes V1's
    flat dictionary and `excel.build_asset_report_workbook` takes sections. The
    projection lives here rather than in either renderer so that both are still
    pure functions of what they are given.
    """
    asset = report.get("asset") or {}
    return {
        "title": f"Relatório - {asset.get('project_name') or 'Instalação'}",
        "subtitle": "Relatorio de performance",
        "asset_name": asset.get("project_name"),
        "energy": {
            "production_kwh": report.get("production_kwh"),
            "self_use_kwh": report.get("self_use_kwh"),
            "export_kwh": report.get("export_kwh"),
            "consumption_kwh": report.get("consumption_kwh"),
            "grid_import_kwh": report.get("grid_import_kwh"),
        },
        "financial": {
            "savings_eur": report.get("savings_eur"),
            "export_revenue_eur": report.get("export_revenue_eur"),
            "solcor_payment_eur": report.get("solcor_payment_eur"),
            "fixed_monthly_fee_eur": report.get("fixed_monthly_fee_eur"),
            "net_benefit_eur": report.get("net_benefit_eur"),
        },
        "quality": {
            "production_state": report.get("production_quality_status"),
            "coverage_pct": report.get("coverage_pct"),
            "daily_total_kwh": report.get("raw_daily_total_kwh"),
        },
        "metadata": {
            "period_type": report.get("period_type"),
            "period_start": report.get("period_start"),
            "period_end": report.get("period_end"),
            "period_label": report.get("period_label"),
            "months_count": report.get("months_count"),
            "tariff_type": report.get("tariff_type"),
            "billing_mode": report.get("billing_mode"),
            "billing_energy_base": report.get("billing_energy_base"),
        },
    }
