from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from nemsei.assets.repository import AssetRepository
from nemsei.assets.service import add_alias, create_asset, create_organization, update_asset
from nemsei.providers.repository import ProviderRepository
from nemsei.providers.service import cross_connection_conflicts, create_connection, create_mapping
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated


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
    return render_template("assets/list.html", assets=AssetRepository(get_request_session()).list_assets(), csrf_token=token())


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
    mappings = providers.mappings_for_asset(asset.id)
    conflicts = {
        mapping.id: cross_connection_conflicts(session, mapping_id=mapping.id)
        for mapping in mappings
    }
    return render_template("assets/detail.html", asset=asset, organizations=repository.list_organizations(), aliases=list(asset.aliases), mappings=mappings, mapping_conflicts=conflicts, connections=providers.list_connections(), csrf_token=token())


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
    session, repository = get_request_session(), AssetRepository(get_request_session())
    if request.method == "POST":
        require_valid_token()
        try:
            create_organization(session, display_name=request.form.get("display_name", ""), tax_id=request.form.get("tax_id"))
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("assets.organizations"))
    return render_template("assets/organizations.html", organizations=repository.list_organizations(), csrf_token=token())


@assets_bp.route("/provider-connections", methods=["GET", "POST"])
@require_authenticated
def provider_connections() -> str:
    session, repository = get_request_session(), ProviderRepository(get_request_session())
    if request.method == "POST":
        require_valid_token()
        try:
            create_connection(session, provider_code=request.form.get("provider_code", ""), connection_key=request.form.get("connection_key", ""), display_name=request.form.get("display_name", ""), account_reference=request.form.get("account_reference"), region=request.form.get("region"), credential_reference=request.form.get("credential_reference"))
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("assets.provider_connections"))
    return render_template("assets/connections.html", connections=repository.list_connections(), csrf_token=token())
