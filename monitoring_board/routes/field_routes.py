from __future__ import annotations

import math
import json
import os
import itertools
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

import requests
from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

from monitoring_board.db import ensure_column, get_db, query_all


field_routes_bp = Blueprint("field_routes", __name__, url_prefix="/field-routes")

PROBLEM_STATUSES = {"Erro", "Desconectada"}
OPEN_TICKET_SQL = "COALESCE(t.open_tickets, 0)"
OPENROUTESERVICE_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
OPENROUTESERVICE_GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
DEFAULT_DEPOT_NAME = "Solcor Portugal"
DEFAULT_DEPOT_ADDRESS = "Av. Alm. Reis 54 3º, 1150-019 Lisboa"
DEFAULT_DEPOT_LAT = 38.7243
DEFAULT_DEPOT_LNG = -9.1362
EXACT_ROUTE_STOP_LIMIT = 8
PORTUGAL_MAINLAND_DISTRICTS = {
    "aveiro",
    "beja",
    "braga",
    "braganca",
    "castelo branco",
    "coimbra",
    "evora",
    "faro",
    "guarda",
    "leiria",
    "lisboa",
    "portalegre",
    "porto",
    "santarem",
    "setubal",
    "viana do castelo",
    "vila real",
    "viseu",
}

FIELD_ROUTE_PLAN_COLUMNS = [
    "origin_name TEXT",
    "origin_address TEXT",
    "origin_lat REAL",
    "origin_lng REAL",
    "return_to_origin INTEGER DEFAULT 1",
    "total_drive_minutes REAL DEFAULT 0",
    "total_work_minutes REAL DEFAULT 0",
    "total_mission_minutes REAL DEFAULT 0",
    "route_warning TEXT",
    "internal_km_cost REAL DEFAULT 0",
    "client_km_price REAL DEFAULT 0",
    "engineer_count INTEGER DEFAULT 0",
    "technician_count INTEGER DEFAULT 0",
    "engineer_internal_hourly REAL DEFAULT 0",
    "technician_internal_hourly REAL DEFAULT 0",
    "engineer_client_hourly REAL DEFAULT 0",
    "technician_client_hourly REAL DEFAULT 0",
    "tolls_cost REAL DEFAULT 0",
    "lodging_cost REAL DEFAULT 0",
    "meals_cost REAL DEFAULT 0",
    "other_costs REAL DEFAULT 0",
    "extra_margin_pct REAL DEFAULT 0",
    "travel_internal_cost REAL DEFAULT 0",
    "travel_client_price REAL DEFAULT 0",
    "drive_labor_internal_cost REAL DEFAULT 0",
    "work_labor_internal_cost REAL DEFAULT 0",
    "drive_labor_client_price REAL DEFAULT 0",
    "work_labor_client_price REAL DEFAULT 0",
    "total_internal_cost REAL DEFAULT 0",
    "total_client_price REAL DEFAULT 0",
    "estimated_margin REAL DEFAULT 0",
]


@dataclass(frozen=True)
class RouteStop:
    asset_id: int
    project_name: str
    latitude: float
    longitude: float
    work_hours: float
    work_minutes: int = 0
    intervention_notes: str = ""


@dataclass(frozen=True)
class RoutePoint:
    label: str
    latitude: float
    longitude: float
    asset_id: int | None = None


@dataclass(frozen=True)
class RouteSegment:
    sequence_order: int
    from_point: RoutePoint
    to_point: RoutePoint
    distance_km: float
    duration_minutes: float
    geometry: list[list[float]]


