from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from monitoring_board.reporting.models import BillingConfig, BillingResult, EnergyBreakdown, ReportType


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


def calculate_customer_billing(energy: EnergyBreakdown, config: BillingConfig) -> BillingResult:
    savings = energy.self_use_kwh * config.electricity_price_eur_kwh
    export_revenue = energy.export_kwh * config.export_price_eur_kwh
    total_benefit = savings + export_revenue
    solcor_payment = energy.production_kwh * config.solcor_price_per_kwh if config.report_type == ReportType.ESCO else ZERO
    return BillingResult(
        savings_eur=savings,
        export_revenue_eur=export_revenue,
        total_benefit_eur=total_benefit,
        solcor_payment_eur=solcor_payment,
        net_benefit_eur=total_benefit - solcor_payment,
        autoconsumption_pct=(energy.self_use_kwh / energy.production_kwh * HUNDRED) if energy.production_kwh else ZERO,
        export_pct=(energy.export_kwh / energy.production_kwh * HUNDRED) if energy.production_kwh else ZERO,
        self_sufficiency_pct=(energy.self_use_kwh / energy.consumption_kwh * HUNDRED) if energy.consumption_kwh else ZERO,
    )

