from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, session as browser_session, url_for, current_app

from nemsei.assets.repository import AssetRepository
from nemsei.assets.service import add_alias, create_asset, create_organization, update_asset
from nemsei.providers.repository import ProviderRepository
from nemsei.providers.service import configure_connection, create_connection, create_mapping, set_connection_enabled
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.queries import asset_detail_data, list_assets_data, organization_list_data, provider_connections_data


assets_bp = Blueprint("assets", __name__)


def optional_decimal(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("Installed power must be numeric.") from exc


def optional_int(value: str | None) -> int | None:
    return int(value) if value and value.strip() else None


@assets_bp.get("/assets")
@require_authenticated
def list_assets() -> str:
    filters = {
        "search": request.args.get("q", "").strip(),
        "needs_review": request.args.get("needs_review", "").strip(),
        "provider": request.args.get("provider", "").strip(),
        "mapping": request.args.get("mapping", "").strip(),
        "page_value": request.args.get("page"),
    }
    return render_template(
        "assets/list.html",
        **list_assets_data(get_request_session(), **filters),
        csrf_token=token(),
        title="Centrais",
    )


@assets_bp.route("/assets/new", methods=["GET", "POST"])
@require_authenticated
def new_asset() -> str:
    session, repository = get_request_session(), AssetRepository(get_request_session())
    if request.method == "POST":
        require_valid_token()
        try:
            asset = create_asset(session, canonical_name=request.form.get("canonical_name", ""), owner_id=optional_int(request.form.get("owner_id")), lifecycle_status=request.form.get("lifecycle_status", "unknown"), country_code=request.form.get("country_code"), timezone=request.form.get("timezone", "Europe/Lisbon"), installed_dc_power_kw=optional_decimal(request.form.get("installed_dc_power_kw")), locality=request.form.get("locality"), address=request.form.get("address"), technical_notes=request.form.get("technical_notes"))
            session.commit()
            return redirect(url_for("assets.asset_detail", asset_id=asset.id))
        except (ValueError, InvalidOperation) as exc:
            session.rollback()
            flash(str(exc), "error")
    return render_template("assets/form.html", asset=None, organizations=repository.list_organizations(), csrf_token=token())


@assets_bp.route("/assets/<int:asset_id>", methods=["GET", "POST"])
@require_authenticated
def asset_detail(asset_id: int) -> str:
    session, repository = get_request_session(), AssetRepository(get_request_session())
    asset = repository.asset(asset_id)
    if asset is None:
        abort(404)
    if request.method == "POST":
        require_valid_token()
        try:
            update_asset(session, asset_id=asset.id, canonical_name=request.form.get("canonical_name", ""), owner_id=optional_int(request.form.get("owner_id")), lifecycle_status=request.form.get("lifecycle_status", "unknown"), country_code=request.form.get("country_code"), timezone=request.form.get("timezone", "Europe/Lisbon"), installed_dc_power_kw=optional_decimal(request.form.get("installed_dc_power_kw")), locality=request.form.get("locality"), address=request.form.get("address"), technical_notes=request.form.get("technical_notes"))
            session.commit()
            flash("Asset saved.", "success")
            return redirect(url_for("assets.asset_detail", asset_id=asset.id))
        except ValueError as exc:
            session.rollback()
            flash(str(exc), "error")
    providers = ProviderRepository(session)
    detail = asset_detail_data(session, asset.id)
    return render_template(
        "assets/detail.html",
        **detail,
        organizations=repository.list_organizations(),
        connections=providers.list_connections(),
        csrf_token=token(),
        title=asset.canonical_name,
    )


@assets_bp.post("/assets/<int:asset_id>/aliases")
@require_authenticated
def create_alias(asset_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        add_alias(session, asset_id=asset_id, alias=request.form.get("alias", ""))
        session.commit()
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.asset_detail", asset_id=asset_id))


@assets_bp.post("/assets/<int:asset_id>/mappings")
@require_authenticated
def create_asset_mapping(asset_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        create_mapping(session, asset_id=asset_id, provider_connection_id=int(request.form["provider_connection_id"]), external_id=request.form.get("external_id", ""), external_name=request.form.get("external_name"))
        session.commit()
    except (KeyError, ValueError) as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.asset_detail", asset_id=asset_id))


@assets_bp.route("/organizations", methods=["GET", "POST"])
@require_authenticated
def organizations() -> str:
    session = get_request_session()
    if request.method == "POST":
        require_valid_token()
        try:
            create_organization(session, display_name=request.form.get("display_name", ""), tax_id=request.form.get("tax_id"))
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("assets.organizations"))
    return render_template(
        "assets/organizations.html",
        **organization_list_data(session, search=request.args.get("q", "").strip(), page_value=request.args.get("page")),
        csrf_token=token(),
        title="Organizações",
    )


@assets_bp.route("/provider-connections", methods=["GET", "POST"])
@require_authenticated
def provider_connections() -> str:
    session = get_request_session()
    if request.method == "POST":
        require_valid_token()
        try:
            create_connection(session, provider_code=request.form.get("provider_code", ""), connection_key=request.form.get("connection_key", ""), display_name=request.form.get("display_name", ""), account_reference=request.form.get("account_reference"), region=request.form.get("region"), credential_reference=request.form.get("credential_reference"))
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("assets.provider_connections"))
    return render_template(
        "assets/connections.html",
        connections=provider_connections_data(session, settings=current_app.extensions["nemsei.settings"]),
        csrf_token=token(),
        title="Ligações",
    )


@assets_bp.post("/provider-connections/<int:connection_id>/configure")
@require_authenticated
def configure_provider_connection(connection_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        configure_connection(
            session,
            connection_id=connection_id,
            display_name=request.form.get("display_name"),
            account_reference=request.form.get("account_reference"),
            region=request.form.get("region"),
            credential_reference=request.form.get("credential_reference") or None,
            actor_username=browser_session.get("username", "web"),
        )
        session.commit()
        flash("Configuração guardada. A ligação continua desativada até ativação explícita.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.provider_connections"))


@assets_bp.post("/provider-connections/<int:connection_id>/enable")
@require_authenticated
def enable_provider_connection(connection_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        set_connection_enabled(session, connection_id=connection_id, enabled=True, actor_username=browser_session.get("username", "web"))
        session.commit()
        flash("Ligação ativada explicitamente; provider_reads global continua a ser obrigatório.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.provider_connections"))


@assets_bp.post("/provider-connections/<int:connection_id>/disable")
@require_authenticated
def disable_provider_connection(connection_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        set_connection_enabled(session, connection_id=connection_id, enabled=False, actor_username=browser_session.get("username", "web"))
        session.commit()
        flash("Ligação desativada.", "success")
    except ValueError as exc:
        session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("assets.provider_connections"))
