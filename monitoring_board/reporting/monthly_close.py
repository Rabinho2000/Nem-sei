from __future__ import annotations

from datetime import date
from typing import Any

from monitoring_board.asset_capacity_repository import resolve_capacity_for_period
from monitoring_board.reporting.data_quality import evaluate_monthly_production_quality
from monitoring_board.reporting.periods import month_bounds
from monitoring_board.reporting.quality_gate import QualityGateResult, evaluate_report_quality
from monitoring_board.reporting.repositories import (
    detect_tariff_validity_warnings,
    get_asset_billing_config_row,
    get_latest_helioscope_expected,
    get_monthly_availability,
    get_monthly_production_record,
    list_daily_production_records,
)


def build_asset_close_payload(
    conn,
    *,
    asset_id: int,
    report_month: str,
    reference_date: date,
) -> dict[str, Any]:
    start, end = month_bounds(report_month)
    asset = conn.execute(
        """
        SELECT a.*, c.name AS customer_name, c.nif AS customer_nif
        FROM assets a
        LEFT JOIN customers c ON c.id = a.customer_id
        WHERE a.id = ?
        """,
        (asset_id,),
    ).fetchone()
    if asset is None:
        raise ValueError("asset_not_found")
    monthly = get_monthly_production_record(conn, asset_id, start)
    daily = list_daily_production_records(conn, asset_id=asset_id, start=start, end=end)
    quality = evaluate_monthly_production_quality(
        asset_id=asset_id,
        month_start=start,
        reference_date=reference_date,
        monthly_records=(monthly,) if monthly else (),
        daily_records=daily,
    )
    integration = conn.execute(
        """
        SELECT provider, external_id
        FROM asset_integrations
        WHERE asset_id = ? AND enabled = 1
        ORDER BY CASE provider WHEN 'FusionSolar' THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    production_provider = (
        str(monthly["provider"])
        if monthly is not None and "provider" in monthly.keys()
        else ""
    )
    provider = production_provider or (str(integration["provider"]) if integration else "")
    capacity = resolve_capacity_for_period(
        conn,
        asset_id=asset_id,
        period_start=start,
        period_end=end,
        fallback_kwp=asset["kwp"],
    )
    expected = get_latest_helioscope_expected(conn, asset_id, start.month)
    tariff_warnings = detect_tariff_validity_warnings(
        conn, asset_id=asset_id, start=start, end=end
    )
    billing = get_asset_billing_config_row(conn, asset_id)
    tariff = conn.execute(
        """
        SELECT id
        FROM asset_tariffs
        WHERE asset_id = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY valid_from DESC, id DESC
        LIMIT 1
        """,
        (asset_id, end.isoformat(), start.isoformat()),
    ).fetchone()
    availability = get_monthly_availability(conn, asset_id, start, end)
    warnings = list(quality.warnings)
    warnings.extend(tariff_warnings)
    if capacity.ambiguous:
        warnings.append("ambiguous_installed_power")
    sigenergy_history_status = ""
    sigenergy_unit_confirmed = False
    if provider.casefold() == "sigenergy":
        sigenergy_unit_confirmed = True
        sigenergy_history_status = (
            "available" if quality.status == "complete" else "backfill_incomplete"
        )
    return {
        "asset_id": asset_id,
        "customer_id": asset["customer_id"],
        "customer_name": asset["customer_name"] or "",
        "customer_nif": asset["customer_nif"] or asset["nif"] or "",
        "installation": asset["project_name"],
        "period_type": "monthly",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "energy_provider": provider,
        "external_id": integration["external_id"] if integration else "",
        "mapping_status": "mapped" if integration else "mapping_pending",
        "production_kwh": quality.production_kwh,
        "production_quality_status": quality.status,
        "production_source": quality.source,
        "production_expected_days": quality.expected_days,
        "production_available_days": quality.available_days,
        "production_missing_dates": [item.isoformat() for item in quality.missing_dates],
        "production_coverage_pct": round(quality.coverage_ratio * 100, 2),
        "expected_production_kwh": float(expected["expected_kwh"]) if expected else None,
        "expected_production_source": "helioscope" if expected else "none",
        "installed_power_kwp": (
            str(capacity.installed_power_kwp)
            if capacity.installed_power_kwp is not None
            else None
        ),
        "installed_power_source": capacity.source,
        "capacity_ambiguous": capacity.ambiguous,
        "availability_pct": availability,
        "tariff_valid": tariff is not None and not tariff_warnings,
        "tariff_warnings": list(tariff_warnings),
        "invoice_status": "missing_invoice",
        "billing_config_valid": billing is not None,
        "billing_config_id": int(billing["id"]) if billing else None,
        "warnings": sorted(set(warnings)),
        "sigenergy_history_status": sigenergy_history_status,
        "sigenergy_energy_unit_confirmed": sigenergy_unit_confirmed,
    }


def evaluate_close_payload(
    payload: dict[str, Any],
    *,
    scope: str,
    requires_financials: bool,
    requires_availability: bool,
    requires_customer: bool,
) -> QualityGateResult:
    return evaluate_report_quality(
        payload,
        scope=scope,
        requires_financials=requires_financials,
        requires_availability=requires_availability,
        requires_customer=requires_customer,
    )
