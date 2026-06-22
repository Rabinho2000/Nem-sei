from __future__ import annotations

from decimal import Decimal

import pytest

from monitoring_board.reporting.billing import calculate_billing
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, EnergyBreakdown, ReportType


def energy(
    *,
    production: str = "100",
    self_use: str = "80",
    export: str = "20",
    consumption: str = "200",
) -> EnergyBreakdown:
    return EnergyBreakdown(
        production_kwh=Decimal(production),
        self_use_kwh=Decimal(self_use),
        export_kwh=Decimal(export),
        consumption_kwh=Decimal(consumption),
    )


def config(
    *,
    report_type: ReportType = ReportType.ESCO,
    billing_mode: BillingMode = BillingMode.ENERGY,
    billing_energy_base: BillingEnergyBase = BillingEnergyBase.SELF_CONSUMPTION,
    solcor_price: str = "0.09",
    fixed_fee: str = "50",
    electricity_price: str = "0.20",
    export_price: str = "0.05",
) -> BillingConfig:
    return BillingConfig(
        report_type=report_type,
        billing_mode=billing_mode,
        billing_energy_base=billing_energy_base,
        solcor_price_per_kwh=Decimal(solcor_price),
        fixed_monthly_fee_eur=Decimal(fixed_fee),
        electricity_price_eur_kwh=Decimal(electricity_price),
        export_price_eur_kwh=Decimal(export_price),
    )


def test_esco_charged_on_self_consumption_by_default() -> None:
    result = calculate_billing(energy(), config())

    assert result.billable_energy_kwh == Decimal("80")
    assert result.solcor_payment_eur == Decimal("7.20")


def test_esco_can_charge_total_production() -> None:
    result = calculate_billing(
        energy(),
        config(billing_energy_base=BillingEnergyBase.TOTAL_PRODUCTION),
    )

    assert result.billable_energy_kwh == Decimal("100")
    assert result.solcor_payment_eur == Decimal("9.00")


def test_esco_fixed_monthly_fee_uses_month_count() -> None:
    result = calculate_billing(
        energy(),
        config(billing_mode=BillingMode.FIXED_MONTHLY_FEE, fixed_fee="123.45"),
        months_count=3,
    )

    assert result.solcor_payment_eur == Decimal("370.35")
    assert result.months_count == 3


def test_epc_solcor_payment_is_zero() -> None:
    result = calculate_billing(energy(), config(report_type=ReportType.EPC))

    assert result.solcor_payment_eur == Decimal("0")
    assert result.net_benefit_eur == result.gross_benefit_eur


def test_grid_import_is_consumption_minus_self_consumption() -> None:
    result = calculate_billing(energy(consumption="200", self_use="80"), config())

    assert result.grid_import_kwh == Decimal("120")


def test_grid_import_is_never_negative() -> None:
    result = calculate_billing(energy(consumption="50", self_use="80"), config())

    assert result.grid_import_kwh == Decimal("0")


def test_export_revenue_belongs_to_customer_and_affects_gross_benefit() -> None:
    result = calculate_billing(energy(export="20"), config())

    assert result.exported_energy_kwh == Decimal("20")
    assert result.export_revenue_eur == Decimal("1.00")
    assert result.gross_benefit_eur == Decimal("17.00")


def test_net_benefit_subtracts_solcor_payment() -> None:
    result = calculate_billing(energy(), config())

    assert result.net_benefit_eur == Decimal("9.80")


def test_money_values_remain_decimal() -> None:
    result = calculate_billing(energy(), config())

    assert isinstance(result.solcor_payment_eur, Decimal)
    assert isinstance(result.net_benefit_eur, Decimal)


def test_zero_prices_emit_warnings_and_calculate_zero_values() -> None:
    result = calculate_billing(
        energy(),
        config(solcor_price="0", electricity_price="0", export_price="0"),
    )

    assert result.savings_eur == Decimal("0")
    assert result.export_revenue_eur == Decimal("0")
    assert result.solcor_payment_eur == Decimal("0")
    assert set(result.warnings) == {"missing_solcor_price", "missing_electricity_price", "missing_export_price"}


@pytest.mark.parametrize("months_count", [1, 3, 6, 12])
def test_fixed_fee_supports_future_period_month_counts(months_count: int) -> None:
    result = calculate_billing(
        energy(),
        config(billing_mode=BillingMode.FIXED_MONTHLY_FEE, fixed_fee="10"),
        months_count=months_count,
    )

    assert result.solcor_payment_eur == Decimal(10 * months_count)
