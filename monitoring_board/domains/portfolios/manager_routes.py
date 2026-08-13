"""HTTP routes for portfolio configuration and asset alias management."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, send_file, url_for

from monitoring_board.portfolio_reports import auto_map_portfolio_assets
from monitoring_board.portfolio_repository import (
    add_member as portfolio_add_member,
    apply_import_run,
    archive_portfolio,
    confirm_mapping,
    copy_members,
    create_import_preview,
    create_portfolio,
    delete_alias as portfolio_delete_alias,
    delete_portfolio,
    detect_portfolio_conflicts,
    duplicate_portfolio,
    export_configuration_workbook,
    get_import_run,
    get_portfolio,
    import_preview_from_json,
    list_aliases as portfolio_list_aliases,
    list_available_assets as portfolio_list_available_assets,
    list_portfolio_members,
    list_portfolios,
    mapping_context as portfolio_mapping_context,
    move_member_up_down,
    reactivate_portfolio,
    rebuild_asset_alias_blob as portfolio_rebuild_asset_alias_blob,
    remove_members,
    reorder_members,
    suggest_mapping as portfolio_suggest_mapping,
    sync_portfolio_asset_members,
    toggle_alias,
    unmap_member,
    update_alias as portfolio_update_alias,
    update_member as portfolio_update_member,
    update_portfolio,
    upsert_alias,
)
from monitoring_board.db import query_all, query_scalar


PORTFOLIO_MANAGER_ERROR_MESSAGES = {
    "member_already_exists": "Esta instalacao ja pertence a este portfolio.",
    "member_not_found": "A entrada selecionada ja nao existe neste portfolio.",
    "no_members_copied": "Nenhuma instalacao pode ser copiada.",
    "no_members_moved": "Nenhuma instalacao pode ser movida.",
    "portfolio_not_found": "O portfolio selecionado nao existe.",
    "alias_conflict": "Este alias ja esta associado a outra instalacao.",
    "portfolio_has_report_history": "Este portfolio nao pode ser apagado porque tem historico de relatorios.",
    "delete_confirmation_mismatch": "Escreve o nome exato do portfolio para confirmar.",
    "asset_not_found": "A instalacao selecionada ja nao existe.",
    "duplicate_ids": "A selecao contem entradas repetidas.",
    "invalid_member_order": "A ordem enviada ja nao corresponde a este portfolio.",
    "no_ids": "Seleciona pelo menos uma instalacao.",
}


def _portfolio_error_message(exc: Exception) -> str:
    code = str(exc)
    return PORTFOLIO_MANAGER_ERROR_MESSAGES.get(code, code or "Nao foi possivel concluir a operacao.")


def _portfolio_manager_redirect(portfolio_id: int | None = None, **overrides: Any):
    values: dict[str, Any] = {}
    selected_id = portfolio_id if portfolio_id is not None else int(request.form.get("portfolio_id", request.args.get("portfolio_id", "0")) or 0)
    if selected_id:
        values["portfolio_id"] = selected_id
    for key in ("tab", "search", "asset_filter", "alias_search", "alias_filter"):
        value = overrides.pop(key, None)
        if value is None:
            value = request.form.get(key, request.args.get(key, ""))
        if value:
            values[key] = value
    values.update({key: value for key, value in overrides.items() if value not in (None, "")})
    return redirect(url_for("portfolio_manager", **values))


def _form_int_list(field_name: str) -> list[int]:
    values = request.form.getlist(field_name)
    if len(values) == 1 and "," in values[0]:
        values = [item.strip() for item in values[0].split(",")]
    return [int(value) for value in values if str(value).strip().isdigit()]


def _alias_return(asset_id: int) -> Any:
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    if request.referrer and "/portfolio-manager" in request.referrer:
        return redirect(request.referrer)
    return redirect(url_for("asset_detail", asset_id=asset_id))


def register_portfolio_manager_routes(app: Flask) -> None:
        @app.route("/portfolio-manager")
        def portfolio_manager() -> str:
            groups = list_portfolios(g.db, include_archived=True)
            selected_portfolio_id = int(request.args.get("portfolio_id", groups[0]["id"] if groups else 0) or 0)
            selected = get_portfolio(g.db, selected_portfolio_id) if selected_portfolio_id else None
            active_tab = request.args.get("tab", "installations").strip()
            if active_tab not in {"installations", "mappings", "aliases", "import", "settings"}:
                active_tab = "installations"
            search = request.args.get("search", "").strip()
            asset_filter = request.args.get("asset_filter", "available").strip()
            alias_search = request.args.get("alias_search", "").strip()
            alias_filter = request.args.get("alias_filter", "all").strip()
            import_id = int(request.args.get("import_id", "0") or 0)
            if import_id and request.args.get("tab") is None:
                active_tab = "import"
            members = list_portfolio_members(g.db, selected_portfolio_id) if selected else []
            assets = portfolio_list_available_assets(
                g.db,
                portfolio_id=selected_portfolio_id or None,
                search=search,
                asset_filter=asset_filter,
            )
            all_assets = portfolio_list_available_assets(g.db, portfolio_id=selected_portfolio_id or None, asset_filter="all")
            portfolio_counts = {
                int(row["portfolio_id"]): int(row["count"] or 0)
                for row in query_all(
                    g.db,
                    "SELECT portfolio_id, COUNT(*) AS count FROM portfolio_assets WHERE active = 1 GROUP BY portfolio_id",
                )
            }
            available_total = query_scalar(
                g.db,
                """
                SELECT COUNT(*)
                FROM assets a
                WHERE NOT EXISTS (
                    SELECT 1 FROM portfolio_assets pa
                    WHERE pa.portfolio_id = ? AND pa.asset_id = a.id AND pa.active = 1
                )
                """,
                (selected_portfolio_id,),
            ) if selected else 0
            pending_count = sum(1 for member in members if not member["asset_id"] or member["mapping_status"] == "mapping_pending")
            conflict_count = sum(1 for member in members if member["mapping_status"] == "mapping_conflict")
            aliases = []
            if active_tab == "aliases":
                aliases = portfolio_list_aliases(g.db, include_inactive=alias_filter != "active")
                if alias_search:
                    alias_search_lower = alias_search.lower()
                    aliases = [alias for alias in aliases if alias_search_lower in str(alias["alias_name"] or "").lower() or alias_search_lower in str(alias["project_name"] or "").lower()]
                if alias_filter == "inactive":
                    aliases = [alias for alias in aliases if not alias["active"]]
            conflicts = detect_portfolio_conflicts(g.db, selected_portfolio_id or None)
            import_run = get_import_run(g.db, import_id) if import_id else None
            import_preview = import_preview_from_json(import_run["preview_json"] or "{}") if import_run else None
            mapping_context = portfolio_mapping_context(g.db)
            mapping_decisions = {
                int(member["id"]): portfolio_suggest_mapping(
                    g.db,
                    external_name=member["external_name"] or "",
                    nif=member["nif"] or "",
                    sub_account=member["sub_account"] or "",
                    context=mapping_context,
                )
                for member in members
                if not member["asset_id"] or member["mapping_status"] in {"mapping_pending", "mapping_conflict", "mapping_suggested"}
            }
            return render_template(
                "portfolio_manager.html",
                title="Gestor de portfolios",
                groups=groups,
                selected=selected,
                active_tab=active_tab,
                selected_portfolio_id=selected_portfolio_id,
                portfolio_counts=portfolio_counts,
                available_total=available_total,
                pending_count=pending_count,
                conflict_count=conflict_count,
                members=members,
                assets=assets,
                all_assets=all_assets,
                aliases=aliases,
                conflicts=conflicts,
                search=search,
                asset_filter=asset_filter,
                alias_search=alias_search,
                alias_filter=alias_filter,
                import_run=import_run,
                import_preview=import_preview,
                mapping_decisions=mapping_decisions,
            )

        @app.route("/portfolio-manager/create", methods=["POST"])
        def portfolio_manager_create():
            try:
                portfolio_id = create_portfolio(g.db, name=request.form.get("name", ""), description=request.form.get("description", ""), notes=request.form.get("notes", ""))
                g.db.commit()
                flash("Portfolio criado.", "success")
                return _portfolio_manager_redirect(portfolio_id, tab="installations")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao criar portfolio: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(0)

        @app.route("/portfolio-manager/update", methods=["POST"])
        def portfolio_manager_update():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                update_portfolio(
                    g.db,
                    portfolio_id=portfolio_id,
                    name=request.form.get("name", ""),
                    description=request.form.get("description", ""),
                    notes=request.form.get("notes", ""),
                    display_order=int(request.form.get("display_order", "0") or 0) or None,
                )
                g.db.commit()
                flash("Portfolio atualizado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao atualizar portfolio: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="settings")

        @app.route("/portfolio-manager/duplicate", methods=["POST"])
        def portfolio_manager_duplicate():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                new_id = duplicate_portfolio(g.db, portfolio_id=portfolio_id, new_name=request.form.get("new_name", ""))
                g.db.commit()
                flash("Portfolio duplicado.", "success")
                return _portfolio_manager_redirect(new_id, tab="installations")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao duplicar portfolio: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(portfolio_id, tab="settings")

        @app.route("/portfolio-manager/archive", methods=["POST"])
        def portfolio_manager_archive():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                archive_portfolio(g.db, portfolio_id)
                g.db.commit()
                flash("Portfolio arquivado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao arquivar portfolio: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="settings")

        @app.route("/portfolio-manager/reactivate", methods=["POST"])
        def portfolio_manager_reactivate():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                reactivate_portfolio(g.db, portfolio_id)
                g.db.commit()
                flash("Portfolio reativado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao reativar portfolio: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="settings")

        @app.route("/portfolio-manager/delete", methods=["POST"])
        def portfolio_manager_delete():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                delete_portfolio(g.db, portfolio_id, confirm_name=request.form.get("confirm_name", ""))
                g.db.commit()
                flash("Portfolio apagado.", "success")
                return _portfolio_manager_redirect(0)
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao apagar portfolio: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(portfolio_id, tab="settings")

        @app.route("/portfolio-manager/members/add", methods=["POST"])
        def portfolio_manager_members_add():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                asset_ids = _form_int_list("asset_ids")
                reactivated = 0
                if asset_ids:
                    for asset_id in asset_ids:
                        inactive = g.db.execute(
                            "SELECT 1 FROM portfolio_assets WHERE portfolio_id = ? AND asset_id = ? AND active = 0",
                            (portfolio_id, asset_id),
                        ).fetchone()
                        portfolio_add_member(g.db, portfolio_id=portfolio_id, asset_id=asset_id)
                        if inactive:
                            reactivated += 1
                else:
                    asset_raw = request.form.get("asset_id", "").strip()
                    asset_id = int(asset_raw) if asset_raw.isdigit() else None
                    inactive = (
                        g.db.execute(
                            "SELECT 1 FROM portfolio_assets WHERE portfolio_id = ? AND asset_id = ? AND active = 0",
                            (portfolio_id, asset_id),
                        ).fetchone()
                        if asset_id
                        else None
                    )
                    portfolio_add_member(
                        g.db,
                        portfolio_id=portfolio_id,
                        asset_id=asset_id,
                        external_name=request.form.get("external_name", ""),
                        nif=request.form.get("nif", ""),
                        sub_account=request.form.get("sub_account", ""),
                        notes=request.form.get("notes", ""),
                    )
                    if inactive:
                        reactivated += 1
                g.db.commit()
                flash("Instalacao reativada no portfolio." if reactivated else "Instalacao adicionada ao portfolio.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao adicionar instalacao: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="installations")

        @app.route("/portfolio-manager/members/apply", methods=["POST"])
        def portfolio_manager_members_apply():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                asset_ids = _form_int_list("asset_ids")
                asset_names = {
                    asset_id: request.form.get(f"asset_name_{asset_id}", "")
                    for asset_id in asset_ids
                }
                result = sync_portfolio_asset_members(g.db, portfolio_id=portfolio_id, asset_ids=asset_ids, asset_names=asset_names)
                g.db.commit()
                flash(f"Alteracoes aplicadas: {result['selected']} instalacoes selecionadas.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao aplicar alteracoes: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="installations")

        @app.route("/portfolio-manager/members/remove", methods=["POST"])
        def portfolio_manager_members_remove():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                remove_members(g.db, portfolio_id=portfolio_id, member_ids=_form_int_list("member_ids"))
                g.db.commit()
                flash("Instalacoes removidas do portfolio.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao remover instalacoes: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="installations")

        @app.route("/portfolio-manager/members/copy", methods=["POST"])
        def portfolio_manager_members_copy():
            source_id = int(request.form.get("portfolio_id", "0") or 0)
            target_id = int(request.form.get("target_portfolio_id", "0") or 0)
            try:
                copy_members(g.db, source_portfolio_id=source_id, target_portfolio_id=target_id, member_ids=_form_int_list("member_ids"), move=False)
                g.db.commit()
                flash("Instalacoes copiadas.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao copiar instalacoes: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(source_id, tab="installations")

        @app.route("/portfolio-manager/members/move", methods=["POST"])
        def portfolio_manager_members_move():
            source_id = int(request.form.get("portfolio_id", "0") or 0)
            target_id = int(request.form.get("target_portfolio_id", "0") or 0)
            try:
                copy_members(g.db, source_portfolio_id=source_id, target_portfolio_id=target_id, member_ids=_form_int_list("member_ids"), move=True)
                g.db.commit()
                flash("Instalacoes movidas.", "success")
                return _portfolio_manager_redirect(target_id, tab="installations")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao mover instalacoes: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(source_id, tab="installations")

        @app.route("/portfolio-manager/members/reorder", methods=["POST"])
        def portfolio_manager_members_reorder():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                ordered = _form_int_list("ordered_ids")
                if not ordered:
                    member_id = int(request.form.get("member_id", "0") or 0)
                    move_member_up_down(g.db, portfolio_id=portfolio_id, member_id=member_id, direction=request.form.get("direction", "down"))
                else:
                    reorder_members(g.db, portfolio_id=portfolio_id, ordered_ids=ordered)
                g.db.commit()
                flash("Ordem guardada.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao reordenar: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="installations")

        @app.route("/portfolio-manager/members/update", methods=["POST"])
        def portfolio_manager_members_update():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            member_id = int(request.form.get("member_id", "0") or 0)
            asset_raw = request.form.get("asset_id", "").strip()
            try:
                portfolio_update_member(
                    g.db,
                    member_id=member_id,
                    portfolio_id=portfolio_id,
                    asset_id=int(asset_raw) if asset_raw.isdigit() else None,
                    external_name=request.form.get("external_name", ""),
                    nif=request.form.get("nif", ""),
                    sub_account=request.form.get("sub_account", ""),
                    notes=request.form.get("notes", ""),
                    active=request.form.get("active", "on") == "on",
                    create_alias=request.form.get("create_alias") == "on",
                )
                g.db.commit()
                flash("Entrada atualizada.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao atualizar entrada: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab=request.form.get("tab") or "installations", focus=f"member-{member_id}")

        @app.route("/portfolio-manager/mappings/suggest", methods=["POST"])
        def portfolio_manager_mappings_suggest():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                result = auto_map_portfolio_assets(g.db, portfolio_id=portfolio_id or None)
                g.db.commit()
                flash(f"Sugestoes calculadas: {result['mapped']} auto, {result['pending']} pendentes, {result['conflicts']} conflitos.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao sugerir mappings: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab="mappings")

        @app.route("/portfolio-manager/mappings/confirm", methods=["POST"])
        def portfolio_manager_mappings_confirm():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                confirm_mapping(
                    g.db,
                    member_id=int(request.form.get("member_id", "0") or 0),
                    portfolio_id=portfolio_id,
                    asset_id=int(request.form.get("asset_id", "0") or 0),
                    create_alias=request.form.get("create_alias", "on") == "on",
                )
                g.db.commit()
                flash("Mapping confirmado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao confirmar mapping: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab=request.form.get("tab") or "mappings", focus=f"member-{request.form.get('member_id', '0')}")

        @app.route("/portfolio-manager/mappings/unmap", methods=["POST"])
        def portfolio_manager_mappings_unmap():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
            try:
                unmap_member(g.db, member_id=int(request.form.get("member_id", "0") or 0), portfolio_id=portfolio_id)
                g.db.commit()
                flash("Mapping removido.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao remover mapping: {_portfolio_error_message(exc)}", "error")
            return _portfolio_manager_redirect(portfolio_id, tab=request.form.get("tab") or "installations", focus=f"member-{request.form.get('member_id', '0')}")

        @app.route("/assets/<int:asset_id>/aliases/add", methods=["POST"])
        def portfolio_alias_add(asset_id: int):
            try:
                upsert_alias(g.db, asset_id=asset_id, alias_name=request.form.get("alias_name", ""), source="manual", notes=request.form.get("notes", ""))
                portfolio_rebuild_asset_alias_blob(g.db, asset_id)
                g.db.commit()
                flash("Alias guardado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao guardar alias: {_portfolio_error_message(exc)}", "error")
            return _alias_return(asset_id)

        @app.route("/assets/<int:asset_id>/aliases/update", methods=["POST"])
        def portfolio_alias_update(asset_id: int):
            try:
                portfolio_update_alias(g.db, asset_id=asset_id, alias_id=int(request.form.get("alias_id", "0") or 0), alias_name=request.form.get("alias_name", ""), notes=request.form.get("notes", ""))
                portfolio_rebuild_asset_alias_blob(g.db, asset_id)
                g.db.commit()
                flash("Alias atualizado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao atualizar alias: {_portfolio_error_message(exc)}", "error")
            return _alias_return(asset_id)

        @app.route("/assets/<int:asset_id>/aliases/toggle", methods=["POST"])
        def portfolio_alias_toggle(asset_id: int):
            try:
                toggle_alias(g.db, asset_id=asset_id, alias_id=int(request.form.get("alias_id", "0") or 0), active=request.form.get("active") == "1")
                portfolio_rebuild_asset_alias_blob(g.db, asset_id)
                g.db.commit()
                flash("Alias atualizado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao alterar alias: {_portfolio_error_message(exc)}", "error")
            return _alias_return(asset_id)

        @app.route("/assets/<int:asset_id>/aliases/delete", methods=["POST"])
        def portfolio_alias_delete(asset_id: int):
            try:
                portfolio_delete_alias(g.db, asset_id=asset_id, alias_id=int(request.form.get("alias_id", "0") or 0))
                portfolio_rebuild_asset_alias_blob(g.db, asset_id)
                g.db.commit()
                flash("Alias apagado.", "success")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao apagar alias: {_portfolio_error_message(exc)}", "error")
            return _alias_return(asset_id)

        @app.route("/portfolio-manager/import", methods=["POST"])
        def portfolio_manager_import():
            portfolio_id = int(request.form.get("portfolio_id", "0") or 0) or None
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                flash("Escolhe um ficheiro CSV ou XLSX.", "error")
                return redirect(url_for("portfolio_manager", portfolio_id=portfolio_id or 0))
            try:
                import_id = create_import_preview(g.db, portfolio_id=portfolio_id, original_filename=Path(upload.filename).name, data=upload.read())
                g.db.commit()
                flash("Preview de importacao criado.", "success")
                return _portfolio_manager_redirect(portfolio_id or 0, tab="import", import_id=import_id)
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao importar configuracao: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(portfolio_id or 0, tab="import")

        @app.route("/portfolio-manager/import/<int:import_id>/apply", methods=["POST"])
        def portfolio_manager_import_apply(import_id: int):
            try:
                run = get_import_run(g.db, import_id)
                selected_rows = _form_int_list("row_numbers")
                overrides = {
                    int(key.removeprefix("asset_override_")): int(value)
                    for key, value in request.form.items()
                    if key.startswith("asset_override_") and str(value).isdigit()
                }
                apply_import_run(g.db, import_id, selected_rows=selected_rows or None, asset_overrides=overrides)
                g.db.commit()
                flash("Importacao aplicada.", "success")
                return _portfolio_manager_redirect(run["portfolio_id"] if run else 0, tab="import")
            except Exception as exc:
                g.db.rollback()
                flash(f"Falha ao aplicar importacao: {_portfolio_error_message(exc)}", "error")
                return _portfolio_manager_redirect(None, tab="import", import_id=import_id)

        @app.route("/portfolio-manager/export")
        def portfolio_manager_export():
            workbook = export_configuration_workbook(g.db)
            output = io.BytesIO()
            workbook.save(output)
            output.seek(0)
            return send_file(
                output,
                as_attachment=True,
                download_name="portfolio_configuration.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

