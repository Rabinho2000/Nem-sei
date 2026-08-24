"""Bloco C: the commercial inputs a report needs, finally reachable.

`import_financial_model`, `set_tariff` and `set_billing_config` were all
written, tested and callable only from a Python shell -- no route touched any
of them. Production has 0 financial models, 1 tariff and 1 billing config for
266 assets, which is why expected production, deviation and performance have
nowhere to come from. None of this needs a provider call, so it is the one
block that moves at full speed while the FusionSolar account is contended.
"""
from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session as browser_session, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nemsei.assets.repository import AssetRepository
from nemsei.reporting.commercial import set_billing_config, set_tariff
from nemsei.reporting.commercial_models import BILLING_ENERGY_BASES, BILLING_MODES, REPORT_TYPES, TARIFF_TYPES
from nemsei.reporting.financial_workbook import FinancialModelParseError
from nemsei.reporting.models import FinancialModel, FinancialModelMonth, ReportSourceFile
from nemsei.reporting.service import import_financial_model, register_source_file
from nemsei.shared.clock import utc_now
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.queries import commercial_panel_data  # noqa: F401  (re-exportado para o detalhe da central)

commercial_bp = Blueprint("commercial", __name__)

ALLOWED_SUFFIXES = {".xlsm", ".xlsx"}


def actor() -> str:
    return browser_session.get("username", "web")


def money(value: str | None, label: str) -> Decimal:
    if not value or not value.strip():
        return Decimal("0")
    try:
        return Decimal(value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{label} tem de ser um número.") from exc


def optional_money(value: str | None, label: str) -> Decimal | None:
    return money(value, label) if value and value.strip() else None


# The database already refuses an incomplete cycle tariff
# (`ck_asset_tariffs_cycle_prices`). Checking here too is not redundant: an
# IntegrityError reaches the operator as a blank 500, while this names the
# field that is missing.
CYCLE_REQUIRED = {
    "simple": (("simple", "Simples"),),
    "bi-hourly": (("vazio", "Vazio"), ("cheia", "Cheia")),
    "tri-hourly": (("vazio", "Vazio"), ("cheia", "Cheia")),
    "tetra-hourly": (("vazio", "Vazio"), ("cheia", "Cheia"), ("super_vazio", "Super-vazio")),
}


def check_tariff_prices(tariff_type: str, prices: dict[str, Decimal | None]) -> None:
    missing = [label for key, label in CYCLE_REQUIRED.get(tariff_type, ()) if prices.get(key) is None]
    if missing:
        raise ValueError(f"Uma tarifa {tariff_type} precisa do preço de: {', '.join(missing)}.")


def required_date(value: str | None, label: str) -> date:
    if not value or not value.strip():
        raise ValueError(f"{label} é obrigatória.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} inválida. Use o formato AAAA-MM-DD.") from exc


@commercial_bp.post("/assets/<int:asset_id>/financial-model")
@require_authenticated
def upload_financial_model(asset_id: int):
    """Accept a workbook, parse it, and keep it as a draft awaiting review.

    Deliberately never `confirm=True` here: a model that has not been looked at
    should not start driving a customer's expected production. Confirmation is
    a separate, explicit act on the preview page.
    """
    require_valid_token()
    session = get_request_session()
    upload = request.files.get("workbook")
    if upload is None or not upload.filename:
        flash("Escolha um ficheiro para carregar.", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset_id))
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        flash("Só são aceites livros Excel .xlsm ou .xlsx.", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset_id))

    # The parser reads from a path, so the bytes touch disk once, inside a
    # temporary directory that is removed either way. The durable copy is the
    # column, because this container has no writable storage of its own.
    payload = upload.read()
    if not payload:
        flash("O ficheiro está vazio.", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset_id))
    try:
        with tempfile.TemporaryDirectory() as scratch:
            workbook_path = Path(scratch) / Path(upload.filename).name
            workbook_path.write_bytes(payload)
            source = register_source_file(
                session,
                asset_id=asset_id,
                path=workbook_path,
                original_filename=upload.filename,
                stored_path=f"db://report_source_files/{upload.filename}",
                uploaded_by=actor(),
                mime_type=upload.mimetype,
                content=payload,
                notes=request.form.get("notes"),
            )
            model = import_financial_model(
                session,
                source_file=source,
                workbook_path=workbook_path,
                operator=actor(),
                base_year_override=int(request.form["base_year"]) if request.form.get("base_year", "").strip() else None,
                confirm=False,
            )
        session.commit()
    except FinancialModelParseError as exc:
        session.rollback()
        flash(f"Não foi possível ler o livro: {exc}", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset_id))
    except (ValueError, KeyError) as exc:
        session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset_id))
    return redirect(url_for("commercial.financial_model", model_id=model.id))


@commercial_bp.get("/financial-models/<int:model_id>")
@require_authenticated
def financial_model(model_id: int) -> str:
    session = get_request_session()
    model = session.get(FinancialModel, model_id)
    if model is None:
        abort(404)
    asset = AssetRepository(session).asset(model.asset_id)
    months = list(
        session.scalars(
            select(FinancialModelMonth)
            .where(FinancialModelMonth.financial_model_id == model.id)
            .order_by(FinancialModelMonth.month)
        )
    )
    return render_template(
        "reporting/financial_model.html",
        title=f"Modelo financeiro · {asset.canonical_name if asset else model.asset_id}",
        model=model,
        asset=asset,
        months=months,
        months_by_index={month.month: month for month in months},
        source=session.get(ReportSourceFile, model.source_file_id),
        expected_total=sum((month.expected_production_kwh or Decimal("0")) for month in months),
        csrf_token=token(),
    )


