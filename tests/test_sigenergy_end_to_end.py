from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.report_artifact_verifier import verify_expertcom_report
from monitoring_board.report_template_repository import list_templates
from monitoring_board.reporting.energy_sources import (
    set_asset_primary_energy_source,
)
from monitoring_board.services.sigenergy_contracts import DiscoveryStatus
from monitoring_board.services.sigenergy_errors import SigenergyApiError


SYSTEM_ID = "TZXRS1780315946"


class SimulatedSigenergyApi:
    calls: list[tuple[str, str]] = []

    def __init__(self, _config: Any, session: Any = None) -> None:
        self.session = session

    def authenticate(self) -> str:
        self.calls.append(("authenticate", ""))
        return "simulated-token"

    def list_systems(self, *, allow_empty: bool = False) -> list[dict[str, Any]]:
        assert allow_empty is True
        self.calls.append(("discovery", ""))
        raise SigenergyApiError(
            "Access restriction",
            api_code=1201,
        )

    def get_energy_flow(self, system_id: str) -> dict[str, Any]:
        self.calls.append(("energy_flow", system_id))
        return {
            "systemStatus": "Normal",
            "acPower": 4.1,
            "batteryPower": 0.2,
            "batterySoc": 78,
            "evPower": 0,
            "gridPower": -0.8,
            "heatPumpPower": 0,
            "loadPower": 3.3,
            "pvPower": 4.1,
        }

    def get_system_history(
        self,
        system_id: str,
        *,
        level: str,
        target_date: str,
    ) -> dict[str, Any]:
        assert level == "Day"
        self.calls.append(("history", f"{system_id}:{target_date}"))
        return {
            "unit": "kWh",
            "powerGenerationKwh": 100,
            "powerUseKwh": 80,
            "powerOneselfKwh": 60,
            "powerSelfConsumptionKwh": 65,
            "powerToGridKwh": 40,
            "powerFromGridKwh": 20,
            "esChargingKwh": 5,
            "esDischargingKwh": 4,
            "itemList": [],
        }


