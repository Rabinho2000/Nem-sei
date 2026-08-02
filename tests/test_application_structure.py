from __future__ import annotations

from pathlib import Path

from flask import Flask

import app as app_module
from monitoring_board import schema
from monitoring_board.scheduler_runtime import (
    SCHEDULER_EXTENSION_KEY,
    attach_scheduler,
    attached_scheduler,
    detach_scheduler,
)


def test_schema_entry_point_initializes_a_database(tmp_path: Path) -> None:
    database = tmp_path / "schema-entry-point.db"

    schema.ensure_database(str(database))

    assert database.exists()


def test_scheduler_runtime_is_owned_by_the_flask_application() -> None:
    app = Flask("scheduler-owner")
    scheduler = object()

    attach_scheduler(app, scheduler)

    assert attached_scheduler(app) is scheduler
    assert app.extensions[SCHEDULER_EXTENSION_KEY] is scheduler

    detach_scheduler(app)

    assert attached_scheduler(app) is None


def test_extracted_settings_routes_preserve_their_endpoints(tmp_path: Path) -> None:
    database = tmp_path / "settings-routes.db"
    app_module.ensure_database(str(database))
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(database)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "test"

        assert client.get("/settings").status_code == 200
        assert client.get("/renewals").status_code == 200
    finally:
        flask_app.config["DATABASE"] = original_database
