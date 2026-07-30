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


HISTORY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sigenergy" / "system_history_day.json"
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
