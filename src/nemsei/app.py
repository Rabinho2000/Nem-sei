"""V2 Flask composition root: configuration and HTTP adapters only."""
from __future__ import annotations

from datetime import timedelta

from flask import Flask

from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.system.health import ReadinessCheck, database_readiness
from nemsei.web.auth_routes import auth_bp
from nemsei.web.health_routes import health_bp
from nemsei.web.home_routes import home_bp


def create_app(
    settings: Settings | None = None,
    *,
    readiness_check: ReadinessCheck | None = None,
) -> Flask:
    configured = (settings or Settings.from_environment()).validate(require_auth=True)
    engine = build_engine(configured)
    app = Flask(__name__, template_folder="web/templates")
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
    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(home_bp)
    return app
