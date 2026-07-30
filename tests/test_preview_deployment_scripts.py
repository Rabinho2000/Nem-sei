import sqlite3
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy_preview_environment.sh"
INSTALL_SCRIPT = REPOSITORY_ROOT / "scripts" / "install_preview_environment.sh"
SANITIZE_SQL = REPOSITORY_ROOT / "scripts" / "sanitize_preview.sql"
PREVIEW_COMPOSE = REPOSITORY_ROOT / "docker-compose.preview.yml"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"


def compose_service(name: str, next_section: str) -> str:
    compose = PREVIEW_COMPOSE.read_text()
    return compose.split(f"  {name}:\n", 1)[1].split(
        f"\n  {next_section}:", 1
    )[0]


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


def test_monitoring_board_is_only_on_internal_network() -> None:
    compose = PREVIEW_COMPOSE.read_text()
    service = compose_service("monitoring-board", "preview-gateway")
    networks = compose.split("\nnetworks:\n", 1)[1]

    assert "    networks:\n      - preview-internal\n" in service
    assert "    ports:" not in service
    assert "preview-edge" not in service
    assert "  preview-internal:\n    internal: true\n" in networks


def test_gateway_is_the_only_published_service() -> None:
    compose = PREVIEW_COMPOSE.read_text()
    app = compose_service("monitoring-board", "preview-gateway")
    gateway = compose_service("preview-gateway", "preview-internal")

    assert "    ports:" not in app
    assert '    ports:\n      - "0.0.0.0:5002:8080"\n' in gateway
    assert (
        "    networks:\n      - preview-internal\n      - preview-edge\n"
        in gateway
    )
    assert "env_file:" not in gateway
    assert "volumes:" not in gateway
    assert compose.count("    ports:") == 1


def test_runtime_and_preview_environment_are_excluded_from_build_context() -> None:
    patterns = set(DOCKERIGNORE.read_text().splitlines())

    assert "runtime/" in patterns
    assert "**/runtime/" in patterns
    assert ".env.preview" in patterns
    assert {"*.db", "*.sqlite", "*.sqlite3"} <= patterns
    assert {"uploads/", "backups/", "logs/", "data/"} <= patterns
    for script_path in (INSTALL_SCRIPT, DEPLOY_SCRIPT):
        script = script_path.read_text()
        assert "validate_build_context()" in script
        assert "validate_build_context\n" in script


def test_status_checks_both_services_and_published_port() -> None:
    script = DEPLOY_SCRIPT.read_text()
    wait_body = script.split("wait_for_preview() {", 1)[1].split(
        "\n}\n\ncheck_gateway_port()", 1
    )[0]
    status_body = script.split("show_status() {", 1)[1].split(
        "\n}\n\nmain()", 1
    )[0]

    assert "compose ps --quiet monitoring-board" in wait_body
    assert "compose ps --quiet preview-gateway" in wait_body
    assert 'app_health}" == "healthy"' in wait_body
    assert 'gateway_health}" == "healthy"' in wait_body
    assert "wait_for_preview" in status_body
    assert "check_gateway_port" in status_body
    assert '"${PREVIEW_URL}"' in status_body
    assert '"${PRODUCTION_URL}"' in status_body
