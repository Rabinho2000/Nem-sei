"""Diagnostic screens: what each device is doing, from persisted facts alone.

Computes nothing. Every row comes from `diagnostics.service.current_device_status`
or `diagnostics.findings.evaluate_asset_findings` (the asset detail page) or
already-persisted `DiagnosticIncident` rows (overview/incidents, D2) -- no
route here ever re-derives a rule's logic.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.diagnostics.handling import record_incident_handling
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.diagnostics_queries import asset_diagnostics, diagnostics_overview, handling_summary, incident_detail, open_incidents_overview
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
        **diagnostics_overview(session, search=search),
    )


@diagnostics_bp.get("/incidents")
@require_authenticated
def incidents() -> str:
    session = get_request_session()
    search = request.args.get("search", "").strip()
    handling = request.args.get("handling", "").strip()
    return render_template(
        "diagnostics/incidents.html",
        title="Incidentes",
        search=search,
        handling=handling,
        summary=handling_summary(session),
        incidents=open_incidents_overview(session, search=search, handling=handling),
    )


@diagnostics_bp.get("/incidents/<int:incident_id>")
@require_authenticated
def incident(incident_id: int) -> str:
    context = incident_detail(get_request_session(), incident_id=incident_id)
    if context is None:
        abort(404)
    return render_template(
        "diagnostics/incident.html",
        title=f"Incidente {incident_id}",
        csrf_token=token(),
        **context,
    )


@diagnostics_bp.post("/incidents/<int:incident_id>/handling")
@require_authenticated
def update_handling(incident_id: int):
    """Move an incident along. Never touches the detector's own status."""
    require_valid_token()
    session = get_request_session()
    try:
        record_incident_handling(
            session,
            incident_id=incident_id,
            actor=browser_session.get("username", "web"),
            handling_state=request.form.get("handling_state") or None,
            assigned_to=request.form.get("assigned_to"),
            clear_assignment=request.form.get("clear_assignment") == "on",
            note=request.form.get("note"),
        )
        session.commit()
        flash("Incidente atualizado.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("diagnostics.incident", incident_id=incident_id))


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
