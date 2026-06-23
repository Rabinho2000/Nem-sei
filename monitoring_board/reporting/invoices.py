from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from monitoring_board.reporting.models import (
    InvoiceCandidate,
    InvoiceExtractionResult,
    InvoiceExtractionStatus,
    InvoiceStatus,
    InvoiceTariffCandidate,
    InvoiceValidationResult,
    TariffType,
)


PARSER_NAME = "heuristic_invoice_parser"
PARSER_VERSION = "1"
SUPPORTED_EXTENSIONS = {".pdf", ".xlsx", ".xlsm", ".csv", ".txt"}
FORBIDDEN_EXTENSIONS = {".exe", ".bat", ".cmd", ".ps1", ".sh", ".js", ".vbs", ".com", ".scr", ".msi"}
PRICE_FIELDS = {
    "simple_price_eur_kwh",
    "ponta_price_eur_kwh",
    "cheia_price_eur_kwh",
    "vazio_price_eur_kwh",
    "super_vazio_price_eur_kwh",
}


def normalize_nif(value: Any) -> str:
    raw = re.sub(r"\D+", "", str(value or "").upper().removeprefix("PT"))
    return raw


def is_valid_portuguese_nif(value: Any) -> bool:
    nif = normalize_nif(value)
    if len(nif) != 9 or not nif.isdigit():
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(nif[:8], range(9, 1, -1), strict=True))
    check_digit = 11 - (checksum % 11)
    if check_digit >= 10:
        check_digit = 0
    return check_digit == int(nif[-1])


def normalize_decimal(value: Any) -> Decimal:
    if value is None or str(value).strip() == "":
        raise ValueError("missing_decimal")
    raw = str(value).strip().replace("€", "").replace("EUR", "").replace("eur", "")
    raw = re.sub(r"\s+", "", raw)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_decimal") from exc
    if not parsed.is_finite():
        raise ValueError("invalid_decimal")
    return parsed


def normalize_date(value: Any) -> date:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError("invalid_date")


def candidate_map(candidates: tuple[InvoiceCandidate, ...]) -> dict[str, InvoiceCandidate]:
    return {candidate.field_name: candidate for candidate in candidates}


def _candidate_value(candidates: dict[str, InvoiceCandidate], field_name: str) -> str | None:
    candidate = candidates.get(field_name)
    return candidate.value if candidate else None


def infer_tariff_type(values: dict[str, Any]) -> TariffType | None:
    present = {field for field in PRICE_FIELDS if values.get(field) not in (None, "")}
    if "simple_price_eur_kwh" in present and len(present) == 1:
        return TariffType.SIMPLE
    if {"cheia_price_eur_kwh", "vazio_price_eur_kwh"}.issubset(present) and not {"ponta_price_eur_kwh", "super_vazio_price_eur_kwh"} & present:
        return TariffType.BI_HOURLY
    if {"ponta_price_eur_kwh", "cheia_price_eur_kwh", "vazio_price_eur_kwh"}.issubset(present) and "super_vazio_price_eur_kwh" not in present:
        return TariffType.TRI_HOURLY
    if {"ponta_price_eur_kwh", "cheia_price_eur_kwh", "vazio_price_eur_kwh", "super_vazio_price_eur_kwh"}.issubset(present):
        return TariffType.TETRA_HOURLY
    return None


def build_tariff_candidate(candidates: tuple[InvoiceCandidate, ...]) -> InvoiceTariffCandidate:
    values = {candidate.field_name: candidate.value for candidate in candidates}
    tariff_type = infer_tariff_type(values)

    def price(field_name: str) -> Decimal | None:
        value = values.get(field_name)
        if value in (None, ""):
            return None
        return normalize_decimal(value)

    return InvoiceTariffCandidate(
        tariff_type=tariff_type,
        simple_price_eur_kwh=price("simple_price_eur_kwh"),
        ponta_price_eur_kwh=price("ponta_price_eur_kwh"),
        cheia_price_eur_kwh=price("cheia_price_eur_kwh"),
        vazio_price_eur_kwh=price("vazio_price_eur_kwh"),
        super_vazio_price_eur_kwh=price("super_vazio_price_eur_kwh"),
    )


