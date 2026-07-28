from __future__ import annotations

from datetime import date, datetime

from app import (
    build_fusionsolar_customer_production_report,
    ensure_database,
)
from monitoring_board.db import get_db


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
