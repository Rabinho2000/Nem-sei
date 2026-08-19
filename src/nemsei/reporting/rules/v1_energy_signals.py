"""Read V1's stored energy signals, and judge whether a row can be believed.

Pure rules over one V1 `production_records` row: no database, no provider, no
Flask. They live beside the other ported rules for the same reason those do —
they can be compared against V1's own outputs without standing an application
up, and `tests_v2/test_v1_energy_extraction_golden.py` does exactly that against
the real rows on the server.

Nothing here derives one metric from another. The identity
`production = self_use + export` holds for FusionSolar and does not hold for
Sigenergy, where a battery absorbs the difference, so it is used only to reject
the impossible — never to fill a value in.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


# The signal each canonical metric is read from, in V1's own alias order.
V1_METRIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "production_energy": ("PVYield", "inverterYield", "inverter_power"),
    "self_use_energy": ("selfUsePower", "selfProvide"),
    "export_energy": ("ongrid_power", "total_feed_in_to_grid"),
    "consumption_energy": ("use_power", "day_use_energy"),
    "grid_import_energy": ("buyPower",),
}
# The V1 column each metric is read from where the provider stores columns.
V1_METRIC_COLUMNS: dict[str, str] = {
    "production_energy": "production_kwh",
    "self_use_energy": "self_use_kwh",
    "export_energy": "export_kwh",
    "consumption_energy": "consumption_kwh",
    "grid_import_energy": "grid_import_kwh",
}
# Energy identities that must hold for a row to be believable. Each is
# (total, parts): the total may not be smaller than its parts beyond tolerance.
IDENTITY_TOLERANCE = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    """A number, or nothing. A blank, a dash or a word is nothing, not zero."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def first_signal(data: dict[str, Any], names: Iterable[str]) -> Decimal | None:
    """V1's own rule: the first alias that carries a number wins."""
    for name in names:
        value = _decimal(data.get(name))
        if value is not None:
            return value
    return None


def metrics_from_row(row: Any) -> dict[str, Decimal | None]:
    """Every metric a V1 daily row states, from its payload or its columns.

    The payload is preferred because it is the provider's own words. The columns
    are the fallback, and they are what Sigenergy rows carry: those have no
    `dataItemMap` at all.
    """
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    items = payload.get("dataItemMap") if isinstance(payload, dict) else {}
    items = items if isinstance(items, dict) else {}

    values: dict[str, Decimal | None] = {}
    for metric, names in V1_METRIC_SIGNALS.items():
        value = first_signal(items, names) if items else None
        if value is None:
            value = _decimal(row[V1_METRIC_COLUMNS[metric]])
        values[metric] = value
    return values


def identity_violations(values: dict[str, Decimal | None]) -> list[str]:
    """Which metrics a row's own arithmetic contradicts.

    Only violations that make a value impossible are reported. Production
    smaller than export means a plant exported energy it never generated, so the
    export figure cannot be believed. Production merely differing from self-use
    plus export is normal wherever a battery is involved, and is not a
    violation — treating it as one would reject every Sigenergy row on the
    server.
    """
    violations: list[str] = []
    production, export = values.get("production_energy"), values.get("export_energy")
    if production is not None and export is not None and export - production > IDENTITY_TOLERANCE:
        violations.append("export_energy")
    consumption, self_use = values.get("consumption_energy"), values.get("self_use_energy")
    if consumption is not None and self_use is not None and self_use - consumption > IDENTITY_TOLERANCE:
        violations.append("self_use_energy")
    return violations

