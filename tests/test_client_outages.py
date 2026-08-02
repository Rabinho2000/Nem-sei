from datetime import date
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict

from monitoring_board.reporting.client_outages import ClientOutageValidationError, apply_client_outage_adjustments, parse_client_outage_adjustments
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, ReportType


def billing_config(*, mode=BillingMode.ENERGY):
    return BillingConfig(report_type=ReportType.ESCO, billing_mode=mode, billing_energy_base=BillingEnergyBase.SELF_CONSUMPTION, solcor_price_per_kwh=Decimal("0.10"))


def test_client_outage_adjustment_only_changes_payment() -> None:
    adjustments = parse_client_outage_adjustments(MultiDict([("client_outage_date", "2026-07-10"), ("client_outage_kwh", "123.5"), ("client_outage_reason", "Desligada pelo cliente")]), start=date(2026, 7, 1), end=date(2026, 7, 31), billing_config=billing_config())
    report = apply_client_outage_adjustments({"production_kwh": 500, "solcor_payment_eur": 20, "net_benefit_eur": 80}, adjustments=adjustments, billing_config=billing_config())
    assert report["production_kwh"] == 500
    assert report["client_outage_charge_eur"] == 12.35
    assert report["solcor_payment_eur"] == 32.35
    assert report["net_benefit_eur"] == 67.65


def test_client_outage_rejects_duplicate_or_fixed_fee() -> None:
    form = MultiDict([("client_outage_date", "2026-07-10"), ("client_outage_kwh", "10"), ("client_outage_reason", "x"), ("client_outage_date", "2026-07-10"), ("client_outage_kwh", "10"), ("client_outage_reason", "x")])
    with pytest.raises(ClientOutageValidationError, match="repetir"):
        parse_client_outage_adjustments(form, start=date(2026, 7, 1), end=date(2026, 7, 31), billing_config=billing_config())
    with pytest.raises(ClientOutageValidationError, match="cobrança por energia"):
        parse_client_outage_adjustments(MultiDict([("client_outage_date", "2026-07-10"), ("client_outage_kwh", "10"), ("client_outage_reason", "x")]), start=date(2026, 7, 1), end=date(2026, 7, 31), billing_config=billing_config(mode=BillingMode.FIXED_MONTHLY_FEE))
