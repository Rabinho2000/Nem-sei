from __future__ import annotations

from monitoring_board.db import get_db, query_scalar
from monitoring_board.routes.field_routes import (
    RouteStop,
    RoutePoint,
    build_mymaps_kml_url,
    build_route_points,
    build_route_segments,
    classify_geocode_confidence,
    choose_best_geocode_feature,
    create_route_plan,
    default_depot,
    ensure_field_routes_schema,
    geocode_asset,
    import_mymaps_points,
    optimize_route_order,
    optimize_route,
    parse_mymaps_kml,
)


def test_field_routes_schema_adds_asset_coordinates_and_route_tables(tmp_path) -> None:
    db_path = tmp_path / "routes.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()

    ensure_field_routes_schema(str(db_path))
    ensure_field_routes_schema(str(db_path))

    conn = get_db(str(db_path))
    try:
        asset_columns = {row["name"] for row in conn.execute("PRAGMA table_info(assets)").fetchall()}
        plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(field_route_plans)").fetchall()}
        segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(field_route_segments)").fetchall()}
        assert {"latitude", "longitude", "route_notes", "coordinates_source", "coordinates_confidence"} <= asset_columns
        assert {"origin_name", "origin_lat", "origin_lng", "return_to_origin", "total_drive_minutes", "total_work_minutes", "total_mission_minutes", "route_geometry_json", "route_provider"} <= plan_columns
        assert {"from_label", "to_label", "distance_km", "duration_minutes", "geometry_json"} <= segment_columns
        assert query_scalar(conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'field_route_plans'") == "field_route_plans"
        assert query_scalar(conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'field_route_stops'") == "field_route_stops"
        assert query_scalar(conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'field_route_segments'") == "field_route_segments"
    finally:
        conn.close()


def test_optimize_route_uses_nearest_next_stop_from_start() -> None:
    stops = [
        RouteStop(1, "A", 0.0, 0.0, 1.0),
        RouteStop(2, "B", 0.0, 2.0, 1.0),
        RouteStop(3, "C", 0.0, 1.0, 1.0),
    ]

    ordered, distances = optimize_route(stops, start=(0.0, 0.2))

    assert [stop.project_name for stop in ordered] == ["A", "C", "B"]
    assert distances[0] > 0
    assert all(distance >= 0 for distance in distances)


def test_geocode_asset_uses_openrouteservice_response(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "features": [
                    {
                        "geometry": {"coordinates": [-8.61, 41.15]},
                        "properties": {"label": "Rua Santa Luzia, Pombal, 3100 483, Portugal"},
                    }
                ]
            }

    captured = {}

    def fake_get(url, params, timeout):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setenv("OPENROUTESERVICE_API_KEY", "test-key")
    monkeypatch.setattr("monitoring_board.routes.field_routes.requests.get", fake_get)

    db_path = ":memory:"
    conn = get_db(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY,
                project_name TEXT,
                installation_group TEXT,
                address TEXT,
                location TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO assets (id, project_name, installation_group, address, location) VALUES (1, 'Central A', 'Grupo A', 'Rua Santa Luzia 3100-483 Pombal', 'Leiria')"
        )
        row = conn.execute("SELECT * FROM assets WHERE id = 1").fetchone()

        result = geocode_asset(row)

        assert result == {"latitude": 41.15, "longitude": -8.61, "label": "Rua Santa Luzia, Pombal, 3100 483, Portugal", "confidence": "ok"}
        assert captured["params"]["api_key"] == "test-key"
        assert captured["params"]["size"] == 8
    finally:
        conn.close()


def test_classify_geocode_confidence_flags_island_results_for_mainland_assets() -> None:
    conn = get_db(":memory:")
    try:
        conn.execute("CREATE TABLE assets (address TEXT, location TEXT)")
        row = conn.execute("SELECT 'Castelo Branco' AS location, 'Rua A' AS address").fetchone()

        assert classify_geocode_confidence(row, "C.M. Alpedrinha, Madeira, Portugal", 32.7, -16.9) == "suspect"
    finally:
        conn.close()


def test_choose_best_geocode_feature_prefers_matching_location_over_first_result() -> None:
    conn = get_db(":memory:")
    try:
        row = conn.execute(
            "SELECT 1 AS id, 'C.M. Alpedrinha' AS project_name, 'Castelo Branco' AS location, 'Rua A' AS address"
        ).fetchone()
        features = [
            {
                "geometry": {"coordinates": [-16.9, 32.7]},
                "properties": {"label": "C.M. Alpedrinha, Madeira, Portugal", "region": "Madeira"},
            },
            {
                "geometry": {"coordinates": [-7.44, 40.1]},
                "properties": {"label": "Alpedrinha, Castelo Branco, Portugal", "region": "Castelo Branco"},
            },
        ]

        selected = choose_best_geocode_feature(row, features)

        assert selected["properties"]["region"] == "Castelo Branco"
    finally:
        conn.close()


def test_create_route_plan_uses_per_asset_work_hours(monkeypatch, tmp_path) -> None:
    from flask import Flask

    db_path = tmp_path / "route_hours.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL)")
        conn.execute(
            """
            CREATE VIEW latest_monitoring_view AS
            SELECT NULL AS asset_id, NULL AS status, NULL AS record_date, NULL AS notes
            WHERE 0
            """
        )
        conn.execute(
            """
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    ensure_field_routes_schema(str(db_path))
    conn = get_db(str(db_path))
    try:
        conn.execute("INSERT INTO assets (id, project_name, latitude, longitude, coordinates_confidence) VALUES (1, 'A', 41.1, -8.6, 'manual')")
        conn.execute("INSERT INTO assets (id, project_name, latitude, longitude, coordinates_confidence) VALUES (2, 'B', 41.2, -8.7, 'manual')")
        conn.commit()
    finally:
        conn.close()

    app = Flask(__name__, template_folder="../templates")
    app.add_url_rule("/field-routes/<int:plan_id>", endpoint="field_routes.route_plan_detail", view_func=lambda plan_id: "")
    app.secret_key = "test"
    app.config["DATABASE"] = str(db_path)
    monkeypatch.setattr("monitoring_board.routes.field_routes.fetch_openrouteservice_segments", lambda points: None)

    with app.test_request_context(
        "/field-routes/",
        method="POST",
        data={
            "asset_ids": ["1", "2"],
            "default_work_hours": "1",
            "work_hours_1": "2.5",
            "work_minutes_1": "0",
            "work_hours_2": "0",
            "work_minutes_2": "45",
        },
    ):
        from flask import g

        g.db = get_db(str(db_path))
        try:
            response = create_route_plan()
            g.db.commit()
        finally:
            g.db.close()

    conn = get_db(str(db_path))
    try:
        rows = conn.execute("SELECT asset_id, work_hours FROM field_route_stops ORDER BY asset_id").fetchall()
        plan = conn.execute("SELECT * FROM field_route_plans").fetchone()
        segments = conn.execute("SELECT * FROM field_route_segments ORDER BY sequence_order").fetchall()
        assert response.status_code == 302
        assert [(row["asset_id"], row["work_hours"]) for row in rows] == [(1, 2.5), (2, 0.75)]
        assert plan["origin_name"] == "Solcor Portugal"
        assert plan["return_to_origin"] == 0
        assert plan["total_work_minutes"] == 195
        assert plan["total_mission_minutes"] == plan["total_drive_minutes"] + 195
        assert len(segments) == 2
    finally:
        conn.close()


def test_create_route_plan_with_return_to_origin_creates_return_segment(monkeypatch, tmp_path) -> None:
    from flask import Flask

    db_path = tmp_path / "route_return.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL)")
        conn.execute(
            """
            CREATE VIEW latest_monitoring_view AS
            SELECT NULL AS asset_id, NULL AS status, NULL AS record_date, NULL AS notes
            WHERE 0
            """
        )
        conn.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, status TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    ensure_field_routes_schema(str(db_path))
    conn = get_db(str(db_path))
    try:
        conn.execute("INSERT INTO assets (id, project_name, latitude, longitude, coordinates_confidence) VALUES (1, 'A', 0, 1, 'manual')")
        conn.execute("INSERT INTO assets (id, project_name, latitude, longitude, coordinates_confidence) VALUES (2, 'B', 0, 2, 'manual')")
        conn.commit()
    finally:
        conn.close()

    app = Flask(__name__, template_folder="../templates")
    app.add_url_rule("/field-routes/<int:plan_id>", endpoint="field_routes.route_plan_detail", view_func=lambda plan_id: "")
    app.secret_key = "test"
    app.config["DATABASE"] = str(db_path)
    monkeypatch.setattr("monitoring_board.routes.field_routes.fetch_openrouteservice_segments", lambda points: None)

    with app.test_request_context(
        "/field-routes/",
        method="POST",
        data={
            "asset_ids": ["1", "2"],
            "return_to_origin": "on",
            "origin_name": "Solcor Portugal",
            "origin_latitude": "0",
            "origin_longitude": "0",
            "default_work_hours": "1",
        },
    ):
        from flask import g

        g.db = get_db(str(db_path))
        try:
            response = create_route_plan()
        finally:
            g.db.close()

    conn = get_db(str(db_path))
    try:
        plan = conn.execute("SELECT * FROM field_route_plans").fetchone()
        segments = conn.execute("SELECT from_label, to_label FROM field_route_segments ORDER BY sequence_order").fetchall()
        assert response.status_code == 302
        assert plan["return_to_origin"] == 1
        assert plan["total_distance_km"] > 0
        assert plan["total_drive_minutes"] > 0
        assert plan["total_work_minutes"] == 120
        assert plan["total_mission_minutes"] == plan["total_drive_minutes"] + 120
        assert len(segments) == 3
        assert segments[-1]["to_label"] == "Solcor Portugal"
    finally:
        conn.close()


def test_field_routes_excludes_review_coordinates_from_mappable_assets(monkeypatch) -> None:
    from flask import Flask, g

    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "test"
    captured = {}

    def fake_render_template(template_name, **context):
        captured.update(context)
        return "ok"

    monkeypatch.setattr("monitoring_board.routes.field_routes.render_template", fake_render_template)
    monkeypatch.setattr("monitoring_board.routes.field_routes.ensure_field_routes_schema", lambda database_path: None)
    monkeypatch.setattr("monitoring_board.routes.field_routes.fetch_latest_route_plans", lambda conn: [])
    monkeypatch.setattr(
        "monitoring_board.routes.field_routes.fetch_route_assets",
        lambda conn, filters: [
            {
                "id": 1,
                "project_name": "Suspect",
                "latitude": 38.6,
                "longitude": -9.1,
                "coordinates_confidence": "suspect",
                "latest_status": "Erro",
                "location": "",
                "address": "",
            },
            {
                "id": 2,
                "project_name": "Ok",
                "latitude": 38.7,
                "longitude": -9.2,
                "coordinates_confidence": "ok",
                "latest_status": "Erro",
                "location": "",
                "address": "",
            },
        ],
    )

    with app.test_request_context("/field-routes/"):
        app.config["DATABASE"] = ""
        g.db = object()
        from monitoring_board.routes.field_routes import field_routes

        assert field_routes() == "ok"

    assert [row["id"] for row in captured["mappable_assets"]] == [2]
    assert [row["id"] for row in captured["suspicious_assets"]] == [1]


def test_default_depot_uses_solcor_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_DEPOT_NAME", "Solcor Portugal")
    monkeypatch.setenv("DEFAULT_DEPOT_ADDRESS", "Av. Alm. Reis 54 3º, 1150-019 Lisboa")
    monkeypatch.setenv("DEFAULT_DEPOT_LAT", "38.7")
    monkeypatch.setenv("DEFAULT_DEPOT_LNG", "-9.1")

    depot = default_depot()

    assert depot["name"] == "Solcor Portugal"
    assert depot["address"] == "Av. Alm. Reis 54 3º, 1150-019 Lisboa"
    assert depot["lat"] == 38.7
    assert depot["lng"] == -9.1


def test_route_points_include_optional_return_to_origin() -> None:
    origin = RoutePoint("Solcor", 0.0, 0.0)
    stops = [RouteStop(1, "A", 0.0, 1.0, 1.0, 60)]

    with_return = build_route_points(origin, stops, True)
    without_return = build_route_points(origin, stops, False)

    assert [point.label for point in with_return] == ["Solcor", "A", "Solcor"]
    assert [point.label for point in without_return] == ["Solcor", "A"]


def test_optimized_order_for_three_stops_uses_origin() -> None:
    origin = RoutePoint("Solcor", 0.0, 0.0)
    stops = [
        RouteStop(1, "Far", 0.0, 3.0, 1.0, 60),
        RouteStop(2, "Near", 0.0, 1.0, 1.0, 60),
        RouteStop(3, "Middle", 0.0, 2.0, 1.0, 60),
    ]

    ordered = optimize_route_order(stops, origin, return_to_origin=False)

    assert [stop.project_name for stop in ordered] == ["Near", "Middle", "Far"]


def test_build_route_segments_falls_back_when_external_api_fails(monkeypatch) -> None:
    points = [
        RoutePoint("Solcor", 0.0, 0.0),
        RoutePoint("A", 0.0, 1.0, 1),
        RoutePoint("Solcor", 0.0, 0.0),
    ]
    monkeypatch.setattr("monitoring_board.routes.field_routes.fetch_openrouteservice_segments", lambda route_points: None)

    segments, provider, geometry = build_route_segments(points)

    assert provider == "local"
    assert geometry == []
    assert len(segments) == 2
    assert segments[0].from_point.label == "Solcor"
    assert segments[0].to_point.label == "A"
    assert segments[0].distance_km > 0
    assert segments[0].duration_minutes > 0


def test_form_does_not_select_visible_assets_by_default() -> None:
    template = open("templates/field_routes.html", encoding="utf-8").read()

    assert 'name="asset_ids" value="{{ asset.id }}" checked' not in template
    assert "Selecionar visíveis" in template
    assert "Limpar seleção" in template


def test_mymaps_kml_url_and_parser() -> None:
    url = "https://www.google.com/maps/d/u/0/viewer?mid=abc123&ll=1,2"
    kml = """
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Central A</name>
          <Point><coordinates>-9.1,38.7,0</coordinates></Point>
        </Placemark>
      </Document>
    </kml>
    """

    points = parse_mymaps_kml(kml)

    assert build_mymaps_kml_url(url) == "https://www.google.com/maps/d/kml?mid=abc123&forcekml=1"
    assert points == [{"name": "Central A", "normalized_name": "central a", "latitude": 38.7, "longitude": -9.1}]


def test_import_mymaps_points_updates_exact_matches_and_preserves_manual() -> None:
    conn = get_db(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY,
                project_name TEXT,
                alias_blob TEXT,
                latitude REAL,
                longitude REAL,
                route_notes TEXT,
                coordinates_source TEXT,
                coordinates_confidence TEXT
            )
            """
        )
        conn.execute("INSERT INTO assets (id, project_name, alias_blob) VALUES (1, 'Central A', '')")
        conn.execute("INSERT INTO assets (id, project_name, alias_blob, coordinates_source) VALUES (2, 'Central B', '', 'manual')")
        points = [
            {"name": "Central A", "normalized_name": "central a", "latitude": 38.7, "longitude": -9.1},
            {"name": "Central B", "normalized_name": "central b", "latitude": 39.0, "longitude": -8.0},
            {"name": "Central C", "normalized_name": "central c", "latitude": 40.0, "longitude": -7.0},
        ]

        result = import_mymaps_points(conn, points, overwrite=False)

        row = conn.execute("SELECT latitude, longitude, coordinates_source, coordinates_confidence FROM assets WHERE id = 1").fetchone()
        manual = conn.execute("SELECT latitude, longitude, coordinates_source FROM assets WHERE id = 2").fetchone()
        assert result == {"updated": 1, "unmatched": 1, "ambiguous": 0, "preserved": 1}
        assert row["latitude"] == 38.7
        assert row["longitude"] == -9.1
        assert row["coordinates_source"] == "google_mymaps"
        assert row["coordinates_confidence"] == "ok"
        assert manual["latitude"] is None
        assert manual["coordinates_source"] == "manual"
    finally:
        conn.close()
