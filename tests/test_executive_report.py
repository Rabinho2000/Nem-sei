from __future__ import annotations

from datetime import date, timedelta

from app import build_executive_report_rows, build_monitoring_report_rows, ensure_database
from monitoring_board.db import get_db


def test_executive_report_prioritizes_long_running_problem(tmp_path) -> None:
    db_path = tmp_path / "report.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO assets (project_name, installation_group, active_contract, maintenance)
            VALUES ('Central Critica', 'Central Critica', 'yes', 'yes')
            """
        )
        asset_id = conn.execute("SELECT id FROM assets WHERE project_name = 'Central Critica'").fetchone()["id"]
        old_date = (date.today() - timedelta(days=8)).isoformat()
        conn.execute(
            """
            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source)
            VALUES (?, 'Erro', ?, 'Falha persistente', 'FusionSolar')
            """,
            (asset_id, old_date),
        )
        conn.commit()

        rows = build_executive_report_rows(conn, {"period": "week"})

        assert rows[0]["project_name"] == "Central Critica"
        assert rows[0]["priority"] == "Critica"
        assert rows[0]["problem_days"] >= 8
    finally:
        conn.close()


def test_monitoring_report_counts_errors_open_tickets_and_visits(tmp_path) -> None:
    db_path = tmp_path / "monitoring_report.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO assets (project_name, installation_group, active_contract, location)
            VALUES ('Central Norte', 'Central Norte', 'yes', 'Porto')
            """
        )
        asset_id = conn.execute("SELECT id FROM assets WHERE project_name = 'Central Norte'").fetchone()["id"]
        today = date.today().isoformat()
        conn.executemany(
            """
            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source)
            VALUES (?, ?, ?, ?, 'FusionSolar')
            """,
            [
                (asset_id, "Erro", today, "Inversor 1",),
                (asset_id, "Erro", today, "Inversor 2",),
                (asset_id, "Desconectada", today, "Sem comunicacao",),
            ],
        )
        conn.execute(
            """
            INSERT INTO tickets (asset_id, title, urgency, status, created_at, updated_at)
            VALUES (?, 'Trocar equipamento', 'Alta', 'Aberto', ?, ?)
            """,
            (asset_id, today, today),
        )
        ticket_id = conn.execute("SELECT id FROM tickets WHERE asset_id = ?", (asset_id,)).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO ticket_visits (ticket_id, visit_date, technician, result)
            VALUES (?, ?, 'Tecnico', 'Visitado')
            """,
            (ticket_id, today),
        )
        conn.commit()

        rows = build_monitoring_report_rows(conn, {"period": "day", "om_only": "yes"})

        assert len(rows) == 1
        assert rows[0]["project_name"] == "Central Norte"
        assert rows[0]["error_records"] == 3
        assert rows[0]["distinct_errors"] == 3
        assert rows[0]["open_tickets"] == 1
        assert rows[0]["visits_period"] == 1
        assert "Inversor 1" in rows[0]["error_types"]
    finally:
        conn.close()
