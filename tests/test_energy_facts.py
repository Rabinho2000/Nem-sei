from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
    upsert_energy_interval_fact,
)


HISTORY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sigenergy" / "system_history_day.json"
)


def history_payload() -> dict:
    return json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))["data"]


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


def test_sigenergy_history_requires_explicit_kwh_contract() -> None:
    with pytest.raises(ValueError, match="confirmada como kWh"):
        parse_sigenergy_daily_history(
            history_payload(),
            system_id="SIG-001",
            period_date=date(2026, 2, 1),
        )


def test_sigenergy_history_materializes_daily_record_idempotently(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-history.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Expertcom')"
            ).lastrowid
        )
        fact = parse_sigenergy_daily_history(
            history_payload(),
            system_id="SIG-001",
            period_date=date(2026, 2, 1),
            confirmed_unit="kWh",
        )
        first_id = persist_sigenergy_daily_history(
            conn,
            asset_id=asset_id,
            fact=fact,
        )
        second_id = persist_sigenergy_daily_history(
            conn,
            asset_id=asset_id,
            fact=fact,
        )
        daily = conn.execute(
            """
            SELECT *
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'day' AND period_date = '2026-02-01'
            """,
            (asset_id,),
        ).fetchone()
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM energy_interval_facts"
        ).fetchone()[0]

    assert first_id == second_id
    assert fact_count == 1
    assert daily["production_kwh"] == 22.94
    assert daily["consumption_kwh"] == 24.83
    assert daily["self_use_kwh"] == 15.16
    assert daily["export_kwh"] == 3.93
    assert daily["grid_import_kwh"] == 9.67
    assert daily["data_quality"] == "complete"


def test_complete_sigenergy_daily_facts_materialize_complete_month(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-complete-month.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Expertcom')"
            ).lastrowid
        )
        for day in range(1, 29):
            fact = parse_sigenergy_daily_history(
                history_payload(),
                system_id="SIG-001",
                period_date=date(2026, 2, day),
                confirmed_unit="kWh",
            )
            persist_sigenergy_daily_history(
                conn,
                asset_id=asset_id,
                fact=fact,
            )
        monthly = conn.execute(
            """
            SELECT *
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'month' AND period_date = '2026-02-01'
            """,
            (asset_id,),
        ).fetchone()

    assert monthly["data_quality"] == "complete"
    assert monthly["production_kwh"] == pytest.approx(22.94 * 28)
    assert monthly["consumption_kwh"] == pytest.approx(24.83 * 28)
