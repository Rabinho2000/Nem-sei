"""Reduce a Python value to what a JSON column can actually store.

Shared, not `reporting`-specific: `reporting/datasets.py` built this first
(payloads carrying real `date` objects and `reportlab.lib.colors.Color`
tuples), but the same problem -- SQLAlchemy's JSON column raising on
`Decimal`/`date`/`datetime` instead of falling back to `str()` the way
`digest_of` does -- is not unique to reporting. `diagnostics/incidents.py`
hit it independently persisting a `DiagnosticFinding.evidence` dict, which is
why this lives here instead of staying private to one domain.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from reportlab.lib.colors import Color


def json_safe(value: Any) -> Any:
    """Recursively reduce a payload to what the JSON column can actually store.

    Applied uniformly wherever a payload with real Python types (not just
    JSON-native ones) needs to be persisted, so the same payload always
    produces the same stored JSON and the same digest, if one is computed
    from it.
    """
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Color):
        # A round-trippable hex string, not a description: `HexColor(hexval())`
        # reconstructs the identical Color, which matters because reportlab
        # renderers compare colors by equality (`if color == NAVY`) to apply
        # theming.
        return value.hexval()
    return value
