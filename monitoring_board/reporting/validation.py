from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, ReportType


class BillingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReportAssetSelection:
    asset_id: int
    report_type: ReportType


def parse_nonnegative_decimal_field(data: dict[str, Any], field_name: str) -> Decimal:
    raw_value = data.get(field_name)
    if raw_value is None or str(raw_value).strip() == "":
        return Decimal("0")
    normalized = str(raw_value).strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise BillingValidationError(f"Valor invalido em {field_name}.") from exc
    if not value.is_finite():
        raise BillingValidationError(f"Valor invalido em {field_name}.")
    if value < 0:
        raise BillingValidationError(f"Valor negativo em {field_name}.")
    return value


def parse_billing_mode(value: Any) -> BillingMode:
    try:
        return BillingMode(str(value or BillingMode.ENERGY.value).strip())
    except ValueError as exc:
        raise BillingValidationError("Modelo de cobranca invalido.") from exc


def parse_billing_energy_base(value: Any) -> BillingEnergyBase:
    try:
        return BillingEnergyBase(str(value or BillingEnergyBase.SELF_CONSUMPTION.value).strip())
    except ValueError as exc:
        raise BillingValidationError("Base de cobranca invalida.") from exc


def parse_billing_values_source(value: Any) -> str:
    source = str(value or "saved").strip()
    if source not in {"saved", "manual"}:
        raise BillingValidationError("Fonte dos valores invalida.")
    return source


def parse_billing_config_form(data: dict[str, Any], report_type: ReportType) -> BillingConfig:
    billing_mode = parse_billing_mode(data.get("billing_mode"))
    billing_energy_base = parse_billing_energy_base(data.get("billing_energy_base"))
    return BillingConfig(
        report_type=report_type,
        billing_mode=billing_mode,
        billing_energy_base=billing_energy_base,
        solcor_price_per_kwh=parse_nonnegative_decimal_field(data, "solcor_price_per_kwh"),
        fixed_monthly_fee_eur=parse_nonnegative_decimal_field(data, "fixed_monthly_fee_eur"),
        electricity_price_eur_kwh=parse_nonnegative_decimal_field(data, "electricity_price"),
        export_price_eur_kwh=parse_nonnegative_decimal_field(data, "sell_price"),
    )


def validate_report_asset_selection(report_assets: list[dict[str, Any]], raw_asset_id: str) -> ReportAssetSelection:
    if not raw_asset_id or not raw_asset_id.strip().isdigit():
        raise BillingValidationError("Escolhe uma instalacao FusionSolar para gerar o relatorio.")
    asset_id = int(raw_asset_id.strip())
    for asset in report_assets:
        if int(asset["asset_id"]) != asset_id:
            continue
        try:
            report_type = ReportType(str(asset["report_type"]))
        except ValueError as exc:
            raise BillingValidationError("Modelo ESCO/EPC invalido para a instalacao.") from exc
        return ReportAssetSelection(asset_id=asset_id, report_type=report_type)
    raise BillingValidationError("Instalacao inexistente ou sem integracao valida para relatorios.")
