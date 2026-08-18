"""V2 Flask composition root: configuration and HTTP adapters only."""
from __future__ import annotations

from datetime import timedelta

from flask import Flask

from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.system.health import ReadinessCheck, database_readiness
from nemsei.web.auth_routes import auth_bp
from nemsei.web.asset_routes import assets_bp
from nemsei.web.db_session import close_request_session
from nemsei.web.health_routes import health_bp
from nemsei.web.home_routes import home_bp
from nemsei.web.mapping_routes import mapping_bp
from nemsei.web.reconciliation_routes import reconciliation_bp
from nemsei.web.source_routes import source_bp


def create_app(
    settings: Settings | None = None,
    *,
    readiness_check: ReadinessCheck | None = None,
) -> Flask:
    configured = (settings or Settings.from_environment()).validate(require_auth=True)
    engine = build_engine(configured)
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static", static_url_path="/static")
    app.config.update(
        SECRET_KEY=configured.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=configured.environment == "production",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    app.extensions["nemsei.settings"] = configured
    app.extensions["nemsei.engine"] = engine
    app.extensions["nemsei.session_factory"] = build_session_factory(engine)
    app.extensions["nemsei.readiness_check"] = readiness_check or (lambda: database_readiness(engine))
    app.teardown_request(close_request_session)
    app.register_blueprint(auth_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(reconciliation_bp)
    app.register_blueprint(mapping_bp)
    app.register_blueprint(source_bp)
    return app
