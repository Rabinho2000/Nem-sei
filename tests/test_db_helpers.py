from __future__ import annotations

from monitoring_board.db import configure_database_for_runtime, ensure_column, get_db, query_all, query_scalar


def test_db_helpers_enable_foreign_keys_and_query_rows(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES (?)", ("Central A",))
        conn.commit()

        rows = query_all(conn, "SELECT * FROM items")

        assert query_scalar(conn, "PRAGMA foreign_keys") == 1
        assert query_scalar(conn, "PRAGMA busy_timeout") == 10000
        assert query_scalar(conn, "PRAGMA synchronous") == 1
        assert query_scalar(conn, "PRAGMA temp_store") == 2
        assert rows[0]["name"] == "Central A"
        assert query_scalar(conn, "SELECT COUNT(*) FROM items") == 1
    finally:
        conn.close()


def test_ensure_column_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

        ensure_column(conn, "items", "notes TEXT")
        ensure_column(conn, "items", "notes TEXT")

        columns = [row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
        assert columns == ["id", "notes"]
    finally:
        conn.close()


def test_runtime_database_pragmas_are_applied(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        configure_database_for_runtime(conn)

        assert query_scalar(conn, "PRAGMA journal_mode") == "wal"
        assert query_scalar(conn, "PRAGMA synchronous") == 1
        assert query_scalar(conn, "PRAGMA temp_store") == 2
    finally:
        conn.close()


def test_connection_scoped_pragmas_are_applied_after_initialization(tmp_path) -> None:
    from app import ensure_database

    db_path = tmp_path / "test.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        assert query_scalar(conn, "PRAGMA journal_mode") == "wal"
        assert query_scalar(conn, "PRAGMA synchronous") == 1
        assert query_scalar(conn, "PRAGMA temp_store") == 2
        assert query_scalar(conn, "PRAGMA busy_timeout") == 10000
        assert query_scalar(conn, "PRAGMA foreign_keys") == 1
    finally:
        conn.close()


def test_error_calendar_groups_monitoring_records_by_record_date(tmp_path) -> None:
    from app import build_error_calendar

    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE monitoring_records (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                project_name TEXT NOT NULL,
                status TEXT NOT NULL,
                record_date TEXT NOT NULL,
                notes TEXT,
                source TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO monitoring_records (
                id, asset_id, project_name, status, record_date, notes, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 7, "Central A", "Erro", "2026-05-20", "Inversor 1", "FusionSolar"),
                (2, 8, "Central B", "Desconectada", "2026-05-20", "Sem comunicacao", "FusionSolar"),
            ],
        )
        rows = conn.execute("SELECT * FROM monitoring_records ORDER BY id").fetchall()

        error_calendar = build_error_calendar("2026-05", rows)

        problem_day = [
            day
            for week in error_calendar["weeks"]
            for day in week
            if day["date"] and day["date"].isoformat() == "2026-05-20"
        ][0]
        assert [record["status"] for record in problem_day["records"]] == ["Erro", "Desconectada"]
        assert error_calendar["record_count"] == 2
    finally:
        conn.close()


def test_asset_error_calendar_marks_problem_start_and_end(tmp_path) -> None:
    from app import build_asset_error_calendar

    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE monitoring_records (
                id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                record_date TEXT NOT NULL,
                notes TEXT,
                source TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO monitoring_records (id, asset_id, status, record_date, notes, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 7, "Operacional", "2026-05-01", "", "FusionSolar"),
                (2, 7, "Erro", "2026-05-03", "Inversor", "FusionSolar"),
                (3, 7, "Desconectada", "2026-05-04", "Sem comunicacao", "FusionSolar"),
                (4, 7, "Resolvido", "2026-05-06", "", "FusionSolar"),
            ],
        )
        rows = conn.execute("SELECT * FROM monitoring_records ORDER BY record_date ASC, id ASC").fetchall()

        asset_calendar = build_asset_error_calendar("2026-05", rows)

        events = {
            day["date"].isoformat(): day["events"]
            for week in asset_calendar["weeks"]
            for day in week
            if day["date"] and day["events"]
        }
        assert events["2026-05-03"][0]["type"] == "start"
        assert events["2026-05-03"][0]["label"] == "Apareceu"
        assert events["2026-05-04"][0]["type"] == "active"
        assert events["2026-05-06"][0]["type"] == "end"
        assert events["2026-05-06"][0]["label"] == "Desapareceu"
        assert asset_calendar["event_count"] == 3
    finally:
        conn.close()


def test_ensure_database_creates_performance_indexes(tmp_path) -> None:
    from app import ensure_database

    db_path = tmp_path / "test.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        expected_indexes = {
            "monitoring_records": {
                "idx_monitoring_records_asset_date_id",
                "idx_monitoring_records_record_date_source",
                "idx_monitoring_records_status_record_date",
            },
            "production_records": {
                "idx_production_records_provider_period_asset",
                "idx_production_records_performance_status",
            },
            "asset_integrations": {
                "idx_asset_integrations_provider_external_id",
                "idx_asset_integrations_provider_enabled_asset",
            },
            "tickets": {"idx_tickets_asset_status"},
            "integration_unresolved": {"idx_integration_unresolved_provider_resolution_created"},
            "telegram_alerts": {"idx_telegram_alerts_alert_key_status"},
            "alert_blacklist": {"idx_alert_blacklist_asset_active"},
            "background_jobs": {"idx_background_jobs_type_status_created"},
        }

        for table_name, index_names in expected_indexes.items():
            existing_names = {
                row["name"]
                for row in conn.execute(f"PRAGMA index_list({table_name})").fetchall()
            }
            assert index_names <= existing_names
    finally:
        conn.close()


def test_background_job_helpers_create_and_update_status(tmp_path) -> None:
    from app import (
        create_background_job,
        ensure_database,
        fetch_latest_background_jobs,
        mark_stale_running_background_jobs_failed,
        mark_background_job_failed,
        mark_background_job_running,
        mark_background_job_success,
    )

    db_path = tmp_path / "test.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        job_id, created = create_background_job(conn, "fusionsolar_production_sync", {"period_type": "day"})
        duplicate_id, duplicate_created = create_background_job(conn, "fusionsolar_production_sync", {"period_type": "month"})
        conn.commit()

        assert created is True
        assert duplicate_created is False
        assert duplicate_id == job_id

        assert mark_background_job_running(conn, job_id) is True
        mark_background_job_success(conn, job_id, {"processed": 1})

        second_id, second_created = create_background_job(conn, "fusionsolar_production_sync", {"period_type": "month"})
        conn.commit()
        assert second_created is True
        assert second_id != job_id
        assert mark_background_job_running(conn, second_id) is True
        mark_background_job_failed(conn, second_id, "boom")

        jobs = fetch_latest_background_jobs(conn, job_types=("fusionsolar_production_sync",))
        assert [row["status"] for row in jobs[:2]] == ["failed", "success"]
        assert jobs[0]["error_message"] == "boom"
        assert '"processed": 1' in jobs[1]["result_json"]

        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running', started_at = '2026-01-01T00:00:00', finished_at = NULL, error_message = NULL
            WHERE id = ?
            """,
            (second_id,),
        )
        conn.commit()

        recovered_count = mark_stale_running_background_jobs_failed(conn, stale_after_minutes=30)
        recovered_job = conn.execute("SELECT status, error_message FROM background_jobs WHERE id = ?", (second_id,)).fetchone()

        assert recovered_count == 1
        assert recovered_job["status"] == "failed"
        assert "running for more than 30 minutes" in recovered_job["error_message"]
    finally:
        conn.close()
