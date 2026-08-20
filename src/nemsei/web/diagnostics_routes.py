"""Diagnostic screens: what each device is doing, from persisted facts alone.

Computes nothing. Every row comes from `diagnostics.service.current_device_status`,
the same read the milestone's foundation exists to make possible.
"""
from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from nemsei.web.csrf import token
from nemsei.web.db_session import get_request_session
from nemsei.web.diagnostics_queries import asset_diagnostics, searchable_assets_with_devices
from nemsei.web.home_routes import require_authenticated


diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")


@diagnostics_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    search = request.args.get("search", "").strip()
    return render_template(
        "diagnostics/index.html",
        title="Diagnóstico",
        search=search,
        assets=searchable_assets_with_devices(session, search=search),
    )


@diagnostics_bp.get("/assets/<int:asset_id>")
@require_authenticated
def asset_detail(asset_id: int) -> str:
    session = get_request_session()
    context = asset_diagnostics(session, asset_id=asset_id)
    if context is None:
        abort(404)
    return render_template(
        "diagnostics/asset_detail.html",
        title=f"Diagnóstico — {context['asset'].canonical_name}",
        csrf_token=token(),
        **context,
    )
