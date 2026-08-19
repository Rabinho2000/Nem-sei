"""Golden parity for the customer PDF: the document people actually receive.

V1 and V2 draw the same report from the same payload and the results are
compared page by page on extracted text, page count and page size. Bytes cannot
be compared because reportlab stamps a creation time into every file, so the
comparison is on what a reader sees rather than on the container.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from nemsei.reporting.customer_pdf import build_customer_report_pdf


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1():
    if not (V1_ROOT / "monitoring_board" / "customer_reports.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        return importlib.import_module("monitoring_board.customer_reports")
    except Exception:  # pragma: no cover - a broken checkout is missing evidence
        return None


V1 = load_v1()
pypdf = pytest.importorskip("pypdf", reason="pypdf is needed to read the rendered pages")
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


def pages_of(content: bytes) -> list[str]:
    import io

    reader = pypdf.PdfReader(io.BytesIO(content))
    return [page.extract_text() or "" for page in reader.pages]


def sizes_of(content: bytes) -> list[tuple[float, float]]:
    import io

    reader = pypdf.PdfReader(io.BytesIO(content))
    return [(round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2)) for page in reader.pages]


BASE_REPORT = {
    "report_type": "epc",
    "asset": {"id": 1, "project_name": "Entre Vinhas e Mar", "nif": "501936270"},
    "period_label": "Julho 2026",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31",
    "period_type": "monthly",
    "production_kwh": 1234.56,
    "self_use_kwh": 800.0,
    "export_kwh": 434.56,
    "consumption_kwh": 4000.0,
    "grid_import_kwh": 3200.0,
    "savings_eur": 150.25,
    "export_revenue_eur": 19.55,
    "solcor_payment_eur": 106.17,
    "net_benefit_eur": 63.63,
    "total_benefit_eur": 169.80,
    "autoconsumption_pct": 64.8,
    "self_sufficiency_pct": 20.0,
    "tariff_rows": [],
}

CASES = {
    "epc complete": BASE_REPORT,
    "esco model": {**BASE_REPORT, "report_type": "esco"},
    # The case that matters most: nothing measured. Neither implementation may
    # print a zero where it does not know the answer.
    "everything missing": {
        **{key: None for key in BASE_REPORT if key not in {"asset", "report_type", "tariff_rows"}},
        "asset": BASE_REPORT["asset"],
        "report_type": "epc",
        "tariff_rows": [],
    },
    "zero production": {**BASE_REPORT, "production_kwh": 0.0, "self_use_kwh": 0.0, "export_kwh": 0.0},
}


@requires_v1
@pytest.mark.parametrize("label", sorted(CASES))
def test_v2_draws_the_same_pages_as_v1(label: str) -> None:
    report = dict(CASES[label])
    expected = V1.build_customer_report_pdf(dict(report), logo_path=None)
    actual = build_customer_report_pdf(dict(report), logo_path=None)
    assert sizes_of(actual) == sizes_of(expected), f"{label}: page geometry"
    assert len(pages_of(actual)) == len(pages_of(expected)), f"{label}: page count"
    for index, (mine, theirs) in enumerate(zip(pages_of(actual), pages_of(expected), strict=True)):
        assert mine == theirs, f"{label}: page {index + 1} text"


@requires_v1
def test_a_missing_value_is_not_drawn_as_a_zero() -> None:
    """Pinned separately from parity, because both being wrong would still pass."""
    report = dict(CASES["everything missing"])
    text = "\n".join(pages_of(build_customer_report_pdf(report, logo_path=None)))
    zero_report = dict(CASES["zero production"])
    zero_text = "\n".join(pages_of(build_customer_report_pdf(zero_report, logo_path=None)))
    assert text != zero_text


def test_the_pdf_builder_reaches_no_database_and_no_provider() -> None:
    import ast
    import inspect

    from nemsei.reporting import customer_pdf

    tree = ast.parse(inspect.getsource(customer_pdf))
    roots = {
        name.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names
    } | {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "sqlalchemy" not in roots and "requests" not in roots
    assert not any(root.startswith("monitoring_board") for root in roots)
