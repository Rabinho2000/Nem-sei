"""Classify a FusionSolar inverter's raw state code, ported from V1 verbatim.

Pure: no database, no provider, no Flask. Ported from
`monitoring_board/services/fusionsolar.py`'s `classify_fusionsolar_inverter_availability`
rather than re-derived, because the raw `inverter_state` codes (512, 40960,
768, 2...) are FusionSolar's own bitmask vocabulary and guessing what one means
is exactly the kind of invented metric this milestone was scoped to avoid.
`tests_v2/test_diagnostics_golden.py` checks this against every real state code
V1 actually saw, not just the three sets below.
"""
from __future__ import annotations

from typing import Any


# V1's own classification sets. Copied, not re-derived: these are provider
# bitmask codes with no documented meaning available to V2 beyond what V1's
# own operators already worked out and encoded here.
AVAILABLE_INVERTER_STATES = {512, 513, 514, 1025, 1026, 2048}
UNAVAILABLE_INVERTER_STATES = {768, 769, 770, 771, 772, 773, 774}
STANDBY_INVERTER_STATES = {0, 1, 2, 3, 7, 256, 1280, 1281, 1536, 1792}


def classify_fusionsolar_inverter_availability(
    raw_state: Any,
    *,
    has_recent_data: bool = True,
    has_critical_alarm: bool = False,
) -> str:
    """A raw `inverter_state` code to V1's four-value availability vocabulary.

    `has_recent_data=False` reads as `unknown` here rather than V1's own
    `no_communication`, because this module's vocabulary
    (`diagnostics.models.AVAILABILITY_STATES`) does not carry that fifth state —
    "última comunicação" is answered by `observed_at` directly instead of by a
    status label, since V1's own `communication_status` column turned out to
    read `"recent"` on every one of 51 289 rows and carries no information to
    port.
    """
    if not has_recent_data:
        return "unknown"
    if has_critical_alarm:
        return "unavailable"
    try:
        inverter_state = int(float(str(raw_state)))
    except (TypeError, ValueError):
        return "unknown"
    if inverter_state in AVAILABLE_INVERTER_STATES:
        return "available"
    if inverter_state in UNAVAILABLE_INVERTER_STATES:
        return "unavailable"
    if inverter_state in STANDBY_INVERTER_STATES:
        return "standby"
    return "unknown"