def ensure_field_routes_schema(database_path: str) -> None:
    conn = get_db(database_path)
    try:
        ensure_column(conn, "assets", "latitude REAL")
        ensure_column(conn, "assets", "longitude REAL")
        ensure_column(conn, "assets", "route_notes TEXT")
        ensure_column(conn, "assets", "coordinates_source TEXT")
        ensure_column(conn, "assets", "coordinates_confidence TEXT")
        mark_existing_suspicious_coordinates(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS field_route_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                team_name TEXT,
                work_date TEXT,
                origin_name TEXT,
                origin_address TEXT,
                origin_lat REAL,
                origin_lng REAL,
                return_to_origin INTEGER DEFAULT 1,
                total_work_hours REAL DEFAULT 0,
                total_drive_minutes REAL DEFAULT 0,
                total_work_minutes REAL DEFAULT 0,
                total_mission_minutes REAL DEFAULT 0,
                total_distance_km REAL DEFAULT 0,
                route_geometry_json TEXT,
                route_provider TEXT,
                route_warning TEXT,
                internal_km_cost REAL DEFAULT 0,
                client_km_price REAL DEFAULT 0,
                engineer_count INTEGER DEFAULT 0,
                technician_count INTEGER DEFAULT 0,
                engineer_internal_hourly REAL DEFAULT 0,
                technician_internal_hourly REAL DEFAULT 0,
                engineer_client_hourly REAL DEFAULT 0,
                technician_client_hourly REAL DEFAULT 0,
                tolls_cost REAL DEFAULT 0,
                lodging_cost REAL DEFAULT 0,
                meals_cost REAL DEFAULT 0,
                other_costs REAL DEFAULT 0,
                extra_margin_pct REAL DEFAULT 0,
                travel_internal_cost REAL DEFAULT 0,
                travel_client_price REAL DEFAULT 0,
                drive_labor_internal_cost REAL DEFAULT 0,
                work_labor_internal_cost REAL DEFAULT 0,
                drive_labor_client_price REAL DEFAULT 0,
                work_labor_client_price REAL DEFAULT 0,
                total_internal_cost REAL DEFAULT 0,
                total_client_price REAL DEFAULT 0,
                estimated_margin REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS field_route_stops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_plan_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                stop_order INTEGER NOT NULL,
                work_hours REAL DEFAULT 0,
                work_minutes INTEGER DEFAULT 0,
                intervention_notes TEXT,
                distance_from_previous_km REAL DEFAULT 0,
                notes TEXT,
                FOREIGN KEY (route_plan_id) REFERENCES field_route_plans(id) ON DELETE CASCADE,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS field_route_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_plan_id INTEGER NOT NULL,
                sequence_order INTEGER NOT NULL,
                from_label TEXT NOT NULL,
                to_label TEXT NOT NULL,
                from_asset_id INTEGER,
                to_asset_id INTEGER,
                from_lat REAL NOT NULL,
                from_lng REAL NOT NULL,
                to_lat REAL NOT NULL,
                to_lng REAL NOT NULL,
                distance_km REAL DEFAULT 0,
                duration_minutes REAL DEFAULT 0,
                geometry_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (route_plan_id) REFERENCES field_route_plans(id) ON DELETE CASCADE,
                FOREIGN KEY (from_asset_id) REFERENCES assets(id) ON DELETE SET NULL,
                FOREIGN KEY (to_asset_id) REFERENCES assets(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_field_route_stops_plan_order
                ON field_route_stops(route_plan_id, stop_order);

            CREATE INDEX IF NOT EXISTS idx_field_route_segments_plan_order
                ON field_route_segments(route_plan_id, sequence_order);
            """
        )
        ensure_column(conn, "field_route_plans", "route_geometry_json TEXT")
        ensure_column(conn, "field_route_plans", "route_provider TEXT")
        for column_definition in FIELD_ROUTE_PLAN_COLUMNS:
            ensure_column(conn, "field_route_plans", column_definition)
        ensure_column(conn, "field_route_stops", "work_minutes INTEGER DEFAULT 0")
        ensure_column(conn, "field_route_stops", "intervention_notes TEXT")
        conn.commit()
    finally:
        conn.close()


@field_routes_bp.record_once
def register_schema(state: Any) -> None:
    ensure_field_routes_schema(state.app.config["DATABASE"])


@field_routes_bp.route("/", methods=["GET", "POST"])
def field_routes() -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    if request.method == "POST":
        return create_route_plan()

    filters = read_filters()
    assets = fetch_route_assets(g.db, filters)
    latest_plans = fetch_latest_route_plans(g.db)
    mappable_assets = [
        row
        for row in assets
        if row["latitude"] is not None
        and row["longitude"] is not None
        and row["coordinates_confidence"] not in {"suspect", "review"}
    ]
    suspicious_assets = [row for row in assets if row["coordinates_confidence"] in {"suspect", "review"}]
    missing_location_assets = [row for row in assets if row["latitude"] is None or row["longitude"] is None]
    map_payload = [asset_to_payload(row) for row in mappable_assets]
    review_map_payload = [
        asset_to_payload(row)
        for row in suspicious_assets
        if row["latitude"] is not None and row["longitude"] is not None
    ]
    selected_ids = {int(row["id"]) for row in mappable_assets}

    return render_template(
        "field_routes.html",
        title="Mapa e rotas",
        filters=filters,
        assets=assets,
        mappable_assets=mappable_assets,
        missing_location_assets=missing_location_assets,
        suspicious_assets=suspicious_assets,
        map_payload=map_payload,
        review_map_payload=review_map_payload,
        latest_plans=latest_plans,
        selected_ids=selected_ids,
        default_depot=default_depot(),
        today_iso=date.today().isoformat(),
    )


@field_routes_bp.route("/asset/<int:asset_id>/location", methods=["POST"])
def update_asset_location(asset_id: int) -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    latitude = parse_optional_float(request.form.get("latitude"))
    longitude = parse_optional_float(request.form.get("longitude"))
    route_notes = request.form.get("route_notes", "").strip()

    if latitude is not None and not -90 <= latitude <= 90:
        flash("Latitude invalida.", "error")
        return redirect(url_for("field_routes.field_routes"))
    if longitude is not None and not -180 <= longitude <= 180:
        flash("Longitude invalida.", "error")
        return redirect(url_for("field_routes.field_routes"))

    g.db.execute(
        "UPDATE assets SET latitude = ?, longitude = ?, route_notes = ?, coordinates_source = ?, coordinates_confidence = ? WHERE id = ?",
        (latitude, longitude, route_notes, "manual", "manual", asset_id),
    )
    g.db.commit()
    flash("Localizacao da instalacao atualizada.", "success")
    return redirect(url_for("field_routes.field_routes", **read_filters()))


@field_routes_bp.route("/asset/<int:asset_id>/confirm-location", methods=["POST"])
def confirm_asset_location(asset_id: int) -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    asset = g.db.execute(
        "SELECT id, latitude, longitude FROM assets WHERE id = ?",
        (asset_id,),
    ).fetchone()
    if asset is None or asset["latitude"] is None or asset["longitude"] is None:
        flash("Nao ha coordenadas para confirmar nesta instalacao.", "error")
        return redirect(url_for("field_routes.field_routes", **read_filters()))
    g.db.execute(
        "UPDATE assets SET coordinates_source = ?, coordinates_confidence = ? WHERE id = ?",
        ("manual", "manual", asset_id),
    )
    g.db.commit()
    flash("Coordenadas confirmadas.", "success")
    return redirect(url_for("field_routes.field_routes", **read_filters()))


@field_routes_bp.route("/geocode-missing", methods=["POST"])
def geocode_missing_assets() -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    filters = read_filters()
    assets = [
        row
        for row in fetch_route_assets(g.db, filters)
        if row["latitude"] is None or row["longitude"] is None
    ]
    if not openrouteservice_api_key():
        flash("Configura OPENROUTESERVICE_API_KEY no .env para preencher coordenadas automaticamente.", "error")
        return redirect(url_for("field_routes.field_routes", **filters))

    updated_count = 0
    failed_count = 0
    review_count = 0
    for row in assets[:25]:
        result = geocode_asset(row)
        if result is None:
            failed_count += 1
            continue
        if result["confidence"] != "ok":
            g.db.execute(
                """
                UPDATE assets
                SET latitude = NULL,
                    longitude = NULL,
                    coordinates_source = 'openrouteservice',
                    coordinates_confidence = ?,
                    route_notes = ?
                WHERE id = ?
                """,
                (result["confidence"], result["label"], int(row["id"])),
            )
            review_count += 1
            time.sleep(0.15)
            continue
        g.db.execute(
            "UPDATE assets SET latitude = ?, longitude = ?, coordinates_source = ?, coordinates_confidence = ?, route_notes = ? WHERE id = ?",
            (
                result["latitude"],
                result["longitude"],
                "openrouteservice",
                result["confidence"],
                result["label"],
                int(row["id"]),
            ),
        )
        updated_count += 1
        time.sleep(0.15)
    g.db.commit()

    if updated_count:
        flash(f"Coordenadas preenchidas em {updated_count} instalacoes.", "success")
    if failed_count:
        flash(f"Nao foi possivel encontrar coordenadas para {failed_count} instalacoes.", "warning")
    if review_count:
        flash(f"{review_count} resultados ficaram para revisao e nao foram colocados no mapa.", "warning")
    if len(assets) > 25:
        flash("Foram processadas 25 instalacoes para evitar limites da API. Repete a acao para continuar.", "warning")
    return redirect(url_for("field_routes.field_routes", **filters))


@field_routes_bp.route("/import-mymaps", methods=["POST"])
def import_mymaps() -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    filters = read_filters()
    source_url = request.form.get("mymaps_url", "").strip()
    overwrite = request.form.get("overwrite_coordinates") == "on"
    if not source_url:
        flash("Cola o link do Google My Maps antes de importar.", "error")
        return redirect(url_for("field_routes.field_routes", **filters))
    try:
        kml_text = download_mymaps_kml(source_url)
        points = parse_mymaps_kml(kml_text)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("field_routes.field_routes", **filters))

    result = import_mymaps_points(g.db, points, overwrite=overwrite)
    g.db.commit()
    flash(
        f"My Maps importado: {result['updated']} coordenadas atualizadas, {result['preserved']} manuais preservadas, "
        f"{result['unmatched']} sem match e {result['ambiguous']} ambíguas.",
        "success" if result["updated"] else "warning",
    )
    return redirect(url_for("field_routes.field_routes", **filters))


@field_routes_bp.route("/<int:plan_id>")
def route_plan_detail(plan_id: int) -> str:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    plan = g.db.execute("SELECT * FROM field_route_plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        flash("Rota nao encontrada.", "error")
        return redirect(url_for("field_routes.field_routes"))
    stops = query_all(
        g.db,
        """
        SELECT frs.*, a.project_name, a.location, a.address, a.latitude, a.longitude,
               lm.status AS latest_status, lm.record_date AS latest_status_date
        FROM field_route_stops frs
        JOIN assets a ON a.id = frs.asset_id
        LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
        WHERE frs.route_plan_id = ?
        ORDER BY frs.stop_order
        """,
        (plan_id,),
    )
    segments = query_all(
        g.db,
        """
        SELECT *
        FROM field_route_segments
        WHERE route_plan_id = ?
        ORDER BY sequence_order
        """,
        (plan_id,),
    )
    return render_template(
        "field_route_plan.html",
        title=f"Rota {plan['name']}",
        plan=plan,
        stops=stops,
        segments=segments,
        cost_rows=build_cost_rows(plan),
        map_payload=[asset_to_payload(row) for row in stops],
        route_geometry=parse_route_geometry(plan["route_geometry_json"]) or route_geometry_from_segments(segments),
        maps_url=build_google_maps_url(stops),
    )


@field_routes_bp.route("/<int:plan_id>/export.csv")
def export_route_plan_csv(plan_id: int) -> Response:
    ensure_field_routes_schema(current_app.config["DATABASE"])
    plan = g.db.execute("SELECT * FROM field_route_plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        flash("Rota nao encontrada.", "error")
        return redirect(url_for("field_routes.field_routes"))
    segments = query_all(g.db, "SELECT * FROM field_route_segments WHERE route_plan_id = ? ORDER BY sequence_order", (plan_id,))
    stops = query_all(
        g.db,
        """
        SELECT frs.stop_order, a.project_name, frs.intervention_notes, frs.work_minutes
        FROM field_route_stops frs
        JOIN assets a ON a.id = frs.asset_id
        WHERE frs.route_plan_id = ?
        ORDER BY frs.stop_order
        """,
        (plan_id,),
    )
    lines = [
        "tipo,ordem,de,para,instalacao,km,minutos,custo_interno,preco_cliente",
        f"resumo,,{csv_cell(plan['origin_name'])},,,{plan['total_distance_km'] or 0},{plan['total_mission_minutes'] or 0},{plan['total_internal_cost'] or 0},{plan['total_client_price'] or 0}",
    ]
    for segment in segments:
        lines.append(
            f"segmento,{segment['sequence_order']},{csv_cell(segment['from_label'])},{csv_cell(segment['to_label'])},,{segment['distance_km'] or 0},{segment['duration_minutes'] or 0},,"
        )
    for stop in stops:
        lines.append(
            f"paragem,{stop['stop_order']},,,{csv_cell(stop['project_name'])},,{stop['work_minutes'] or 0},,"
        )
    for row in build_cost_rows(plan):
        lines.append(f"custo,,,{csv_cell(row['label'])},,,,{row['internal']},{row['client']}")
    csv_body = "\n".join(lines) + "\n"
    return Response(
        csv_body,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=rota_{plan_id}.csv"},
    )


def create_route_plan() -> str:
    selected_ids = sorted({int(value) for value in request.form.getlist("asset_ids") if value.isdigit()})
    if not selected_ids:
        flash("Escolhe pelo menos uma instalacao com coordenadas.", "error")
        return redirect(url_for("field_routes.field_routes", **read_filters()))

    placeholders = ",".join("?" for _ in selected_ids)
    rows = query_all(
        g.db,
        f"""
        SELECT id, project_name, latitude, longitude
        FROM assets
        WHERE id IN ({placeholders})
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND COALESCE(coordinates_confidence, '') NOT IN ('suspect', 'review')
        """,
        selected_ids,
    )
    origin = read_origin_from_form()
    if origin is None:
        flash("Origem invalida. Confirma latitude e longitude.", "error")
        return redirect(url_for("field_routes.field_routes", **read_filters()))
    return_to_origin = request.form.get("return_to_origin") == "on"
    default_work_minutes = parse_work_minutes(request.form.get("default_work_hours"), request.form.get("default_work_minutes"), fallback_minutes=60)
    stops = []
    for row in rows:
        asset_id = int(row["id"])
        asset_work_minutes = parse_work_minutes(
            request.form.get(f"work_hours_{asset_id}"),
            request.form.get(f"work_minutes_{asset_id}"),
            fallback_minutes=default_work_minutes,
        )
        stops.append(
            RouteStop(
                asset_id,
                row["project_name"],
                float(row["latitude"]),
                float(row["longitude"]),
                round(asset_work_minutes / 60, 2),
                asset_work_minutes,
                request.form.get(f"intervention_notes_{asset_id}", "").strip(),
            )
        )
    if not stops:
        flash("As instalacoes escolhidas nao tem coordenadas validas.", "error")
        return redirect(url_for("field_routes.field_routes", **read_filters()))

    ordered_stops = optimize_route_order(stops, origin, return_to_origin)
    route_points = build_route_points(origin, ordered_stops, return_to_origin)
    segments, route_provider, route_geometry = build_route_segments(route_points)
    if route_provider != "openrouteservice":
        flash("OpenRouteService indisponivel; valores aproximados por distancia direta.", "warning")

    now = datetime.now().isoformat(timespec="seconds")
    name = request.form.get("name", "").strip() or f"Rota {date.today().isoformat()}"
    team_name = request.form.get("team_name", "").strip()
    work_date = request.form.get("work_date", "").strip()
    notes = request.form.get("notes", "").strip()
    total_distance = round(sum(segment.distance_km for segment in segments), 1)
    total_drive_minutes = round(sum(segment.duration_minutes for segment in segments), 1)
    total_work_minutes = sum(stop.work_minutes for stop in ordered_stops)
    total_mission_minutes = round(total_drive_minutes + total_work_minutes, 1)
    total_work = round(total_work_minutes / 60, 2)
    costs = calculate_route_costs(total_distance, total_drive_minutes, total_work_minutes, request.form)

    cursor = g.db.execute(
        """
        INSERT INTO field_route_plans (
            name, team_name, work_date, origin_name, origin_address, origin_lat, origin_lng,
            return_to_origin, total_work_hours, total_drive_minutes, total_work_minutes,
            total_mission_minutes, total_distance_km, route_geometry_json, route_provider,
            route_warning, internal_km_cost, client_km_price, engineer_count, technician_count,
            engineer_internal_hourly, technician_internal_hourly, engineer_client_hourly,
            technician_client_hourly, tolls_cost, lodging_cost, meals_cost, other_costs,
            extra_margin_pct, travel_internal_cost, travel_client_price,
            drive_labor_internal_cost, work_labor_internal_cost, drive_labor_client_price,
            work_labor_client_price, total_internal_cost, total_client_price, estimated_margin,
            created_at, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            team_name,
            work_date,
            origin.label,
            request.form.get("origin_address", "").strip(),
            origin.latitude,
            origin.longitude,
            1 if return_to_origin else 0,
            total_work,
            total_drive_minutes,
            total_work_minutes,
            total_mission_minutes,
            total_distance,
            json.dumps(route_geometry, ensure_ascii=True) if route_geometry else "",
            route_provider,
            "Valores aproximados" if route_provider != "openrouteservice" else "",
            costs["internal_km_cost"],
            costs["client_km_price"],
            costs["engineer_count"],
            costs["technician_count"],
            costs["engineer_internal_hourly"],
            costs["technician_internal_hourly"],
            costs["engineer_client_hourly"],
            costs["technician_client_hourly"],
            costs["tolls_cost"],
            costs["lodging_cost"],
            costs["meals_cost"],
            costs["other_costs"],
            costs["extra_margin_pct"],
            costs["travel_internal_cost"],
            costs["travel_client_price"],
            costs["drive_labor_internal_cost"],
            costs["work_labor_internal_cost"],
            costs["drive_labor_client_price"],
            costs["work_labor_client_price"],
            costs["total_internal_cost"],
            costs["total_client_price"],
            costs["estimated_margin"],
            now,
            notes,
        ),
    )
    plan_id = int(cursor.lastrowid)
    for index, stop in enumerate(ordered_stops, start=1):
        g.db.execute(
            """
            INSERT INTO field_route_stops (
                route_plan_id, asset_id, stop_order, work_hours, work_minutes,
                intervention_notes, distance_from_previous_km
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                stop.asset_id,
                index,
                stop.work_hours,
                stop.work_minutes,
                stop.intervention_notes,
                round(segments[index - 1].distance_km if index - 1 < len(segments) else 0, 1),
            ),
        )
    for segment in segments:
        g.db.execute(
            """
            INSERT INTO field_route_segments (
                route_plan_id, sequence_order, from_label, to_label, from_asset_id, to_asset_id,
                from_lat, from_lng, to_lat, to_lng, distance_km, duration_minutes,
                geometry_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                segment.sequence_order,
                segment.from_point.label,
                segment.to_point.label,
                segment.from_point.asset_id,
                segment.to_point.asset_id,
                segment.from_point.latitude,
                segment.from_point.longitude,
                segment.to_point.latitude,
                segment.to_point.longitude,
                round(segment.distance_km, 2),
                round(segment.duration_minutes, 1),
                json.dumps(segment.geometry, ensure_ascii=True) if segment.geometry else "",
                now,
            ),
        )
    g.db.commit()
    flash("Rota criada.", "success")
    return redirect(url_for("field_routes.route_plan_detail", plan_id=plan_id))


def read_filters() -> dict[str, str]:
    filters = {
        "scope": request.values.get("scope", "problems").strip(),
        "om_only": request.values.get("om_only", "yes").strip(),
        "status": request.values.get("status", "").strip(),
        "search": request.values.get("search", "").strip(),
    }
    if filters["scope"] not in {"problems", "open_tickets", "all"}:
        filters["scope"] = "problems"
    if filters["om_only"] not in {"yes", "no"}:
        filters["om_only"] = "yes"
    return filters


def fetch_route_assets(conn: sqlite3.Connection, filters: dict[str, str]) -> list[sqlite3.Row]:
    conditions = ["COALESCE(a.monitoring_status, 'active') != 'disabled'"]
    params: list[Any] = []
    if filters["om_only"] == "yes":
        conditions.append("COALESCE(a.active_contract, '') = 'yes'")
    if filters["scope"] == "problems":
        conditions.append("lm.status IN ('Erro', 'Desconectada')")
    elif filters["scope"] == "open_tickets":
        conditions.append(f"{OPEN_TICKET_SQL} > 0")
    if filters["status"]:
        conditions.append("lm.status = ?")
        params.append(filters["status"])
    if filters["search"]:
        term = f"%{filters['search']}%"
        conditions.append("(a.project_name LIKE ? OR a.installation_group LIKE ? OR a.location LIKE ? OR a.address LIKE ?)")
        params.extend([term, term, term, term])

    return query_all(
        conn,
        f"""
        SELECT a.id, a.project_name, a.installation_group, a.location, a.address,
               a.active_contract, a.latitude, a.longitude, a.route_notes,
               a.coordinates_source, a.coordinates_confidence,
               lm.status AS latest_status, lm.record_date AS latest_status_date,
               {OPEN_TICKET_SQL} AS open_tickets
        FROM assets a
        LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
        LEFT JOIN (
            SELECT asset_id, COUNT(*) AS open_tickets
            FROM tickets
            WHERE status != 'Fechado'
            GROUP BY asset_id
        ) t ON t.asset_id = a.id
        WHERE {" AND ".join(conditions)}
        ORDER BY
            CASE WHEN lm.status = 'Erro' THEN 0 WHEN lm.status = 'Desconectada' THEN 1 ELSE 2 END,
            {OPEN_TICKET_SQL} DESC,
            a.project_name COLLATE NOCASE
        """,
        params,
    )


def fetch_latest_route_plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT frp.*, COUNT(frs.id) AS stop_count
        FROM field_route_plans frp
        LEFT JOIN field_route_stops frs ON frs.route_plan_id = frp.id
        GROUP BY frp.id
        ORDER BY frp.created_at DESC
        LIMIT 8
        """,
    )


def default_depot() -> dict[str, Any]:
    return {
        "name": os.environ.get("DEFAULT_DEPOT_NAME", DEFAULT_DEPOT_NAME).strip() or DEFAULT_DEPOT_NAME,
        "address": os.environ.get("DEFAULT_DEPOT_ADDRESS", DEFAULT_DEPOT_ADDRESS).strip() or DEFAULT_DEPOT_ADDRESS,
        "lat": parse_optional_float(os.environ.get("DEFAULT_DEPOT_LAT")) or DEFAULT_DEPOT_LAT,
        "lng": parse_optional_float(os.environ.get("DEFAULT_DEPOT_LNG")) or DEFAULT_DEPOT_LNG,
    }


def read_origin_from_form() -> RoutePoint | None:
    depot = default_depot()
    name = request.form.get("origin_name", "").strip() or depot["name"]
    lat = parse_optional_float(request.form.get("origin_latitude"))
    lng = parse_optional_float(request.form.get("origin_longitude"))
    if lat is None:
        lat = float(depot["lat"])
    if lng is None:
        lng = float(depot["lng"])
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return RoutePoint(name, lat, lng, None)


def parse_work_minutes(hours_value: str | None, minutes_value: str | None, fallback_minutes: int = 60) -> int:
    hours = parse_optional_float(hours_value)
    minutes = parse_optional_float(minutes_value)
    if hours is None and minutes is None:
        return max(int(fallback_minutes), 0)
    total = 0
    if hours is not None:
        total += int(round(hours * 60))
    if minutes is not None:
        total += int(round(minutes))
    return max(total, 0)


def stop_point(stop: RouteStop) -> RoutePoint:
    return RoutePoint(stop.project_name, stop.latitude, stop.longitude, stop.asset_id)


def build_route_points(origin: RoutePoint, stops: list[RouteStop], return_to_origin: bool) -> list[RoutePoint]:
    points = [origin] + [stop_point(stop) for stop in stops]
    if return_to_origin:
        points.append(origin)
    return points


def route_distance_for_order(origin: RoutePoint, stops: list[RouteStop], return_to_origin: bool) -> float:
    points = build_route_points(origin, stops, return_to_origin)
    return sum(
        haversine_km(points[index].latitude, points[index].longitude, points[index + 1].latitude, points[index + 1].longitude)
        for index in range(len(points) - 1)
    )


def optimize_route_order(stops: list[RouteStop], origin: RoutePoint, return_to_origin: bool) -> list[RouteStop]:
    if len(stops) <= 1:
        return stops[:]
    if len(stops) <= EXACT_ROUTE_STOP_LIMIT:
        return list(
            min(
                itertools.permutations(stops),
                key=lambda order: route_distance_for_order(origin, list(order), return_to_origin),
            )
        )
    return nearest_neighbour_route(stops, origin)


def nearest_neighbour_route(stops: list[RouteStop], origin: RoutePoint) -> list[RouteStop]:
    # Fallback for larger routes where exhaustive permutation becomes expensive.
    remaining = stops[:]
    ordered: list[RouteStop] = []
    current = origin
    while remaining:
        next_index, next_stop = min(
            enumerate(remaining),
            key=lambda item: haversine_km(current.latitude, current.longitude, item[1].latitude, item[1].longitude),
        )
        ordered.append(next_stop)
        current = stop_point(next_stop)
        remaining.pop(next_index)
    return ordered


def build_route_segments(points: list[RoutePoint]) -> tuple[list[RouteSegment], str, list[list[float]]]:
    if len(points) < 2:
        return [], "local", []
    ors_result = fetch_openrouteservice_segments(points)
    if ors_result is not None:
        return ors_result["segments"], "openrouteservice", ors_result["geometry"]
    return build_fallback_segments(points), "local", []


def build_fallback_segments(points: list[RoutePoint]) -> list[RouteSegment]:
    segments: list[RouteSegment] = []
    for index in range(len(points) - 1):
        from_point = points[index]
        to_point = points[index + 1]
        distance = haversine_km(from_point.latitude, from_point.longitude, to_point.latitude, to_point.longitude)
        duration = (distance / 55) * 60 if distance > 0 else 0
        segments.append(RouteSegment(index + 1, from_point, to_point, distance, duration, []))
    return segments


def fetch_openrouteservice_segments(points: list[RoutePoint]) -> dict[str, Any] | None:
    api_key = openrouteservice_api_key()
    if not api_key or len(points) < 2:
        return None
    coordinates = [[point.longitude, point.latitude] for point in points]
    try:
        response = requests.post(
            OPENROUTESERVICE_DIRECTIONS_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"coordinates": coordinates},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.warning("OpenRouteService route calculation failed: %s", exc)
        return None

    features = payload.get("features") or []
    if not features:
        return None
    feature = features[0]
    properties = feature.get("properties") or {}
    raw_segments = properties.get("segments") or []
    if len(raw_segments) < len(points) - 1:
        return None
    segments: list[RouteSegment] = []
    for index, raw_segment in enumerate(raw_segments[: len(points) - 1]):
        segments.append(
            RouteSegment(
                index + 1,
                points[index],
                points[index + 1],
                float(raw_segment.get("distance") or 0) / 1000,
                float(raw_segment.get("duration") or 0) / 60,
                [],
            )
        )
    geometry_coordinates = ((feature.get("geometry") or {}).get("coordinates") or [])
    geometry = [
        [float(point[1]), float(point[0])]
        for point in geometry_coordinates
        if isinstance(point, list) and len(point) >= 2
    ]
    return {"segments": segments, "geometry": geometry}


def calculate_route_costs(total_distance_km: float, total_drive_minutes: float, total_work_minutes: float, form: Any) -> dict[str, float]:
    values = {
        "internal_km_cost": parse_optional_float(form.get("internal_km_cost")) or 0,
        "client_km_price": parse_optional_float(form.get("client_km_price")) or 0,
        "engineer_count": int(parse_optional_float(form.get("engineer_count")) or 0),
        "technician_count": int(parse_optional_float(form.get("technician_count")) or 0),
        "engineer_internal_hourly": parse_optional_float(form.get("engineer_internal_hourly")) or 0,
        "technician_internal_hourly": parse_optional_float(form.get("technician_internal_hourly")) or 0,
        "engineer_client_hourly": parse_optional_float(form.get("engineer_client_hourly")) or 0,
        "technician_client_hourly": parse_optional_float(form.get("technician_client_hourly")) or 0,
        "tolls_cost": parse_optional_float(form.get("tolls_cost")) or 0,
        "lodging_cost": parse_optional_float(form.get("lodging_cost")) or 0,
        "meals_cost": parse_optional_float(form.get("meals_cost")) or 0,
        "other_costs": parse_optional_float(form.get("other_costs")) or 0,
        "extra_margin_pct": parse_optional_float(form.get("extra_margin_pct")) or 0,
    }
    drive_hours = total_drive_minutes / 60
    work_hours = total_work_minutes / 60
    internal_hourly = values["engineer_count"] * values["engineer_internal_hourly"] + values["technician_count"] * values["technician_internal_hourly"]
    client_hourly = values["engineer_count"] * values["engineer_client_hourly"] + values["technician_count"] * values["technician_client_hourly"]
    extra_costs = values["tolls_cost"] + values["lodging_cost"] + values["meals_cost"] + values["other_costs"]
    values["travel_internal_cost"] = round(total_distance_km * values["internal_km_cost"], 2)
    values["travel_client_price"] = round(total_distance_km * values["client_km_price"], 2)
    values["drive_labor_internal_cost"] = round(drive_hours * internal_hourly, 2)
    values["work_labor_internal_cost"] = round(work_hours * internal_hourly, 2)
    values["drive_labor_client_price"] = round(drive_hours * client_hourly, 2)
    values["work_labor_client_price"] = round(work_hours * client_hourly, 2)
    values["total_internal_cost"] = round(
        values["travel_internal_cost"] + values["drive_labor_internal_cost"] + values["work_labor_internal_cost"] + extra_costs,
        2,
    )
    base_client_price = values["travel_client_price"] + values["drive_labor_client_price"] + values["work_labor_client_price"] + extra_costs
    values["total_client_price"] = round(base_client_price * (1 + values["extra_margin_pct"] / 100), 2)
    values["estimated_margin"] = round(values["total_client_price"] - values["total_internal_cost"], 2)
    return values


def build_cost_rows(plan: sqlite3.Row) -> list[dict[str, Any]]:
    return [
        {"label": "Deslocacao", "internal": plan["travel_internal_cost"] or 0, "client": plan["travel_client_price"] or 0},
        {"label": "Mao de obra em viagem", "internal": plan["drive_labor_internal_cost"] or 0, "client": plan["drive_labor_client_price"] or 0},
        {"label": "Mao de obra em intervencao", "internal": plan["work_labor_internal_cost"] or 0, "client": plan["work_labor_client_price"] or 0},
        {"label": "Portagens", "internal": plan["tolls_cost"] or 0, "client": plan["tolls_cost"] or 0},
        {"label": "Alojamento", "internal": plan["lodging_cost"] or 0, "client": plan["lodging_cost"] or 0},
        {"label": "Refeicoes", "internal": plan["meals_cost"] or 0, "client": plan["meals_cost"] or 0},
        {"label": "Outros custos", "internal": plan["other_costs"] or 0, "client": plan["other_costs"] or 0},
        {"label": "Total", "internal": plan["total_internal_cost"] or 0, "client": plan["total_client_price"] or 0},
        {"label": "Margem estimada", "internal": "", "client": plan["estimated_margin"] or 0},
    ]


def route_geometry_from_segments(segments: list[sqlite3.Row]) -> list[list[float]]:
    points: list[list[float]] = []
    for segment in segments:
        if not points:
            points.append([float(segment["from_lat"]), float(segment["from_lng"])])
        points.append([float(segment["to_lat"]), float(segment["to_lng"])])
    return points


def csv_cell(value: Any) -> str:
    text = str(value or "")
    return '"' + text.replace('"', '""') + '"'


def optimize_route(stops: list[RouteStop], start: tuple[float, float] | None = None) -> tuple[list[RouteStop], list[float]]:
    remaining = stops[:]
    ordered: list[RouteStop] = []
    distances: list[float] = []
    current = start

    while remaining:
        if current is None:
            next_stop = remaining.pop(0)
            distance = 0.0
        else:
            next_index, next_stop, distance = min(
                (
                    (index, stop, haversine_km(current[0], current[1], stop.latitude, stop.longitude))
                    for index, stop in enumerate(remaining)
                ),
                key=lambda item: item[2],
            )
            remaining.pop(next_index)
        ordered.append(next_stop)
        distances.append(distance)
        current = (next_stop.latitude, next_stop.longitude)

    return ordered, distances


def fetch_openrouteservice_route(ordered_stops: list[RouteStop]) -> dict[str, Any] | None:
    api_key = openrouteservice_api_key()
    if not api_key or len(ordered_stops) < 2:
        return None

    coordinates = [[stop.longitude, stop.latitude] for stop in ordered_stops]
    try:
        response = requests.post(
            OPENROUTESERVICE_DIRECTIONS_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"coordinates": coordinates},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.warning("OpenRouteService route calculation failed: %s", exc)
        return None

    features = payload.get("features") or []
    if not features:
        return None
    feature = features[0]
    properties = feature.get("properties") or {}
    segments = properties.get("segments") or []
    segment_distances = [float(segment.get("distance") or 0) / 1000 for segment in segments]
    distances = [0.0] + segment_distances
    if len(distances) < len(ordered_stops):
        distances.extend([0.0] * (len(ordered_stops) - len(distances)))
    geometry_coordinates = ((feature.get("geometry") or {}).get("coordinates") or [])
    geometry = [
        [float(point[1]), float(point[0])]
        for point in geometry_coordinates
        if isinstance(point, list) and len(point) >= 2
    ]
    return {"segment_distances_km": distances[: len(ordered_stops)], "geometry": geometry}


def geocode_asset(row: sqlite3.Row) -> dict[str, Any] | None:
    api_key = openrouteservice_api_key()
    query_texts = build_geocode_queries(row)
    if not api_key or not query_texts:
        return None

    features: list[dict[str, Any]] = []
    try:
        for text in query_texts:
            response = requests.get(
                OPENROUTESERVICE_GEOCODE_URL,
                params={
                    "api_key": api_key,
                    "text": text,
                    "size": 8,
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            features.extend(payload.get("features") or [])
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.warning("OpenRouteService geocoding failed for asset %s: %s", row["id"], exc)
        return None

    if not features:
        return None
    feature = choose_best_geocode_feature(row, features)
    if feature is None:
        return None
    coordinates = ((feature.get("geometry") or {}).get("coordinates") or [])
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    properties = feature.get("properties") or {}
    label = str(properties.get("label") or properties.get("name") or "")
    return {
        "latitude": float(coordinates[1]),
        "longitude": float(coordinates[0]),
        "label": label[:500],
        "confidence": classify_geocode_confidence(row, label, float(coordinates[1]), float(coordinates[0])),
    }


def download_mymaps_kml(source_url: str) -> str:
    kml_url = build_mymaps_kml_url(source_url)
    try:
        response = requests.get(kml_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValueError(f"Nao foi possivel descarregar o KML do My Maps: {exc}") from exc
    text = response.text
    if "<kml" not in text[:500].lower():
        raise ValueError("O link nao devolveu um KML valido. Confirma que o mapa esta publico.")
    return text


def build_mymaps_kml_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    query = parse_qs(parsed.query)
    mid = (query.get("mid") or [""])[0].strip()
    if "/maps/d/kml" in parsed.path and mid:
        return source_url
    if not mid:
        raise ValueError("Nao encontrei o parametro mid no link do Google My Maps.")
    return f"https://www.google.com/maps/d/kml?mid={quote_plus(mid)}&forcekml=1"


def parse_mymaps_kml(kml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as exc:
        raise ValueError("O KML descarregado nao e valido.") from exc
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    points: list[dict[str, Any]] = []
    for placemark in root.findall(".//k:Placemark", ns):
        name = (placemark.findtext("k:name", default="", namespaces=ns) or "").strip()
        coordinates_raw = (placemark.findtext(".//k:coordinates", default="", namespaces=ns) or "").strip().split()
        if not name or not coordinates_raw:
            continue
        parts = coordinates_raw[0].split(",")
        if len(parts) < 2:
            continue
        try:
            longitude = float(parts[0])
            latitude = float(parts[1])
        except ValueError:
            continue
        points.append({"name": name, "normalized_name": normalize_text(name), "latitude": latitude, "longitude": longitude})
    if not points:
        raise ValueError("Nao encontrei pontos com coordenadas no KML.")
    return points


def import_mymaps_points(conn: sqlite3.Connection, points: list[dict[str, Any]], overwrite: bool = False) -> dict[str, int]:
    assets = query_all(conn, "SELECT id, project_name, alias_blob, coordinates_source FROM assets")
    by_name: dict[str, list[sqlite3.Row]] = {}
    for asset in assets:
        names = {normalize_text(asset["project_name"])}
        for alias in (asset["alias_blob"] or "").split("|"):
            if alias.strip():
                names.add(normalize_text(alias))
        for name in names:
            if name:
                by_name.setdefault(name, []).append(asset)

    result = {"updated": 0, "unmatched": 0, "ambiguous": 0, "preserved": 0}
    now_note = "Importado de Google My Maps"
    for point in points:
        matches = by_name.get(point["normalized_name"], [])
        if not matches:
            result["unmatched"] += 1
            continue
        if len(matches) > 1:
            result["ambiguous"] += 1
            continue
        asset = matches[0]
        if asset["coordinates_source"] == "manual" and not overwrite:
            result["preserved"] += 1
            continue
        conn.execute(
            """
            UPDATE assets
            SET latitude = ?,
                longitude = ?,
                coordinates_source = 'google_mymaps',
                coordinates_confidence = 'ok',
                route_notes = ?
            WHERE id = ?
            """,
            (point["latitude"], point["longitude"], f"{now_note}: {point['name']}", int(asset["id"])),
        )
        result["updated"] += 1
    return result


def classify_geocode_confidence(row: sqlite3.Row, label: str, latitude: float, longitude: float) -> str:
    label_normalized = normalize_text(label)
    postcode = extract_portuguese_postcode(row["address"] or "")
    locality_candidates = extract_locality_candidates(row)
    street_tokens = relevant_tokens(strip_postcode(row["address"] or ""))
    matching_street_tokens = [token for token in street_tokens if token in label_normalized]

    if locality_candidates and not any(locality in label_normalized for locality in locality_candidates):
        return "suspect"
    if is_portuguese_mainland_hint(row) and is_islands_result(label_normalized, latitude, longitude):
        return "suspect"
    if postcode and postcode in label_normalized:
        return "ok"
    if locality_candidates and matching_street_tokens:
        return "ok"
    if locality_candidates:
        return "review"
    if matching_street_tokens:
        return "review"
    return "suspect"


def choose_best_geocode_feature(row: sqlite3.Row, features: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored_features = []
    for feature in features:
        coordinates = ((feature.get("geometry") or {}).get("coordinates") or [])
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        properties = feature.get("properties") or {}
        label = str(properties.get("label") or properties.get("name") or "")
        score = score_geocode_candidate(row, properties, label, float(coordinates[1]), float(coordinates[0]))
        scored_features.append((score, feature))
    if not scored_features:
        return None
    scored_features.sort(key=lambda item: item[0], reverse=True)
    return scored_features[0][1]


def score_geocode_candidate(row: sqlite3.Row, properties: dict[str, Any], label: str, latitude: float, longitude: float) -> int:
    haystack = normalize_text(
        " ".join(
            str(value or "")
            for value in [
                label,
                properties.get("name"),
                properties.get("locality"),
                properties.get("county"),
                properties.get("region"),
                properties.get("macroregion"),
                properties.get("country"),
                properties.get("postalcode"),
            ]
        )
    )
    score = 0
    address = normalize_text(row["address"] or "")
    project_name = normalize_text(row["project_name"] or "")
    postcode = extract_portuguese_postcode(row["address"] or "")
    locality_candidates = extract_locality_candidates(row)

    if locality_candidates and any(locality in haystack for locality in locality_candidates):
        score += 90
    elif locality_candidates:
        score -= 100
    if postcode and postcode in haystack:
        score += 80
    elif postcode:
        score -= 20
    for token in relevant_tokens(strip_postcode(address)):
        score += 14 if token in haystack else -3
    for token in relevant_tokens(project_name):
        if token in haystack:
            score += 3
    if is_portuguese_mainland_hint(row) and is_islands_result(haystack, latitude, longitude):
        score -= 90
    return score


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(ascii_text.lower().replace(",", " ").replace("-", " ").split())


def relevant_tokens(value: str) -> list[str]:
    ignored = {
        "rua",
        "avenida",
        "av",
        "estrada",
        "largo",
        "travessa",
        "quinta",
        "sala",
        "lote",
        "apartado",
        "portugal",
        "municipality",
        "municipio",
        "concelho",
        "s/n",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "dr",
        "doutor",
        "e",
        "n",
        "no",
    }
    return [token for token in normalize_text(value).split() if len(token) >= 3 and token not in ignored and not token.isdigit()]


def extract_portuguese_postcode(value: str) -> str:
    match = re.search(r"\b(\d{4})\s*[- ]\s*(\d{3})\b", value or "")
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2)}"


def strip_postcode(value: str) -> str:
    return re.sub(r"\b\d{4}\s*[- ]\s*\d{3}\b", " ", value or "")


def build_geocode_queries(row: sqlite3.Row) -> list[str]:
    address = (row["address"] or "").strip()
    project_name = (row["project_name"] or "").strip()
    installation_group = (row["installation_group"] or "").strip() if "installation_group" in row.keys() else ""
    locality_candidates = extract_locality_candidates(row)
    postcode = extract_portuguese_postcode(address)
    queries: list[str] = []

    if address:
        queries.append(f"{address}, Portugal")
    for locality in locality_candidates:
        if postcode:
            queries.append(f"{postcode}, {locality}, Portugal")
        if address:
            queries.append(f"{strip_postcode(address)}, {locality}, Portugal")
        if project_name:
            queries.append(f"{project_name}, {locality}, Portugal")
        if installation_group and installation_group != project_name:
            queries.append(f"{installation_group}, {locality}, Portugal")

    unique_queries = []
    seen = set()
    for query in queries:
        normalized = normalize_text(query)
        if normalized and normalized not in seen:
            unique_queries.append(query)
            seen.add(normalized)
    return unique_queries[:5]


def extract_locality_candidates(row: sqlite3.Row) -> list[str]:
    address = row["address"] or ""
    candidates: list[str] = []
    postcode_match = re.search(r"\b\d{4}\s*[- ]\s*\d{3}\b\s*([^,;]+)", address)
    if postcode_match:
        candidates.append(postcode_match.group(1))

    parts = [part.strip() for part in re.split(r"[,;]", address) if part.strip()]
    if parts:
        for part in reversed(parts):
            normalized_part = normalize_text(clean_locality_candidate(part))
            if not normalized_part or normalized_part in {"portugal", "pt"}:
                continue
            if normalized_part in PORTUGAL_MAINLAND_DISTRICTS and len(parts) > 1:
                continue
            if extract_portuguese_postcode(part):
                tail = re.sub(r".*\b\d{4}\s*[- ]\s*\d{3}\b", "", part).strip()
                if tail:
                    candidates.append(tail)
                continue
            if not looks_like_street_fragment(part):
                candidates.append(part)
                break

    location = row["location"] or ""
    if location and not any(normalize_text(location) == normalize_text(district) for district in PORTUGAL_MAINLAND_DISTRICTS):
        candidates.append(location)

    normalized_candidates: list[str] = []
    seen = set()
    for candidate in candidates:
        cleaned = clean_locality_candidate(candidate)
        normalized = normalize_text(cleaned)
        if normalized and normalized not in seen:
            normalized_candidates.append(normalized)
            seen.add(normalized)
    return normalized_candidates[:3]


def clean_locality_candidate(value: str) -> str:
    value = re.sub(r"\b\d{4}\s*[- ]\s*\d{3}\b", "", value or "")
    value = re.sub(r"\bportugal\b", "", value, flags=re.IGNORECASE)
    return value.strip(" ,;-")


def looks_like_street_fragment(value: str) -> bool:
    normalized = normalize_text(value)
    street_markers = {"rua", "avenida", "av", "estrada", "travessa", "largo", "praca", "quinta", "r"}
    return any(marker in normalized.split() for marker in street_markers) or bool(re.search(r"\d", value or ""))


def is_portuguese_mainland_hint(row: sqlite3.Row) -> bool:
    haystack = normalize_text(f"{row['address'] or ''} {row['location'] or ''}")
    if "madeira" in haystack or "acores" in haystack or "azores" in haystack:
        return False
    return bool(extract_portuguese_postcode(row["address"] or "") or any(token in haystack for token in PORTUGAL_MAINLAND_DISTRICTS))


def is_islands_result(label_normalized: str, latitude: float, longitude: float) -> bool:
    return (
        "madeira" in label_normalized
        or "acores" in label_normalized
        or "azores" in label_normalized
        or longitude < -12.0
        or latitude < 35.0
    )


def openrouteservice_api_key() -> str:
    return os.environ.get("OPENROUTESERVICE_API_KEY", "").strip()


def mark_existing_suspicious_coordinates(conn: sqlite3.Connection) -> int:
    asset_columns = {row["name"] for row in conn.execute("PRAGMA table_info(assets)").fetchall()}
    address_expr = "address" if "address" in asset_columns else "''"
    location_expr = "location" if "location" in asset_columns else "''"
    source_expr = "coordinates_source" if "coordinates_source" in asset_columns else "''"
    rows = query_all(
        conn,
        f"""
        SELECT id, project_name, address, location, latitude, longitude
        FROM (
            SELECT id, project_name, {address_expr} AS address, {location_expr} AS location,
                   latitude, longitude, coordinates_confidence, {source_expr} AS coordinates_source
            FROM assets
        )
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND (
              COALESCE(coordinates_confidence, '') = ''
              OR COALESCE(coordinates_source, '') = 'openrouteservice'
          )
        """,
    )
    updated = 0
    for row in rows:
        confidence = classify_geocode_confidence(row, "", float(row["latitude"]), float(row["longitude"]))
        conn.execute(
            "UPDATE assets SET coordinates_confidence = ? WHERE id = ?",
            (confidence, int(row["id"])),
        )
        updated += 1
    return updated


def haversine_km(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    radius_km = 6371.0
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lng_b - lng_a)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def parse_optional_float(value: str | None) -> float | None:
    normalized = (value or "").strip().replace(",", ".")
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def asset_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["asset_id"] if "asset_id" in row.keys() else row["id"]),
        "name": row["project_name"],
        "lat": float(row["latitude"]),
        "lng": float(row["longitude"]),
        "status": row["latest_status"] or "Sem estado",
        "location": row["location"] or "",
        "address": row["address"] or "",
        "confidence": row["coordinates_confidence"] or "",
    }


def parse_route_geometry(value: str | None) -> list[list[float]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    points: list[list[float]] = []
    for point in parsed:
        if isinstance(point, list) and len(point) >= 2:
            points.append([float(point[0]), float(point[1])])
    return points


def build_google_maps_url(stops: list[sqlite3.Row]) -> str:
    points = [f"{row['latitude']},{row['longitude']}" for row in stops if row["latitude"] is not None and row["longitude"] is not None]
    if not points:
        return ""
    if len(points) == 1:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(points[0])}"
    origin = quote_plus(points[0])
    destination = quote_plus(points[-1])
    waypoints = quote_plus("|".join(points[1:-1]))
    url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&travelmode=driving"
    if waypoints:
        url += f"&waypoints={waypoints}"
    return url
