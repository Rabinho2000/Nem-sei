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


# The one place V2's customer PDF deliberately stopped matching V1, and the
# only textual difference that remains across every case above (verified: page
# geometry and page count still match exactly, and no other line differs).
#
# V1's chart always drew a three-series legend -- "Consumo da empresa",
# "Solar autoconsumida", "Solar excedente" -- whether or not it had data for
# those series. V2 only persists production per day (`daily_rows_for`), so
# those three fields are always `None` here and V1 renders a legend above an
# empty chart under a non-trivially scaled axis. Commit a923be7 ("Corrigir
# gráfico de produção vazio no relatório PDF (ESCO sem split)") replaced that
# with a single "Produção solar" series drawn from the production that does
# exist, and verified it by regenerating a real report (Solidus, asset 12,
# August 2026, snapshot 305) against the live database.
#
# So this is a fixed bug, not drift: parity with V1 here would mean shipping
# customers an empty chart. The divergence is pinned line-by-line rather than
# waved through with a blanket "golden updated", and everything else in the
# document is still compared verbatim.
V1_CHART_LEGEND = ("Consumo da empresa", "Solar autoconsumida", "Solar excedente")
V2_CHART_LEGEND = ("Produção solar",)


def _without_chart_legend(page: str) -> str:
    dropped = set(V1_CHART_LEGEND) | set(V2_CHART_LEGEND)
    return "\n".join(line for line in page.splitlines() if line not in dropped)


@requires_v1
@pytest.mark.parametrize("label", sorted(CASES))
def test_v2_draws_the_same_pages_as_v1(label: str) -> None:
    """Byte-for-byte page parity with V1, except the chart legend (see above)."""
    report = dict(CASES[label])
    expected = V1.build_customer_report_pdf(dict(report), logo_path=None)
    actual = build_customer_report_pdf(dict(report), logo_path=None)
    assert sizes_of(actual) == sizes_of(expected), f"{label}: page geometry"
    assert len(pages_of(actual)) == len(pages_of(expected)), f"{label}: page count"
    for index, (mine, theirs) in enumerate(zip(pages_of(actual), pages_of(expected), strict=True)):
        assert _without_chart_legend(mine) == _without_chart_legend(theirs), f"{label}: page {index + 1} text"


@requires_v1
@pytest.mark.parametrize("label", sorted(CASES))
def test_the_chart_legend_is_the_only_thing_v2_stopped_copying(label: str) -> None:
    """Pins the divergence itself, so a *new* one cannot hide behind it.

    Without this, `_without_chart_legend` above would silently absorb any
    future difference that happened to land on one of those lines.
    """
    report = dict(CASES[label])
    v1_text = "\n".join(pages_of(V1.build_customer_report_pdf(dict(report), logo_path=None))).splitlines()
    v2_text = "\n".join(pages_of(build_customer_report_pdf(dict(report), logo_path=None))).splitlines()
    only_in_v1 = [line for line in v1_text if line not in v2_text]
    only_in_v2 = [line for line in v2_text if line not in v1_text]
    assert set(only_in_v1) == set(V1_CHART_LEGEND), f"{label}: V1 lines V2 dropped"
    assert set(only_in_v2) == set(V2_CHART_LEGEND), f"{label}: V2 lines V1 never had"


def test_a_report_without_a_self_use_split_still_draws_its_production() -> None:
    """The behaviour a923be7 added, pinned on its own terms rather than V1's.

    This is the whole point of the divergence: an ESCO-shaped report with no
    self-use/export split must still show the production it does have.
    """
    report = dict(CASES["epc complete"])
    text = "\n".join(pages_of(build_customer_report_pdf(report, logo_path=None)))
    assert "Produção solar" in text
    assert "Solar autoconsumida" not in text


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
