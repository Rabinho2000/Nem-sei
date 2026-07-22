from __future__ import annotations

import pytest

from monitoring_board.reporting.financial_quality import (
    PRODUCTION_DERIVED_FIELDS,
    PRODUCTION_FINANCIALS_NOT_FINAL_WARNING,
    apply_production_financial_gate,
    financial_quality_warnings,
)


@pytest.mark.parametrize("status", ["partial", "missing", "conflict", "in_progress"])
def test_non_final_production_clears_every_derived_financial_field(status: str) -> None:
    values = {field: 12.5 for field in PRODUCTION_DERIVED_FIELDS}
    values.update(
        production_quality_status=status,
        consumption_kwh=200,
        expected_production_kwh=300,
        availability_pct=99,
        raw_daily_production_kwh=10,
    )

    assert apply_production_financial_gate(values) is False
    assert all(values[field] is None for field in PRODUCTION_DERIVED_FIELDS)
    assert values["consumption_kwh"] == 200
    assert values["expected_production_kwh"] == 300
    assert values["availability_pct"] == 99
    assert values["raw_daily_production_kwh"] == 10
    assert PRODUCTION_FINANCIALS_NOT_FINAL_WARNING in financial_quality_warnings(status)


def test_complete_production_preserves_financial_values_with_partial_daily_coverage() -> None:
    values = {
        "production_quality_status": "complete",
        "daily_coverage": "partial",
        "estimated_value_eur": 25,
        "self_use_kwh": 100,
        "net_benefit_eur": 20,
    }

    assert apply_production_financial_gate(values) is True
    assert values["estimated_value_eur"] == 25
    assert values["self_use_kwh"] == 100
    assert values["net_benefit_eur"] == 20
    assert PRODUCTION_FINANCIALS_NOT_FINAL_WARNING not in financial_quality_warnings("complete")
