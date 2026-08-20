"""Golden parity: the ported classification against every real state V1 saw.

Not a fixture written from the same assumptions as the code — the comparison
is against V1's own precomputed `availability_status` column, for every
distinct `inverter_state` value the real device_realtime_snapshots table
actually contains.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nemsei.diagnostics.rules import classify_fusionsolar_inverter_availability


V1_DB = Path("/opt/server/apps/Nem-sei/data/monitoring_board.db")
requires_v1 = pytest.mark.skipif(not V1_DB.is_file(), reason="the frozen V1 database is not available here")


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
        mine = classify_fusionsolar_inverter_availability(row["inverter_state"])
        assert mine == row["availability_status"], (
            f"state {row['inverter_state']!r}: got {mine!r}, V1 said {row['availability_status']!r}"
        )
        compared += 1
    assert compared >= 8, f"expected real state-code diversity, only saw {compared}"


@requires_v1
def test_communication_status_really_does_carry_no_information() -> None:
    """The reason this column is not imported, checked rather than assumed."""
    rows = v1_rows("SELECT DISTINCT communication_status FROM device_realtime_snapshots")
    values = {row["communication_status"] for row in rows}
    assert values == {"recent"}, f"communication_status now varies ({values}); reconsider importing it"
