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
    "production": ("pv production", "production", "producao", "producao pv", "producao fotovoltaica", "producao solar"),
    "consumption": ("consumption", "consumo"),
    "self_use": ("self-consumption", "self consumption", "self-c", "autoconsumo"),
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
        return ParsedFinancialModel(
            detected_name=detected_name,
            detected_nif=detected_nif,
            detected_kwp=detected_kwp,
            base_year=base_year,
            sheet_name=sheet.title,
            monthly=tuple(monthly),
            warnings=tuple(sorted(set(warnings))),
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
                if base_year is None and "ano base" in label:
                    parsed = parse_number(next_value)
                    base_year = int(parsed) if parsed else None
                    cells["base_year"] = cell_ref(sheet.title, cell.row, cell.column + 1)
    return name, nif, kwp, base_year, cells
