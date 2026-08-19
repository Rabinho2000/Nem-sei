from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from nemsei.reporting.rules.billing import decimal_from_value, decimal_to_float
from nemsei.reporting.rules.types import BillingConfig, BillingMode, ReportType


class ClientOutageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ClientOutageAdjustment:
    outage_date: date
    estimated_kwh: Decimal
    reason: str = ""


def parse_client_outage_adjustments(form: Any, *, start: date, end: date, billing_config: BillingConfig) -> tuple[ClientOutageAdjustment, ...]:
    raw_dates = list(form.getlist("client_outage_date"))
    raw_kwh = list(form.getlist("client_outage_kwh"))
    raw_reasons = list(form.getlist("client_outage_reason"))
    if not any(str(value).strip() for value in [*raw_dates, *raw_kwh, *raw_reasons]):
        return ()
    if billing_config.billing_mode != BillingMode.ENERGY:
        raise ClientOutageValidationError("O ajuste por indisponibilidade só está disponível para cobrança por energia.")
    if billing_config.report_type != ReportType.ESCO:
        raise ClientOutageValidationError("O ajuste por indisponibilidade só está disponível para relatórios ESCO.")
    if not (len(raw_dates) == len(raw_kwh) == len(raw_reasons)):
        raise ClientOutageValidationError("Linhas de indisponibilidade inválidas.")
    adjustments: list[ClientOutageAdjustment] = []
    seen: set[date] = set()
    for raw_date, raw_energy, raw_reason in zip(raw_dates, raw_kwh, raw_reasons):
        if not any(str(value).strip() for value in (raw_date, raw_energy, raw_reason)):
            continue
        try:
            outage_date = date.fromisoformat(str(raw_date).strip())
        except ValueError as exc:
            raise ClientOutageValidationError("Indica uma data válida para a indisponibilidade.") from exc
        if outage_date < start or outage_date > end:
            raise ClientOutageValidationError("A data de indisponibilidade tem de pertencer ao período do relatório.")
        if outage_date in seen:
            raise ClientOutageValidationError("Não podes repetir o mesmo dia de indisponibilidade.")
        estimated_kwh = decimal_from_value(raw_energy)
        if str(raw_energy).strip() and estimated_kwh == 0 and str(raw_energy).strip().replace(",", ".") not in {"0", "0.0", "0.00"}:
            raise ClientOutageValidationError("Indica uma estimativa de produção válida.")
        seen.add(outage_date)
        adjustments.append(ClientOutageAdjustment(outage_date, estimated_kwh, str(raw_reason or "").strip()[:240]))
    return tuple(sorted(adjustments, key=lambda item: item.outage_date))


def apply_client_outage_adjustments(report: dict[str, Any], *, adjustments: tuple[ClientOutageAdjustment, ...], billing_config: BillingConfig) -> dict[str, Any]:
    prepared = dict(report)
    total_kwh = sum((item.estimated_kwh for item in adjustments), Decimal("0"))
    charge = total_kwh * billing_config.solcor_price_per_kwh
    measured_payment = decimal_from_value(prepared.get("solcor_payment_eur"))
    prepared["client_outage_adjustments"] = [
        {"date": item.outage_date.isoformat(), "estimated_kwh": decimal_to_float(item.estimated_kwh), "reason": item.reason}
        for item in adjustments
    ]
    prepared["client_outage_billable_kwh"] = decimal_to_float(total_kwh)
    prepared["client_outage_charge_eur"] = decimal_to_float(charge)
    prepared["measured_solcor_payment_eur"] = decimal_to_float(measured_payment)
    prepared["solcor_payment_eur"] = decimal_to_float(measured_payment + charge)
    prepared["net_benefit_eur"] = decimal_to_float(decimal_from_value(prepared.get("net_benefit_eur")) - charge)
    return prepared
