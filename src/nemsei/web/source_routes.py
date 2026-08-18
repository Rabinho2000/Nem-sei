from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.providers.models import AssetProviderMapping
from nemsei.sources.service import create_source_policy
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.queries import mapping_review_data, source_policy_data


source_bp = Blueprint("sources", __name__)


@source_bp.route("/source-policies", methods=["GET", "POST"])
@require_authenticated
def index() -> str:
    session = get_request_session()
    if request.method == "POST":
        require_valid_token()
        try:
            valid_from = date.fromisoformat(request.form.get("valid_from", ""))
            valid_to_value = request.form.get("valid_to", "").strip()
            valid_to = date.fromisoformat(valid_to_value) if valid_to_value else None
            mapping = session.get(AssetProviderMapping, int(request.form["provider_mapping_id"]))
            if mapping is None:
                raise ValueError("Mapping desconhecido.")
            create_source_policy(
                session,
                asset_id=mapping.asset_id,
                provider_mapping_id=int(request.form["provider_mapping_id"]),
                source_use=request.form.get("source_use", ""),
                priority=int(request.form.get("priority", "1")),
                valid_from=valid_from,
                valid_to=valid_to,
                is_fallback=request.form.get("is_fallback") == "on",
                actor_username=browser_session.get("username", "web"),
            )
            session.commit()
            flash("Política de fonte criada.", "success")
        except (KeyError, TypeError, ValueError) as exc:
            session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("sources.index"))
    mappings = [item for item in mapping_review_data(session, status="active")["mappings"]]
    return render_template(
        "source_policies.html",
        title="Políticas de fonte",
        policies=source_policy_data(session),
        mappings=mappings,
        csrf_token=token(),
    )
