"""Golden parity for plant availability, the number customers argue about."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from nemsei.reporting.rules.availability import float_or_none, weighted_sampled_availability


V1_ROOT = Path("/opt/server/apps/Nem-sei")


def load_v1():
    if not (V1_ROOT / "monitoring_board" / "services" / "sampled_availability.py").is_file():
        return None
    if str(V1_ROOT) not in sys.path:
        sys.path.insert(0, str(V1_ROOT))
    try:
        return importlib.import_module("monitoring_board.services.sampled_availability")
    except Exception:  # pragma: no cover - a broken checkout is missing evidence
        return None


V1 = load_v1()
requires_v1 = pytest.mark.skipif(V1 is None, reason="the frozen V1 checkout is not available here")


CASES = {
    "empty": [],
    "single full": [{"availability_pct": 100.0, "rated_power_kw": 20.0}],
    "equal weights": [
        {"availability_pct": 100.0, "rated_power_kw": 10.0},
        {"availability_pct": 50.0, "rated_power_kw": 10.0},
    ],
    "weighting actually matters": [
        {"availability_pct": 100.0, "rated_power_kw": 90.0},
        {"availability_pct": 0.0, "rated_power_kw": 10.0},
    ],
    "one device unknown availability": [
        {"availability_pct": 100.0, "rated_power_kw": 10.0},
        {"availability_pct": None, "rated_power_kw": 10.0},
    ],
    "one device unrated falls back to mean": [
        {"availability_pct": 100.0, "rated_power_kw": 90.0},
        {"availability_pct": 0.0, "rated_power_kw": None},
    ],
    "zero rating is not a weight": [
        {"availability_pct": 100.0, "rated_power_kw": 0.0},
        {"availability_pct": 40.0, "rated_power_kw": 10.0},
    ],
    "negative rating": [
        {"availability_pct": 80.0, "rated_power_kw": -5.0},
        {"availability_pct": 40.0, "rated_power_kw": 10.0},
    ],
    "rounding boundary": [
        {"availability_pct": 99.995, "rated_power_kw": 1.0},
        {"availability_pct": 99.994, "rated_power_kw": 1.0},
    ],
    "string values": [
        {"availability_pct": 90.0, "rated_power_kw": "30"},
        {"availability_pct": 60.0, "rated_power_kw": "10"},
    ],
    "unparseable rating": [
        {"availability_pct": 90.0, "rated_power_kw": "n/a"},
        {"availability_pct": 60.0, "rated_power_kw": "10"},
    ],
}


@requires_v1
@pytest.mark.parametrize("label", sorted(CASES))
def test_weighted_availability_matches_v1(label: str) -> None:
    rows = [dict(row) for row in CASES[label]]
    assert weighted_sampled_availability(rows) == V1._weighted_sampled_availability([dict(row) for row in CASES[label]]), label


@requires_v1
@pytest.mark.parametrize("value", [None, "", "10", "10.5", "abc", 0, -1, True])
def test_float_coercion_matches_v1(value) -> None:
    assert float_or_none(value) == V1._float_or_none(value)


def test_an_unknown_device_makes_the_plant_unknown_not_zero() -> None:
    """The rule that matters commercially, pinned without needing V1."""
    assert weighted_sampled_availability([{"availability_pct": None, "rated_power_kw": 10.0}]) is None
    assert weighted_sampled_availability([]) is None
