"""Golden parity for the energy import, against V1's own stored rows.

The comparison is not against a fixture written from the same assumptions as the
code. It reads V1's real `production_records` and checks the values this
importer extracts from each stored provider payload against the values V1 itself
persisted in its own columns for that same row.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from nemsei.reporting.rules.v1_energy_signals import identity_violations, metrics_from_row


V1_DB = Path("/opt/server/apps/Nem-sei/data/monitoring_board.db")
requires_v1 = pytest.mark.skipif(not V1_DB.is_file(), reason="the frozen V1 database is not available here")


def v1_rows(where: str, limit: int) -> list[sqlite3.Row]:
    """Read V1 strictly read-only, or skip.

    V1's live database runs in WAL mode, which SQLite cannot open read-only from
    a read-only mount: it needs to touch the shared-memory file. Inside the test
    container the file is therefore visible but unopenable, and that is a
    missing-evidence skip rather than a failure — the same condition
    `test_financial_workbook_golden.py` already documents.
    """
    try:
        connection = sqlite3.connect(f"file:{V1_DB.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        pytest.skip("V1's live database cannot be opened read-only from here")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        return list(
            connection.execute(
                f"SELECT * FROM production_records WHERE period_type='day' AND {where} LIMIT {int(limit)}"
            )
        )
    except sqlite3.OperationalError:
        pytest.skip("V1's live database cannot be opened read-only from here")
    finally:
        connection.close()


@requires_v1
def test_extraction_matches_what_v1_persisted_for_the_same_row() -> None:
    """V1 stored the payload and, separately, its own production figure."""
    rows = v1_rows("provider='FusionSolar' AND production_kwh IS NOT NULL AND payload_json LIKE '%PVYield%'", 500)
    if not rows:
        pytest.skip("no FusionSolar rows with both a payload and a stored figure")
    compared = 0
    for row in rows:
        extracted = metrics_from_row(row)["production_energy"]
        stored = Decimal(str(row["production_kwh"]))
        assert extracted is not None
        assert abs(extracted - stored) <= Decimal("0.01"), f"row {row['id']}: {extracted} vs {stored}"
        compared += 1
    assert compared >= 100


@requires_v1
def test_the_sigenergy_columns_are_read_where_there_is_no_payload() -> None:
    rows = v1_rows("provider='Sigenergy' AND self_use_kwh IS NOT NULL", 50)
    if not rows:
        pytest.skip("no Sigenergy rows on this server")
    for row in rows:
        values = metrics_from_row(row)
        assert values["self_use_energy"] == Decimal(str(row["self_use_kwh"]))
        assert values["export_energy"] == Decimal(str(row["export_kwh"]))
        assert values["consumption_energy"] == Decimal(str(row["consumption_kwh"]))
        # A battery plant fails production = self-use + export and must not be
        # rejected for it.
        assert identity_violations(values) == []


@requires_v1
def test_the_energy_identity_holds_on_the_overwhelming_majority_of_real_rows() -> None:
    """The evidence this import rests on, asserted rather than remembered.

    If a future provider change broke the semantics of these signals, this is
    the test that would notice.
    """
    rows = v1_rows(
        "provider='FusionSolar' AND payload_json LIKE '%selfUsePower%' AND payload_json LIKE '%ongrid_power%'",
        5000,
    )
    if len(rows) < 500:
        pytest.skip("not enough FusionSolar rows carrying all three signals")
    exact = violations = 0
    for row in rows:
        values = metrics_from_row(row)
        production, self_use, export = (
            values["production_energy"], values["self_use_energy"], values["export_energy"]
        )
        if None in (production, self_use, export):
            continue
        if production == self_use + export:
            exact += 1
        if identity_violations(values):
            violations += 1
    assert exact / len(rows) > 0.90, f"only {exact}/{len(rows)} rows satisfy production = self-use + export"
    # A minority genuinely is impossible, and those are rejected rather than imported.
    assert violations > 0
    assert violations / len(rows) < 0.10
