from __future__ import annotations

from datetime import date
from decimal import Decimal

from monitoring_board.customer_reports import (
    REPORT_TYPES,
    build_customer_report_pdf,
    detect_report_type,
    prepare_customer_report,
)
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, ReportType


def sample_report(contract_type: str | None) -> dict:
    return {
        "asset": {
            "asset_id": 7,
            "project_name": "Instalacao Teste",
            "contract_type": contract_type,
            "asset_type": "",
            "coverage_type": "",
            "sell_to": "",
        },
        "month_start": date(2026, 5, 1),
        "month_end": date(2026, 5, 31),
        "month_label": "Maio 2026",
        "daily_rows": [
            {
                "date": date(2026, 5, 1),
                "production_kwh": 100,
                "self_use_kwh": 80,
                "export_kwh": 20,
                "consumption_kwh": 200,
            }
        ],
        "production_kwh": 100,
        "self_use_kwh": 80,
        "export_kwh": 20,
        "consumption_kwh": 200,
        "electricity_price": 0.20,
        "sell_price": 0.05,
        "data_source": "Teste",
    }


def test_detect_report_type_is_tolerant_and_defaults_to_epc() -> None:
    assert detect_report_type({"contract_type": "  Modelo ESCO  "}) == "esco"
    assert detect_report_type({"asset_type": "Contrato epc"}) == "epc"
    assert detect_report_type({"coverage_type": "Esco - operacao"}) == "esco"
    assert detect_report_type({"contract_type": "EPC", "project_name": "Antiga ESCO"}) == "epc"
    assert detect_report_type({"project_name": "Sem modelo"}) == "epc"


def test_prepare_esco_report_calculates_payment_and_net_benefit() -> None:
    report = prepare_customer_report(sample_report("ESCO"), solcor_price_per_kwh=0.09)

    assert report["report_type"] == "esco"
    assert report["savings_eur"] == 16
    assert report["export_revenue_eur"] == 1
    assert report["total_benefit_eur"] == 17
    assert report["billable_energy_kwh"] == 80
    assert report["solcor_payment_eur"] == 7.2
    assert report["net_benefit_eur"] == 9.8
    assert report["autoconsumption_pct"] == 80
    assert report["self_sufficiency_pct"] == 40
    assert any(label == "Pagamento à Solcor" for label, *_ in REPORT_TYPES["esco"]["summary"])


def test_prepare_epc_report_ignores_solcor_price_and_internal_rows() -> None:
    report = prepare_customer_report(sample_report("EPC"), solcor_price_per_kwh=0.09)

    assert report["report_type"] == "epc"
    assert report["solcor_payment_eur"] == 0
    assert report["net_benefit_eur"] == report["total_benefit_eur"]
    assert all("Solcor" not in label for label, *_ in REPORT_TYPES["epc"]["summary"])
    assert all("Líquido" not in label for label, *_ in REPORT_TYPES["epc"]["summary"])


def test_prepare_esco_report_can_charge_total_production() -> None:
    report = prepare_customer_report(
        sample_report("ESCO"),
        billing_config=BillingConfig(
            report_type=ReportType.ESCO,
            billing_energy_base=BillingEnergyBase.TOTAL_PRODUCTION,
            solcor_price_per_kwh=Decimal("0.09"),
            electricity_price_eur_kwh=Decimal("0.20"),
            export_price_eur_kwh=Decimal("0.05"),
        ),
    )

    assert report["billable_energy_kwh"] == 100
    assert report["solcor_payment_eur"] == 9


def test_prepare_report_infers_self_use_and_handles_missing_secondary_metrics() -> None:
    raw = sample_report(None)
    raw["self_use_kwh"] = None
    raw["export_kwh"] = 25
    raw["consumption_kwh"] = None

    report = prepare_customer_report(raw)

    assert report["report_type"] == "epc"
    assert report["self_use_kwh"] == 75
    assert report["self_sufficiency_pct"] == 0
    assert sum(value for _, value, _ in report["tariff_rows"]) == 0


def test_both_report_types_render_as_single_page_pdf() -> None:
    for contract_type in ("ESCO", "EPC"):
        report = prepare_customer_report(sample_report(contract_type), solcor_price_per_kwh=0.09)
        pdf = build_customer_report_pdf(report)

        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 4000
        assert pdf.count(b"/Type /Page") >= 1
