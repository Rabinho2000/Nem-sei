from __future__ import annotations

from io import BytesIO
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from monitoring_board.customer_reports import (
    REPORT_TYPES,
    build_customer_report_pdf,
    detect_report_type,
    draw_donut_charts,
    prepare_customer_report,
    client_outage_charge_basis_text,
    solcor_charge_basis_text,
    summary_rows,
)
from monitoring_board.reporting.client_outages import (
    ClientOutageAdjustment,
    apply_client_outage_adjustments,
)
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, ReportType
from monitoring_board.reporting.periods import ReportingPeriodError, build_period, period_from_form


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


def test_esco_reports_show_the_charge_rate_next_to_solcor_payment() -> None:
    report = prepare_customer_report(sample_report("ESCO"), solcor_price_per_kwh=0.09)

    assert solcor_charge_basis_text(report) == "Autoconsumo de 80 kWh x 0.09000 €/kWh"
    text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(build_customer_report_pdf(report))).pages
    )
    assert "Autoconsumo de 80 kWh x 0.09000 €/kWh" in text
    assert "Tarifa Solcor" in text
    assert "Benefício Total" not in text


def test_prepare_epc_report_ignores_solcor_price_and_internal_rows() -> None:
    report = prepare_customer_report(sample_report("EPC"), solcor_price_per_kwh=0.09)

    assert report["report_type"] == "epc"
    assert report["solcor_payment_eur"] == 0
    assert report["net_benefit_eur"] == report["total_benefit_eur"]
    assert all("Solcor" not in label for label, *_ in REPORT_TYPES["epc"]["summary"])
    assert all("Líquido" not in label for label, *_ in REPORT_TYPES["epc"]["summary"])


def test_report_hides_export_revenue_when_the_client_does_not_sell_to_grid() -> None:
    report = prepare_customer_report(
        sample_report("ESCO"),
        billing_config=BillingConfig(
            report_type=ReportType.ESCO,
            solcor_price_per_kwh=Decimal("0.09"),
            electricity_price_eur_kwh=Decimal("0.20"),
            export_price_eur_kwh=Decimal("0.05"),
            export_revenue_enabled=False,
        ),
    )

    assert report["export_kwh"] == 20
    assert report["export_revenue_eur"] == 0
    assert all(key != "export_revenue_eur" for _, key, _ in summary_rows(report))
    text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(build_customer_report_pdf(report))).pages
    )
    assert "Receita da venda de excedente" not in text


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


def test_esco_summary_separates_client_outage_adjustment() -> None:
    billing = BillingConfig(
        report_type=ReportType.ESCO,
        solcor_price_per_kwh=Decimal("0.09"),
        electricity_price_eur_kwh=Decimal("0.20"),
        export_price_eur_kwh=Decimal("0.05"),
    )
    report = apply_client_outage_adjustments(
        prepare_customer_report(sample_report("ESCO"), billing_config=billing),
        adjustments=(ClientOutageAdjustment(date(2026, 5, 2), Decimal("10")),),
        billing_config=billing,
    )

    rows = summary_rows(report)

    assert ("Pagamento à Solcor — produção medida", "measured_solcor_payment_eur", "eur") in rows
    assert ("Ajuste por indisponibilidade imputável ao cliente", "client_outage_charge_eur", "eur") in rows
    assert ("Pagamento total à Solcor", "solcor_payment_eur", "eur") in rows
    assert client_outage_charge_basis_text(report) == "10 kWh x 0.09000 €/kWh"


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


def test_prepare_report_uses_hourly_tariff_breakdown_for_the_visual_chart() -> None:
    raw = sample_report("ESCO")
    raw["tariff_period_breakdown"] = [
        {"period_name": "ponta", "energy_kwh": 12},
        {"period_name": "cheia", "energy_kwh": 34},
        {"period_name": "vazio", "energy_kwh": 56},
        {"period_name": "super_vazio", "energy_kwh": 7},
    ]

    report = prepare_customer_report(raw)

    assert [(label, value) for label, value, _ in report["tariff_rows"]] == [
        ("Cheia", 34.0),
        ("Ponta", 12.0),
        ("Vazio", 56.0),
        ("Super vazio", 7.0),
    ]


