from __future__ import annotations

from pathlib import Path

from app import app as flask_app
from app import ensure_database
from monitoring_board.db import get_db


def _authenticated_client(database: Path):
    previous_database = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(database)
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "test"
        session["csrf_token"] = "token"
    return client, previous_database


def test_ticket_routes_preserve_legacy_endpoints_and_ticket_workflow(tmp_path: Path) -> None:
    database = tmp_path / "tickets.db"
    ensure_database(str(database))
    conn = get_db(str(database))
    try:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name, active_contract) VALUES (?, ?)",
                ("Central Tickets", "yes"),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    client, previous_database = _authenticated_client(database)
    try:
        assert flask_app.url_map._rules_by_endpoint["tickets"]
        assert flask_app.url_map._rules_by_endpoint["update_ticket"]
        assert flask_app.url_map._rules_by_endpoint["add_visit"]
        assert flask_app.url_map._rules_by_endpoint["delete_ticket"]

        created = client.post(
            "/tickets",
            data={
                "csrf_token": "token",
                "asset_id": str(asset_id),
                "title": "Substituir inversor",
                "urgency": "Alta",
                "status": "Aberto",
                "planned_date": "2026-08-20",
                "estimated_minutes": "90",
                "material_status": "Necessario",
                "work_type": "Inversor",
            },
        )
        assert created.status_code == 302
        assert created.headers["Location"].endswith(f"/tickets?asset_id={asset_id}")

        conn = get_db(str(database))
        try:
            ticket = conn.execute("SELECT * FROM tickets").fetchone()
            assert ticket is not None
            ticket_id = int(ticket["id"])
            assert ticket["title"] == "Substituir inversor"
            assert ticket["estimated_minutes"] == 90
            assert ticket["material_status"] == "Necessario"
        finally:
            conn.close()

        updated = client.post(
            f"/tickets/{ticket_id}/update",
            data={
                "csrf_token": "token",
                "status": "Agendado",
                "urgency": "Critica",
                "next_action": "Confirmar tecnico",
                "planned_date": "2026-08-21",
                "estimated_minutes": "120",
                "material_status": "Pronto",
                "work_type": "Inversor",
            },
        )
        assert updated.status_code == 302

        visited = client.post(
            f"/tickets/{ticket_id}/visit",
            data={
                "csrf_token": "token",
                "visit_date": "2026-08-21",
                "technician": "Tecnico A",
                "result": "Agendado",
                "next_action": "Aguardar material",
            },
        )
        assert visited.status_code == 302
        listed = client.get(f"/tickets?asset_id={asset_id}")
        assert listed.status_code == 200
        assert b"Substituir inversor" in listed.data

        conn = get_db(str(database))
        try:
            ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            visit = conn.execute("SELECT * FROM ticket_visits WHERE ticket_id = ?", (ticket_id,)).fetchone()
            assert ticket["status"] == "Agendado"
            assert ticket["next_action"] == "Aguardar material"
            assert visit["technician"] == "Tecnico A"
        finally:
            conn.close()

        deleted = client.post(f"/tickets/{ticket_id}/delete", data={"csrf_token": "token"})
        assert deleted.status_code == 302
        conn = get_db(str(database))
        try:
            assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM ticket_visits").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        flask_app.config["DATABASE"] = previous_database
