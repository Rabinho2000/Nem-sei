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
from nemsei.reporting.commercial import (
    billing_config_from,
    report_type_is_resolved,
    representative_price,
    resolve_billing_config,
    resolve_tariff,
    tariff_price_summary,
)
from nemsei.reporting.datasets import DATASET_METRICS, build_dataset
from nemsei.reporting.models import ReportingDataset, ReportingDatasetRow
from nemsei.reporting.periods import exclusive_end
from nemsei.reporting.rules.types import BillingConfig, ReportPeriodType, ReportingPeriod


# Fields V1's payload carries that V2 has no persisted source for. Each one is
# emitted as None; this tuple is what turns "absent" into something a caller can
# assert on rather than discover in a rendered document.
# Self-use split by tariff period. V1 reads these from provider payloads that
# state them per period; no provider V2 talks to does, and splitting a monthly
# total across periods would be arithmetic invented to fill a chart.
TARIFF_SPLIT_FIELDS_WITHOUT_SOURCE = (
    "self_use_cheia_kwh",
    "self_use_ponta_kwh",
    "self_use_vazio_kwh",
    "self_use_super_vazio_kwh",
)
# Availability needs device-level facts, which V2 does not collect yet.
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

    The four contract attributes decide EPC against ESCO. They are persisted now,
    but they are still nullable: an asset that states none of them makes
    `detect_report_type` fall back to EPC, and the payload flags that through
    `report_type_resolved` rather than letting a guess pass for a fact.
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
        "contract_type": asset.contract_type,
        "asset_type": asset.asset_type,
        "coverage_type": asset.coverage_type,
        "sell_to": asset.sell_to,
    }


