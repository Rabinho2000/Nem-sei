from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from monitoring_board.reporting.invoices import (
    extraction_result_from_candidates,
    is_supported_invoice_extension,
    normalize_decimal,
    normalize_nif,
)
from monitoring_board.reporting.models import InvoiceCandidate, InvoiceExtractionResult


@dataclass(frozen=True)
class InvoiceFileReadResult:
    text: str
    method: str
    warnings: tuple[str, ...] = ()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_invoice_file(path: Path) -> InvoiceExtractionResult:
    if not is_supported_invoice_extension(path.name):
        return extraction_result_from_candidates(method="unsupported", candidates=(), warnings=("unsupported_invoice_format",), errors=("unsupported_invoice_format",))
    read = read_invoice_text(path)
    if not read.text.strip():
        return extraction_result_from_candidates(method=read.method, candidates=(), warnings=(*read.warnings, "invoice_requires_manual_review"))
    candidates = tuple(extract_candidates_from_text(read.text, source=read.method))
    warnings = list(read.warnings)
    if not candidates:
        warnings.append("invoice_values_incomplete")
    return extraction_result_from_candidates(method=read.method, candidates=candidates, warnings=tuple(warnings))


def read_invoice_text(path: Path) -> InvoiceFileReadResult:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_text(path)
    if suffix in {".xlsx", ".xlsm"}:
        return read_excel_text(path)
    if suffix == ".csv":
        return read_csv_text(path)
    if suffix == ".txt":
        return InvoiceFileReadResult(path.read_text(encoding="utf-8", errors="replace"), "txt")
    return InvoiceFileReadResult("", "unsupported", ("unsupported_invoice_format",))


def read_pdf_text(path: Path) -> InvoiceFileReadResult:
    try:
        from pypdf import PdfReader
    except ImportError:
        return InvoiceFileReadResult("", "pdf", ("unsupported_invoice_format",))
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page_index, page in enumerate(reader.pages[:8], start=1):
        text = page.extract_text() or ""
        if text:
            parts.append(f"[page {page_index}]\n{text}")
    if not parts:
        return InvoiceFileReadResult("", "pdf", ("scanned_pdf_requires_manual_review",))
    return InvoiceFileReadResult("\n".join(parts), "pdf")


def read_excel_text(path: Path) -> InvoiceFileReadResult:
    workbook = load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    parts: list[str] = []
    for sheet in workbook.worksheets[:4]:
        for row in sheet.iter_rows(max_row=200, max_col=20, values_only=True):
            values = [str(value).strip() for value in row if value not in (None, "")]
            if values:
                parts.append(f"{sheet.title}: " + " | ".join(values))
    workbook.close()
    return InvoiceFileReadResult("\n".join(parts), "excel")


