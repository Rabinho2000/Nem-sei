"""SQLite queries owned by the tickets domain."""
from __future__ import annotations

import sqlite3
from typing import Any

from monitoring_board.db import query_all


def create_ticket(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO tickets (
            asset_id, title, urgency, status, installation_ref, notes, next_action,
            planned_date, due_date, estimated_minutes, assigned_to, material_status,
            work_type, planning_notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["asset_id"],
            values["title"],
            values["urgency"],
            values["status"],
            values["installation_ref"],
            values["notes"],
            values["next_action"],
            values["planned_date"],
            values["due_date"],
            values["estimated_minutes"],
            values["assigned_to"],
            values["material_status"],
            values["work_type"],
            values["planning_notes"],
            values["created_at"],
            values["updated_at"],
        ),
    )


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT asset_id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def update_ticket(conn: sqlite3.Connection, ticket_id: int, values: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE tickets
        SET status = ?, urgency = ?, next_action = ?, notes = ?,
            planned_date = ?, due_date = ?, estimated_minutes = ?, assigned_to = ?,
            material_status = ?, work_type = ?, planning_notes = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            values["status"],
            values["urgency"],
            values["next_action"],
            values["notes"],
            values["planned_date"],
            values["due_date"],
            values["estimated_minutes"],
            values["assigned_to"],
            values["material_status"],
            values["work_type"],
            values["planning_notes"],
            values["updated_at"],
            ticket_id,
        ),
    )


def add_visit(conn: sqlite3.Connection, ticket_id: int, values: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT INTO ticket_visits (ticket_id, visit_date, technician, result, notes, next_action)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            values["visit_date"],
            values["technician"],
            values["result"],
            values["notes"],
            values["next_action"],
        ),
    )
    if values["next_action"]:
        conn.execute(
            "UPDATE tickets SET next_action = ?, updated_at = ? WHERE id = ?",
            (values["next_action"], values["updated_at"], ticket_id),
        )


def delete_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))


def list_tickets(conn: sqlite3.Connection, filters: dict[str, str]) -> list[sqlite3.Row]:
    conditions: list[str] = []
    params: list[Any] = []
    search = filters["search"]
    if search:
        wildcard = f"%{search}%"
        conditions.append(
            "(a.project_name LIKE ? OR a.alias_blob LIKE ? OR t.title LIKE ? OR COALESCE(t.notes, '') LIKE ?)"
        )
        params.extend([wildcard, wildcard, wildcard, wildcard])
    if filters["asset_id"]:
        conditions.append("a.id = ?")
        params.append(filters["asset_id"])
    if filters["status"]:
        conditions.append("t.status = ?")
        params.append(filters["status"])
    if filters["urgency"]:
        conditions.append("t.urgency = ?")
        params.append(filters["urgency"])
    if filters["scope"] == "open":
        conditions.append("t.status != 'Fechado'")
    if filters["om_only"] == "yes":
        conditions.append("a.active_contract = 'yes'")

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return query_all(
        conn,
        f"""
        SELECT t.*, a.project_name, a.location, a.active_contract, a.contract_type
        FROM tickets t
        JOIN assets a ON a.id = t.asset_id
        {where_sql}
        ORDER BY
            CASE a.active_contract WHEN 'yes' THEN 1 ELSE 2 END,
            a.project_name COLLATE NOCASE,
            CASE t.status
                WHEN 'Aberto' THEN 1 WHEN 'Em analise' THEN 2 WHEN 'Agendado' THEN 3
                WHEN 'Em visita' THEN 4 WHEN 'Resolvido' THEN 5 ELSE 6
            END,
            CASE t.urgency
                WHEN 'Critica' THEN 1 WHEN 'Alta' THEN 2 WHEN 'Media' THEN 3 ELSE 4
            END,
            t.updated_at DESC, t.id DESC
        """,
        params,
    )


def list_assets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(conn, "SELECT id, project_name FROM assets ORDER BY project_name COLLATE NOCASE")


def list_visits(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(conn, "SELECT * FROM ticket_visits ORDER BY visit_date DESC, id DESC")


def list_error_calendar_rows(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    start_date: str,
    end_date: str,
) -> list[sqlite3.Row]:
    conditions = ["mr.status IN ('Erro', 'Desconectada')", "mr.record_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]
    search = filters["search"]
    if search:
        wildcard = f"%{search}%"
        conditions.append(
            "(a.project_name LIKE ? OR a.alias_blob LIKE ? OR COALESCE(mr.notes, '') LIKE ? OR COALESCE(mr.source, '') LIKE ?)"
        )
        params.extend([wildcard, wildcard, wildcard, wildcard])
    if filters["asset_id"]:
        conditions.append("a.id = ?")
        params.append(filters["asset_id"])
    if filters["om_only"] == "yes":
        conditions.append("a.active_contract = 'yes'")

    return query_all(
        conn,
        f"""
        SELECT mr.id, mr.asset_id, mr.status, mr.record_date, mr.notes, mr.source, a.project_name
        FROM monitoring_records mr
        JOIN assets a ON a.id = mr.asset_id
        WHERE {' AND '.join(conditions)}
        ORDER BY
            mr.record_date,
            CASE mr.status WHEN 'Erro' THEN 1 WHEN 'Desconectada' THEN 2 ELSE 3 END,
            a.project_name COLLATE NOCASE,
            mr.id DESC
        """,
        params,
    )


def get_asset(conn: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()


def list_asset_ticket_history(conn: sqlite3.Connection, asset_id: str) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT t.*, (
            SELECT COUNT(*) FROM ticket_visits tv WHERE tv.ticket_id = t.id
        ) AS visit_count
        FROM tickets t
        WHERE t.asset_id = ?
        ORDER BY t.updated_at DESC, t.id DESC
        """,
        (asset_id,),
    )