def daily_rows_for(session: Session, *, asset_id: int, period: ReportingPeriod) -> list[dict[str, Any]]:
    """V1-shaped daily rows from the current revision of each daily fact.

    V1's rows carry production, self-use, export and consumption together. V2
    persists only production, so the remaining series are None and the PDF's
    daily chart plots the one series that exists — which is precisely the axis
    difference recorded in REPORTING_PARITY.md, now visible in the data rather
    than only in a document.
    """
    repository = CanonicalFactRepository(session)
    window = {
        "asset_id": asset_id,
        "period_start": _as_datetime(period.start),
        "period_end": _as_datetime(exclusive_end(period)),
    }
    by_day: dict[Any, dict[str, Any]] = {}

    def collect(metric_kind: str, field: str) -> None:
        for fact in repository.current_production_facts_for_asset(metric_kind=metric_kind, **window):
            if fact.granularity != "day":
                continue
            day = fact.period_start.date()
            row = by_day.setdefault(
                day,
                {
                    "date": day,
                    "production_kwh": None,
                    "self_use_kwh": None,
                    "export_kwh": None,
                    "consumption_kwh": None,
                    "grid_import_kwh": None,
                    "quality": {},
                    "source_fact_keys": {},
                },
            )
            row[field] = _float_or_none(fact.value)
            row["quality"][field] = fact.quality
            row["source_fact_keys"][field] = fact.source_fact_key

    collect("production_energy", "production_kwh")
    for name, metric_kind in DATASET_METRICS.items():
        collect(metric_kind, f"{name}_kwh")
    return [by_day[day] for day in sorted(by_day)]


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
            **{f"{name}_kwh": _float_or_none(getattr(row, f"{name}_kwh")) for name in DATASET_METRICS},
            **{f"{name}_state": getattr(row, f"{name}_state") for name in DATASET_METRICS},
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

    # Each metric totals independently. A month that measured production but not
    # consumption must report a production total and a missing consumption, not
    # a consumption of zero.
    metric_totals: dict[str, Any] = {}
    for name in DATASET_METRICS:
        present = [
            getattr(row, f"{name}_kwh")
            for row in dataset_rows
            if getattr(row, f"{name}_state") != "missing" and getattr(row, f"{name}_kwh") is not None
        ]
        metric_totals[f"{name}_kwh"] = float(sum(present, Decimal("0"))) if present else None
        metric_totals[f"{name}_state"] = (
            "missing" if not present
            else ("partial" if len(present) < len(dataset_rows) else "measured")
        )

    daily_values = [row["production_kwh"] for row in daily_rows if row["production_kwh"] is not None]
    coverage_pct = (len(months_with_data) / len(dataset_rows) * 100.0) if dataset_rows else 0.0

    if not measured:
        production_status = "missing"
    elif partial or missing_months:
        production_status = "partial"
    else:
        production_status = "complete"

    return {
        **metric_totals,
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

    # Commercial inputs, resolved from persisted rows for the period's first day
    # rather than passed in by whoever generated the report.
    tariff = resolve_tariff(session, asset_id=asset_id, on=period.start)
    persisted_billing = resolve_billing_config(session, asset_id=asset_id, on=period.start)
    if billing_config is None and persisted_billing is not None:
        billing_config = billing_config_from(persisted_billing)

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


    unavailable = list(TARIFF_SPLIT_FIELDS_WITHOUT_SOURCE + AVAILABILITY_FIELDS_WITHOUT_SOURCE)
    for name in DATASET_METRICS:
        if aggregate[f"{name}_state"] == "missing":
            unavailable.append(f"{name}_kwh")
    if aggregate["production_status"] == "missing":
        unavailable.append("production_kwh")
    if tariff is None:
        unavailable.extend(["tariff_value_eur", "tariff_type", "tariff_period_breakdown", "tariff_coverage_pct"])
    if persisted_billing is None:
        unavailable.extend(
            ["electricity_price", "sell_price", "solcor_price_per_kwh", "fixed_monthly_fee_eur"]
        )
    if not report_type_is_resolved(asset):
        unavailable.append("asset.contract_type")
    unavailable = tuple(dict.fromkeys(unavailable))

    descriptor = asset_descriptor(session, asset)
    # V1 resolves report type from the asset's own text fields and then forces
    # the billing configuration to agree, so a persisted configuration alone
    # would be overridden by an asset that states nothing. Rather than change
    # that ported rule, an explicitly configured report type is written into the
    # field the rule reads. An asset that already names its contract wins,
    # because that is the customer's paperwork rather than a later setting.
    if persisted_billing is not None and not report_type_is_resolved(asset):
        descriptor["contract_type"] = persisted_billing.report_type.upper()
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
        "tariff_type": tariff.tariff_type if tariff is not None else None,
        "tariff_types_used": [tariff.tariff_type] if tariff is not None else [],
        "tariff_source": (
            f"persisted:{tariff.source_kind}" if tariff is not None else "unavailable"
        ),
        "tariff_value_eur": None,
        "tariff_period_breakdown": (
            [
                {"period": name, "price_eur_kwh": float(price)}
                for name, price in sorted(tariff_price_summary(tariff).items())
            ]
            if tariff is not None
            else []
        ),
        "tariff_coverage_pct": None,
        "tariff_warnings": [] if tariff is not None else ["no_tariff_in_force_for_this_period"],
        "electricity_price": (
            float(persisted_billing.default_electricity_price) if persisted_billing is not None else None
        ),
        "sell_price": (
            float(persisted_billing.default_export_price) if persisted_billing is not None else None
        ),
        "solcor_price_per_kwh": (
            float(persisted_billing.solcor_price_per_kwh) if persisted_billing is not None else None
        ),
        "fixed_monthly_fee_eur": (
            float(persisted_billing.fixed_monthly_fee_eur) if persisted_billing is not None else None
        ),
        "billing_mode": persisted_billing.billing_mode if persisted_billing is not None else None,
        "billing_energy_base": (
            persisted_billing.billing_energy_base if persisted_billing is not None else None
        ),
        "export_revenue_enabled": (
            persisted_billing.export_revenue_enabled if persisted_billing is not None else True
        ),
        # Whether the report type was decided or merely defaulted. EPC is what
        # `detect_report_type` answers both when it reads "EPC" and when it reads
        # nothing, and a customer on an ESCO contract must not silently receive
        # the wrong document.
        "report_type_resolved": report_type_is_resolved(asset),
        "report_type_source": (
            "billing_config" if persisted_billing is not None
            else ("contract_attributes" if report_type_is_resolved(asset) else "default")
        ),
        "include_availability_kpi": False,
        # Provenance a reader can follow back to the rows it came from.
        "dataset_id": dataset.id,
        "dataset_input_digest": dataset.input_digest,
        "financial_model_id": dataset.financial_model_id,
        "unavailable_fields": list(unavailable),
    }
    for name in DATASET_METRICS:
        report[f"{name}_kwh"] = aggregate[f"{name}_kwh"]
        report[f"{name}_state"] = aggregate[f"{name}_state"]
    # What still has no source anywhere is explicitly absent, never zero.
    for name in TARIFF_SPLIT_FIELDS_WITHOUT_SOURCE + AVAILABILITY_FIELDS_WITHOUT_SOURCE:
        report[name] = None

    # A tariff prices the energy; it does not state a euro total. V1 computes
    # that from hourly rows split by tariff period, which V2 does not hold, so
    # the value stays absent and billing falls back to its own calculation
    # rather than a number this module made up.
    if tariff is not None:
        price = representative_price(tariff)
        report["tariff_value_eur"] = None
        report["tariff_simple_price_eur_kwh"] = None if price is None else float(price)

    prepared = prepare_customer_report(report, billing_config=billing_config, months_count=period.month_count)
    prepared["unavailable_fields"] = list(unavailable)
    # Where the energy came from, and whether any of it was estimated rather
    # than measured. Set after `prepare_customer_report` so the payload the
    # renderers already agree on is untouched, and read from `quality_json`,
    # which is outside the dataset digest -- so saying this out loud cannot
    # make two identical reports look different from each other.
    quality = dataset.quality_json or {}
    prepared["energy_sources"] = list(quality.get("actual_sources") or [])
    estimated_months = int(quality.get("months_with_estimated_energy") or 0)
    prepared["energy_estimated"] = estimated_months > 0
    prepared["energy_estimated_months"] = estimated_months
    if estimated_months:
        prepared.setdefault("report_notes", []).append(
            "energy_integrated_from_power_samples_not_metered"
        )
    prepared["report_type_resolved"] = report["report_type_resolved"]
    prepared["report_type_source"] = report["report_type_source"]
    if persisted_billing is None and not report["report_type_resolved"]:
        prepared.setdefault("report_notes", []).append(
            "report_type_defaulted_to_epc_without_contract_evidence"
        )
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
