"""The three values an ESCO contract is made of, and what each one buys.

The columns were already right and are not renamed here. What these pin is the
mapping between what the business says and what `calculate_billing` does, so
that a later change to either one has to answer for the other:

    taxa de venda       -> solcor_price_per_kwh      -> billable  x taxa
    taxa de poupança    -> default_electricity_price -> self-use  x taxa
    venda de excedente  -> default_export_price      -> export    x taxa

The numbers below are acceptance examples chosen to be checkable by hand. They
are examples only; nothing in the product hard-codes them.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from nemsei.reporting.commercial import billing_config_from, set_billing_config
from nemsei.reporting.rules.billing import calculate_billing
from nemsei.reporting.rules.types import (
    BillingConfig,
    BillingEnergyBase,
    BillingMode,
    EnergyBreakdown,
    ReportType,
)
from nemsei.web.commercial_routes import ESCO_RATE_LABELS, collect_rates


def esco(
    *,
    taxa_de_venda="0.10",
    taxa_de_poupanca="0.06",
    venda_de_excedente="0.045",
    export_revenue_enabled=True,
) -> BillingConfig:
    return BillingConfig(
        report_type=ReportType.ESCO,
        billing_mode=BillingMode.ENERGY,
        billing_energy_base=BillingEnergyBase.SELF_CONSUMPTION,
        solcor_price_per_kwh=Decimal(taxa_de_venda),
        fixed_monthly_fee_eur=Decimal("0"),
        electricity_price_eur_kwh=Decimal(taxa_de_poupanca),
        export_price_eur_kwh=Decimal(venda_de_excedente),
        export_revenue_enabled=export_revenue_enabled,
    )


def energy(*, production="25000", self_use="20000", export="5000", consumption="30000") -> EnergyBreakdown:
    return EnergyBreakdown(
        production_kwh=Decimal(production),
        self_use_kwh=Decimal(self_use),
        export_kwh=Decimal(export),
        consumption_kwh=Decimal(consumption),
    )


# --- the three formulas -----------------------------------------------------


def test_a_poupanca_is_self_consumption_times_the_taxa_de_poupanca() -> None:
    result = calculate_billing(energy(self_use="20000"), esco(taxa_de_poupanca="0.06"))
    assert result.savings_eur == Decimal("1200.00")


def test_b_receita_de_excedente_is_export_times_venda_de_excedente() -> None:
    result = calculate_billing(energy(export="5000"), esco(venda_de_excedente="0.045"))
    assert result.export_revenue_eur == Decimal("225.000")


def test_c_faturacao_esco_is_billable_energy_times_the_taxa_de_venda() -> None:
    result = calculate_billing(energy(self_use="20000"), esco(taxa_de_venda="0.10"))
    assert result.billable_energy_kwh == Decimal("20000")
    assert result.solcor_payment_eur == Decimal("2000.00")


def test_the_three_together_net_out_the_way_the_contract_reads() -> None:
    result = calculate_billing(energy(), esco())
    assert result.gross_benefit_eur == Decimal("1425.000")
    assert result.net_benefit_eur == Decimal("-575.000")


# --- what the simple flow does and does not require -------------------------


def test_the_simple_flow_needs_no_avenca() -> None:
    """A fixed fee stays supported and stays out of the normal ESCO path."""
    result = calculate_billing(energy(), esco())
    assert result.fixed_monthly_fee_eur == Decimal("0")
    assert result.solcor_payment_eur == Decimal("2000.00")
    assert "missing_fixed_monthly_fee" not in result.warnings


def test_the_simple_flow_needs_no_tariff_period_split() -> None:
    """Cheias/Ponta/Vazio never enter the calculation; only the three rates do."""
    result = calculate_billing(energy(), esco())
    assert result.warnings == ()


def test_total_production_billing_survives_as_the_advanced_option_it_was() -> None:
    config = BillingConfig(
        report_type=ReportType.ESCO,
        billing_mode=BillingMode.ENERGY,
        billing_energy_base=BillingEnergyBase.TOTAL_PRODUCTION,
        solcor_price_per_kwh=Decimal("0.10"),
        fixed_monthly_fee_eur=Decimal("0"),
        electricity_price_eur_kwh=Decimal("0.06"),
        export_price_eur_kwh=Decimal("0.045"),
        export_revenue_enabled=True,
    )
    result = calculate_billing(energy(production="25000"), config)
    assert result.billable_energy_kwh == Decimal("25000")
    assert result.solcor_payment_eur == Decimal("2500.00")


def test_an_epc_is_never_charged_a_solcor_payment() -> None:
    config = BillingConfig(
        report_type=ReportType.EPC,
        billing_mode=BillingMode.ENERGY,
        billing_energy_base=BillingEnergyBase.SELF_CONSUMPTION,
        solcor_price_per_kwh=Decimal("0.10"),
        fixed_monthly_fee_eur=Decimal("0"),
        electricity_price_eur_kwh=Decimal("0.06"),
        export_price_eur_kwh=Decimal("0.045"),
        export_revenue_enabled=True,
    )
    assert calculate_billing(energy(), config).solcor_payment_eur == Decimal("0")


# --- the persisted row means the same three things --------------------------


def test_the_persisted_columns_carry_the_three_rates_unchanged(settings, monkeypatch) -> None:
    """No column was added to rename one, so this proves the mapping holds."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    from nemsei.assets.service import create_asset
    from nemsei.db.session import build_session_factory

    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    factory = build_session_factory(create_engine(settings.database_url))

    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central ESCO")
        stored = set_billing_config(
            session,
            asset_id=asset.id,
            report_type="esco",
            valid_from=date(2026, 1, 1),
            created_by="operator",
            solcor_price_per_kwh=Decimal("0.10"),
            default_electricity_price=Decimal("0.06"),
            default_export_price=Decimal("0.045"),
        )
        config = billing_config_from(stored)

    result = calculate_billing(energy(), config)
    assert result.solcor_payment_eur == Decimal("2000.00")
    assert result.savings_eur == Decimal("1200.00")
    assert result.export_revenue_eur == Decimal("225.000")


