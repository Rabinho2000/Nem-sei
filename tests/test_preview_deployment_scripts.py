import sqlite3
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy_preview_environment.sh"
SANITIZE_SQL = REPOSITORY_ROOT / "scripts" / "sanitize_preview.sql"


def test_preview_sanitization_cancels_sendable_distributions(tmp_path: Path) -> None:
    database = tmp_path / "preview.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE integration_configs (
            username TEXT, password TEXT, enabled INTEGER,
            auto_sync_enabled INTEGER, production_sync_enabled INTEGER,
            diagnostics_sync_enabled INTEGER, last_error TEXT
        );
        CREATE TABLE alert_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE report_automations (
            active INTEGER, last_blocked_reason TEXT
        );
        CREATE TABLE background_jobs (
            status TEXT, error_message TEXT, finished_at TEXT
        );
        CREATE TABLE report_distributions (
            status TEXT, error_message TEXT, updated_at TEXT
        );
        """
    )
    statuses = (
        "ready_to_send",
        "approved_to_send",
        "queued",
        "approved",
        "sending",
        "sent",
    )
    connection.executemany(
        "INSERT INTO report_distributions (status) VALUES (?)",
        ((status,) for status in statuses),
    )

    connection.executescript(SANITIZE_SQL.read_text())

    actual = [
        row[0]
        for row in connection.execute(
            "SELECT status FROM report_distributions ORDER BY rowid"
        )
    ]
    assert actual == ["cancelled"] * 5 + ["sent"]


def test_update_stops_old_preview_before_running_schema() -> None:
    script = DEPLOY_SCRIPT.read_text()
    update_body = script.split("update_preview() {", 1)[1].split(
        "\n}\n\nshow_status()", 1
    )[0]

    build_position = update_body.index("compose build monitoring-board")
    stop_position = update_body.index("compose stop monitoring-board")
    schema_position = update_body.index("run_schema")
    start_position = update_body.index(
        "compose up --detach --remove-orphans monitoring-board"
    )

    assert build_position < stop_position < schema_position < start_position
