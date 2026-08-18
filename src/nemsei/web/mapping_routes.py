from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.providers.preflight import activation_preflight
from nemsei.providers.service import approve_mapping, reject_mapping
from nemsei.providers.registry import ProviderCapability
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.queries import mapping_review_data


mapping_bp = Blueprint("mappings", __name__)


@mapping_bp.get("/mappings")
@require_authenticated
def index() -> str:
    return render_template(
        "mappings.html",
        title="Revisão de mappings",
        **mapping_review_data(
            get_request_session(),
            provider=request.args.get("provider", "").strip(),
            connection_id=request.args.get("connection_id", "").strip(),
            status=request.args.get("status", "").strip(),
            asset_search=request.args.get("asset", "").strip(),
            organization_search=request.args.get("organization", "").strip(),
            needs_review=request.args.get("needs_review", "").strip(),
        ),
        csrf_token=token(),
    )


@mapping_bp.post("/mappings/<int:mapping_id>/approve")
@require_authenticated
def approve(mapping_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        approve_mapping(session, mapping_id=mapping_id, actor_username=browser_session.get("username", "web"))
        session.commit()
        flash("Mapping aprovado explicitamente.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("mappings.index"))


@mapping_bp.post("/mappings/<int:mapping_id>/reject")
@require_authenticated
def reject(mapping_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        reject_mapping(session, mapping_id=mapping_id, actor_username=browser_session.get("username", "web"))
        session.commit()
        flash("Mapping marcado como inválido.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("mappings.index"))


@mapping_bp.get("/mappings/<int:mapping_id>/preflight")
@require_authenticated
def preflight(mapping_id: int) -> str:
    capability = request.args.get("capability", ProviderCapability.CURRENT_MONITORING.value)
    try:
        result = activation_preflight(
            get_request_session(),
            settings=current_app.extensions["nemsei.settings"],
            mapping_id=mapping_id,
            capability=capability,
        )
    except ValueError:
        result = activation_preflight(
            get_request_session(),
            settings=current_app.extensions["nemsei.settings"],
            mapping_id=mapping_id,
            capability=ProviderCapability.CURRENT_MONITORING,
        )
    return render_template(
        "preflight.html",
        title="Preflight de ativação",
        preflight=result,
        csrf_token=token(),
    )
