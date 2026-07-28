from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.services.energy_facts import upsert_energy_interval_fact


def test_energy_fact_persists_provenance_timezone_and_quality(tmp_path) -> None:
    db_path = tmp_path / "energy-facts.db"
    ensure_database(str(db_path))
    start = datetime(2026, 1, 1, 10, 0)
    with get_db(str(db_path)) as conn:
        fact_id = upsert_energy_interval_fact(
            conn,
            asset_id=None,
            provider="Sigenergy",
            external_id="SIG-001",
            period_start=start,
            period_end=start + timedelta(minutes=15),
            granularity="15m",
            provenance="data_subscription_telemetry",
            data_quality="complete",
            production_kwh=1.25,
            consumption_kwh=0.75,
            payload={"eventId": "anon-1"},
        )
        row = conn.execute(
            "SELECT * FROM energy_interval_facts WHERE id = ?",
            (fact_id,),
        ).fetchone()

    assert row["provider"] == "Sigenergy"
    assert row["production_kwh"] == 1.25
    assert row["timezone"] == "Europe/Lisbon"
    assert row["data_quality"] == "complete"


def test_complete_energy_fact_rejects_missing_or_negative_kwh(tmp_path) -> None:
    db_path = tmp_path / "invalid-energy-facts.db"
    ensure_database(str(db_path))
    start = datetime(2026, 1, 1, 10, 0)
    with get_db(str(db_path)) as conn:
        with pytest.raises(ValueError, match="pelo menos um valor"):
            upsert_energy_interval_fact(
                conn,
                asset_id=None,
                provider="Sigenergy",
                external_id="SIG-001",
                period_start=start,
                period_end=start + timedelta(hours=1),
                granularity="hour",
                provenance="confirmed_history",
                data_quality="complete",
            )
        with pytest.raises(ValueError, match="nao pode ser negativo"):
            upsert_energy_interval_fact(
                conn,
                asset_id=None,
                provider="Sigenergy",
                external_id="SIG-001",
                period_start=start,
                period_end=start + timedelta(hours=1),
                granularity="hour",
                provenance="confirmed_history",
                data_quality="partial",
                production_kwh=-1,
            )
