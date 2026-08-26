"""The O&M contract screens: one installation's engagements, and the renewals.

Every write here goes through `contracts.service`, so the temporal rules -- a
renewal is a new row, two rows may not claim the same day -- are enforced in
one place rather than restated per form.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.contracts.service import close_service_contract, set_service_contract, update_renewal
from nemsei.providers.audit import record_operator_action
from nemsei.web.contract_queries import contracts_page_data
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated

contracts_bp = Blueprint("contracts", __name__)


def actor() -> str:
    return str(browser_session.get("username") or "operador")


def optional_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Data inválida.") from exc


def optional_money(value: str | None) -> Decimal | None:
    text = (value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("O valor anual não é válido.") from exc


def optional_choice(value: str | None) -> str | None:
    return (value or "").strip() or None


@contracts_bp.get("/contratos")
@require_authenticated
def index() -> str:
    return render_template(
        "contracts/list.html",
        **contracts_page_data(get_request_session(), bucket=request.args.get("bucket", "").strip()),
        csrf_token=token(),
        title="Contratos O&M",
    )


@contracts_bp.post("/assets/<int:asset_id>/contratos")
@require_authenticated
def create_contract(asset_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        contract = set_service_contract(
            session,
            asset_id=asset_id,
            created_by=actor(),
            valid_from=optional_date(request.form.get("valid_from")),
            valid_to=optional_date(request.form.get("valid_to")),
            annual_value_eur=optional_money(request.form.get("annual_value_eur")),
            renewal_status=optional_choice(request.form.get("renewal_status")),
            last_contact_on=optional_date(request.form.get("last_contact_on")),
            notes=request.form.get("notes"),
        )
        record_operator_action(
            session,
            actor_username=actor(),
            action="service_contract_created",
            entity_type="asset_service_contract",
            entity_id=contract.id,
            metadata={"asset_id": asset_id, "contract_id": contract.id},
        )
        session.commit()
        flash("Contrato O&M registado.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.asset_detail", asset_id=asset_id) + "#om")


@contracts_bp.post("/contratos/<int:contract_id>/fechar")
@require_authenticated
def close_contract(contract_id: int):
    require_valid_token()
    session = get_request_session()
    asset_id = request.form.get("asset_id", type=int)
    try:
        valid_to = optional_date(request.form.get("valid_to"))
        if valid_to is None:
            raise ValueError("Indique a data em que o contrato termina.")
        contract = close_service_contract(session, contract_id=contract_id, valid_to=valid_to, actor=actor())
        record_operator_action(
            session,
            actor_username=actor(),
            action="service_contract_closed",
            entity_type="asset_service_contract",
            entity_id=contract.id,
            metadata={"asset_id": contract.asset_id, "contract_id": contract.id},
        )
        session.commit()
        flash("Contrato fechado.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    if asset_id:
        return redirect(url_for("assets.asset_detail", asset_id=asset_id) + "#om")
    return redirect(url_for("contracts.index"))


@contracts_bp.post("/contratos/<int:contract_id>/renovacao")
@require_authenticated
def save_renewal(contract_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        contract = update_renewal(
            session,
            contract_id=contract_id,
            renewal_status=optional_choice(request.form.get("renewal_status")),
            last_contact_on=optional_date(request.form.get("last_contact_on")),
            notes=request.form.get("notes"),
        )
        record_operator_action(
            session,
            actor_username=actor(),
            action="service_contract_renewal_updated",
            entity_type="asset_service_contract",
            entity_id=contract.id,
            metadata={"asset_id": contract.asset_id, "contract_id": contract.id},
        )
        session.commit()
        flash("Seguimento de renovação atualizado.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(request.form.get("back") or url_for("contracts.index"))
