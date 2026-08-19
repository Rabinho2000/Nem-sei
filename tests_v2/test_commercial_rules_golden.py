"""Golden parity for the commercial rules: V1 and V2 must agree exactly.

This is the one place V2 is allowed to load V1, and only to compare against it.
The frozen V1 modules are imported dynamically from the server so that a missing
V1 checkout simply skips the comparison instead of breaking the suite, and so
that no V2 source file ever carries a V1 import.

The rules are pure functions over dataclasses, so parity can be asserted
directly on outputs rather than inferred from a report that happens to look the
same.
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from nemsei.reporting.rules import billing as v2_billing
from nemsei.reporting.rules import invoices as v2_invoices
from nemsei.reporting.rules import tariffs as v2_tariffs
from nemsei.reporting.rules import types as v2_types


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1() -> ModuleType | None:
    """Import the frozen V1 reporting package without adding a static import."""
    if not (V1_ROOT / "monitoring_board" / "reporting" / "billing.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        modules = {}
        for name in ("models", "billing", "tariffs", "invoices"):
            spec = importlib.util.find_spec(f"monitoring_board.reporting.{name}")
            if spec is None:  # pragma: no cover - defensive
                return None
            modules[name] = importlib.import_module(f"monitoring_board.reporting.{name}")
        return type("V1", (), modules)
    except Exception:  # pragma: no cover - a broken V1 checkout is missing evidence
        return None


V1 = load_v1()
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


BILLING_CASES = (
    # production, export, raw self use
    ("1000", "200", None),
    ("1000", "200", "750"),
    ("0", "0", None),
    ("1234.56789", "234.56789", None),
    ("500", "600", None),  # export larger than production: the rule must not go negative silently
)


@requires_v1
@pytest.mark.parametrize("production, export, raw_self_use", BILLING_CASES)
def test_self_use_inference_matches_v1(production: str, export: str, raw_self_use) -> None:
    kwargs = dict(production_kwh=Decimal(production), export_kwh=Decimal(export), raw_self_use=raw_self_use)
    assert v2_billing.infer_self_use_kwh(**kwargs) == V1.billing.infer_self_use_kwh(**kwargs)


@requires_v1
@pytest.mark.parametrize(
    "value",
    ["1234.56", "1 234,56", "1,234.56", "0", "-5", "", None, "abc", "1.234.567,89", "12,5"],
)
def test_decimal_normalisation_matches_v1(value) -> None:
    """Number formats differ per customer invoice; the two must read them alike."""
    try:
        expected = V1.invoices.normalize_decimal(value)
        failed = None
    except Exception as exc:  # noqa: BLE001 - parity includes failing the same way
        expected, failed = None, type(exc).__name__
    try:
        actual = v2_invoices.normalize_decimal(value)
        actual_failed = None
    except Exception as exc:  # noqa: BLE001
        actual, actual_failed = None, type(exc).__name__
    assert (actual, actual_failed) == (expected, failed)


@requires_v1
@pytest.mark.parametrize("value", ["123456789", "PT123456789", "501936270", "", "12345678", None, "999999990"])
def test_portuguese_nif_validation_matches_v1(value) -> None:
    assert v2_invoices.is_valid_portuguese_nif(value) == V1.invoices.is_valid_portuguese_nif(value)
    assert v2_invoices.normalize_nif(value) == V1.invoices.normalize_nif(value)


@requires_v1
@pytest.mark.parametrize(
    "sample, start, end",
    [
        ("08:00", "07:00", "09:00"),
        ("23:30", "22:00", "02:00"),  # a window that crosses midnight
        ("02:00", "22:00", "02:00"),
        ("07:00", "07:00", "09:00"),
        ("09:00", "07:00", "09:00"),
    ],
)
def test_tariff_window_membership_matches_v1(sample: str, start: str, end: str) -> None:
    parse = v2_tariffs.parse_hhmm
    assert v2_tariffs.time_in_rule(parse(sample), parse(start), parse(end)) == V1.tariffs.time_in_rule(
        V1.tariffs.parse_hhmm(sample), V1.tariffs.parse_hhmm(start), V1.tariffs.parse_hhmm(end)
    )


@requires_v1
def test_the_two_rule_sets_expose_the_same_vocabulary() -> None:
    """A silently renamed enum member would change reports without failing a test."""
    for name in ("TariffType", "ReportType", "BillingMode"):
        v2_enum, v1_enum = getattr(v2_types, name), getattr(V1.models, name)
        assert sorted(member.value for member in v2_enum) == sorted(member.value for member in v1_enum), name


@requires_v1
@pytest.mark.parametrize("value", ["simple", "bi-horario", "tri-horario", "tetra-horario", "unknown", ""])
def test_tariff_type_parsing_matches_v1(value: str) -> None:
    try:
        expected, failed = V1.tariffs.parse_tariff_type(value), None
    except Exception as exc:  # noqa: BLE001
        expected, failed = None, type(exc).__name__
    try:
        actual, actual_failed = v2_tariffs.parse_tariff_type(value), None
    except Exception as exc:  # noqa: BLE001
        actual, actual_failed = None, type(exc).__name__
    assert (getattr(actual, "value", None), actual_failed) == (getattr(expected, "value", None), failed)
