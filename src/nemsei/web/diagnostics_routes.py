"""Diagnostic screens: what each device is doing, from persisted facts alone.

Computes nothing. Every row comes from `diagnostics.service.current_device_status`
or `diagnostics.findings.evaluate_asset_findings` (the asset detail page) or
already-persisted `DiagnosticIncident` rows (overview/incidents, D2) -- no
route here ever re-derives a rule's logic.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.diagnostics.handling import record_incident_handling
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.diagnostics_queries import asset_diagnostics, diagnostics_overview, handling_summary, incident_detail, open_incidents_overview
from nemsei.contracts.priority import COMMERCIAL_FAMILIES, FAMILY_LABELS
from nemsei.web.home_routes import require_authenticated
from nemsei.web.work_order_queries import new_work_order_form_context
from nemsei.work_orders.service import create_work_order


diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")


def _parse_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("Data inválida. Use o formato AAAA-MM-DD.") from exc


def _parse_decimal(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("Custo estimado inválido.") from exc


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
    family = request.args.get("family", "").strip()
    om = request.args.get("om", "").strip()
    return render_template(
        "diagnostics/incidents.html",
        title="Incidentes",
        search=search,
        handling=handling,
        family=family,
        om=om,
        family_options=[(value, FAMILY_LABELS[value]) for value in COMMERCIAL_FAMILIES],
        summary=handling_summary(session),
        incidents=open_incidents_overview(session, search=search, handling=handling, family=family, om=om),
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


@diagnostics_bp.get("/incidents/<int:incident_id>/trabalho/novo")
@require_authenticated
def new_work_order(incident_id: int) -> str:
    """Criar trabalho, a partir de um incidente: instalação e incidente já
    resolvidos e bloqueados, título e prioridade sugeridos. Mostra primeiro
    qualquer trabalho já aberto para este incidente -- criar outro fica
    disponível, mas nunca como a ação óbvia por omissão."""
    context = new_work_order_form_context(get_request_session(), incident_id=incident_id)
    if context is None:
        abort(404)
    return render_template(
        "diagnostics/incident_new_work_order.html",
        title=f"Criar trabalho — Incidente {incident_id}",
        csrf_token=token(),
        **context,
    )


@diagnostics_bp.post("/incidents/<int:incident_id>/trabalho")
@require_authenticated
def create_work_order_from_incident(incident_id: int):
    require_valid_token()
    session = get_request_session()
    context = new_work_order_form_context(session, incident_id=incident_id)
    if context is None:
        abort(404)
    installation = context["installation"]
    if installation is None:
        flash("Sem Instalação associada a esta central; não é possível criar um trabalho.", "error")
        return redirect(url_for("diagnostics.incident", incident_id=incident_id))
    try:
        work_order = create_work_order(
            session,
            installation_id=installation.id,
            work_type=request.form.get("work_type", ""),
            title=request.form.get("title", ""),
            priority=request.form.get("priority", "normal"),
            created_by=browser_session.get("username", "web"),
            description=request.form.get("description"),
            planned_date=_parse_date(request.form.get("planned_date")),
            due_date=_parse_date(request.form.get("due_date")),
            assigned_to=request.form.get("assigned_to"),
            material_status=request.form.get("material_status", "not_applicable"),
            material_notes=request.form.get("material_notes"),
            estimated_cost_eur=_parse_decimal(request.form.get("estimated_cost_eur")),
            incident_ids=[incident_id],
        )
        session.commit()
        flash("Trabalho criado.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("diagnostics.new_work_order", incident_id=incident_id))
    return redirect(url_for("work_orders.show", work_order_id=work_order.id))


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