def _run_pending_job(flask_app, job_id: int) -> None:
    app_module.run_background_job(flask_app, job_id)
    with get_db(flask_app.config["DATABASE"]) as conn:
        job = conn.execute(
            "SELECT status, error_message FROM background_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert job["status"] == "success", job["error_message"]


def test_discovery_restricted_to_real_internal_pdf_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-contract-e2e.db"
    storage_root = tmp_path / "uploads" / "generated_reports"
    app_module.ensure_database(str(db_path))
    SimulatedSigenergyApi.calls = []
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EXTERNAL_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("SIGENERGY_HISTORY_ENERGY_UNIT", "kWh")
    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        SimulatedSigenergyApi,
    )
    monkeypatch.setattr(
        app_module,
        "execute_queued_sigenergy_call",
        lambda _conn, _config, callback, **_kwargs: callback(),
    )
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        app_module,
        "refresh_integration_scheduler",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "current_lisbon_date",
        lambda: date(2026, 7, 30),
    )
    monkeypatch.setattr(app_module, "UPLOAD_DIR", storage_root.parent)
    monkeypatch.setattr(
        app_module,
        "store_runtime_relative_path",
        lambda path: str(Path(path).resolve()),
    )

    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        conn.execute(
            """
            UPDATE integration_configs
            SET username = 'simulated-key',
                password = 'simulated-secret',
                enabled = 1,
                auto_sync_enabled = 1,
                updated_at = '2026-07-30T18:00:00'
            WHERE provider = 'Sigenergy'
            """
        )
        conn.commit()

        credential = app_module.test_sigenergy_credentials(conn)
        discovery = app_module.discover_sigenergy_systems(
            conn,
            persist=True,
        )
        conn.commit()

    assert credential.authenticated is True
    assert discovery.status is DiscoveryStatus.RESTRICTED

    flask_app = app_module.app
    previous_database = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "e2e-admin"
        session["csrf_token"] = "token"
    try:
        preview_response = client.post(
            "/integrations/import",
            data={
                "csrf_token": "token",
                "provider": "Sigenergy",
                "mode": "create",
                "action": "preview",
                "external_id": SYSTEM_ID,
                "seed_json": json.dumps(
                    {
                        "systemId": SYSTEM_ID,
                        "systemName": "Expertcom",
                    }
                ),
            },
        )
        assert preview_response.status_code in {302, 303}
        with get_db(str(db_path)) as conn:
            installation_import = conn.execute(
                """
                SELECT id, background_job_id
                FROM installation_imports
                WHERE provider = 'Sigenergy' AND external_id = ?
                """,
                (SYSTEM_ID,),
            ).fetchone()
        import_id = int(installation_import["id"])
        preview_job_id = int(installation_import["background_job_id"])
        _run_pending_job(flask_app, preview_job_id)

        with get_db(str(db_path)) as conn:
            preview = conn.execute(
                """
                SELECT status, access_status, validation_method
                FROM installation_imports
                WHERE id = ?
                """,
                (import_id,),
            ).fetchone()
            assert dict(preview) == {
                "status": "preview",
                "access_status": "accessible",
                "validation_method": "direct_energy_flow",
            }
            assert conn.execute(
                "SELECT COUNT(*) FROM assets"
            ).fetchone()[0] == 0

        confirmation = client.post(
            f"/integrations/import/{import_id}/confirm",
            data={
                "csrf_token": "token",
                "project_name": "Expertcom",
                "company_name": "Expertcom",
                "country": "Portugal",
                "timezone": "Europe/Lisbon",
                "kwp": "10",
                "kwac": "10",
                "enable_auto_sync": "on",
            },
        )
        assert confirmation.status_code in {302, 303}
        with get_db(str(db_path)) as conn:
            installation_import = conn.execute(
                """
                SELECT asset_id, background_job_id, status
                FROM installation_imports
                WHERE id = ?
                """,
                (import_id,),
            ).fetchone()
            asset_id = int(installation_import["asset_id"])
            state_job_id = int(installation_import["background_job_id"])
            mapping = conn.execute(
                """
                SELECT enabled, is_primary_energy_source
                FROM asset_integrations
                WHERE provider = 'Sigenergy' AND external_id = ?
                """,
                (SYSTEM_ID,),
            ).fetchone()
        assert installation_import["status"] == "completed"
        assert mapping["enabled"] == 1
        assert mapping["is_primary_energy_source"] == 0
        _run_pending_job(flask_app, state_job_id)

        with get_db(str(db_path)) as conn:
            jobs = app_module.enqueue_sigenergy_energy_backfill(
                conn,
                external_id=SYSTEM_ID,
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
            )
            conn.commit()
        assert len(jobs) == 30
        assert all(created for _job_id, created in jobs)
        for history_job_id, _created in jobs:
            _run_pending_job(flask_app, history_job_id)

        with get_db(str(db_path)) as conn:
            daily_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM production_records
                WHERE asset_id = ? AND provider = 'Sigenergy'
                  AND external_id = ? AND period_type = 'day'
                  AND period_date BETWEEN '2026-06-01' AND '2026-06-30'
                  AND data_quality = 'complete'
                """,
                (asset_id, SYSTEM_ID),
            ).fetchone()[0]
            fact_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM energy_interval_facts
                WHERE asset_id = ? AND provider = 'Sigenergy'
                  AND external_id = ? AND granularity = 'day'
                  AND substr(period_start, 1, 10)
                      BETWEEN '2026-06-01' AND '2026-06-30'
                  AND data_quality = 'complete'
                """,
                (asset_id, SYSTEM_ID),
            ).fetchone()[0]
            month = conn.execute(
                """
                SELECT production_kwh, data_quality
                FROM production_records
                WHERE asset_id = ? AND provider = 'Sigenergy'
                  AND external_id = ? AND period_type = 'month'
                  AND period_date = '2026-06-01'
                """,
                (asset_id, SYSTEM_ID),
            ).fetchone()
            set_asset_primary_energy_source(
                conn,
                asset_id=asset_id,
                provider="Sigenergy",
                confirmed=True,
            )
            conn.commit()
            template_id = next(
                int(row["id"])
                for row in list_templates(conn, "individual")
                if row["name"] == "Individual padrao"
            )

        assert daily_count == 30
        assert fact_count == 30
        assert month["data_quality"] == "complete"
        assert month["production_kwh"] == 3000

        report_response = client.post(
            "/report-generation",
            data={
                "csrf_token": "token",
                "report_type": "individual",
                "template_id": str(template_id),
                "asset_id": str(asset_id),
                "report_month": "2026-06",
                "period_type": "monthly",
                "formats": ["pdf"],
            },
        )
        assert report_response.status_code in {302, 303}

        with get_db(str(db_path)) as conn:
            generated = conn.execute(
                """
                SELECT generated.id, generated.run_id
                FROM report_generated_files generated
                JOIN report_generation_runs run ON run.id = generated.run_id
                WHERE generated.asset_id = ?
                  AND generated.format = 'pdf'
                  AND generated.status = 'completed'
                  AND run.status = 'completed'
                ORDER BY generated.id DESC
                LIMIT 1
                """,
                (asset_id,),
            ).fetchone()
            verification = verify_expertcom_report(
                conn,
                storage_root=storage_root,
                file_id=int(generated["id"]),
            )

        download = client.get(verification["download_path"])
    finally:
        flask_app.config["DATABASE"] = previous_database
        flask_app.config["TESTING"] = previous_testing

    assert download.status_code == 200
    assert download.mimetype == "application/pdf"
    assert verification["status"] == "validated"
    assert verification["daily_records"] == 30
    assert verification["energy_interval_facts"] == 30
    assert verification["production_kwh"] == 3000
    assert verification["pdf_text_validated"] is True
    assert verification["storage_integrity"] == "valid"
    assert SimulatedSigenergyApi.calls[0] == ("authenticate", "")
    assert SimulatedSigenergyApi.calls[1] == ("discovery", "")
    assert (
        "discovery",
        "",
    ) not in SimulatedSigenergyApi.calls[2:]
    assert (
        sum(1 for operation, _value in SimulatedSigenergyApi.calls if operation == "history")
        == 30
    )
