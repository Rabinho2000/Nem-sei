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
    sigenergy_history_unit_confirmed,
    upsert_energy_interval_fact,
)


HISTORY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sigenergy" / "system_history_day.json"
)


def history_payload() -> dict:
    return json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))["data"]


def test_sigenergy_history_prefers_kwh_fields_without_double_counting() -> None:
    payload = {
        "powerGenerationKwh": 12.5,
        "powerGeneration": 999,
        "powerUseKwh": 10,
        "powerUse": 888,
        "powerOneselfKwh": 7,
        "powerOneself": 777,
        "powerSelfConsumptionKwh": 8,
        "powerToGridKwh": 5.5,
        "powerFromGridKwh": 3,
    }

    fact = parse_sigenergy_daily_history(
        payload,
        system_id="SIG-001",
        period_date=date(2026, 2, 1),
        confirmed_unit="kWh",
    )

    assert fact.values["production_kwh"] == 12.5
    assert fact.values["consumption_kwh"] == 10
    assert fact.values["self_use_kwh"] == 7
    assert fact.values["export_kwh"] == 5.5
    assert fact.payload["powerSelfConsumptionKwh"] == 8
    assert fact.payload["powerGeneration"] == 999
    assert fact.payload["_source_energy_unit"] == "kWh"
    assert fact.payload["_normalized_energy_unit"] == "kWh"


def test_sigenergy_history_unit_confirmation_comes_from_running_process(monkeypatch) -> None:
    monkeypatch.delenv("SIGENERGY_HISTORY_ENERGY_UNIT", raising=False)
    assert not sigenergy_history_unit_confirmed()
    monkeypatch.setenv("SIGENERGY_HISTORY_ENERGY_UNIT", "KWH")
    assert sigenergy_history_unit_confirmed()


def test_sigenergy_history_legacy_fallback_and_missing_fields_stay_null() -> None:
    fact = parse_sigenergy_daily_history(
        {
            "powerGeneration": 4,
            "powerUse": 3,
            "powerOneself": 2,
            "powerToGrid": 1,
        },
        system_id="SIG-LEGACY",
        period_date=date(2026, 2, 1),
        confirmed_unit="kWh",
    )

    assert fact.values["production_kwh"] == 4
    assert fact.values["self_use_kwh"] == 2
    assert fact.values["grid_import_kwh"] is None
    assert fact.values["battery_charge_kwh"] is None
    assert fact.data_quality == "partial"


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
    assert monthly["self_use_kwh"] == pytest.approx(15.16 * 28)
    assert monthly["export_kwh"] == pytest.approx(3.93 * 28)
    assert monthly["grid_import_kwh"] == pytest.approx(9.67 * 28)


def test_partial_and_current_sigenergy_months_never_close(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-partial-month.db"
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
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        historical = conn.execute(
            """
            SELECT data_quality
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'month' AND period_date = '2026-02-01'
            """,
            (asset_id,),
        ).fetchone()

        current_day = date.today().replace(day=1)
        current_fact = parse_sigenergy_daily_history(
            history_payload(),
            system_id="SIG-001",
            period_date=current_day,
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(
            conn,
            asset_id=asset_id,
            fact=current_fact,
        )
        current = conn.execute(
            """
            SELECT data_quality
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'month' AND period_date = ?
            """,
            (asset_id, current_day.isoformat()),
        ).fetchone()

    assert historical["data_quality"] == "partial"
    assert current["data_quality"] == "in_progress"
