"""Classify a FusionSolar inverter's raw state code, ported from V1 verbatim.

Pure: no database, no provider, no Flask. Ported from
`monitoring_board/services/fusionsolar.py`'s `classify_fusionsolar_inverter_availability`
rather than re-derived, because the raw `inverter_state` codes (512, 40960,
768, 2...) are FusionSolar's own bitmask vocabulary and guessing what one means
is exactly the kind of invented metric this milestone was scoped to avoid.
`tests_v2/test_diagnostics_golden.py` checks this against every real state code
V1 actually saw, not just the sets below.

One code is classified here that V1 left unclassified — see
`V2_STANDBY_INVERTER_STATES`. That is a deliberate divergence from the port,
argued from evidence in V1's own history rather than from documentation, and
the golden test pins it as a divergence instead of letting it read as drift.
"""
from __future__ import annotations

from typing import Any


# V1's own classification sets. Copied, not re-derived: these are provider
# bitmask codes with no documented meaning available to V2 beyond what V1's
# own operators already worked out and encoded here.
AVAILABLE_INVERTER_STATES = {512, 513, 514, 1025, 1026, 2048}
UNAVAILABLE_INVERTER_STATES = {768, 769, 770, 771, 772, 773, 774}
STANDBY_INVERTER_STATES = {0, 1, 2, 3, 7, 256, 1280, 1281, 1536, 1792}

# Codes V1 recorded but never classified, resolved by V2 from what V1's own
# rows show. Adding one here requires that kind of proof — the raw history,
# not a plausible reading of the number.
#
# 40960 (0xA000) is the whole set today, and it was not a marginal gap: it is
# 10 736 of V1's 51 289 device readings, and the last reading 220 of the 325
# imported devices ever got, which is why almost every inverter in the V2
# diagnostics table read "desconhecido". What V1's own rows say about it:
#
#   * `active_power_kw` is 0 in all 10 736 rows — never null, never positive;
#   * every observation falls between 19:00 and 05:59 UTC (20h–06h Lisbon over
#     the June–July 2026 data), with zero occurrences from 06:00 to 18:59 UTC;
#   * 4 384 of the rows still carry that day's accumulated energy, up to
#     874.7 kWh — the inverter produced during the day and is now at rest.
#
# An inverter shut down for the night after producing is `standby` in V1's own
# four-value vocabulary. V1 simply never added the code to its sets, so it fell
# through to `unknown`. `test_diagnostics_golden.py` re-derives all three
# observations from V1's database on every run, so this stops being true out
# loud if it ever stops being true.
V2_STANDBY_INVERTER_STATES = {40960}


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
    # Kept as a separate check, not folded into the set above, so that the one
    # answer V2 gives which V1 did not stays visible at the point of decision.
    if inverter_state in V2_STANDBY_INVERTER_STATES:
        return "standby"
    return "unknown"
