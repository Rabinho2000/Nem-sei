"""The automations screen: what runs on its own, and what a person can change."""
from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.notifications.digests import build_digest_payload, render_digest_text
from nemsei.system.automations import (
    ASSET_SCOPE_LABELS,
    automations_overview,
    digest_preview_window,
    set_channel_enabled,
    set_policy_asset_scope,
    set_policy_enabled,
)
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated

automation_bp = Blueprint("automations", __name__, url_prefix="/automations")


def actor() -> str:
    return browser_session.get("username", "web")


@automation_bp.get("")
@require_authenticated
def index() -> str:
    return render_template(
        "automations.html",
        title="Automações",
        csrf_token=token(),
        **automations_overview(get_request_session()),
    )


@automation_bp.get("/digest-preview")
@require_authenticated
def digest_preview() -> str:
    """What the digest would say if it ran now. Reads only; writes nothing.

    `build_digest_payload` is the same function the real run uses, called
    without `generate_digest` around it -- so the preview cannot create a
    DigestRun, and cannot advance the window that the next real digest chains
    from.
    """
    session = get_request_session()
    settings = current_app.extensions["nemsei.settings"]
    interval = getattr(settings, "digest_generation_interval_minutes", 1440) or 1440
    window_start, window_end = digest_preview_window(interval_minutes=interval)
    payload = build_digest_payload(session, window_start=window_start, window_end=window_end)
    return render_template(
        "automations_digest_preview.html",
        title="Pré-visualização do digest",
        interval_minutes=interval,
        window_start=window_start,
        window_end=window_end,
        text=render_digest_text(payload),
        payload=payload,
    )


@automation_bp.post("/channels/<int:channel_id>")
@require_authenticated
def toggle_channel(channel_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        channel = set_channel_enabled(session, channel_id=channel_id, enabled=request.form.get("enabled") == "on", actor=actor())
        session.commit()
        flash(f"Canal {channel.name} {'ativado' if channel.enabled else 'desativado'}.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("automations.index"))


@automation_bp.post("/policies/<int:policy_id>/ambito")
@require_authenticated
def set_policy_scope(policy_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        policy = set_policy_asset_scope(
            session,
            policy_id=policy_id,
            asset_scope=request.form.get("asset_scope", "").strip(),
            actor=actor(),
        )
        session.commit()
        flash(f"Política {policy.name}: âmbito {ASSET_SCOPE_LABELS[policy.asset_scope].lower()}.", "success")
    except (ValueError, KeyError) as exc:
        session.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "Âmbito de centrais desconhecido.", "error")
    return redirect(url_for("automations.index"))


@automation_bp.post("/policies/<int:policy_id>")
@require_authenticated
def toggle_policy(policy_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        policy = set_policy_enabled(session, policy_id=policy_id, enabled=request.form.get("enabled") == "on", actor=actor())
        session.commit()
        flash(f"Política {policy.name} {'ativada' if policy.enabled else 'desativada'}.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("automations.index"))
