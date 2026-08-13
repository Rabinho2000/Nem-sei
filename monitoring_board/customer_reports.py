from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from monitoring_board.reporting.billing import (
    calculate_customer_billing,
    decimal_from_value,
    decimal_to_float,
    detect_report_type_value,
    infer_self_use_kwh,
)
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, EnergyBreakdown, ReportType


LOGGER = logging.getLogger(__name__)

NAVY = colors.HexColor("#0B2D52")
GREEN = colors.HexColor("#4BA52E")
LIGHT_GREEN = colors.HexColor("#EAF4E5")
ORANGE = colors.HexColor("#F5A623")
LIGHT_ORANGE = colors.HexColor("#FFF1DA")
LIGHT_BLUE = colors.HexColor("#EDF3F8")
LIGHT_GRAY = colors.HexColor("#F5F6F7")
MID_GRAY = colors.HexColor("#D8DDE2")
TEXT_GRAY = colors.HexColor("#66717E")


@dataclass(frozen=True)
class CustomerReportVisualStyle:
    title: str = ""
    subtitle: str = ""
    company_name: str = "SOLCOR"
    primary_color: str = "#0B2D52"
    secondary_color: str = "#4BA52E"
    footer: str = ""
    contacts: str = ""
    disclaimer: str = ""
    enabled_sections: frozenset[str] | None = None


def _visual_style(pdf: canvas.Canvas) -> CustomerReportVisualStyle:
    return getattr(pdf, "_customer_report_visual_style", CustomerReportVisualStyle())


def _primary(pdf: canvas.Canvas):
    return colors.HexColor(_visual_style(pdf).primary_color)


def _secondary(pdf: canvas.Canvas):
    return colors.HexColor(_visual_style(pdf).secondary_color)


def _secondary_light(pdf: canvas.Canvas):
    color = _secondary(pdf)
    return colors.Color(
        0.88 + color.red * 0.12,
        0.88 + color.green * 0.12,
        0.88 + color.blue * 0.12,
    )


def _themed_color(pdf: canvas.Canvas, color):
    if color == NAVY:
        return _primary(pdf)
    if color == GREEN:
        return _secondary(pdf)
    if color == LIGHT_GREEN:
        return _secondary_light(pdf)
    return color


def _section_enabled(pdf: canvas.Canvas, *keys: str) -> bool:
    enabled = _visual_style(pdf).enabled_sections
    return enabled is None or any(key in enabled for key in keys)


REPORT_TYPES: dict[str, dict[str, Any]] = {
    "esco": {
        "badge": "Modelo ESCO",
        "note": "Instalação operada em modelo ESCO",
        "kpis": (
            ("Produção Total", "production_kwh", "kwh", NAVY),
            ("Autoconsumo", "self_use_kwh", "kwh", GREEN),
            ("Excedente", "export_kwh", "kwh", NAVY),
            ("Poupança na Fatura", "savings_eur", "eur", GREEN),
            ("Pagamento à Solcor", "solcor_payment_eur", "eur", NAVY),
            ("Benefício Líquido", "net_benefit_eur", "eur", ORANGE),
        ),
        "summary": (
            ("Redução da fatura de eletricidade", "savings_eur", "eur"),
            ("Receita da venda de excedente", "export_revenue_eur", "eur"),
            ("Benefício total", "total_benefit_eur", "eur"),
            ("Pagamento à Solcor", "solcor_payment_eur", "eur"),
            ("Benefício líquido", "net_benefit_eur", "eur"),
        ),
    },
    "epc": {
        "badge": "Modelo EPC",
        "note": "Instalação em modelo EPC - sistema propriedade do cliente",
        "kpis": (
            ("Produção Total", "production_kwh", "kwh", NAVY),
            ("Autoconsumo", "self_use_kwh", "kwh", GREEN),
            ("Excedente", "export_kwh", "kwh", NAVY),
            ("Benefício Total", "total_benefit_eur", "eur", GREEN),
            ("Poupança na Fatura", "savings_eur", "eur", NAVY),
            ("Receita de Excedente", "export_revenue_eur", "eur", ORANGE),
        ),
        "summary": (
            ("Redução da fatura de eletricidade", "savings_eur", "eur"),
            ("Receita da venda de excedente", "export_revenue_eur", "eur"),
            ("Benefício total", "total_benefit_eur", "eur"),
            ("Consumo total da empresa", "consumption_kwh", "kwh"),
        ),
    },
}


