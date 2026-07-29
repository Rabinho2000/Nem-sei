from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from werkzeug.datastructures import MultiDict

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.portfolio_repository import create_portfolio
from monitoring_board.report_template_repository import (
    create_generation_run,
    finish_generation_run,
    get_default_template,
    list_report_automations,
    save_report_automation,
)
from monitoring_board.reporting.quality_gate import evaluate_report_quality
from monitoring_board.reporting.snapshots import (
    approve_snapshot,
    create_snapshot,
    reject_snapshot,
    validate_snapshot,
)
from monitoring_board.reporting.templates import template_to_config


def seed_asset(conn: sqlite3.Connection, name: str = "Central Automática") -> int:
    cursor = conn.execute(
        """
        INSERT INTO assets (project_name, active_contract, contract_type, kwp)
        VALUES (?, 'yes', 'ESCO', '25')
        """,
        (name,),
    )
    asset_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, 'FusionSolar', ?, ?, 1)
        """,
        (asset_id, f"AUTO-{asset_id}", name),
    )
    conn.commit()
    return asset_id


@pytest.fixture()
def reports_client(tmp_path):
    db_path = tmp_path / "report-automations.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"
        session["csrf_token"] = "token"
    try:
        yield client, db_path, asset_id
    finally:
        flask_app.config["DATABASE"] = previous_db


def test_reports_page_has_real_tabs_navigation_and_empty_state(reports_client) -> None:
    client, _db_path, _asset_id = reports_client

    response = client.get("/exports?tab=automations")
    html = response.data.decode()

    assert response.status_code == 200
    assert "Relatórios" in html
    assert "Gerar agora" in html
    assert 'aria-current="page">Automatizações' in html
    assert "Templates e regras" in html
    assert "Sem automatizações" in html
    assert "Email interno" not in html
    assert "Dados usados" not in html
    assert "Instalações disponíveis" not in html


def test_create_edit_toggle_and_delete_report_automation(reports_client, monkeypatch) -> None:
    client, db_path, asset_id = reports_client
    monkeypatch.setattr(app_module, "refresh_integration_scheduler", lambda _app: None)
    with get_db(str(db_path)) as conn:
        template = get_default_template(conn, "individual")

    create_response = client.post(
        "/report-automations",
        data={
            "csrf_token": "token",
            "name": "Fecho mensal",
            "report_type": "individual",
            "asset_id": str(asset_id),
            "template_id": str(template.id),
            "schedule_day": "3",
            "schedule_time": "08:15",
            "active": "on",
            "include_excel": "on",
        },
    )
    assert create_response.status_code in {302, 303}
    with get_db(str(db_path)) as conn:
        rows = list_report_automations(conn)
        assert len(rows) == 1
        automation_id = int(rows[0]["id"])
        assert rows[0]["name"] == "Fecho mensal"
        assert rows[0]["formats_json"] == '["pdf", "excel"]'

    toggle_response = client.post(
        f"/report-automations/{automation_id}/toggle",
        data={"csrf_token": "token"},
    )
    assert toggle_response.status_code in {302, 303}
    with get_db(str(db_path)) as conn:
        assert conn.execute("SELECT active FROM report_automations WHERE id = ?", (automation_id,)).fetchone()["active"] == 0

    delete_response = client.post(
        f"/report-automations/{automation_id}/delete",
        data={"csrf_token": "token"},
    )
    assert delete_response.status_code in {302, 303}
    with get_db(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_automations").fetchone()[0] == 0


def test_automation_rejects_template_from_wrong_scope(reports_client, monkeypatch) -> None:
    client, db_path, asset_id = reports_client
    monkeypatch.setattr(app_module, "refresh_integration_scheduler", lambda _app: None)
    with get_db(str(db_path)) as conn:
        template = get_default_template(conn, "portfolio")

    response = client.post(
        "/report-automations",
        data={
            "csrf_token": "token",
            "name": "Scope inválido",
            "report_type": "individual",
            "asset_id": str(asset_id),
            "template_id": str(template.id),
            "schedule_day": "2",
            "schedule_time": "09:00",
            "active": "on",
        },
    )

    assert response.status_code in {302, 303}
    with get_db(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_automations").fetchone()[0] == 0


def test_create_portfolio_report_automation(reports_client, monkeypatch) -> None:
    client, db_path, _asset_id = reports_client
    monkeypatch.setattr(app_module, "refresh_integration_scheduler", lambda _app: None)
    with get_db(str(db_path)) as conn:
        portfolio_id = create_portfolio(conn, name="Portefólio Automático")
        template = get_default_template(conn, "portfolio", portfolio_id)
        conn.commit()

    response = client.post(
        "/report-automations",
        data={
            "csrf_token": "token",
            "name": "Fecho do portefólio",
            "report_type": "portfolio",
            "portfolio_id": str(portfolio_id),
            "template_id": str(template.id),
            "schedule_day": "4",
            "schedule_time": "07:45",
            "active": "on",
            "include_availability": "on",
        },
    )

    assert response.status_code in {302, 303}
    with get_db(str(db_path)) as conn:
        row = conn.execute("SELECT * FROM report_automations").fetchone()
        assert row["report_type"] == "portfolio"
        assert row["portfolio_id"] == portfolio_id
        assert row["asset_id"] is None
        assert row["include_availability"] == 1


@dataclass
class FakeJob:
    id: str
    next_run_time: Any = None


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.add_calls: list[dict[str, Any]] = []

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def add_job(self, **kwargs):
        self.add_calls.append(kwargs)
        self.jobs[str(kwargs["id"])] = FakeJob(str(kwargs["id"]))

    def remove_job(self, job_id: str):
        self.jobs.pop(job_id, None)


def test_refresh_scheduler_registers_active_report_automation(reports_client) -> None:
    _client, db_path, asset_id = reports_client
    flask_app = app_module.app
    with get_db(str(db_path)) as conn:
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Scheduler mensal",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=7,
            schedule_time="06:30",
            formats=["pdf"],
            include_availability=0,
        )
        conn.commit()
    original_scheduler = app_module.SCHEDULER
    fake = FakeScheduler()
    app_module.SCHEDULER = fake
    try:
        app_module.refresh_integration_scheduler(flask_app)
    finally:
        app_module.SCHEDULER = original_scheduler

    call = next(item for item in fake.add_calls if item["id"] == f"report-automation-{automation_id}")
    assert call["trigger"] == "cron"
    assert call["day"] == 7
    assert call["hour"] == 6
    assert call["minute"] == 30
    assert call["timezone"] == "Europe/Lisbon"
    assert call["replace_existing"] is True
    assert call["max_instances"] == 1
    assert call["coalesce"] is True


def test_completed_automation_period_is_not_generated_twice(tmp_path) -> None:
    db_path = tmp_path / "automation-dedup.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Sem duplicados",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        run_id = create_generation_run(
            conn,
            template_id=template.id,
            template_version=1,
            report_type="individual",
            asset_id=asset_id,
            automation_id=automation_id,
            period_type="monthly",
            period_start="2026-06-01",
            period_end="2026-06-30",
            requested_count=1,
        )
        finish_generation_run(conn, run_id, status="completed", completed_count=1, failed_count=0)
        conn.commit()
        result = app_module.execute_persisted_report_generation(
            conn,
            MultiDict(
                [
                    ("report_type", "individual"),
                    ("asset_id", str(asset_id)),
                    ("template_id", str(template.id)),
                    ("period_type", "monthly"),
                    ("report_month", "2026-06"),
                    ("formats", "pdf"),
                ]
            ),
            output_dir=tmp_path / "generated",
            automation_id=automation_id,
        )

    assert result["duplicate"] is True
    assert result["run_id"] == run_id


def test_stale_automated_run_is_recovered_after_restart(tmp_path) -> None:
    db_path = tmp_path / "automation-recovery.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Recuperável",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        run_id = create_generation_run(
            conn,
            template_id=template.id,
            template_version=1,
            report_type="individual",
            asset_id=asset_id,
            automation_id=automation_id,
            period_type="monthly",
            period_start="2026-05-01",
            period_end="2026-05-31",
            requested_count=1,
        )
        conn.execute(
            "UPDATE report_generation_runs SET created_at = datetime('now', '-3 hours') WHERE id = ?",
            (run_id,),
        )
        conn.commit()

        recovered = app_module.recover_stale_report_automation_runs(conn)
        run = conn.execute("SELECT status, error_message FROM report_generation_runs WHERE id = ?", (run_id,)).fetchone()

    assert recovered == 1
    assert run["status"] == "failed"
    assert "interrompida" in run["error_message"]


def test_automation_without_approved_snapshot_is_blocked(tmp_path) -> None:
    db_path = tmp_path / "automation-failure.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Template indisponível",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        conn.commit()

        result = app_module.run_report_automation_generation(
            conn, automation_id, reference_date=date(2026, 7, 5)
        )
        run = conn.execute(
            "SELECT status, error_message FROM report_generation_runs WHERE automation_id = ?",
            (automation_id,),
        ).fetchone()

    assert result["status"] == "blocked"
    assert run["status"] == "blocked"
    assert "snapshot aprovado" in run["error_message"]


def test_automation_generates_only_from_approved_snapshot(tmp_path) -> None:
    db_path = tmp_path / "automation-approved.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Snapshot aprovado",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf", "excel"],
            include_availability=0,
        )
        payload = {
            "asset_id": asset_id,
            "installation": "Central congelada",
            "energy_provider": "FusionSolar",
            "production_quality_status": "complete",
            "production_source": "monthly",
            "production_kwh": "321.00",
            "availability_pct": "99.0",
            "invoice_status": "confirmed",
        }
        snapshot_id = create_snapshot(
            conn,
            scope_type="individual",
            asset_id=asset_id,
            period_type="monthly",
            period_start="2026-06-01",
            period_end="2026-06-30",
            payload=payload,
            template_id=template.id,
            template_version=1,
            template_snapshot=template_to_config(template),
            engine_version="test-engine",
        )
        quality = evaluate_report_quality(payload, scope="individual")
        validate_snapshot(conn, snapshot_id, quality)
        approve_snapshot(conn, snapshot_id, actor="tester")
        conn.execute("UPDATE assets SET project_name = 'Nome mutável' WHERE id = ?", (asset_id,))
        conn.commit()

        result = app_module.run_report_automation_generation(
            conn,
            automation_id,
            reference_date=date(2026, 7, 5),
        )
        files = conn.execute(
            "SELECT * FROM report_generated_files WHERE run_id = ? ORDER BY format",
            (result["run_id"],),
        ).fetchall()
        duplicate = app_module.run_report_automation_generation(
            conn,
            automation_id,
            reference_date=date(2026, 7, 5),
        )

    assert result["status"] == "completed"
    assert result["snapshot_id"] == snapshot_id
    assert {row["format"] for row in files} == {"pdf", "xlsx"}
    assert all(row["snapshot_id"] == snapshot_id and row["sha256"] for row in files)
    assert duplicate["duplicate"] is True


def test_rejected_snapshot_does_not_bypass_automation_gate(tmp_path) -> None:
    db_path = tmp_path / "automation-rejected.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = seed_asset(conn)
        template = get_default_template(conn, "individual")
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Snapshot rejeitado",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        payload = {
            "asset_id": asset_id,
            "energy_provider": "FusionSolar",
            "production_quality_status": "partial",
        }
        snapshot_id = create_snapshot(
            conn,
            scope_type="individual",
            asset_id=asset_id,
            period_type="monthly",
            period_start="2026-06-01",
            period_end="2026-06-30",
            payload=payload,
            template_id=template.id,
            template_version=1,
            template_snapshot=template_to_config(template),
            engine_version="test-engine",
        )
        reject_snapshot(conn, snapshot_id, actor="tester", reason="Produção parcial")
        conn.commit()

        result = app_module.run_report_automation_generation(
            conn, automation_id, reference_date=date(2026, 7, 5)
        )
        file_count = conn.execute(
            "SELECT COUNT(*) FROM report_generated_files"
        ).fetchone()[0]

    assert result["status"] == "blocked"
    assert file_count == 0