def confidence_from_candidates(candidates: tuple[InvoiceCandidate, ...]) -> Decimal:
    if not candidates:
        return Decimal("0")
    total = sum((candidate.confidence for candidate in candidates), Decimal("0"))
    return (total / Decimal(len(candidates))).quantize(Decimal("0.01"))


def validate_invoice_values(values: dict[str, Any], *, asset_nif: str | None = None) -> InvoiceValidationResult:
    warnings: list[str] = []
    errors: list[str] = []
    normalized_asset_nif = normalize_nif(asset_nif)
    customer_nif = normalize_nif(values.get("customer_nif"))
    if not values.get("invoice_number"):
        warnings.append("missing_invoice_number")
    if not values.get("issue_date"):
        warnings.append("missing_issue_date")
    if not values.get("billing_period_start") or not values.get("billing_period_end"):
        warnings.append("missing_billing_period")
    if not customer_nif:
        warnings.append("missing_customer_nif")
    elif not is_valid_portuguese_nif(customer_nif):
        warnings.append("invalid_customer_nif")
    elif normalized_asset_nif and customer_nif != normalized_asset_nif:
        warnings.append("customer_nif_mismatch")
    elif not normalized_asset_nif:
        warnings.append("missing_asset_nif")
    try:
        if values.get("billing_period_start") and values.get("billing_period_end"):
            start = normalize_date(values["billing_period_start"])
            end = normalize_date(values["billing_period_end"])
            if end < start:
                errors.append("invalid_billing_period")
    except ValueError:
        errors.append("invalid_billing_period")
    for field_name in ("total_amount", "total_energy_kwh", *PRICE_FIELDS):
        raw = values.get(field_name)
        if raw in (None, ""):
            continue
        try:
            parsed = normalize_decimal(raw)
        except ValueError:
            errors.append(f"invalid_{field_name}")
            continue
        if parsed < 0:
            errors.append(f"negative_{field_name}")
    currency = str(values.get("currency") or "EUR").upper()
    if currency not in {"", "EUR"}:
        warnings.append("unexpected_currency")
    tariff_type = infer_tariff_type(values)
    if any(values.get(field) not in (None, "") for field in PRICE_FIELDS) and tariff_type is None:
        warnings.append("invoice_values_incomplete")
    status = InvoiceStatus.REVIEW_REQUIRED if warnings or errors else InvoiceStatus.EXTRACTED
    return InvoiceValidationResult(valid=not errors, status=status, warnings=tuple(sorted(set(warnings))), errors=tuple(sorted(set(errors))))


def extraction_result_from_candidates(
    *,
    method: str,
    candidates: tuple[InvoiceCandidate, ...],
    warnings: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
) -> InvoiceExtractionResult:
    tariff_candidate = build_tariff_candidate(candidates)
    confidence = confidence_from_candidates(candidates)
    status = InvoiceExtractionStatus.EXTRACTION_FAILED if errors else InvoiceExtractionStatus.REVIEW_REQUIRED
    if not errors and candidates and confidence >= Decimal("0.75") and not warnings:
        status = InvoiceExtractionStatus.EXTRACTED
    return InvoiceExtractionResult(
        method=method,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        status=status,
        candidates=candidates,
        tariff_candidate=tariff_candidate,
        confidence=confidence,
        warnings=tuple(sorted(set(warnings))),
        errors=tuple(sorted(set(errors))),
        requires_review=True,
    )


def is_supported_invoice_extension(filename: str) -> bool:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return suffix in SUPPORTED_EXTENSIONS and suffix not in FORBIDDEN_EXTENSIONS


def decimal_to_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def finite_confidence(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if not parsed.is_finite() or math.isnan(float(parsed)):
        return Decimal("0")
    return min(max(parsed, Decimal("0")), Decimal("1"))