@commercial_bp.post("/financial-models/<int:model_id>/confirm")
@require_authenticated
def confirm_financial_model(model_id: int):
    """Promote a reviewed draft, retiring whichever model it replaces."""
    session = get_request_session()
    require_valid_token()
    model = session.get(FinancialModel, model_id)
    if model is None:
        abort(404)
    if model.status != "draft":
        flash("Só um rascunho pode ser confirmado.", "error")
        return redirect(url_for("commercial.financial_model", model_id=model_id))
    previous = session.scalar(
        select(FinancialModel)
        .where(FinancialModel.asset_id == model.asset_id, FinancialModel.status == "confirmed")
        .order_by(FinancialModel.version.desc())
    )
    if previous is not None:
        previous.status = "superseded"
        model.supersedes_model_id = previous.id
    model.status = "confirmed"
    model.confirmed_by = actor()
    model.confirmed_at = utc_now()
    model.updated_at = model.confirmed_at
    session.commit()
    flash("Modelo confirmado. A produção esperada passa a sair deste modelo.", "success")
    return redirect(url_for("commercial.financial_model", model_id=model_id))


@commercial_bp.post("/financial-models/<int:model_id>/reject")
@require_authenticated
def reject_financial_model(model_id: int):
    session = get_request_session()
    require_valid_token()
    model = session.get(FinancialModel, model_id)
    if model is None:
        abort(404)
    if model.status not in {"draft", "confirmed"}:
        flash("Este modelo já não pode ser rejeitado.", "error")
        return redirect(url_for("commercial.financial_model", model_id=model_id))
    model.status = "rejected"
    session.commit()
    flash("Modelo rejeitado.", "success")
    return redirect(url_for("assets.asset_detail", asset_id=model.asset_id))


@commercial_bp.get("/financial-models/<int:model_id>/download")
@require_authenticated
def download_financial_model(model_id: int):
    session = get_request_session()
    model = session.get(FinancialModel, model_id)
    if model is None:
        abort(404)
    source = session.get(ReportSourceFile, model.source_file_id)
    if source is None or source.content is None:
        abort(404)
    return Response(
        source.content,
        mimetype=source.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{source.original_filename}"'},
    )


@commercial_bp.post("/assets/<int:asset_id>/tariff")
@require_authenticated
def save_tariff(asset_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        tariff_type = request.form.get("tariff_type", "")
        if tariff_type not in TARIFF_TYPES:
            raise ValueError("Tipo de tarifa desconhecido.")
        prices = {
            key: optional_money(request.form.get(f"price_{key}"), f"Preço {key}")
            for key in ("simple", "ponta", "cheia", "vazio", "super_vazio")
        }
        check_tariff_prices(tariff_type, prices)
        set_tariff(
            session,
            asset_id=asset_id,
            tariff_type=tariff_type,
            valid_from=required_date(request.form.get("valid_from"), "Data de início"),
            created_by=actor(),
            cycle_type=request.form.get("cycle_type") or None,
            prices=prices,
            source_kind="operator",
            notes=request.form.get("notes") or None,
        )
        session.commit()
        flash("Tarifa gravada.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    except IntegrityError:
        # Backstop: no database rule may reach an operator as a blank 500.
        session.rollback()
        flash("A tarifa não respeita as regras de coerência de preços.", "error")
    return redirect(url_for("assets.asset_detail", asset_id=asset_id))


@commercial_bp.post("/assets/<int:asset_id>/billing")
@require_authenticated
def save_billing(asset_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        report_type = request.form.get("report_type", "")
        billing_mode = request.form.get("billing_mode", "energy")
        billing_energy_base = request.form.get("billing_energy_base", "self_consumption")
        if report_type not in REPORT_TYPES:
            raise ValueError("Tipo de relatório desconhecido.")
        if billing_mode not in BILLING_MODES:
            raise ValueError("Modo de faturação desconhecido.")
        if billing_energy_base not in BILLING_ENERGY_BASES:
            raise ValueError("Base de energia desconhecida.")
        set_billing_config(
            session,
            asset_id=asset_id,
            report_type=report_type,
            valid_from=required_date(request.form.get("valid_from"), "Data de início"),
            created_by=actor(),
            billing_mode=billing_mode,
            billing_energy_base=billing_energy_base,
            solcor_price_per_kwh=money(request.form.get("solcor_price_per_kwh"), "Preço Solcor"),
            fixed_monthly_fee_eur=money(request.form.get("fixed_monthly_fee_eur"), "Avença mensal"),
            default_electricity_price=money(request.form.get("default_electricity_price"), "Preço de eletricidade"),
            default_export_price=money(request.form.get("default_export_price"), "Preço de venda à rede"),
            export_revenue_enabled=request.form.get("export_revenue_enabled") == "on",
            source_kind="operator",
        )
        session.commit()
        flash("Configuração de faturação gravada.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    except IntegrityError:
        session.rollback()
        flash("A configuração de faturação não respeita as regras da base de dados.", "error")
    return redirect(url_for("assets.asset_detail", asset_id=asset_id))
