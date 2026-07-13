from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


PARSER_NAME = "financial_model_workbook"
PARSER_VERSION = "1"


MONTH_LABELS = {
    1: ("1", "jan", "janeiro", "january"),
    2: ("2", "fev", "fevereiro", "feb", "february"),
    3: ("3", "mar", "marco", "marco", "march"),
    4: ("4", "abr", "abril", "apr", "april"),
    5: ("5", "mai", "maio", "may"),
    6: ("6", "jun", "junho", "june"),
    7: ("7", "jul", "julho", "july"),
    8: ("8", "ago", "agosto", "aug", "august"),
    9: ("9", "set", "setembro", "sep", "september"),
    10: ("10", "out", "outubro", "oct", "october"),
    11: ("11", "nov", "novembro", "november"),
    12: ("12", "dez", "dezembro", "dec", "december"),
}

METRIC_ALIASES = {
    "production": ("pv", "pv production", "production", "producao", "producao pv", "producao fotovoltaica", "producao solar"),
    "consumption": ("consumption", "consumo"),
    "self_use": ("sc", "self-consumption", "self consumption", "self-c", "autoconsumo"),
    "self_consumption_rate": ("self-consumption rate", "self consumption rate", "taxa de autoconsumo", "% ac"),
    "export": ("export", "exported energy", "excedente", "injecao na rede"),
    "grid_import": ("grid import", "import from grid", "importacao da rede", "energia importada", "buy from grid"),
}

REQUIRED_MONTHS = set(range(1, 13))


@dataclass(frozen=True)
class ParsedFinancialModel:
    detected_name: str
    detected_nif: str
    detected_kwp: float | None
    base_year: int | None
    sheet_name: str
    monthly: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    details: dict[str, Any]
    source_cells: dict[str, Any]
    parser_name: str = PARSER_NAME
    parser_version: str = PARSER_VERSION


class FinancialModelParseError(ValueError):
    pass


def normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_value.strip().lower())


def parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(" ", "").replace("\u00a0", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw in {"", "-", "."}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_month(value: Any) -> int | None:
    raw = normalize_text(value)
    if not raw:
        return None
    if raw.endswith(".0"):
        raw = raw[:-2]
    for month, labels in MONTH_LABELS.items():
        if raw in labels:
            return month
    return None


def metric_key(value: Any) -> str | None:
    raw = normalize_text(value)
    if not raw:
        return None
    for key in ("self_consumption_rate", "self_use", "grid_import", "export", "production", "consumption"):
        if raw in METRIC_ALIASES[key]:
            return key
    for key in ("self_consumption_rate", "self_use", "grid_import", "export", "production", "consumption"):
        if any(alias in raw for alias in METRIC_ALIASES[key]):
            return key
    return None


def cell_ref(sheet_name: str, row: int, column: int) -> str:
    return f"{sheet_name}!{get_column_letter(column)}{row}"


def parse_financial_model_workbook(path: Path) -> ParsedFinancialModel:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True, keep_vba=False, keep_links=False)
    except (BadZipFile, OSError, ValueError) as exc:
        raise FinancialModelParseError("workbook_invalid") from exc
    try:
        sheet = _select_monthly_sheet(workbook)
        monthly, warnings, source_cells = _parse_monthly_sheet(sheet)
        detected_name, detected_nif, detected_kwp, base_year, metadata_cells = _parse_metadata(workbook)
        details = _parse_details(workbook)
        return ParsedFinancialModel(
            detected_name=detected_name,
            detected_nif=detected_nif,
            detected_kwp=detected_kwp,
            base_year=base_year,
            sheet_name=sheet.title,
            monthly=tuple(monthly),
            warnings=tuple(sorted(set(warnings))),
            details=details,
            source_cells={**source_cells, **metadata_cells},
        )
    finally:
        workbook.close()


