"""Golden parity for the quality rules that decide whether a report may ship.

These are the rules that stop a wrong number reaching a customer, so V2 must
reach exactly V1's verdict, including which findings it raises and in what
order. Loaded dynamically from the frozen V1, and skipped when it is absent.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from nemsei.reporting.rules import data_quality as v2_data_quality
from nemsei.reporting.rules import quality_gate as v2_quality_gate
from nemsei.reporting.rules import validation as v2_validation


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1() -> ModuleType | None:
    if not (V1_ROOT / "monitoring_board" / "reporting" / "quality_gate.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        return type(
            "V1",
            (),
            {name: importlib.import_module(f"monitoring_board.reporting.{name}") for name in ("quality_gate", "data_quality", "validation")},
        )
    except Exception:  # pragma: no cover - a broken checkout is missing evidence
        return None


V1 = load_v1()
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


@requires_v1
@pytest.mark.parametrize("monthly", [0.0, 1.0, 100.0, 1234.5, 50000.0, 1_000_000.0])
def test_daily_tolerance_matches_v1(monthly: float) -> None:
    """The tolerance decides what counts as a suspicious day; drift here is silent."""
    assert v2_data_quality.monthly_daily_tolerance_kwh(monthly) == V1.data_quality.monthly_daily_tolerance_kwh(monthly)


def daily_rows(values: list[float | None], year: int = 2026, month: int = 7) -> list[dict]:
    return [
        {"record_date": date(year, month, index + 1).isoformat(), "production_kwh": value}
        for index, value in enumerate(values)
        if value is not None
    ]


QUALITY_CASES = {
    "complete month": [10.0] * 31,
    "one missing day": [10.0] * 15 + [None] + [10.0] * 15,
    "several missing days": [10.0] * 10 + [None] * 5 + [10.0] * 16,
    "all zero": [0.0] * 31,
    "empty": [],
    "one day only": [10.0],
    "negative value": [10.0] * 30 + [-5.0],
}


@requires_v1
@pytest.mark.parametrize("label", sorted(QUALITY_CASES))
@pytest.mark.parametrize("reference", [date(2026, 7, 15), date(2026, 8, 1)])
def test_monthly_production_quality_matches_v1(label: str, reference: date) -> None:
    """Mid-month and after month close are different verdicts; both must agree."""
    values = QUALITY_CASES[label]
    daily = daily_rows(values)
    monthly = [{"period_date": "2026-07-01", "production_kwh": sum(v for v in values if v)}] if values else []
    kwargs = dict(
        asset_id=1,
        month_start=date(2026, 7, 1),
        reference_date=reference,
        monthly_records=monthly,
        daily_records=daily,
    )
    expected = V1.data_quality.evaluate_monthly_production_quality(**kwargs)
    actual = v2_data_quality.evaluate_monthly_production_quality(**kwargs)
    assert asdict(actual) == asdict(expected), label
    assert v2_data_quality.production_quality_notice(actual) == V1.data_quality.production_quality_notice(expected)


@requires_v1
@pytest.mark.parametrize("scope", ["asset", "portfolio"])
@pytest.mark.parametrize("requires_expected_production", [False, True])
def test_quality_gate_reaches_the_same_verdict_and_findings(scope: str, requires_expected_production: bool) -> None:
    payloads = (
        {"rows": []},
        {"rows": [{"asset_id": 1, "production_kwh": 100.0, "expected_production_kwh": 120.0}]},
        {"rows": [{"asset_id": 1, "production_kwh": None, "expected_production_kwh": 120.0}]},
        {"rows": [{"asset_id": 1, "production_kwh": 0.0, "expected_production_kwh": 0.0}]},
        {"rows": [{"asset_id": 1, "production_kwh": 100.0}, {"asset_id": 2, "production_kwh": None}]},
    )
    for payload in payloads:
        kwargs = dict(scope=scope, requires_expected_production=requires_expected_production)
        expected = V1.quality_gate.evaluate_report_quality(payload, **kwargs)
        actual = v2_quality_gate.evaluate_report_quality(payload, **kwargs)
        assert asdict(actual) == asdict(expected), (scope, payload)


@requires_v1
@pytest.mark.parametrize("value", ["0", "10.5", "-1", "", "abc", None, "1 234,56"])
def test_nonnegative_decimal_parsing_matches_v1(value) -> None:
    data = {"field": value}
    try:
        expected, failed = V1.validation.parse_nonnegative_decimal_field(data, "field"), None
    except Exception as exc:  # noqa: BLE001 - parity includes failing the same way
        expected, failed = None, type(exc).__name__
    try:
        actual, actual_failed = v2_validation.parse_nonnegative_decimal_field(data, "field"), None
    except Exception as exc:  # noqa: BLE001
        actual, actual_failed = None, type(exc).__name__
    assert (actual, actual_failed) == (expected, failed)
