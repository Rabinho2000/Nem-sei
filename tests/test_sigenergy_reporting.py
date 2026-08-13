from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app import (
    build_fusionsolar_customer_production_report,
    ensure_database,
)
from monitoring_board.db import get_db
from monitoring_board.reporting.energy_sources import (
    set_asset_primary_energy_source,
)
from monitoring_board.reporting.quality_gate import (
    BLOCKED,
    READY,
    evaluate_report_quality,
)
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
)
from monitoring_board.services.sigenergy_client import SigenergyClient
from monitoring_board.services.sigenergy_models import (
    SigenergyCredentials,
    SigenergyEndpoints,
)


HISTORY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sigenergy" / "system_history_day.json"
)
HISTORY_KWH_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "sigenergy"
    / "system_history_day_kwh.json"
)


def _sigenergy_asset(conn, name: str) -> int:
    asset_id = int(
        conn.execute(
            """
            INSERT INTO assets (
                project_name, active_contract, contract_type, kwp
            ) VALUES (?, 'yes', 'EPC', '10')
            """,
            (name,),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO asset_integrations (
            asset_id, provider, external_id, external_name, enabled,
            is_primary_energy_source
        ) VALUES (?, 'Sigenergy', ?, ?, 1, 1)
        """,
        (asset_id, f"SIG-{asset_id}", name),
    )
    return asset_id


def test_individual_report_reads_persisted_sigenergy_facts(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-individual-report.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Individual")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO production_records (
                asset_id, provider, external_id, period_type, period_date,
                production_kwh, data_quality, created_at, updated_at
            ) VALUES (?, 'Sigenergy', ?, 'month', '2026-01-01',
                      456.7, 'complete', ?, ?)
            """,
            (asset_id, f"SIG-{asset_id}", now, now),
        )
        conn.commit()

        report = build_fusionsolar_customer_production_report(
            conn,
            asset_id=asset_id,
            report_month="2026-01",
            electricity_price=0.2,
            sell_price=0.05,
            reference_date=date(2026, 2, 1),
        )

    assert report["energy_provider"] == "Sigenergy"
    assert report["production_kwh"] == 456.7
    assert report["production_quality_status"] == "complete"
    assert report["data_origin"] == "production_records"


def test_official_history_fixture_flows_from_fact_to_complete_report(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-api-to-report.db"
    ensure_database(str(db_path))
    payload = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))["data"]
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy API Flow")
        external_id = f"SIG-{asset_id}"
        for day in range(1, 29):
            fact = parse_sigenergy_daily_history(
                payload,
                system_id=external_id,
                period_date=date(2026, 2, day),
                confirmed_unit="kWh",
            )
            persist_sigenergy_daily_history(
                conn,
                asset_id=asset_id,
                fact=fact,
            )
        conn.commit()

        report = build_fusionsolar_customer_production_report(
            conn,
            asset_id=asset_id,
            report_month="2026-02",
            electricity_price=0.2,
            sell_price=0.05,
            reference_date=date(2026, 3, 1),
        )

    assert report["energy_provider"] == "Sigenergy"
    assert report["production_kwh"] == pytest.approx(22.94 * 28)
    assert report["consumption_kwh"] == pytest.approx(24.83 * 28)
    assert report["self_use_kwh"] == pytest.approx(15.16 * 28)
    assert report["export_kwh"] == pytest.approx(3.93 * 28)
    assert report["grid_import_kwh"] == pytest.approx(9.67 * 28)
    assert report["production_quality_status"] == "complete"
    assert report["production_is_final"] is True


