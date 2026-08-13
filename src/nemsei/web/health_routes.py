from __future__ import annotations

from flask import Blueprint, current_app, jsonify


health_bp = Blueprint("health", __name__)


@health_bp.get("/healthz")
def healthz():
    return jsonify(status="ok", service="web"), 200


@health_bp.get("/readyz")
def readyz():
    ready, reason = current_app.extensions["nemsei.readiness_check"]()
    return jsonify(status="ok" if ready else "not_ready", reason=reason), 200 if ready else 503