def test_tariff_donut_does_not_render_overlapping_technical_rows() -> None:
    raw = sample_report("ESCO")
    raw["tariff_period_breakdown"] = [
        {"period_name": "ponta", "energy_kwh": 12, "price_eur_kwh": 0.3, "value_eur": 3.6},
        {"period_name": "cheia", "energy_kwh": 34, "price_eur_kwh": 0.2, "value_eur": 6.8},
        {"period_name": "vazio", "energy_kwh": 56, "price_eur_kwh": 0.1, "value_eur": 5.6},
    ]
    report = prepare_customer_report(raw)
    pdf = build_customer_report_pdf(report)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "Autoconsumo por Período Tarifário" in text
    assert "simple:" not in text


def test_tariff_donut_uses_arc_extents_and_hides_sigenergy_availability_footer_note() -> None:
    class RecordingCanvas:
        def __init__(self) -> None:
            self.wedges = []

        def setFillColor(self, *args, **kwargs): pass
        def setStrokeColor(self, *args, **kwargs): pass
        def setLineWidth(self, *args, **kwargs): pass
        def roundRect(self, *args, **kwargs): pass
        def circle(self, *args, **kwargs): pass
        def setFont(self, *args, **kwargs): pass
        def drawCentredString(self, *args, **kwargs): pass
        def drawString(self, *args, **kwargs): pass
        def wedge(self, *args, **kwargs): self.wedges.append(args[-2:])

    raw = sample_report("ESCO")
    raw["tariff_period_breakdown"] = [
        {"period_name": "cheia", "energy_kwh": 50},
        {"period_name": "ponta", "energy_kwh": 20},
        {"period_name": "vazio", "energy_kwh": 10},
    ]
    report = prepare_customer_report(raw)
    canvas = RecordingCanvas()
    draw_donut_charts(canvas, report, 20, 26, 802, 112)

    # ReportLab wedges receive (start_angle, angular_extent), not end_angle.
    assert canvas.wedges[0] == pytest.approx((90, 360))
    assert canvas.wedges[1] == pytest.approx((90, 288))
    assert canvas.wedges[2] == pytest.approx((90, 225))
    assert canvas.wedges[3] == pytest.approx((315, 90))
    assert canvas.wedges[4] == pytest.approx((45, 45))

    report["availability_error"] = "availability_not_supported_by_provider"
    report["report_notes"] = ["Disponibilidade nao suportada para a fonte de energia selecionada."]
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(build_customer_report_pdf(report))).pages)
    assert "Disponibilidade nao suportada" not in text


def test_both_report_types_render_as_single_page_pdf() -> None:
    for contract_type in ("ESCO", "EPC"):
        report = prepare_customer_report(sample_report(contract_type), solcor_price_per_kwh=0.09)
        pdf = build_customer_report_pdf(report)

        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 4000
        assert pdf.count(b"/Type /Page") >= 1


def test_availability_kpi_is_optional_in_customer_pdf() -> None:
    hidden = build_customer_report_pdf(prepare_customer_report(sample_report("EPC")))
    hidden_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(hidden)).pages)

    raw = sample_report("EPC")
    report = prepare_customer_report(raw)
    report["include_availability_kpi"] = True
    report["availability_pct"] = 98.5
    visible = build_customer_report_pdf(report)
    visible_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(visible)).pages)

    assert "Disponibilidade (%)" not in hidden_text
    assert "Disponibilidade (%)" in visible_text
    assert "98%" in visible_text


