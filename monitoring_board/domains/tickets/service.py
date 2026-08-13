"""Ticket workflows that are independent of Flask request globals."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any, Mapping

from monitoring_board.constants import TICKET_MATERIAL_STATUSES, TICKET_WORK_TYPES
from monitoring_board.domains.tickets import repository
from monitoring_board.shared.calendar import build_error_calendar, calendar_month_bounds, normalize_calendar_month


def normalize_optional_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def parse_positive_int(value: str | None, default: int = 0) -> int:
    try:
        parsed = int(float(value or ""))
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def normalize_choice(value: str | None, choices: list[str], default: str) -> str:
    value = (value or "").strip()
    return value if value in choices else default


def build_visits_by_ticket(visits: list[sqlite3.Row]) -> dict[int, list[sqlite3.Row]]:
    visits_by_ticket: dict[int, list[sqlite3.Row]] = {}
    for visit in visits:
        visits_by_ticket.setdefault(visit["ticket_id"], []).append(visit)
    return visits_by_ticket


def group_tickets_by_asset(tickets: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for ticket in tickets:
        asset_id = int(ticket["asset_id"])
        bucket = grouped.setdefault(
            asset_id,
            {
                "asset_id": asset_id,
                "project_name": ticket["project_name"],
                "location": ticket["location"],
                "active_contract": ticket["active_contract"],
                "contract_type": ticket["contract_type"],
                "tickets": [],
            },
        )
        bucket["tickets"].append(ticket)

    ordered = []
    for bucket in grouped.values():
        tickets_list = bucket["tickets"]
        bucket["open_count"] = sum(ticket["status"] != "Fechado" for ticket in tickets_list)
        bucket["critical_count"] = sum(
            ticket["urgency"] == "Critica" and ticket["status"] != "Fechado" for ticket in tickets_list
        )
        bucket["last_update"] = max(ticket["updated_at"] for ticket in tickets_list)
        ordered.append(bucket)

    ordered.sort(
        key=lambda item: (
            0 if item["active_contract"] == "yes" else 1,
            -item["critical_count"],
            -item["open_count"],
            item["project_name"].lower(),
        )
    )
    return ordered


def create_ticket(conn: sqlite3.Connection, form: Mapping[str, str]) -> int | None:
    title = form.get("title", "").strip()
    if not title:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    asset_id = int(form["asset_id"])
    repository.create_ticket(
        conn,
        {
            "asset_id": asset_id,
            "title": title,
            "urgency": form.get("urgency", "Media"),
            "status": form.get("status", "Aberto"),
            "installation_ref": form.get("installation_ref", "").strip(),
            "notes": form.get("notes", "").strip(),
            "next_action": form.get("next_action", "").strip(),
            "planned_date": normalize_optional_date(form.get("planned_date")),
            "due_date": normalize_optional_date(form.get("due_date")),
            "estimated_minutes": parse_positive_int(form.get("estimated_minutes"), default=60),
            "assigned_to": form.get("assigned_to", "").strip(),
            "material_status": normalize_choice(
                form.get("material_status", "Nao definido"), TICKET_MATERIAL_STATUSES, "Nao definido"
            ),
            "work_type": normalize_choice(form.get("work_type", "Diagnostico"), TICKET_WORK_TYPES, "Diagnostico"),
            "planning_notes": form.get("planning_notes", "").strip(),
            "created_at": now,
            "updated_at": now,
        },
    )
    conn.commit()
    return asset_id


def update_ticket(conn: sqlite3.Connection, ticket_id: int, form: Mapping[str, str]) -> sqlite3.Row | None:
    ticket = repository.get_ticket(conn, ticket_id)
    repository.update_ticket(
        conn,
        ticket_id,
        {
            "status": form.get("status", "Aberto"),
            "urgency": form.get("urgency", "Media"),
            "next_action": form.get("next_action", "").strip(),
            "notes": form.get("notes", "").strip(),
            "planned_date": normalize_optional_date(form.get("planned_date")),
            "due_date": normalize_optional_date(form.get("due_date")),
            "estimated_minutes": parse_positive_int(form.get("estimated_minutes"), default=60),
            "assigned_to": form.get("assigned_to", "").strip(),
            "material_status": normalize_choice(
                form.get("material_status", "Nao definido"), TICKET_MATERIAL_STATUSES, "Nao definido"
            ),
            "work_type": normalize_choice(form.get("work_type", "Diagnostico"), TICKET_WORK_TYPES, "Diagnostico"),
            "planning_notes": form.get("planning_notes", "").strip(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    conn.commit()
    return ticket


def add_ticket_visit(conn: sqlite3.Connection, ticket_id: int, form: Mapping[str, str]) -> sqlite3.Row | None:
    ticket = repository.get_ticket(conn, ticket_id)
    repository.add_visit(
        conn,
        ticket_id,
        {
            "visit_date": form.get("visit_date", date.today().isoformat()),
            "technician": form.get("technician", "").strip(),
            "result": form.get("result", "").strip(),
            "notes": form.get("notes", "").strip(),
            "next_action": form.get("next_action", "").strip(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    conn.commit()
    return ticket


def delete_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row | None:
    ticket = repository.get_ticket(conn, ticket_id)
    if ticket is None:
        return None
    repository.delete_ticket(conn, ticket_id)
    conn.commit()
    return ticket


def ticket_page_context(conn: sqlite3.Connection, args: Mapping[str, str]) -> dict[str, Any]:
    filters = {
        "search": args.get("search", "").strip(),
        "asset_id": args.get("asset_id", "").strip(),
        "status": args.get("status", "").strip(),
        "urgency": args.get("urgency", "").strip(),
        "scope": args.get("scope", "").strip(),
        "om_only": args.get("om_only", "yes").strip(),
    }
    calendar_month = normalize_calendar_month(args.get("calendar_month", ""))
    ticket_rows = repository.list_tickets(conn, filters)
    grouped_tickets = group_tickets_by_asset(ticket_rows)
    calendar_start, calendar_end, previous_month, next_month = calendar_month_bounds(calendar_month)
    calendar_rows = repository.list_error_calendar_rows(
        conn, filters, calendar_start.isoformat(), calendar_end.isoformat()
    )

    selected_asset = None
    central_history: list[sqlite3.Row] = []
    central_summary = None
    if filters["asset_id"]:
        selected_asset = repository.get_asset(conn, filters["asset_id"])
        central_history = repository.list_asset_ticket_history(conn, filters["asset_id"])
        central_summary = {
            "total": len(central_history),
            "open": sum(ticket["status"] != "Fechado" for ticket in central_history),
            "critical": sum(
                ticket["urgency"] == "Critica" and ticket["status"] != "Fechado" for ticket in central_history
            ),
            "visits": sum(ticket["visit_count"] for ticket in central_history),
        }

    return {
        "tickets": ticket_rows,
        "grouped_tickets": grouped_tickets,
        "assets": repository.list_assets(conn),
        "visits_by_ticket": build_visits_by_ticket(repository.list_visits(conn)),
        "selected_asset": selected_asset,
        "central_history": central_history,
        "central_summary": central_summary,
        "ticket_stats": {
            "centrals": len(grouped_tickets),
            "tickets": len(ticket_rows),
            "open": sum(ticket["status"] != "Fechado" for ticket in ticket_rows),
            "critical": sum(
                ticket["urgency"] == "Critica" and ticket["status"] != "Fechado" for ticket in ticket_rows
            ),
        },
        "error_calendar": build_error_calendar(calendar_month, calendar_rows),
        "calendar_month": calendar_month,
        "previous_month": previous_month,
        "next_month": next_month,
        "search": filters["search"],
        "asset_filter": filters["asset_id"],
        "status_filter": filters["status"],
        "urgency_filter": filters["urgency"],
        "scope": filters["scope"],
        "om_only": filters["om_only"],
    }