# --- the form asks for what the arrangement uses, and only that --------------


def test_the_operator_never_has_to_type_a_column_name() -> None:
    assert set(ESCO_RATE_LABELS.values()) >= {"Taxa de venda", "Taxa de poupança", "Venda de excedente"}


def test_an_esco_on_energy_must_state_all_three_rates() -> None:
    complete = {
        "solcor_price_per_kwh": "0.10",
        "default_electricity_price": "0.06",
        "default_export_price": "0.045",
    }
    rates = collect_rates(complete, report_type="esco", billing_mode="energy", export_revenue_enabled=True)
    assert rates["solcor_price_per_kwh"] == Decimal("0.10")

    for omitted in complete:
        partial = {key: value for key, value in complete.items() if key != omitted}
        try:
            collect_rates(partial, report_type="esco", billing_mode="energy", export_revenue_enabled=True)
        except ValueError as error:
            assert ESCO_RATE_LABELS[omitted] in str(error)
        else:  # pragma: no cover - the assertion below reports which one slipped
            raise AssertionError(f"{omitted} was accepted as blank and would have been stored as 0")


def test_an_esco_that_does_not_sell_its_excedente_is_not_asked_for_that_rate() -> None:
    rates = collect_rates(
        {"solcor_price_per_kwh": "0.10", "default_electricity_price": "0.06"},
        report_type="esco",
        billing_mode="energy",
        export_revenue_enabled=False,
    )
    assert rates["default_export_price"] == Decimal("0")


def test_a_rate_of_zero_is_refused_as_firmly_as_a_blank_one() -> None:
    try:
        collect_rates(
            {"solcor_price_per_kwh": "0.10", "default_electricity_price": "0", "default_export_price": "0.045"},
            report_type="esco",
            billing_mode="energy",
            export_revenue_enabled=True,
        )
    except ValueError as error:
        assert "Taxa de poupança" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a zero taxa de poupança would report a saving of 0,00 EUR as if measured")