def read_csv_text(path: Path) -> InvoiceFileReadResult:
    content = path.read_text(encoding="utf-8", errors="replace")
    sample = content[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows: list[str] = []
    for index, row in enumerate(csv.reader(content.splitlines(), dialect), start=1):
        if index > 300:
            break
        if any(cell.strip() for cell in row):
            rows.append(" | ".join(cell.strip() for cell in row))
    return InvoiceFileReadResult("\n".join(rows), "csv")


def extract_candidates_from_text(text: str, *, source: str) -> list[InvoiceCandidate]:
    normalized = re.sub(r"[ \t]+", " ", text.replace("\r", "\n"))
    candidates: list[InvoiceCandidate] = []
    add_regex_candidate(candidates, "invoice_number", normalized, r"(?:Fatura|Factura|Invoice)\s*(?:n[.ºo]*|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-.]{2,})", "0.86", source)
    add_regex_candidate(candidates, "supplier_nif", normalized, r"(?:Fornecedor|Supplier|Emitente).*?(?:NIF|VAT)\s*[:\-]?\s*(PT?\s*\d[\d\s\-]{7,})", "0.72", source)
    nifs = re.findall(r"(?:NIF|VAT)\s*[:\-]?\s*(PT?\s*\d[\d\s\-]{7,})", normalized, flags=re.I | re.S)
    if nifs:
        candidates.append(InvoiceCandidate("customer_nif", normalize_nif(nifs[-1]), Decimal("0.82"), short_evidence(f"NIF: {nifs[-1]}"), source))
        if len(nifs) > 1:
            candidates.append(InvoiceCandidate("supplier_nif", normalize_nif(nifs[0]), Decimal("0.78"), short_evidence(f"NIF: {nifs[0]}"), source))
    add_regex_candidate(candidates, "issue_date", normalized, r"(?:Data de emiss[aã]o|Issue date|Data)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}|\d{4}-\d{2}-\d{2})", "0.80", source)
    period = re.search(r"(?:Per[ií]odo(?: de fatura[cç][aã]o)?|Billing period)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}|\d{4}-\d{2}-\d{2})\s*(?:a|até|-|to)\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}|\d{4}-\d{2}-\d{2})", normalized, flags=re.I)
    if period:
        candidates.append(InvoiceCandidate("billing_period_start", period.group(1), Decimal("0.84"), short_evidence(period.group(0)), source))
        candidates.append(InvoiceCandidate("billing_period_end", period.group(2), Decimal("0.84"), short_evidence(period.group(0)), source))
    add_decimal_candidate(candidates, "total_amount", normalized, r"(?:Total(?: da fatura)?|Total amount)\s*[:\-]?\s*([0-9][0-9\s.,]*)\s*(?:€|EUR)", "0.76", source)
    add_decimal_candidate(candidates, "total_energy_kwh", normalized, r"(?:Energia ativa|Consumo|Total energia).*?([0-9][0-9\s.,]*)\s*kWh", "0.68", source)
    price_patterns = {
        "ponta_price_eur_kwh": r"(?:Ponta).*?([0-9]+[,.][0-9]+)\s*(?:EUR\/kWh|€\/kWh|eur\/kWh)?",
        "cheia_price_eur_kwh": r"(?:Cheias?|Fora de vazio).*?([0-9]+[,.][0-9]+)\s*(?:EUR\/kWh|€\/kWh|eur\/kWh)?",
        "vazio_price_eur_kwh": r"(?:Vazio).*?([0-9]+[,.][0-9]+)\s*(?:EUR\/kWh|€\/kWh|eur\/kWh)?",
        "super_vazio_price_eur_kwh": r"(?:Super vazio).*?([0-9]+[,.][0-9]+)\s*(?:EUR\/kWh|€\/kWh|eur\/kWh)?",
        "simple_price_eur_kwh": r"(?:Pre[cç]o unit[aá]rio|Energia ativa).*?([0-9]+[,.][0-9]+)\s*(?:EUR\/kWh|€\/kWh|eur\/kWh)",
    }
    for field_name, pattern in price_patterns.items():
        add_decimal_candidate(candidates, field_name, normalized, pattern, "0.74", source)
    return dedupe_candidates(candidates)


def add_regex_candidate(candidates: list[InvoiceCandidate], field_name: str, text: str, pattern: str, confidence: str, source: str) -> None:
    match = re.search(pattern, text, flags=re.I | re.S)
    if match:
        candidates.append(InvoiceCandidate(field_name, match.group(1).strip(), Decimal(confidence), short_evidence(match.group(0)), source))


def add_decimal_candidate(candidates: list[InvoiceCandidate], field_name: str, text: str, pattern: str, confidence: str, source: str) -> None:
    match = re.search(pattern, text, flags=re.I | re.S)
    if not match:
        return
    try:
        value = normalize_decimal(match.group(1))
    except ValueError:
        return
    candidates.append(InvoiceCandidate(field_name, str(value), Decimal(confidence), short_evidence(match.group(0)), source))


def short_evidence(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:160]


def dedupe_candidates(candidates: list[InvoiceCandidate]) -> list[InvoiceCandidate]:
    best: dict[str, InvoiceCandidate] = {}
    for candidate in candidates:
        current = best.get(candidate.field_name)
        if current is None or candidate.confidence > current.confidence:
            best[candidate.field_name] = candidate
    return list(best.values())
