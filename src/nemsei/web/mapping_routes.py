from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.providers.preflight import activation_preflight
from nemsei.providers.service import approve_mapping, reject_mapping
from nemsei.providers.registry import ProviderCapability
from nemsei.integrations.fusionsolar.validation import FusionSolarSingleAssetValidation
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


@mapping_bp.post("/mappings/bulk")
@require_authenticated
def bulk_decide():
    """Approve or reject many mappings at once, selection always explicit.

    Each one still goes through `approve_mapping`/`reject_mapping` unchanged --
    this is a loop over the same guarded call, not a shortcut past it. One
    refusal does not discard the rest: the successes commit and the failures
    are reported with their count, because a batch of 460 in which one asset is
    unreviewed should not be an all-or-nothing.
    """
    require_valid_token()
    session = get_request_session()
    decision = request.form.get("decision", "")
    selected = [int(value) for value in request.form.getlist("mapping_ids") if value.isdigit()]
    if decision not in {"approve", "reject"} or not selected:
        flash("Escolha uma ação e pelo menos um mapping.", "error")
        return redirect(request.referrer or url_for("mappings.index"))

    act = approve_mapping if decision == "approve" else reject_mapping
    actor = browser_session.get("username", "web")
    done, failures = 0, []
    for mapping_id in selected:
        try:
            act(session, mapping_id=mapping_id, actor_username=actor)
            session.commit()
            done += 1
        except ValueError as exc:
            session.rollback()
            failures.append(str(exc))
    verb = "aprovados" if decision == "approve" else "rejeitados"
    if done:
        flash(f"{done} de {len(selected)} mappings {verb}.", "success")
    if failures:
        reason = max(set(failures), key=failures.count)
        flash(f"{len(failures)} recusados. Motivo mais comum: {reason}", "error")
    return redirect(request.referrer or url_for("mappings.index"))


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


@mapping_bp.post("/mappings/<int:mapping_id>/validate")
@require_authenticated
def validate_single_asset(mapping_id: int):
    """Run only the explicitly requested, one-mapping read-only validation."""
    require_valid_token()
    settings = current_app.extensions["nemsei.settings"]
    factory = current_app.extensions["nemsei.session_factory"]
    try:
        result = FusionSolarSingleAssetValidation(factory, settings).run(
            mapping_id,
            actor_username=browser_session.get("username", "web"),
        )
        if result.status == "blocked":
            flash("Validação bloqueada: " + ", ".join(result.findings), "error")
        elif result.status == "success":
            flash(f"Validação read-only concluída ({result.provider_calls} chamadas provider).", "success")
        else:
            flash(f"Validação terminou com estado {result.status} ({result.provider_calls} chamadas provider).", "error")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("mappings.preflight", mapping_id=mapping_id, capability=ProviderCapability.CURRENT_MONITORING.value))
