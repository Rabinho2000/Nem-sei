from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from monitoring_board.reporting.models import (
    BillingConfig,
    BillingEnergyBase,
    BillingMode,
    BillingResult,
    EnergyBreakdown,
    ReportType,
)


ZERO = Decimal("0")
HUNDRED = Decimal("100")


def decimal_from_value(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        parsed = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return ZERO
    return max(parsed, ZERO)


def decimal_to_float(value: Decimal) -> float:
    return float(value)


def _asset_value(asset: Any, key: str) -> Any:
    if isinstance(asset, dict):
        return asset.get(key)
    try:
        return asset[key]
    except (KeyError, TypeError, IndexError):
        return getattr(asset, key, None)


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).lower().strip()


def detect_report_type_value(asset: Any) -> ReportType:
    fields = ("contract_type", "asset_type", "coverage_type", "sell_to", "project_name")
    for field in fields:
        value = _normalized_text(_asset_value(asset, field))
        if re.search(r"(^|\W)esco($|\W)", value):
            return ReportType.ESCO
        if re.search(r"(^|\W)epc($|\W)", value):
            return ReportType.EPC
    return ReportType.EPC


def infer_self_use_kwh(*, production_kwh: Decimal, export_kwh: Decimal, raw_self_use: Any) -> Decimal:
    if raw_self_use is not None:
        return decimal_from_value(raw_self_use)
    return max(production_kwh - export_kwh, ZERO)


def calculate_customer_billing(
    energy: EnergyBreakdown,
    config: BillingConfig,
    *,
    months_count: int = 1,
) -> BillingResult:
    return calculate_billing(energy, config, months_count=months_count)


def calculate_billing(energy: EnergyBreakdown, config: BillingConfig, *, months_count: int = 1) -> BillingResult:
    months = max(int(months_count or 1), 1)
    billable_energy = (
        energy.production_kwh
        if config.billing_energy_base == BillingEnergyBase.TOTAL_PRODUCTION
        else energy.self_use_kwh
    )
    grid_import = max(energy.consumption_kwh - energy.self_use_kwh, ZERO)
    savings = energy.self_use_kwh * config.electricity_price_eur_kwh
    export_revenue = energy.export_kwh * config.export_price_eur_kwh
    gross_benefit = savings + export_revenue
    if config.report_type != ReportType.ESCO:
        solcor_payment = ZERO
    elif config.billing_mode == BillingMode.FIXED_MONTHLY_FEE:
        solcor_payment = config.fixed_monthly_fee_eur * Decimal(months)
    else:
        solcor_payment = billable_energy * config.solcor_price_per_kwh

    warnings: list[str] = []
    if config.report_type == ReportType.ESCO:
        if config.billing_mode == BillingMode.ENERGY and config.solcor_price_per_kwh == ZERO:
            warnings.append("missing_solcor_price")
        if config.billing_mode == BillingMode.FIXED_MONTHLY_FEE and config.fixed_monthly_fee_eur == ZERO:
            warnings.append("missing_fixed_monthly_fee")
    if config.electricity_price_eur_kwh == ZERO:
        warnings.append("missing_electricity_price")
    if energy.export_kwh > ZERO and config.export_price_eur_kwh == ZERO:
        warnings.append("missing_export_price")

    return BillingResult(
        production_kwh=energy.production_kwh,
        self_use_kwh=energy.self_use_kwh,
        export_kwh=energy.export_kwh,
        consumption_kwh=energy.consumption_kwh,
        billable_energy_kwh=billable_energy,
        grid_import_kwh=grid_import,
        exported_energy_kwh=energy.export_kwh,
        savings_eur=savings,
        export_revenue_eur=export_revenue,
        gross_benefit_eur=gross_benefit,
        solcor_payment_eur=solcor_payment,
        net_benefit_eur=gross_benefit - solcor_payment,
        autoconsumption_pct=(energy.self_use_kwh / energy.production_kwh * HUNDRED) if energy.production_kwh else ZERO,
        export_pct=(energy.export_kwh / energy.production_kwh * HUNDRED) if energy.production_kwh else ZERO,
        self_sufficiency_pct=(energy.self_use_kwh / energy.consumption_kwh * HUNDRED) if energy.consumption_kwh else ZERO,
        billing_mode=config.billing_mode,
        billing_energy_base=config.billing_energy_base,
        solcor_price_per_kwh=config.solcor_price_per_kwh,
        fixed_monthly_fee_eur=config.fixed_monthly_fee_eur,
        months_count=months,
        warnings=tuple(warnings),
    )