def test_sigenergy_history_item_list_materializes_hourly_energy(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-hourly-history.db"
    ensure_database(str(db_path))
    payload = {
        "powerGenerationKwh": 24.0,
        "powerUseKwh": 48.0,
        "powerOneselfKwh": 18.0,
        "powerToGridKwh": 6.0,
        "powerFromGridKwh": 30.0,
        "itemList": [
            {
                "dataTime": f"20260701 {hour:02d}:00",
                "powerGeneration": float(hour),
                "powerUse": float(hour * 2),
                "powerOneself": float(hour * 0.75),
                "powerToGrid": float(hour * 0.25),
                "powerFromGrid": float(hour * 1.25),
            }
            for hour in range(24)
        ],
    }
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Horaria")
        fact = parse_sigenergy_daily_history(
            payload,
            system_id=f"SIG-{asset_id}",
            period_date=date(2026, 7, 1),
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        hourly_rows = conn.execute(
            """
            SELECT period_start, production_kwh, self_use_kwh, export_kwh,
                   consumption_kwh, grid_import_kwh, data_quality
            FROM production_hourly_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
            ORDER BY period_start
            """,
            (asset_id,),
        ).fetchall()

    assert len(hourly_rows) == 24
    assert hourly_rows[0]["period_start"] == "2026-07-01T00:00:00+01:00"
    assert hourly_rows[-1]["production_kwh"] == pytest.approx(1.0)
    assert sum(row["production_kwh"] for row in hourly_rows) == pytest.approx(24.0)
    assert sum(row["self_use_kwh"] for row in hourly_rows) == pytest.approx(18.0)
    assert sum(row["export_kwh"] for row in hourly_rows) == pytest.approx(6.0)
    assert all(row["data_quality"] == "complete" for row in hourly_rows)


def test_sigenergy_hourly_history_rejects_conflicting_daily_total(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-hourly-conflict.db"
    ensure_database(str(db_path))
    payload = {
        "powerGenerationKwh": 0.0,
        "powerUseKwh": 0.0,
        "powerOneselfKwh": 0.0,
        "powerToGridKwh": 0.0,
        "powerFromGridKwh": 0.0,
        "itemList": [
            {
                "dataTime": "20260724 00:00",
                "powerGeneration": 0.0,
                "powerUse": 0.0,
                "powerOneself": 0.0,
                "powerToGrid": 0.0,
                "powerFromGrid": 0.0,
            },
            {
                "dataTime": "20260724 01:00",
                "powerGeneration": 1.0,
                "powerUse": 1.0,
                "powerOneself": 1.0,
                "powerToGrid": 0.0,
                "powerFromGrid": 0.0,
            },
        ],
    }
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Conflito")
        fact = parse_sigenergy_daily_history(
            payload,
            system_id=f"SIG-{asset_id}",
            period_date=date(2026, 7, 24),
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        hourly_count = conn.execute(
            "SELECT COUNT(*) FROM production_hourly_records WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()[0]

    assert hourly_count == 0


def test_sigenergy_hourly_history_reconciles_full_day_counter_correction(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-hourly-reconciled.db"
    ensure_database(str(db_path))
    payload = {
        "powerGenerationKwh": 10.0,
        "powerUseKwh": 10.0,
        "powerOneselfKwh": 10.0,
        "powerToGridKwh": 0.0,
        "powerFromGridKwh": 0.0,
        "itemList": [
            {
                "dataTime": f"20260724 {hour:02d}:00",
                "powerGeneration": 12.0 if hour == 1 else 10.0 if hour > 1 else 0.0,
                "powerUse": 12.0 if hour == 1 else 10.0 if hour > 1 else 0.0,
                "powerOneself": 12.0 if hour == 1 else 10.0 if hour > 1 else 0.0,
                "powerToGrid": 0.0,
                "powerFromGrid": 0.0,
            }
            for hour in range(24)
        ],
    }
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Reconciliada")
        fact = parse_sigenergy_daily_history(
            payload,
            system_id=f"SIG-{asset_id}",
            period_date=date(2026, 7, 24),
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        hourly_rows = conn.execute(
            "SELECT production_kwh, self_use_kwh, data_quality FROM production_hourly_records WHERE asset_id = ?",
            (asset_id,),
        ).fetchall()

    assert len(hourly_rows) == 24
    assert sum(row["production_kwh"] for row in hourly_rows) == pytest.approx(10.0)
    assert sum(row["self_use_kwh"] for row in hourly_rows) == pytest.approx(10.0)
    assert {row["data_quality"] for row in hourly_rows} == {"reconciled"}


def test_sigenergy_completed_item_list_overrides_contradictory_zero_daily_total(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-daily-zero-contradiction.db"
    ensure_database(str(db_path))
    payload = {
        "powerGenerationKwh": 0.0,
        "powerUseKwh": 0.0,
        "powerOneselfKwh": 0.0,
        "powerToGridKwh": 0.0,
        "powerFromGridKwh": 0.0,
        "itemList": [
            {
                "dataTime": f"20260724 {hour:02d}:00",
                "powerGeneration": 12.0 if hour >= 12 else 0.0,
                "powerUse": 20.0 if hour >= 12 else 0.0,
                "powerOneself": 10.0 if hour >= 12 else 0.0,
                "powerToGrid": 2.0 if hour >= 12 else 0.0,
                "powerFromGrid": 10.0 if hour >= 12 else 0.0,
            }
            for hour in range(24)
        ],
    }
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Daily Zero")
        fact = parse_sigenergy_daily_history(
            payload,
            system_id=f"SIG-{asset_id}",
            period_date=date(2026, 7, 24),
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        daily = conn.execute(
            "SELECT production_kwh, self_use_kwh, export_kwh FROM production_records WHERE asset_id = ? AND period_type = 'day'",
            (asset_id,),
        ).fetchone()
        hourly_total = conn.execute(
            "SELECT SUM(production_kwh) FROM production_hourly_records WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()[0]

    assert daily["production_kwh"] == pytest.approx(12.0)
    assert daily["self_use_kwh"] == pytest.approx(10.0)
    assert daily["export_kwh"] == pytest.approx(2.0)
    assert hourly_total == pytest.approx(12.0)


def test_real_kwh_response_flows_client_to_complete_month_and_report(
    tmp_path,
) -> None:
    history_response = json.loads(
        HISTORY_KWH_FIXTURE.read_text(encoding="utf-8")
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def post(self, *_args, **_kwargs):
            return Response(
                {
                    "code": 0,
                    "data": {
                        "accessToken": "e2e-token",
                        "expiresIn": 1200,
                    },
                }
            )

        def request(self, *_args, **_kwargs):
            return Response(history_response)

    client = SigenergyClient(
        SigenergyEndpoints(
            base_url="https://sigenergy.example.test",
            login_endpoint="/openapi/auth/login/key",
            systems_endpoint="/openapi/system",
            energy_flow_endpoint="/openapi/systems/{system_id}/energyFlow",
            history_endpoint="/openapi/systems/{system_id}/history",
            region="eu",
        ),
        SigenergyCredentials("e2e-key", "e2e-secret"),
        session=Session(),
        token_cache={},
    )
    db_path = tmp_path / "sigenergy-real-kwh-e2e.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Expertcom")
        external_id = f"SIG-{asset_id}"
        for day in range(1, 29):
            history = client.get_system_history(
                external_id,
                level="Day",
                target_date=date(2026, 2, day).isoformat(),
            )
            fact = parse_sigenergy_daily_history(
                history,
                system_id=external_id,
                period_date=date(2026, 2, day),
                confirmed_unit="kWh",
            )
            persist_sigenergy_daily_history(
                conn,
                asset_id=asset_id,
                fact=fact,
            )
        conn.commit()
        daily_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'day' AND data_quality = 'complete'
            """,
            (asset_id,),
        ).fetchone()[0]
        monthly = conn.execute(
            """
            SELECT *
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'month' AND period_date = '2026-02-01'
            """,
            (asset_id,),
        ).fetchone()
        report = build_fusionsolar_customer_production_report(
            conn,
            asset_id=asset_id,
            report_month="2026-02",
            electricity_price=0.2,
            sell_price=0.05,
            reference_date=date(2026, 3, 1),
        )

    assert daily_count == 28
    assert monthly["data_quality"] == "complete"
    assert monthly["production_kwh"] == pytest.approx(22.94 * 28)
    assert monthly["consumption_kwh"] == pytest.approx(24.83 * 28)
    assert monthly["self_use_kwh"] == pytest.approx(15.16 * 28)
    assert monthly["export_kwh"] == pytest.approx(3.93 * 28)
    assert monthly["grid_import_kwh"] == pytest.approx(9.67 * 28)
    assert report["production_kwh"] == pytest.approx(22.94 * 28)
    assert report["self_use_kwh"] == pytest.approx(15.16 * 28)
    assert report["production_quality_status"] == "complete"


def test_sigenergy_report_with_missing_kwh_never_queues_fusionsolar_or_finalizes_financials(
    tmp_path,
) -> None:
    db_path = tmp_path / "sigenergy-missing-report.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Missing")
        conn.commit()

        report = build_fusionsolar_customer_production_report(
            conn,
            asset_id=asset_id,
            report_month="2026-01",
            electricity_price=0.2,
            sell_price=0.05,
            reference_date=date(2026, 2, 1),
        )
        queued = conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE job_type LIKE 'fusionsolar%'"
        ).fetchone()[0]

    assert report["production_kwh"] is None
    assert report["production_is_final"] is False
    assert report["savings_eur"] is None
    assert queued == 0
    assert any(
        "dados insuficientes para calculo financeiro" in warning
        for warning in report["data_request_warnings"]
    )


def test_sigenergy_cannot_become_primary_without_complete_month(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-primary-gate.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Gate")
        conn.execute(
            """
            UPDATE asset_integrations
            SET is_primary_energy_source = 0
            WHERE asset_id = ?
            """,
            (asset_id,),
        )

        with pytest.raises(ValueError, match="mes energetico completo"):
            set_asset_primary_energy_source(
                conn,
                asset_id=asset_id,
                provider="Sigenergy",
                confirmed=True,
            )


def test_fusionsolar_to_ready_sigenergy_switch_requires_confirmation(
    tmp_path,
) -> None:
    db_path = tmp_path / "sigenergy-primary-confirmation.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Ready")
        conn.execute(
            """
            UPDATE asset_integrations
            SET is_primary_energy_source = 0
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled,
                is_primary_energy_source
            ) VALUES (?, 'FusionSolar', 'FS-READY', 'Fusion Ready', 1, 1)
            """,
            (asset_id,),
        )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO production_records (
                asset_id, provider, external_id, period_type, period_date,
                production_kwh, consumption_kwh, self_use_kwh, export_kwh,
                grid_import_kwh, data_quality, created_at, updated_at
            ) VALUES (
                ?, 'Sigenergy', ?, 'month', '2026-01-01',
                100, 80, 60, 40, 20, 'complete', ?, ?
            )
            """,
            (asset_id, f"SIG-{asset_id}", now, now),
        )

        with pytest.raises(ValueError, match="Confirma explicitamente"):
            set_asset_primary_energy_source(
                conn,
                asset_id=asset_id,
                provider="Sigenergy",
            )
        set_asset_primary_energy_source(
            conn,
            asset_id=asset_id,
            provider="Sigenergy",
            confirmed=True,
        )
        selected = conn.execute(
            """
            SELECT provider
            FROM asset_integrations
            WHERE asset_id = ? AND is_primary_energy_source = 1
            """,
            (asset_id,),
        ).fetchone()

    assert selected["provider"] == "Sigenergy"


def test_sigenergy_primary_gate_uses_the_selected_mapping_external_id(
    tmp_path,
) -> None:
    db_path = tmp_path / "sigenergy-primary-external-id.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy External ID Gate")
        conn.execute(
            """
            UPDATE asset_integrations
            SET is_primary_energy_source = 0
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO production_records (
                asset_id, provider, external_id, period_type, period_date,
                production_kwh, consumption_kwh, self_use_kwh, export_kwh,
                grid_import_kwh, data_quality, created_at, updated_at
            ) VALUES (
                ?, 'Sigenergy', 'DIFFERENT-SYSTEM', 'month', '2026-01-01',
                100, 80, 60, 40, 20, 'complete', ?, ?
            )
            """,
            (asset_id, now, now),
        )

        with pytest.raises(ValueError, match="mes energetico completo"):
            set_asset_primary_energy_source(
                conn,
                asset_id=asset_id,
                provider="Sigenergy",
                confirmed=True,
            )


def test_sigenergy_primary_selection_targets_the_requested_external_id(
    tmp_path,
) -> None:
    db_path = tmp_path / "sigenergy-primary-explicit-mapping.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = _sigenergy_asset(conn, "Sigenergy Explicit Primary")
        first_external_id = f"SIG-{asset_id}"
        conn.execute(
            """
            UPDATE asset_integrations
            SET is_primary_energy_source = 0
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled,
                is_primary_energy_source
            ) VALUES (
                ?, 'Sigenergy', 'SIG-SECOND', 'Second system', 1, 0
            )
            """,
            (asset_id,),
        )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO production_records (
                asset_id, provider, external_id, period_type, period_date,
                production_kwh, consumption_kwh, self_use_kwh, export_kwh,
                grid_import_kwh, data_quality, created_at, updated_at
            ) VALUES (
                ?, 'Sigenergy', 'SIG-SECOND', 'month', '2026-01-01',
                100, 80, 60, 40, 20, 'complete', ?, ?
            )
            """,
            (asset_id, now, now),
        )

        set_asset_primary_energy_source(
            conn,
            asset_id=asset_id,
            provider="Sigenergy",
            external_id="SIG-SECOND",
            confirmed=True,
        )
        mappings = {
            row["external_id"]: row["is_primary_energy_source"]
            for row in conn.execute(
                """
                SELECT external_id, is_primary_energy_source
                FROM asset_integrations
                WHERE asset_id = ? AND provider = 'Sigenergy'
                """,
                (asset_id,),
            )
        }

    assert mappings == {
        first_external_id: 0,
        "SIG-SECOND": 1,
    }


def _quality_payload(provider: str) -> dict:
    return {
        "asset_id": 1,
        "mapping_status": "mapped",
        "energy_provider": provider,
        "production_quality_status": "complete",
        "production_source": "monthly",
        "availability_pct": "99",
        "invoice_status": "confirmed",
    }


def test_fusionsolar_remains_usable_without_sigenergy_history() -> None:
    payload = {
        **_quality_payload("FusionSolar"),
        "sigenergy_history_status": "missing_permission",
        "sigenergy_energy_unit_confirmed": False,
    }
    assert evaluate_report_quality(payload, scope="individual").status == READY


def test_sigenergy_incomplete_month_and_unit_block_monthly_close() -> None:
    payload = {
        **_quality_payload("Sigenergy"),
        "sigenergy_history_status": "backfill_incomplete",
        "sigenergy_energy_unit_confirmed": False,
    }
    result = evaluate_report_quality(payload, scope="individual")
    assert result.status == BLOCKED
    assert {
        "sigenergy_history_incomplete",
        "sigenergy_energy_unit_unconfirmed",
    } <= {item.code for item in result.blockers}


def test_sigenergy_valid_month_is_ready_after_successful_history_sync() -> None:
    payload = {
        **_quality_payload("Sigenergy"),
        "sigenergy_history_status": "available",
        "sigenergy_energy_unit_confirmed": True,
    }
    assert evaluate_report_quality(payload, scope="individual").status == READY