def _select_monthly_sheet(workbook: Any) -> Any:
    prod_month = [sheet for sheet in workbook.worksheets if normalize_text(sheet.title) == "prod month"]
    if len(prod_month) == 1:
        return prod_month[0]
    compatible = []
    for sheet in workbook.worksheets:
        rows = _preview_rows(sheet, max_rows=30, max_cols=20)
        if _row_or_column_month_layout(rows):
            compatible.append(sheet)
    if len(compatible) == 1:
        return compatible[0]
    if len(compatible) > 1:
        raise FinancialModelParseError("ambiguous_financial_model")
    raise FinancialModelParseError("financial_model_missing_month")


def _preview_rows(sheet: Any, *, max_rows: int, max_cols: int) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or max_rows, max_rows), max_col=min(sheet.max_column or max_cols, max_cols), values_only=True)
    ]


def _row_or_column_month_layout(rows: list[tuple[Any, ...]]) -> bool:
    for row in rows:
        if sum(1 for value in row if parse_month(value)) >= 10:
            return True
    month_rows = sum(1 for row in rows if row and parse_month(row[0]))
    return month_rows >= 10


def _parse_monthly_sheet(sheet: Any) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    rows = list(sheet.iter_rows(values_only=False))
    parsed = _parse_metric_rows(sheet.title, rows)
    if parsed is None:
        parsed = _parse_month_rows(sheet.title, rows)
    if parsed is None:
        raise FinancialModelParseError("financial_model_missing_month")
    monthly, source_cells = parsed
    warnings: list[str] = []
    months = {row["month"] for row in monthly}
    if months != REQUIRED_MONTHS:
        raise FinancialModelParseError("financial_model_missing_month")
    if any(row.get("expected_production_kwh") is None for row in monthly):
        raise FinancialModelParseError("financial_model_missing_month")
    for row in monthly:
        calculated: dict[str, str] = {}
        row_warnings: list[str] = []
        production = row.get("expected_production_kwh")
        consumption = row.get("expected_consumption_kwh")
        self_use = row.get("expected_self_use_kwh")
        if row.get("expected_export_kwh") is None and production is not None and self_use is not None:
            row["expected_export_kwh"] = max(production - self_use, 0)
            calculated["expected_export_kwh"] = "production_minus_self_use"
            row_warnings.append("financial_model_calculated_export")
        if row.get("expected_grid_import_kwh") is None and consumption is not None and self_use is not None:
            row["expected_grid_import_kwh"] = max(consumption - self_use, 0)
            calculated["expected_grid_import_kwh"] = "consumption_minus_self_use"
            row_warnings.append("financial_model_calculated_grid_import")
        row["expected_self_consumption_rate_pct"] = _ratio_pct(self_use, production)
        row["expected_self_sufficiency_rate_pct"] = _ratio_pct(self_use, consumption)
        row["calculated_fields"] = calculated
        row["warnings"] = row_warnings
        warnings.extend(row_warnings)
    return monthly, warnings, source_cells


def _parse_metric_rows(sheet_name: str, rows: list[tuple[Any, ...]]) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    for row_index, row in enumerate(rows, start=1):
        month_columns = {parse_month(cell.value): col for col, cell in enumerate(row, start=1) if parse_month(cell.value)}
        if len(month_columns) < 12:
            continue
        metrics: dict[str, dict[int, tuple[float, str, str | None]]] = {}
        for metric_row_index in range(row_index + 1, min(row_index + 12, len(rows)) + 1):
            metric_row = rows[metric_row_index - 1]
            metric = metric_key(metric_row[0].value if metric_row else None)
            if not metric:
                continue
            if metric in metrics:
                raise FinancialModelParseError("ambiguous_financial_model")
            metrics[metric] = {}
            unit = _unit_for_label(metric_row[0].value)
            for month, col in month_columns.items():
                cell = metric_row[col - 1]
                value = parse_number(cell.value)
                if value is None:
                    continue
                converted, conversion = _normalize_energy_value(value, metric, unit)
                metrics[metric][month] = (converted, cell_ref(sheet_name, metric_row_index, col), conversion)
        return _build_monthly_from_metrics(metrics)
    return None