def test_reporting_periods_cover_month_quarters_semesters_and_years() -> None:
    assert build_period("monthly", report_month="2026-02").end == date(2026, 2, 28)
    assert build_period("monthly", report_month="2024-02").end == date(2024, 2, 29)

    quarters = [
        build_period("quarterly", year="2026", quarter=str(quarter))
        for quarter in range(1, 5)
    ]
    assert [(period.start.month, period.end.month, period.month_count) for period in quarters] == [
        (1, 3, 3),
        (4, 6, 3),
        (7, 9, 3),
        (10, 12, 3),
    ]
    assert quarters[0].label == "T1 2026"
    assert quarters[3].included_months == (date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1))

    first_semester = build_period("semiannual", year=2026, semester=1)
    second_semester = build_period("semiannual", year=2026, semester=2)
    annual = build_period("annual", year=2026)
    assert first_semester.included_months[0] == date(2026, 1, 1)
    assert first_semester.included_months[-1] == date(2026, 6, 1)
    assert second_semester.included_months[0] == date(2026, 7, 1)
    assert second_semester.month_count == 6
    assert annual.start == date(2026, 1, 1)
    assert annual.end == date(2026, 12, 31)
    assert annual.month_count == 12


def test_reporting_period_form_rejects_invalid_inputs() -> None:
    invalid_inputs = [
        {"period_type": "bad"},
        {"period_type": "monthly", "report_month": "2026-13"},
        {"period_type": "quarterly", "report_year": "2026", "report_quarter": "5"},
        {"period_type": "semiannual", "report_year": "2026", "report_semester": "3"},
        {"period_type": "annual", "report_year": "abcd"},
    ]

    for values in invalid_inputs:
        try:
            period_from_form(values)
        except ReportingPeriodError:
            continue
        raise AssertionError(f"Invalid values accepted: {values}")


def test_fixed_monthly_fee_billing_uses_period_month_count() -> None:
    raw = sample_report("ESCO")
    raw.update(
        months_count=6,
        billing_mode=BillingMode.FIXED_MONTHLY_FEE.value,
        fixed_monthly_fee_eur=125,
    )

    report = prepare_customer_report(raw, solcor_price_per_kwh=0.09)

    assert report["solcor_payment_eur"] == 750
    assert report["savings_eur"] == 16


@pytest.mark.parametrize(("months_count", "expected_payment"), [(1, 125), (3, 375), (6, 750), (12, 1500)])
def test_fixed_monthly_fee_billing_scales_by_month_count_without_multiplying_energy(months_count: int, expected_payment: int) -> None:
    raw = sample_report("ESCO")
    raw.update(
        months_count=months_count,
        billing_mode=BillingMode.FIXED_MONTHLY_FEE.value,
        fixed_monthly_fee_eur=125,
    )

    report = prepare_customer_report(raw, solcor_price_per_kwh=99)

    assert report["solcor_payment_eur"] == expected_payment
    assert report["savings_eur"] == 16
    assert report["export_revenue_eur"] == 1


@pytest.mark.parametrize(
    ("period_type", "period_label", "months_count"),
    [("quarterly", "T1 2026", 3), ("semiannual", "S1 2026", 6), ("annual", "2026", 12)],
)
def test_multi_month_pdf_uses_monthly_series(period_type: str, period_label: str, months_count: int) -> None:
    raw = sample_report("EPC")
    raw.update(
        period_type=period_type,
        period_start=date(2026, 1, 1),
        period_end=date(2026, months_count, 31 if months_count == 12 else 30),
        period_label=period_label,
        months_count=months_count,
        chart_granularity="monthly",
        monthly_rows=[
            {"date": date(2026, 1, 1), "label": "01/26", "production_kwh": 100, "self_use_kwh": 80, "export_kwh": 20, "consumption_kwh": 200},
            {"date": date(2026, 2, 1), "label": "02/26", "production_kwh": 90, "self_use_kwh": 70, "export_kwh": 20, "consumption_kwh": 190},
            {"date": date(2026, 3, 1), "label": "03/26", "production_kwh": 110, "self_use_kwh": 85, "export_kwh": 25, "consumption_kwh": 210},
        ],
    )
    report = prepare_customer_report(raw)
    pdf = build_customer_report_pdf(report)

    assert report["period_label"] == period_label
    assert report["chart_granularity"] == "monthly"
    assert pdf.startswith(b"%PDF-")
