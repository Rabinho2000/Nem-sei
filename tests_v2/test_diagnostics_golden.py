"""Golden parity: the ported classification against every real state V1 saw.

Not a fixture written from the same assumptions as the code — the comparison
is against V1's own precomputed `availability_status` column, for every
distinct `inverter_state` value the real device_realtime_snapshots table
actually contains.

Parity is the rule, not the goal. Where V2 answers a code V1 left as
`unknown`, the divergence is listed in `DELIBERATE_DIVERGENCES` and the
evidence behind it is re-derived from V1's own rows on every run — so the
day the evidence stops holding, the test fails instead of the product
quietly keeping an answer it can no longer justify.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nemsei.diagnostics.rules import V2_STANDBY_INVERTER_STATES, classify_fusionsolar_inverter_availability


V1_DB = Path("/opt/server/apps/Nem-sei/data/monitoring_board.db")
requires_v1 = pytest.mark.skipif(not V1_DB.is_file(), reason="the frozen V1 database is not available here")

# state code -> (what V2 answers now, what V1 had stored). Both halves are
# asserted: a divergence is only allowed where V1 really did leave the code
# unclassified, so this can never quietly paper over a changed answer to a
# code V1 had already decided.
DELIBERATE_DIVERGENCES = {40960: ("standby", "unknown")}


def v1_rows(query: str) -> list[sqlite3.Row]:
    try:
        connection = sqlite3.connect(f"file:{V1_DB.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        pytest.skip("V1's live database cannot be opened read-only from here")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        return list(connection.execute(query))
    except sqlite3.OperationalError:
        pytest.skip("V1's live database cannot be opened read-only from here")
    finally:
        connection.close()


@requires_v1
def test_every_real_state_code_classifies_the_same_way_v1_did() -> None:
    rows = v1_rows(
        "SELECT DISTINCT inverter_state, availability_status FROM device_realtime_snapshots"
        " WHERE availability_status IS NOT NULL"
    )
    if not rows:
        pytest.skip("no device_realtime_snapshots on this server")
    compared = 0
    for row in rows:
        state = row["inverter_state"]
        mine = classify_fusionsolar_inverter_availability(state)
        if state in DELIBERATE_DIVERGENCES:
            expected_v2, expected_v1 = DELIBERATE_DIVERGENCES[state]
            assert mine == expected_v2, f"state {state!r}: got {mine!r}, this divergence claims {expected_v2!r}"
            assert row["availability_status"] == expected_v1, (
                f"state {state!r}: V1 now says {row['availability_status']!r}, not the {expected_v1!r}"
                " this divergence was granted against — re-argue it before keeping it"
            )
            compared += 1
            continue
        assert mine == row["availability_status"], (
            f"state {row['inverter_state']!r}: got {mine!r}, V1 said {row['availability_status']!r}"
        )
        compared += 1
    assert compared >= 8, f"expected real state-code diversity, only saw {compared}"


@requires_v1
def test_the_codes_v2_classifies_alone_are_all_declared_divergences() -> None:
    """No code may be answered past V1 without being on the record above."""
    assert set(V2_STANDBY_INVERTER_STATES) <= set(DELIBERATE_DIVERGENCES)


@requires_v1
def test_state_40960_really_is_an_inverter_at_rest_for_the_night() -> None:
    """The evidence for the one divergence, re-derived rather than restated.

    Three independent things have to hold in V1's own rows for `standby` to be
    the honest answer: the inverter is never producing, it is only ever seen
    at night, and it has been producing earlier in the day often enough that
    "asleep" explains the reading better than "broken".
    """
    rows = v1_rows(
        "SELECT active_power_kw, day_energy_kwh, collected_at FROM device_realtime_snapshots"
        " WHERE inverter_state = 40960"
    )
    if not rows:
        pytest.skip("no 40960 readings on this server")

    powers = [row["active_power_kw"] for row in rows]
    assert all(power is not None and power == 0 for power in powers), (
        "40960 now appears with active power; it is no longer only an inverter at rest"
    )

    hours = {int(row["collected_at"][11:13]) for row in rows}
    daylight = {hour for hour in hours if 6 <= hour <= 18}
    assert not daylight, f"40960 now appears in daylight hours {sorted(daylight)} UTC; re-argue the divergence"

    produced_that_day = sum(1 for row in rows if (row["day_energy_kwh"] or 0) > 0)
    assert produced_that_day > 0, "no 40960 reading follows a day of production; 'at rest' is no longer supported"


@requires_v1
def test_communication_status_really_does_carry_no_information() -> None:
    """The reason this column is not imported, checked rather than assumed."""
    rows = v1_rows("SELECT DISTINCT communication_status FROM device_realtime_snapshots")
    values = {row["communication_status"] for row in rows}
    assert values == {"recent"}, f"communication_status now varies ({values}); reconsider importing it"