def _parse_month_rows(sheet_name: str, rows: list[tuple[Any, ...]]) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    header_index = None
    headers: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        detected = {metric_key(cell.value): col for col, cell in enumerate(row, start=1) if metric_key(cell.value)}
        if len(detected) >= 1:
            header_index = index
            headers = {key: col for key, col in detected.items() if key}
            break
    if header_index is None:
        return None
    metrics = {key: {} for key in headers}
    for row_index in range(header_index + 1, len(rows) + 1):
        row = rows[row_index - 1]
        month = parse_month(row[0].value if row else None)
        if not month:
            continue
        for metric, col in headers.items():
            cell = row[col - 1]
            value = parse_number(cell.value)
            if value is None:
                continue
            metrics[metric][month] = (value, cell_ref(sheet_name, row_index, col), None)
    return _build_monthly_from_metrics(metrics)


def _build_monthly_from_metrics(metrics: dict[str, dict[int, tuple[float, str, str | None]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    monthly: list[dict[str, Any]] = []
    source_cells: dict[str, Any] = {"monthly": {}}
    mapping = {
        "production": "expected_production_kwh",
        "consumption": "expected_consumption_kwh",
        "self_use": "expected_self_use_kwh",
        "export": "expected_export_kwh",
        "grid_import": "expected_grid_import_kwh",
    }
    for month in range(1, 13):
        row: dict[str, Any] = {"month": month, "source_fields": {}}
        source_cells["monthly"][str(month)] = {}
        for metric, target in mapping.items():
            item = metrics.get(metric, {}).get(month)
            if item is None:
                row[target] = None
                continue
            value, ref, conversion = item
            row[target] = value
            row["source_fields"][target] = {"cell": ref, **({"conversion": conversion} if conversion else {})}
            source_cells["monthly"][str(month)][target] = ref
        monthly.append(row)
    return monthly, source_cells


def _unit_for_label(value: Any) -> str:
    raw = normalize_text(value)
    if "mwh" in raw:
        return "mwh"
    if "kwh" in raw or raw:
        return "kwh"
    return ""


def _normalize_energy_value(value: float, metric: str, unit: str) -> tuple[float, str | None]:
    if metric == "self_consumption_rate":
        return (value * 100 if value <= 1 else value), None
    if unit == "mwh":
        return value * 1000, "mwh_to_kwh"
    return value, None


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100


def _parse_metadata(workbook: Any) -> tuple[str, str, float | None, int | None, dict[str, Any]]:
    cells: dict[str, Any] = {}
    name = ""
    nif = ""
    kwp = None
    base_year = None
    if "UPAC" in workbook.sheetnames:
        sheet = workbook["UPAC"]
        name = str(sheet["A4"].value or "").strip()
        kwp = parse_number(sheet["D4"].value)
        cells.update({"detected_name": "UPAC!A4", "detected_kwp": "UPAC!D4"})
    if "Projeto" in workbook.sheetnames:
        sheet = workbook["Projeto"]
        name = name or str(sheet["C5"].value or "").strip()
        kwp = kwp if kwp is not None else parse_number(sheet["H8"].value)
        cells.update({"detected_name": cells.get("detected_name") or "Projeto!C5", "detected_kwp": cells.get("detected_kwp") or "Projeto!H8"})
    for sheet in workbook.worksheets[:5]:
        for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 30, 30), max_col=min(sheet.max_column or 12, 12), values_only=False):
            for index, cell in enumerate(row):
                label = normalize_text(cell.value)
                if not label:
                    continue
                next_value = row[index + 1].value if index + 1 < len(row) else None
                if not nif and label == "nif":
                    nif = re.sub(r"\D+", "", str(next_value or ""))
                    cells["detected_nif"] = cell_ref(sheet.title, cell.row, cell.column + 1)
                if base_year is None and ("ano base" in label or label in {"year", "ano"}):
                    parsed = parse_number(next_value)
                    base_year = int(parsed) if parsed else None
                    cells["base_year"] = cell_ref(sheet.title, cell.row, cell.column + 1)
    return name, nif, kwp, base_year, cells


def _parse_details(workbook: Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if "UPAC" in workbook.sheetnames:
        details["upac_summary"] = _parse_upac_summary(workbook["UPAC"])
        details["tariff_periods"] = _parse_upac_tariff_periods(workbook["UPAC"])
        details["electricity_costs"] = _parse_upac_electricity_costs(workbook["UPAC"])
    if "Data PV Proposal" in workbook.sheetnames:
        details["proposal_rows"] = _parse_proposal_rows(workbook["Data PV Proposal"])
    if "Detalhes da fatura" in workbook.sheetnames:
        invoice = _parse_invoice_details(workbook["Detalhes da fatura"])
        details.update(invoice)
    return {key: value for key, value in details.items() if value}


def _parse_upac_summary(sheet: Any) -> list[dict[str, Any]]:
    cells = (
        ("project_name", "Project name", "A4", ""),
        ("installed_capacity_kwp", "Installed capacity", "D4", "kWp"),
        ("selling_price_eur_kwp", "Selling price", "D5", "EUR/kWp"),
        ("selling_price_total_eur", "Selling price total", "D6", "EUR"),
        ("buying_price_eur_kwp", "Buying price", "D7", "EUR/kWp"),
        ("buying_price_total_eur", "Buying price total", "D8", "EUR"),
        ("contract_duration_years", "Contract duration", "D9", "years"),
        ("degradation_pct", "Degradation", "D11", "%"),
        ("tariff_scheme", "Tariff scheme", "H12", ""),
        ("omie_eur_kwh", "OMIE", "H19", "EUR/kWh"),
        ("annual_consumption_kwh", "Consumption", "K4", "kWh"),
        ("annual_pv_production_kwh", "PV production", "K5", "kWh"),
        ("annual_grid_import_kwh", "Buy from grid", "K6", "kWh"),
        ("annual_self_consumption_kwh", "Self-consumption", "K7", "kWh"),
        ("annual_feed_in_kwh", "Feed-in", "K8", "kWh"),
        ("self_consumption_rate_pct", "Self-consumption rate", "K9", "%"),
        ("self_sufficiency_rate_pct", "Self-sufficiency rate", "K10", "%"),
        ("specific_yield_kwh_kwp", "Specific yield", "K11", "kWh/kWp"),
        ("self_consumption_value_eur_kwh", "Value self-consumption", "K14", "EUR/kWh"),
        ("client_charge_eur_kwh", "Charge client", "K15", "EUR/kWh"),
        ("all_electricity_value_eur_kwh", "Value all electricity", "K19", "EUR/kWh"),
        ("all_electricity_charge_eur_kwh", "Charge client all electricity", "K20", "EUR/kWh"),
        ("savings_year_1_eur", "Savings year 1", "N4", "EUR"),
        ("savings_during_eur", "Savings during", "N6", "EUR"),
        ("savings_after_eur", "Savings after", "N7", "EUR"),
        ("npv_client_eur", "NPV client", "N9", "EUR"),
        ("iberdrola_eur", "Iberdrola", "N11", "EUR"),
        ("solcor_eur", "Solcor", "N12", "EUR"),
        ("current_energy_cost_eur", "Current cost energy", "Q3", "EUR"),
        ("current_power_cost_eur", "Current cost power", "Q4", "EUR"),
        ("current_total_cost_eur", "Current total cost", "Q5", "EUR"),
        ("savings_cost_ratio_pct", "Savings/cost ratio", "Q7", "%"),
        ("co2_ton_year", "CO2", "Q11", "ton/year"),
        ("trees", "Trees", "Q12", ""),
        ("km_year", "km/year", "Q13", ""),
    )
    return [_detail_item(key, label, sheet[cell].value, cell_ref(sheet.title, sheet[cell].row, sheet[cell].column), unit) for key, label, cell, unit in cells if sheet[cell].value not in (None, "")]


def _parse_upac_tariff_periods(sheet: Any) -> list[dict[str, Any]]:
    rows = []
    for row_index in range(13, 20):
        label = sheet.cell(row_index, 7).value
        value = sheet.cell(row_index, 8).value
        if label not in (None, "") and value not in (None, ""):
            rows.append(_detail_item(normalize_text(label).replace(" ", "_"), str(label), value, cell_ref(sheet.title, row_index, 8), "EUR/kWh"))
    return rows


def _parse_upac_electricity_costs(sheet: Any) -> list[dict[str, Any]]:
    rows = []
    for row_index in range(16, 20):
        label = sheet.cell(row_index, 13).value
        energy = sheet.cell(row_index, 14).value
        network = sheet.cell(row_index, 15).value
        if label in (None, ""):
            continue
        rows.append(
            {
                "period": str(label),
                "energy_eur_kwh": parse_number(energy),
                "network_eur_kwh": parse_number(network),
                "source_cells": {
                    "energy_eur_kwh": cell_ref(sheet.title, row_index, 14),
                    "network_eur_kwh": cell_ref(sheet.title, row_index, 15),
                },
            }
        )
    return rows


def _parse_proposal_rows(sheet: Any) -> list[dict[str, Any]]:
    rows = []
    month_columns = list(range(3, 15))
    for row_index in range(1, min(sheet.max_row or 0, 90) + 1):
        label = sheet.cell(row_index, 2).value
        if label in (None, ""):
            continue
        month_values = [parse_number(sheet.cell(row_index, column).value) for column in month_columns]
        annual = parse_number(sheet.cell(row_index, 16).value)
        if not any(value is not None for value in month_values) and annual is None:
            continue
        rows.append(
            {
                "label": str(label),
                "monthly": month_values,
                "annual": annual,
                "source_row": row_index,
            }
        )
    return rows


def _parse_invoice_details(sheet: Any) -> dict[str, Any]:
    periods = []
    for row_index in range(3, min(sheet.max_row or 0, 8) + 1):
        label = sheet.cell(row_index, 2).value
        if label in (None, "") or normalize_text(label) == "grand total":
            continue
        periods.append(
            {
                "period": str(label),
                "self_consumption_kwh": parse_number(sheet.cell(row_index, 3).value),
                "savings_energy_eur": parse_number(sheet.cell(row_index, 4).value),
                "share_pct": _as_pct(parse_number(sheet.cell(row_index, 5).value)),
                "source_row": row_index,
            }
        )
    prices = []
    for row_index in range(3, min(sheet.max_row or 0, 11) + 1):
        label = sheet.cell(row_index, 6).value
        if label in (None, ""):
            continue
        prices.append(
            {
                "label": str(label),
                "energy_kwh": parse_number(sheet.cell(row_index, 7).value),
                "energy_eur_kwh": parse_number(sheet.cell(row_index, 8).value),
                "network_eur_kwh": parse_number(sheet.cell(row_index, 9).value),
                "energy_cost_eur": parse_number(sheet.cell(row_index, 11).value),
                "network_cost_eur": parse_number(sheet.cell(row_index, 12).value),
                "source_row": row_index,
            }
        )
    totals = []
    for label, row_index, column, unit in (
        ("Savings energy total", 7, 4, "EUR"),
        ("Peak power savings", 8, 9, "EUR"),
        ("Revenue surplus", 10, 9, "EUR"),
        ("Total benefit", 12, 7, "EUR"),
        ("Value", 13, 7, "EUR/kWh"),
    ):
        value = sheet.cell(row_index, column).value
        if value not in (None, ""):
            totals.append(_detail_item(normalize_text(label).replace(" ", "_"), label, value, cell_ref(sheet.title, row_index, column), unit))
    return {
        "invoice_periods": periods,
        "invoice_prices": prices,
        "invoice_totals": totals,
    }


def _detail_item(key: str, label: str, value: Any, source_cell: str, unit: str) -> dict[str, Any]:
    parsed = parse_number(value)
    if parsed is not None and unit == "%":
        parsed = _as_pct(parsed)
    return {
        "key": key,
        "label": label,
        "value": parsed if parsed is not None else str(value),
        "unit": unit,
        "source_cell": source_cell,
    }


def _as_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100 if abs(value) <= 1 else value