def summary_rows(report: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Return the financial summary, separating temporary client outages."""

    rows = tuple(
        row
        for row in REPORT_TYPES[report["report_type"]]["summary"]
        if report.get("export_revenue_enabled", True) or row[1] != "export_revenue_eur"
    )
    if report["report_type"] != "esco" or not report.get("client_outage_adjustments"):
        return rows
    return tuple(row for row in (
        ("Redução da fatura de eletricidade", "savings_eur", "eur"),
        ("Receita da venda de excedente", "export_revenue_eur", "eur"),
        ("Benefício total", "total_benefit_eur", "eur"),
        ("Pagamento à Solcor — produção medida", "measured_solcor_payment_eur", "eur"),
        ("Ajuste por indisponibilidade imputável ao cliente", "client_outage_charge_eur", "eur"),
        ("Pagamento total à Solcor", "solcor_payment_eur", "eur"),
        ("Benefício líquido", "net_benefit_eur", "eur"),
    ) if report.get("export_revenue_enabled", True) or row[1] != "export_revenue_eur")


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


def detect_report_type(asset: Any) -> str:
    report_type = detect_report_type_value(asset)
    if report_type != ReportType.EPC:
        return report_type.value
    fields = ("contract_type", "asset_type", "coverage_type", "sell_to", "project_name")
    for field in fields:
        value = _normalized_text(_asset_value(asset, field))
        if re.search(r"(^|\W)epc($|\W)", value):
            return report_type.value
    LOGGER.warning(
        "Customer report type not detected for asset_id=%s; defaulting to EPC",
        _asset_value(asset, "asset_id") or _asset_value(asset, "id"),
    )
    return report_type.value


def _number(value: Any) -> float:
    return decimal_to_float(decimal_from_value(value))


def prepare_customer_report(
    report: dict[str, Any],
    *,
    solcor_price_per_kwh: float = 0.0,
    billing_config: BillingConfig | None = None,
    months_count: int = 1,
) -> dict[str, Any]:
    prepared = dict(report)
    prepared["asset"] = dict(report.get("asset") or {})
    prepared["report_type"] = detect_report_type(prepared["asset"])
    production_is_final = bool(report.get("production_is_final", report.get("production_kwh") is not None))
    production_decimal = decimal_from_value(report.get("production_kwh"))
    export_decimal = decimal_from_value(report.get("export_kwh"))
    export_decimal = min(export_decimal, production_decimal) if production_decimal else export_decimal
    raw_self_use = report.get("self_use_kwh")
    self_use_decimal = infer_self_use_kwh(
        production_kwh=production_decimal,
        export_kwh=export_decimal,
        raw_self_use=raw_self_use,
    )
    if production_decimal and not self_use_decimal and export_decimal < production_decimal:
        self_use_decimal = production_decimal - export_decimal
    consumption_decimal = decimal_from_value(report.get("consumption_kwh"))
    months_count = int(report.get("months_count") or report.get("month_count") or months_count or 1)
    fallback_solcor_price = decimal_from_value(solcor_price_per_kwh)
    config = billing_config or BillingConfig(
        report_type=ReportType(prepared["report_type"]),
        billing_mode=BillingMode(str(report.get("billing_mode") or BillingMode.ENERGY.value)),
        billing_energy_base=BillingEnergyBase(str(report.get("billing_energy_base") or BillingEnergyBase.SELF_CONSUMPTION.value)),
        solcor_price_per_kwh=fallback_solcor_price,
        fixed_monthly_fee_eur=decimal_from_value(report.get("fixed_monthly_fee_eur")),
        electricity_price_eur_kwh=decimal_from_value(report.get("electricity_price")),
        export_price_eur_kwh=decimal_from_value(report.get("sell_price")),
        export_revenue_enabled=str(report.get("export_revenue_enabled", True)).strip().casefold() not in {"0", "false", "off", "no"},
    )
    if config.report_type != ReportType(prepared["report_type"]):
        config = BillingConfig(
            report_type=ReportType(prepared["report_type"]),
            billing_mode=config.billing_mode,
            billing_energy_base=config.billing_energy_base,
            solcor_price_per_kwh=config.solcor_price_per_kwh,
            fixed_monthly_fee_eur=config.fixed_monthly_fee_eur,
            electricity_price_eur_kwh=config.electricity_price_eur_kwh,
            export_price_eur_kwh=config.export_price_eur_kwh,
            export_revenue_enabled=config.export_revenue_enabled,
        )
    billing = calculate_customer_billing(
        EnergyBreakdown(
            production_kwh=production_decimal,
            self_use_kwh=self_use_decimal,
            export_kwh=export_decimal,
            consumption_kwh=consumption_decimal,
        ),
        config,
        months_count=months_count,
    )
    grid_import_decimal = (
        decimal_from_value(report.get("grid_import_kwh"))
        if report.get("grid_import_kwh") is not None
        else billing.grid_import_kwh
    )
    tariff_value = decimal_from_value(report.get("tariff_value_eur")) if report.get("tariff_value_eur") is not None else None
    savings_eur = tariff_value if tariff_value is not None else billing.savings_eur
    gross_benefit_eur = savings_eur + billing.export_revenue_eur
    net_benefit_eur = gross_benefit_eur - billing.solcor_payment_eur

    prepared.update(
        period_type=str(report.get("period_type") or "monthly"),
        period_start=report.get("period_start") or report.get("month_start"),
        period_end=report.get("period_end") or report.get("month_end"),
        period_label=report.get("period_label") or report.get("month_label"),
        included_months=list(report.get("included_months") or []),
        months_with_data=list(report.get("months_with_data") or []),
        missing_months=list(report.get("missing_months") or []),
        coverage_pct=_number(report.get("coverage_pct")),
        chart_granularity=str(report.get("chart_granularity") or "daily"),
        production_kwh=decimal_to_float(production_decimal) if report.get("production_kwh") is not None else None,
        self_use_kwh=(
            decimal_to_float(self_use_decimal)
            if report.get("self_use_kwh") is not None
            or (production_is_final and report.get("production_kwh") is not None and report.get("export_kwh") is not None)
            else None
        ),
        export_kwh=decimal_to_float(export_decimal) if report.get("export_kwh") is not None else None,
        consumption_kwh=decimal_to_float(consumption_decimal) if report.get("consumption_kwh") is not None else None,
        savings_eur=decimal_to_float(savings_eur),
        export_revenue_eur=decimal_to_float(billing.export_revenue_eur),
        export_revenue_enabled=config.export_revenue_enabled,
        total_benefit_eur=decimal_to_float(gross_benefit_eur),
        gross_benefit_eur=decimal_to_float(gross_benefit_eur),
        solcor_price_per_kwh=decimal_to_float(billing.solcor_price_per_kwh),
        fixed_monthly_fee_eur=decimal_to_float(billing.fixed_monthly_fee_eur),
        solcor_payment_eur=decimal_to_float(billing.solcor_payment_eur),
        net_benefit_eur=decimal_to_float(net_benefit_eur),
        billable_energy_kwh=decimal_to_float(billing.billable_energy_kwh),
        grid_import_kwh=decimal_to_float(grid_import_decimal),
        exported_energy_kwh=decimal_to_float(billing.exported_energy_kwh),
        billing_mode=billing.billing_mode.value,
        billing_energy_base=billing.billing_energy_base.value,
        months_count=billing.months_count,
        billing_warnings=list(billing.warnings),
        tariff_type=report.get("tariff_type") or "",
        tariff_types_used=list(report.get("tariff_types_used") or []),
        tariff_source=report.get("tariff_source") or ("billing_default" if tariff_value is None else "stored_tariff"),
        tariff_period_breakdown=list(report.get("tariff_period_breakdown") or []),
        tariff_value_eur=decimal_to_float(savings_eur),
        tariff_coverage_pct=_number(report.get("tariff_coverage_pct")),
        tariff_warnings=list(report.get("tariff_warnings") or []),
        autoconsumption_pct=decimal_to_float(billing.autoconsumption_pct),
        export_pct=decimal_to_float(billing.export_pct),
        self_sufficiency_pct=decimal_to_float(billing.self_sufficiency_pct),
        production_is_final=production_is_final,
    )
    if not production_is_final:
        for key in (
            "savings_eur",
            "export_revenue_eur",
            "total_benefit_eur",
            "gross_benefit_eur",
            "solcor_payment_eur",
            "net_benefit_eur",
            "billable_energy_kwh",
            "grid_import_kwh",
            "exported_energy_kwh",
            "tariff_value_eur",
            "autoconsumption_pct",
            "export_pct",
            "self_sufficiency_pct",
        ):
            prepared[key] = None
        prepared.setdefault("warnings", []).append("production_not_final")
    prepared["report_notes"] = list(report.get("report_notes") or [])
    warning_messages = {
        "missing_solcor_price": "Preço Solcor não indicado; pagamento calculado a 0 EUR/kWh.",
        "missing_fixed_monthly_fee": "Mensalidade Solcor não indicada; pagamento calculado a 0 EUR.",
        "missing_electricity_price": "Preço de eletricidade não indicado; poupança calculada a 0 EUR/kWh.",
        "missing_export_price": "Preço de venda do excedente não indicado; receita calculada a 0 EUR/kWh.",
    }
    for warning in billing.warnings:
        message = warning_messages.get(warning)
        if message:
            prepared["report_notes"].append(message)
    # Current reports carry the real hourly valuation as a period breakdown.
    # Keep the older flat fields as a fallback for historical report payloads.
    breakdown_by_period = {
        str(item.get("period_name") or "").strip().casefold(): _number(item.get("energy_kwh"))
        for item in prepared["tariff_period_breakdown"]
        if isinstance(item, dict)
    }
    prepared["tariff_rows"] = [
        ("Cheia", breakdown_by_period.get("cheia", _number(report.get("self_use_cheia_kwh"))), NAVY),
        ("Ponta", breakdown_by_period.get("ponta", _number(report.get("self_use_ponta_kwh"))), ORANGE),
        ("Vazio", breakdown_by_period.get("vazio", _number(report.get("self_use_vazio_kwh"))), MID_GRAY),
        ("Super vazio", breakdown_by_period.get("super_vazio", _number(report.get("self_use_super_vazio_kwh"))), GREEN),
    ]
    prepared["month_label"] = prepared["period_label"]
    prepared["month_start"] = prepared["period_start"]
    prepared["month_end"] = prepared["period_end"]
    return prepared


def format_kwh(value: Any) -> str:
    if value is None:
        return "Dados indisponíveis"
    return f"{_number(value):,.0f}".replace(",", " ") + " kWh"


def solcor_charge_basis_text(report: dict[str, Any]) -> str:
    """Describe the contractual rate behind an ESCO payment."""

    if str(report.get("report_type") or "").casefold() != "esco":
        return ""
    if str(report.get("billing_mode") or "energy") == "fixed_monthly_fee":
        fee = report.get("fixed_monthly_fee_eur")
        return f"Mensalidade fixa de {format_eur(fee)} / mês" if fee is not None else "Mensalidade fixa"
    rate = report.get("solcor_price_per_kwh")
    billable_energy = report.get("billable_energy_kwh")
    if rate is None or billable_energy is None:
        return ""
    base = (
        "Produção"
        if str(report.get("billing_energy_base") or "self_consumption") == "total_production"
        else "Autoconsumo"
    )
    return f"{base} de {format_kwh(billable_energy)} x {_number(rate):.5f} €/kWh"


def client_outage_charge_basis_text(report: dict[str, Any]) -> str:
    if not report.get("client_outage_adjustments"):
        return ""
    estimated_energy = report.get("client_outage_billable_kwh")
    rate = report.get("solcor_price_per_kwh")
    if estimated_energy is None or rate is None:
        return ""
    return f"{format_kwh(estimated_energy)} x {_number(rate):.5f} €/kWh"


def format_eur(value: Any) -> str:
    if value is None:
        return "Dados indisponíveis"
    return f"{float(value or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", " ") + " €"


def format_pct(value: Any) -> str:
    if value is None:
        return "Dados indisponíveis"
    return f"{_number(value):.0f}%"


def _scaled_text(pdf: canvas.Canvas, text: str, x: float, y: float, width: float, *, size: int, color=None) -> None:
    font = "Helvetica-Bold"
    while size > 6 and pdf.stringWidth(text, font, size) > width:
        size -= 1
    pdf.setFillColor(_themed_color(pdf, color) if color is not None else _primary(pdf))
    pdf.setFont(font, size)
    pdf.drawString(x, y, text)


def _card(pdf: canvas.Canvas, x: float, y: float, width: float, height: float, *, fill=colors.white) -> None:
    pdf.setFillColor(fill)
    pdf.setStrokeColor(MID_GRAY)
    pdf.setLineWidth(0.45)
    pdf.roundRect(x, y, width, height, 6, fill=1, stroke=1)


def _draw_logo(pdf: canvas.Canvas, logo_path: Path | None, x: float, y: float, width: float) -> None:
    if logo_path and logo_path.exists():
        pdf.drawImage(ImageReader(str(logo_path)), x, y, width=width, height=width * 0.28, preserveAspectRatio=True, mask="auto")
        return
    pdf.setFillColor(_secondary(pdf))
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawRightString(x + width, y + 10, _visual_style(pdf).company_name or "SOLCOR")


def draw_report_icon(pdf: canvas.Canvas, kind: str, cx: float, cy: float, color) -> None:
    pdf.setStrokeColor(color)
    pdf.setFillColor(color)
    pdf.setLineWidth(1.2)
    if kind == "production":
        pdf.circle(cx, cy + 5, 3.5, fill=0, stroke=1)
        pdf.rect(cx - 7, cy - 7, 14, 8, fill=0, stroke=1)
        pdf.line(cx - 3, cy - 7, cx - 4, cy + 1)
        pdf.line(cx + 3, cy - 7, cx + 4, cy + 1)
        pdf.line(cx - 7, cy - 3, cx + 7, cy - 3)
        for dx, dy in ((0, 10), (-7, 8), (7, 8), (-10, 3), (10, 3)):
            pdf.line(cx + dx * 0.75, cy + dy * 0.75, cx + dx, cy + dy)
    elif kind == "self_use":
        pdf.line(cx - 8, cy - 1, cx, cy + 7)
        pdf.line(cx, cy + 7, cx + 8, cy - 1)
        pdf.rect(cx - 6, cy - 8, 12, 8, fill=0, stroke=1)
        pdf.rect(cx - 2, cy - 8, 4, 5, fill=0, stroke=1)
    elif kind == "export":
        pdf.line(cx, cy + 8, cx, cy - 8)
        pdf.line(cx - 7, cy - 8, cx + 7, cy - 8)
        pdf.line(cx - 6, cy + 1, cx + 6, cy + 1)
        pdf.line(cx - 8, cy - 3, cx + 8, cy - 3)
        pdf.line(cx - 8, cy - 8, cx - 2, cy + 8)
        pdf.line(cx + 8, cy - 8, cx + 2, cy + 8)
    elif kind == "money":
        pdf.setFont("Helvetica-Bold", 17)
        pdf.drawCentredString(cx, cy - 6, "€")
    elif kind == "solcor":
        pdf.circle(cx, cy + 3, 4, fill=0, stroke=1)
        pdf.arc(cx - 8, cy - 10, cx + 8, cy + 2, 0, 180)
    elif kind == "wallet":
        pdf.roundRect(cx - 8, cy - 6, 16, 11, 2, fill=0, stroke=1)
        pdf.line(cx - 5, cy + 5, cx + 4, cy + 8)
        pdf.line(cx + 4, cy + 8, cx + 7, cy + 5)
        pdf.circle(cx + 4, cy - 1, 1.2, fill=1, stroke=0)
    elif kind == "target":
        pdf.circle(cx, cy, 8, fill=0, stroke=1)
        pdf.circle(cx, cy, 4, fill=0, stroke=1)
        pdf.circle(cx, cy, 1.3, fill=1, stroke=0)
        pdf.line(cx + 3, cy + 3, cx + 9, cy + 9)
        pdf.line(cx + 9, cy + 9, cx + 11, cy + 6)
    elif kind == "bars":
        pdf.rect(cx - 8, cy - 7, 3, 7, fill=1, stroke=0)
        pdf.rect(cx - 2, cy - 7, 3, 11, fill=1, stroke=0)
        pdf.rect(cx + 4, cy - 7, 3, 15, fill=1, stroke=0)
        pdf.line(cx - 9, cy - 7, cx + 10, cy - 7)


def kpi_icon_kind(key: str) -> str:
    return {
        "production_kwh": "production",
        "self_use_kwh": "self_use",
        "export_kwh": "export",
        "total_benefit_eur": "money",
        "solcor_payment_eur": "solcor",
        "net_benefit_eur": "wallet",
        "savings_eur": "money",
        "export_revenue_eur": "wallet",
        "availability_pct": "target",
    }.get(key, "money")


def draw_report_header(pdf: canvas.Canvas, report: dict[str, Any], logo_path: Path | None, page_width: float, page_height: float) -> None:
    x = 20
    style = _visual_style(pdf)
    _scaled_text(
        pdf,
        style.title or str(report["asset"].get("project_name") or "Instalacao"),
        x,
        page_height - 31,
        430,
        size=22,
    )
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(_primary(pdf))
    months_count = int(report.get("months_count") or 1)
    title = "Relatório Mensal - Energia Solar" if months_count == 1 else f"Relatório {report.get('period_label')} - Energia Solar"
    pdf.drawString(x, page_height - 47, style.subtitle or title)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(x, page_height - 62, str(report.get("month_label") or ""))
    config = REPORT_TYPES[report["report_type"]]
    badge_x = 186
    pdf.setFillColor(_secondary(pdf))
    pdf.roundRect(badge_x, page_height - 55, 78, 16, 8, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(badge_x + 39, page_height - 50, config["badge"])
    _draw_logo(pdf, logo_path, page_width - 178, page_height - 53, 154)
    pdf.setFillColor(TEXT_GRAY)
    pdf.setFont("Helvetica", 7)
    pdf.drawRightString(page_width - 20, page_height - 67, config["note"])
    pdf.setStrokeColor(_secondary(pdf))
    pdf.setLineWidth(1)
    pdf.line(20, page_height - 72, page_width - 20, page_height - 72)


def draw_kpi_cards(pdf: canvas.Canvas, report: dict[str, Any], page_width: float, page_height: float) -> None:
    config = REPORT_TYPES[report["report_type"]]
    items = list(config["kpis"])
    if not report.get("export_revenue_enabled", True):
        items = [item for item in items if item[1] != "export_revenue_eur"]
    if (
        _section_enabled(pdf, "availability")
        and report.get("include_availability_kpi")
        and report.get("availability_pct") is not None
    ):
        items.append(("Disponibilidade (%)", "availability_pct", "pct", GREEN))
    gap = 6
    x0 = 20
    y = page_height - 134
    card_w = (page_width - 40 - gap * (len(items) - 1)) / len(items)
    for index, (label, key, kind, accent) in enumerate(items):
        x = x0 + index * (card_w + gap)
        _card(pdf, x, y, card_w, 52)
        accent = _themed_color(pdf, accent)
        pdf.setFillColor(accent)
        pdf.circle(x + 19, y + 26, 13, fill=0, stroke=1)
        draw_report_icon(pdf, kpi_icon_kind(key), x + 19, y + 26, accent)
        pdf.setFillColor(_primary(pdf))
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(x + 38, y + 33, label)
        if kind == "kwh":
            value = format_kwh(report[key])
        elif kind == "pct":
            value = format_pct(report[key])
        else:
            value = format_eur(report[key])
        _scaled_text(pdf, value, x + 38, y + 14, card_w - 43, size=11, color=accent)


def draw_daily_chart(pdf: canvas.Canvas, report: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
    _card(pdf, x, y, width, height)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(x + 12, y + height - 17, "Produção Diária de Eletricidade (kWh)")
    plot_x, plot_y = x + 28, y + 28
    plot_w, plot_h = width - 42, height - 55
    rows = report.get("daily_rows") or []
    days = int(getattr(report.get("month_end"), "day", 31))
    by_day = {row["date"].day: row for row in rows if isinstance(row.get("date"), date)}
    max_value = max([_number(row.get("consumption_kwh")) for row in rows] + [_number(row.get("production_kwh")) for row in rows] + [1])
    pdf.setStrokeColor(MID_GRAY)
    for step in range(5):
        line_y = plot_y + plot_h * step / 4
        pdf.line(plot_x, line_y, plot_x + plot_w, line_y)
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 5.5)
        pdf.drawRightString(plot_x - 4, line_y - 2, f"{max_value * step / 4:.0f}")
    gap = 2
    bar_w = max((plot_w - gap * (days - 1)) / days, 2.4)
    for day in range(1, days + 1):
        row = by_day.get(day, {})
        self_use = _number(row.get("self_use_kwh"))
        export = _number(row.get("export_kwh"))
        consumption = _number(row.get("consumption_kwh"))
        x_bar = plot_x + (day - 1) * (bar_w + gap)
        if consumption:
            pdf.setFillColor(MID_GRAY)
            pdf.rect(x_bar, plot_y, bar_w, plot_h * consumption / max_value, fill=1, stroke=0)
        pdf.setFillColor(ORANGE)
        self_h = plot_h * self_use / max_value
        pdf.rect(x_bar, plot_y, bar_w, self_h, fill=1, stroke=0)
        if export:
            pdf.setFillColor(_primary(pdf))
            pdf.rect(x_bar, plot_y + self_h, bar_w, plot_h * export / max_value, fill=1, stroke=0)
        if day == 1 or day == days or day % 3 == 0:
            pdf.setFillColor(TEXT_GRAY)
            pdf.setFont("Helvetica", 5)
            pdf.drawCentredString(x_bar + bar_w / 2, plot_y - 9, str(day))
    legend = ((MID_GRAY, "Consumo da empresa"), (ORANGE, "Solar autoconsumida"), (_primary(pdf), "Solar excedente"))
    legend_x = x + 150
    for color, label in legend:
        pdf.setFillColor(color)
        pdf.rect(legend_x, y + 8, 6, 6, fill=1, stroke=0)
        pdf.setFillColor(_primary(pdf))
        pdf.setFont("Helvetica", 6)
        pdf.drawString(legend_x + 9, y + 8, label)
        legend_x += 112


def draw_monthly_chart(pdf: canvas.Canvas, report: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
    _card(pdf, x, y, width, height)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(x + 12, y + height - 17, "Produção Mensal de Eletricidade (kWh)")
    plot_x, plot_y = x + 36, y + 28
    plot_w, plot_h = width - 52, height - 55
    rows = report.get("monthly_rows") or []
    max_value = max([_number(row.get("consumption_kwh")) for row in rows] + [_number(row.get("production_kwh")) for row in rows] + [1])
    pdf.setStrokeColor(MID_GRAY)
    for step in range(5):
        line_y = plot_y + plot_h * step / 4
        pdf.line(plot_x, line_y, plot_x + plot_w, line_y)
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 5.5)
        pdf.drawRightString(plot_x - 4, line_y - 2, f"{max_value * step / 4:.0f}")
    if not rows:
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(plot_x + plot_w / 2, plot_y + plot_h / 2, "Sem dados suficientes")
        return
    gap = 8
    bar_w = max((plot_w - gap * (len(rows) - 1)) / len(rows), 10)
    for index, row in enumerate(rows):
        self_use = _number(row.get("self_use_kwh"))
        export = _number(row.get("export_kwh"))
        consumption = _number(row.get("consumption_kwh"))
        x_bar = plot_x + index * (bar_w + gap)
        if consumption:
            pdf.setFillColor(MID_GRAY)
            pdf.rect(x_bar, plot_y, bar_w, plot_h * consumption / max_value, fill=1, stroke=0)
        pdf.setFillColor(ORANGE)
        self_h = plot_h * self_use / max_value
        pdf.rect(x_bar, plot_y, bar_w, self_h, fill=1, stroke=0)
        if export:
            pdf.setFillColor(_primary(pdf))
            pdf.rect(x_bar, plot_y + self_h, bar_w, plot_h * export / max_value, fill=1, stroke=0)
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 5.5)
        pdf.drawCentredString(x_bar + bar_w / 2, plot_y - 9, str(row.get("label") or ""))
    legend = ((MID_GRAY, "Consumo da empresa"), (ORANGE, "Solar autoconsumida"), (_primary(pdf), "Solar excedente"))
    legend_x = x + 150
    for color, label in legend:
        pdf.setFillColor(color)
        pdf.rect(legend_x, y + 8, 6, 6, fill=1, stroke=0)
        pdf.setFillColor(_primary(pdf))
        pdf.setFont("Helvetica", 6)
        pdf.drawString(legend_x + 9, y + 8, label)
        legend_x += 112


def draw_highlights(pdf: canvas.Canvas, report: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
    _card(pdf, x, y, width, height, fill=LIGHT_GRAY)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(x + 12, y + height - 17, "Destaques do Periodo")
    solcor_rate = (
        f"{_number(report.get('solcor_price_per_kwh')):.5f} €/kWh"
        if str(report.get("billing_mode") or "energy") == "energy"
        else f"{format_eur(report.get('fixed_monthly_fee_eur'))} / mês"
    )
    first_highlight = (
        ("Tarifa Solcor", solcor_rate, LIGHT_GREEN, "solcor")
        if report.get("report_type") == "esco"
        else ("Poupança na Fatura", format_eur(report["savings_eur"]), LIGHT_GREEN, "money")
    )
    items = (
        first_highlight,
        ("Receita de Excedente", format_eur(report["export_revenue_eur"]), LIGHT_GREEN, "wallet"),
        ("Taxa de Autoconsumo", format_pct(report["autoconsumption_pct"]), LIGHT_GREEN, "target"),
        ("Autossuficiência", format_pct(report["self_sufficiency_pct"]), LIGHT_GREEN, "bars"),
    )
    card_h = (height - 30 - 9) / 4
    for index, (label, value, fill, icon_kind) in enumerate(items):
        card_y = y + height - 28 - (index + 1) * card_h - index * 3
        _card(pdf, x + 7, card_y, width - 14, card_h, fill=colors.white)
        pdf.setFillColor(_themed_color(pdf, fill))
        pdf.circle(x + 31, card_y + card_h / 2, 14, fill=1, stroke=0)
        draw_report_icon(pdf, icon_kind, x + 31, card_y + card_h / 2, _secondary(pdf))
        pdf.setFillColor(_primary(pdf))
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(x + 52, card_y + card_h - 15, label)
        _scaled_text(pdf, value, x + 52, card_y + 9, width - 65, size=12, color=_secondary(pdf))


def draw_monthly_summary(pdf: canvas.Canvas, report: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
    _card(pdf, x, y, width, height)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(x + 12, y + height - 16, "Resumo do Periodo")
    rows = summary_rows(report)
    row_h = (height - 22) / len(rows)
    for index, (label, key, kind) in enumerate(rows):
        row_y = y + height - 22 - (index + 1) * row_h
        is_total = label in {"Benefício total", "Benefício líquido"}
        fill = _secondary(pdf) if label == "Benefício total" else ORANGE if label == "Benefício líquido" else colors.white
        if is_total:
            pdf.setFillColor(fill)
            pdf.rect(x + 6, row_y, width - 12, row_h, fill=1, stroke=0)
        pdf.setStrokeColor(MID_GRAY)
        pdf.line(x + 6, row_y, x + width - 6, row_y)
        pdf.setFillColor(colors.white if is_total else _primary(pdf))
        pdf.setFont("Helvetica-Bold" if is_total else "Helvetica", 6.5)
        pdf.drawString(x + 12, row_y + row_h / 2 - 2, label)
        value = format_kwh(report[key]) if kind == "kwh" else format_eur(report[key])
        pdf.setFont("Helvetica-Bold", 7.5)
        pdf.drawRightString(x + width - 12, row_y + row_h / 2 - 2, value)
        if key in {"solcor_payment_eur", "measured_solcor_payment_eur", "client_outage_charge_eur"} and label != "Pagamento total à Solcor":
            basis = (
                client_outage_charge_basis_text(report)
                if key == "client_outage_charge_eur"
                else solcor_charge_basis_text(report)
            )
            if basis:
                value_width = pdf.stringWidth(value, "Helvetica-Bold", 7.5)
                detail_right = x + width - 20 - value_width
                _scaled_text(
                    pdf,
                    basis,
                    x + width * 0.42,
                    row_y + row_h / 2 - 2,
                    max(detail_right - (x + width * 0.42), 1),
                    size=5.5,
                    color=TEXT_GRAY,
                )


def _draw_donut(pdf: canvas.Canvas, center_x: float, center_y: float, radius: float, pct: float | None, color, label: str) -> None:
    display_pct = min(max(pct, 0), 100) if pct is not None else None
    pdf.setFillColor(MID_GRAY)
    pdf.wedge(center_x - radius, center_y - radius, center_x + radius, center_y + radius, 90, 360, fill=1, stroke=0)
    if display_pct is not None:
        pdf.setFillColor(_themed_color(pdf, color))
        # ReportLab's sixth wedge argument is an angular *extent*, not the
        # finishing angle. Passing 90 + extent made the arc overflow and
        # could cover the previous tariff periods in the compact chart.
        pdf.wedge(center_x - radius, center_y - radius, center_x + radius, center_y + radius, 90, 360 * display_pct / 100, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.circle(center_x, center_y, radius * 0.58, fill=1, stroke=0)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawCentredString(center_x, center_y - 4, format_pct(display_pct) if display_pct is not None else "N/D")
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawCentredString(center_x, center_y + radius + 10, label)


def draw_donut_charts(pdf: canvas.Canvas, report: dict[str, Any], x: float, y: float, width: float, height: float) -> None:
    gap = 7
    card_w = (width - gap * 2) / 3
    for index in range(3):
        _card(pdf, x + index * (card_w + gap), y, card_w, height)
    _draw_donut(pdf, x + 54, y + 41, 29, report["autoconsumption_pct"], ORANGE, "Taxa de Autoconsumo")
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica", 6.5)
    pdf.drawString(x + 95, y + 46, format_kwh(report["self_use_kwh"]))
    pdf.drawString(x + 95, y + 31, f"Excedente: {format_kwh(report['export_kwh'])}")

    tariff_x = x + card_w + gap
    tariff_total = sum(value for _, value, _ in report["tariff_rows"])
    if tariff_total:
        angle = 90.0
        # Keep the heading centred in the card, while the donut and legend
        # share a balanced horizontal group underneath it.
        center_x, center_y, radius = tariff_x + 62, y + 50, 29
        for label, value, color in report["tariff_rows"]:
            if not value:
                continue
            extent = 360 * value / tariff_total
            pdf.setFillColor(_themed_color(pdf, color))
            # Keep the start angle normalized. This prevents a later slice
            # from receiving an angle above 360º and painting over Cheia.
            pdf.wedge(center_x - radius, center_y - radius, center_x + radius, center_y + radius, angle % 360, extent, fill=1, stroke=0)
            angle += extent
        pdf.setFillColor(colors.white)
        pdf.circle(center_x, center_y, 17, fill=1, stroke=0)
        pdf.setFillColor(_primary(pdf))
        # The full “17 500 kWh” label did not fit in the donut hole. Keep the
        # number prominent and place the unit on its own line.
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawCentredString(center_x, center_y, f"{_number(tariff_total):,.0f}".replace(",", " "))
        pdf.setFont("Helvetica", 5)
        pdf.drawCentredString(center_x, center_y - 7, "kWh")
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(tariff_x + card_w / 2, y + height - 18, "Autoconsumo por Período Tarifário")
        legend_y = center_y + 16
        for label, value, color in report["tariff_rows"]:
            if value:
                pdf.setFillColor(_themed_color(pdf, color))
                pdf.circle(tariff_x + 108, legend_y, 3, fill=1, stroke=0)
                pdf.setFillColor(_primary(pdf))
                pdf.setFont("Helvetica", 5.5)
                pdf.drawString(tariff_x + 116, legend_y - 2, f"{label}: {format_kwh(value)}")
                legend_y -= 13
    else:
        pdf.setFillColor(_primary(pdf))
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(tariff_x + card_w / 2, y + height - 18, "Autoconsumo por Período Tarifário")
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 7)
        pdf.drawCentredString(tariff_x + card_w / 2, y + 35, "Sem dados suficientes")
    # The legend already contains the useful visual breakdown. Rendering the
    # technical rows here made them collide with the footer in this compact
    # card; prices and values remain available in the financial report/Excel.
    if report.get("tariff_coverage_pct") is not None and (
        report.get("tariff_coverage_pct") < 100 or report.get("tariff_warnings")
    ):
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 5.5)
        warnings = ", ".join((report.get("tariff_warnings") or [])[:3])
        pdf.drawString(tariff_x + 10, y + 13, f"Cobertura: {format_pct(report.get('tariff_coverage_pct'))}" + (f" | {warnings}" if warnings else ""))

    suff_x = x + 2 * (card_w + gap)
    _draw_donut(pdf, suff_x + 54, y + 41, 29, report["self_sufficiency_pct"], ORANGE, "Autossuficiência Energética")
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica", 6.5)
    pdf.drawString(suff_x + 95, y + 46, f"Consumo: {format_kwh(report['consumption_kwh'])}")
    pdf.drawString(suff_x + 95, y + 31, f"Producao: {format_kwh(report['production_kwh'])}")


def draw_report_footer(pdf: canvas.Canvas, report: dict[str, Any], page_width: float) -> None:
    style = _visual_style(pdf)
    pdf.setStrokeColor(_secondary(pdf))
    pdf.line(20, 18, page_width - 20, 18)
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-BoldOblique", 7)
    pdf.drawString(24, 7, style.footer or "Obrigado pela sua confiança!")
    notes = list(report.get("report_notes") or []) if _section_enabled(pdf, "warnings", "data_quality", "notes") else []
    # The operational state of Sigenergy installations belongs in monitoring,
    # not in the PDF footer. Besides not being actionable in the report, this
    # long provider-capability note was rendered below the visual cards.
    if report.get("availability_error") == "availability_not_supported_by_provider":
        notes = [
            note
            for note in notes
            if note != "Disponibilidade nao suportada para a fonte de energia selecionada."
        ]
    notes.extend(item for item in (style.contacts, style.disclaimer) if item)
    if notes:
        pdf.setFillColor(TEXT_GRAY)
        pdf.setFont("Helvetica", 5.5)
        _scaled_text(pdf, " ".join(notes), 340, 7, page_width - 360, size=5, color=TEXT_GRAY)


def draw_client_outage_adjustments(pdf: canvas.Canvas, report: dict[str, Any], page_width: float, page_height: float) -> None:
    adjustments = list(report.get("client_outage_adjustments") or [])
    if not adjustments:
        return
    pdf.setFillColor(_primary(pdf))
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(28, page_height - 45, "Cobrança por indisponibilidade imputável ao cliente")
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(TEXT_GRAY)
    pdf.drawString(28, page_height - 60, "A produção real mantém-se inalterada; estes valores aplicam-se apenas ao pagamento à Solcor deste relatório.")
    y = page_height - 95
    headers = (("Data", 28), ("Produção estimada", 160), ("Motivo", 300))
    pdf.setFillColor(_secondary(pdf))
    pdf.rect(24, y - 5, page_width - 48, 20, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 8)
    for label, x in headers:
        pdf.drawString(x, y + 2, label)
    y -= 25
    pdf.setFont("Helvetica", 8)
    for item in adjustments:
        pdf.setFillColor(_primary(pdf))
        pdf.drawString(28, y, str(item.get("date") or "-"))
        pdf.drawString(160, y, format_kwh(item.get("estimated_kwh")))
        _scaled_text(pdf, str(item.get("reason") or "-"), 300, y, page_width - 328, size=8, color=_primary(pdf))
        pdf.setStrokeColor(MID_GRAY)
        pdf.line(24, y - 6, page_width - 24, y - 6)
        y -= 22
    y -= 8
    pdf.setFont("Helvetica-Bold", 9)
    pdf.setFillColor(_primary(pdf))
    pdf.drawString(28, y, f"Total estimado: {format_kwh(report.get('client_outage_billable_kwh'))}")
    pdf.drawRightString(page_width - 28, y, f"Ajuste à Solcor: {format_eur(report.get('client_outage_charge_eur'))}")
    draw_report_footer(pdf, report, page_width)


def build_customer_report_pdf(
    report: dict[str, Any],
    *,
    logo_path: Path | None = None,
    visual_style: CustomerReportVisualStyle | None = None,
) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=landscape(A4), pageCompression=1)
    pdf._customer_report_visual_style = visual_style or CustomerReportVisualStyle()
    page_width, page_height = landscape(A4)
    draw_report_header(pdf, report, logo_path, page_width, page_height)
    if _section_enabled(pdf, "executive_summary", "production", "financial"):
        draw_kpi_cards(pdf, report, page_width, page_height)
    if _section_enabled(pdf, "production", "comparison"):
        if report.get("chart_granularity") == "monthly":
            draw_monthly_chart(pdf, report, 20, 236, 604, 216)
        else:
            draw_daily_chart(pdf, report, 20, 236, 604, 216)
    if _section_enabled(pdf, "executive_summary", "financial"):
        draw_highlights(pdf, report, 632, 236, page_width - 652, 216)
        draw_monthly_summary(pdf, report, 20, 146, 604, 82)
    if _section_enabled(pdf, "self_consumption", "tariffs"):
        draw_donut_charts(pdf, report, 20, 26, page_width - 40, 112)
    draw_report_footer(pdf, report, page_width)
    pdf.showPage()
    if report.get("client_outage_adjustments"):
        draw_client_outage_adjustments(pdf, report, page_width, page_height)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
