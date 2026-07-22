from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Iterable


PRODUCTION_FINANCIALS_NOT_FINAL_WARNING = "production_financials_not_final"

# Values in this set are produced from actual production, self-use, export, or
# their tariff/billing valuation. They must never survive a non-final
# production status as publishable report values.
PRODUCTION_DERIVED_FIELDS = frozenset(
    {
        "production_ponta_kwh",
        "production_cheia_kwh",
        "production_vazio_kwh",
        "production_super_vazio_kwh",
        "self_use_kwh",
        "self_use_ponta_kwh",
        "self_use_cheia_kwh",
        "self_use_vazio_kwh",
        "self_use_super_vazio_kwh",
        "self_use_simple_kwh",
        "self_use_value_ponta_eur",
        "self_use_value_cheia_eur",
        "self_use_value_vazio_eur",
        "self_use_value_super_vazio_eur",
        "self_use_value_simple_eur",
        "export_kwh",
        "grid_import_kwh",
        "self_consumption_rate_pct",
        "self_sufficiency_rate_pct",
        "estimated_value_eur",
        "savings_eur",
        "tariff_value_eur",
        "export_revenue_eur",
        "esco_payment_eur",
        "solcor_payment_eur",
        "billable_energy_kwh",
        "exported_energy_kwh",
        "total_benefit_eur",
        "gross_benefit_eur",
        "net_benefit_eur",
        "autoconsumption_pct",
        "self_sufficiency_pct",
        "export_pct",
    }
)


def production_financials_are_final(production_quality_status: str | None) -> bool:
    return production_quality_status == "complete"


def apply_production_financial_gate(
    values: MutableMapping[str, Any],
    *,
    production_quality_status: str | None = None,
) -> bool:
    """Clear production-derived values unless production is final.

    The mapping is changed in place so the same gate can be applied to legacy
    dictionaries, configurable report rows, and summary dictionaries.
    Diagnostic and production-quality fields are intentionally untouched.
    """

    status = production_quality_status or str(values.get("production_quality_status") or "")
    is_final = production_financials_are_final(status)
    if not is_final:
        for field in PRODUCTION_DERIVED_FIELDS:
            if field in values:
                values[field] = None
    return is_final


def financial_quality_warnings(
    production_quality_status: str | None,
    warnings: Iterable[str] = (),
) -> tuple[str, ...]:
    result = set(warnings)
    if not production_financials_are_final(production_quality_status):
        result.add(PRODUCTION_FINANCIALS_NOT_FINAL_WARNING)
    return tuple(sorted(result))
