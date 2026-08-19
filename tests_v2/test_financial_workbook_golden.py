"""Golden parity: V2 must reproduce V1's parse of the real customer workbook.

The evidence lives outside the repository, because a 16 MB customer workbook and
its financial contents do not belong in Git. The test reads the frozen V1
database read-only and the stored workbook read-only, and skips when either is
absent, so CI stays green while the server run stays honest.

Nothing here writes to V1.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nemsei.reporting.financial_workbook import PARSER_NAME, PARSER_VERSION, parse_financial_model_workbook


V1_ROOT = Path("/opt/server/apps/Nem-sei")
V1_DATABASE = V1_ROOT / "data/monitoring_board.db"
V1_UPLOADS = V1_ROOT / "data/uploads"
GOLDEN_SHA256 = "734ecd4bc6a26fb98a343a958e33ee692a6d58d641515b012d3204ad3867c563"


def v1_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{V1_DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def golden_case():
    """The confirmed V1 model whose workbook is still on disk."""
    if not V1_DATABASE.is_file():
        return None
    try:
        with v1_connection() as connection:
            row = connection.execute(
                "SELECT m.id, m.base_year, m.detected_name, m.detected_kwp, m.warnings_json, m.details_json,"
                "       m.parser_name, m.parser_version, s.stored_path, s.sha256"
                "  FROM financial_models m JOIN source_files s ON s.id = m.source_file_id"
                " WHERE s.sha256 = ?",
                (GOLDEN_SHA256,),
            ).fetchone()
    except sqlite3.Error:
        # A live WAL database is unreadable from a read-only mount; that is a
        # missing-evidence condition, not a failure of the parser under test.
        return None
    if row is None:
        return None
    workbook = V1_UPLOADS / str(row["stored_path"]).split("uploads/", 1)[-1]
    return (row, workbook) if workbook.is_file() else None


CASE = golden_case()
requires_golden = pytest.mark.skipif(CASE is None, reason="the real V1 workbook and database are not available here")


@requires_golden
def test_v2_reads_the_same_identity_and_parser_contract() -> None:
    row, workbook = CASE
    parsed = parse_financial_model_workbook(workbook)
    assert parsed.detected_name == row["detected_name"]
    assert parsed.detected_kwp == pytest.approx(row["detected_kwp"])
    # V1 recorded 2026 here because an operator overrode the workbook's own year.
    assert parsed.base_year == 2025
    assert parsed.sheet_name == "Projeto"
    assert (parsed.parser_name, parsed.parser_version) == (PARSER_NAME, PARSER_VERSION)
    assert (parsed.parser_name, parsed.parser_version) == (row["parser_name"], row["parser_version"])


@requires_golden
def test_v2_reproduces_every_monthly_value_and_its_source_cell() -> None:
    row, workbook = CASE
    parsed = parse_financial_model_workbook(workbook)
    with v1_connection() as connection:
        stored = {
            int(month["month"]): month
            for month in connection.execute(
                "SELECT * FROM financial_model_monthly WHERE financial_model_id = ? ORDER BY month",
                (row["id"],),
            )
        }
    assert len(parsed.monthly) == 12 and set(stored) == set(range(1, 13))

    fields = (
        "expected_production_kwh",
        "expected_consumption_kwh",
        "expected_self_use_kwh",
        "expected_export_kwh",
        "expected_grid_import_kwh",
    )
    for entry in parsed.monthly:
        month = int(entry["month"])
        reference = stored[month]
        for field in fields:
            expected = reference[field]
            actual = entry.get(field)
            if expected is None:
                # A missing value must stay missing; it is never a zero.
                assert actual is None, f"month {month} {field} invented a value"
            else:
                assert actual == pytest.approx(expected, rel=1e-9), f"month {month} {field}"
        # Provenance must survive too: same source cell, same derivation rule.
        assert entry["source_fields"] == json.loads(reference["source_fields_json"])
        assert entry.get("calculated_fields", {}) == json.loads(reference["calculated_fields_json"])


@requires_golden
def test_v2_reproduces_the_warnings_and_the_detail_payload() -> None:
    row, workbook = CASE
    parsed = parse_financial_model_workbook(workbook)
    assert sorted(parsed.warnings) == sorted(json.loads(row["warnings_json"]))
    details = json.loads(row["details_json"])
    assert parsed.details["format"] == details["format"] == "financial_automatic_as_sold"
    for section in ("upac_summary", "tariff_periods", "electricity_costs", "invoice_periods", "invoice_prices", "invoice_totals"):
        assert parsed.details[section] == details[section], f"section {section} diverged"


@requires_golden
def test_the_workbook_is_only_ever_read() -> None:
    import hashlib

    _, workbook = CASE
    digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    parse_financial_model_workbook(workbook)
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == digest == GOLDEN_SHA256
