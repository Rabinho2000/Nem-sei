"""Contract-renewal and local Excel-import routes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, url_for


def register_settings_routes(
    app: Flask,
    *,
    backup_dir: Path,
    create_database_backup: Callable[[Path, Path], Path],
    import_excel_data: Callable[[Any, Path], dict[str, int]],
    normalize_date_value: Callable[[str], str],
    query_all: Callable[..., Any],
    query_one: Callable[..., Any],
    query_scalar: Callable[..., Any],
    sync_asset_contract_status: Callable[[Any, int, str, str], None],
) -> None:
    @app.route("/renewals", methods=["GET", "POST"])
    def renewals() -> str:
        focus = request.args.get("focus", "").strip()
        if request.method == "POST":
            asset_id = int(request.form["asset_id"])
            renewal_status = request.form.get("renewal_status", "Por contactar").strip() or "Por contactar"
            last_contact_date = request.form.get("last_contact_date", "").strip()
            renewal_notes = request.form.get("renewal_notes", "").strip()
            annual_value_raw = request.form.get("annual_value", "").strip()
            contract_end_date = normalize_date_value(request.form.get("contract_end_date", "").strip())
            contract_start_date = normalize_date_value(request.form.get("contract_start_date", "").strip())

            annual_value = None
            if annual_value_raw:
                normalized_value = annual_value_raw.replace(" ", "").replace(",", ".")
                try:
                    annual_value = float(normalized_value)
                except ValueError:
                    flash("O valor anual nao e valido.", "error")
                    return redirect(url_for("renewals"))

            existing_contract = query_one("SELECT id FROM om_contracts WHERE asset_id = ?", (asset_id,))
            if existing_contract:
                g.db.execute(
                    """
                    UPDATE om_contracts
                    SET renewal_status = ?, last_contact_date = ?, renewal_notes = ?,
                        annual_value = COALESCE(?, annual_value),
                        contract_start_date = CASE WHEN ? != '' THEN ? ELSE contract_start_date END,
                        contract_end_date = CASE WHEN ? != '' THEN ? ELSE contract_end_date END,
                        updated_at = ?
                    WHERE asset_id = ?
                    """,
                    (
                        renewal_status,
                        last_contact_date,
                        renewal_notes,
                        annual_value,
                        contract_start_date,
                        contract_start_date,
                        contract_end_date,
                        contract_end_date,
                        datetime.now().isoformat(timespec="seconds"),
                        asset_id,
                    ),
                )
            else:
                g.db.execute(
                    """
                    INSERT INTO om_contracts (
                        asset_id, contract_start_date, contract_end_date, annual_value, renewal_status, last_contact_date,
                        renewal_notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        contract_start_date,
                        contract_end_date,
                        annual_value,
                        renewal_status,
                        last_contact_date,
                        renewal_notes,
                        datetime.now().isoformat(timespec="seconds"),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )

            if renewal_status == "Renovado":
                g.db.execute(
                    """
                    UPDATE assets
                    SET maintenance = 'yes',
                        active_contract = 'yes',
                        start_contract = CASE WHEN ? != '' THEN ? ELSE start_contract END,
                        end_contract = CASE WHEN ? != '' THEN ? ELSE end_contract END
                    WHERE id = ?
                    """,
                    (
                        contract_start_date,
                        contract_start_date,
                        contract_end_date,
                        contract_end_date,
                        asset_id,
                    ),
                )
                sync_asset_contract_status(g.db, asset_id, contract_start_date, contract_end_date)
            g.db.commit()
            flash("Follow-up de renovacao atualizado.", "success")
            return redirect(url_for("renewals"))

        today_iso = date.today().isoformat()
        year_end = f"{date.today().year}-12-31"
        renewal_rows = query_all(
            g.db,
            """
            SELECT
                a.id AS asset_id,
                a.project_name,
                a.installation_group,
                a.company_name,
                a.location,
                a.address,
                a.contact_name,
                a.contact_email,
                a.contact_phone,
                a.active_contract,
                COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) AS contract_end_date,
                COALESCE(NULLIF(oc.contract_start_date, ''), NULLIF(a.start_contract, '')) AS contract_start_date,
                oc.annual_value,
                oc.pdf_path,
                oc.renewal_status,
                oc.last_contact_date,
                oc.renewal_notes,
                CASE
                    WHEN COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) < ? THEN 'Expirado'
                    WHEN julianday(COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))) - julianday(?) <= 30 THEN '0-30 dias'
                    WHEN julianday(COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))) - julianday(?) <= 90 THEN '31-90 dias'
                    ELSE 'Este ano'
                END AS renewal_bucket
            FROM assets a
            LEFT JOIN om_contracts oc ON oc.asset_id = a.id
            WHERE (a.maintenance = 'yes' OR oc.id IS NOT NULL)
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) NOT IN ('', '-')
            ORDER BY COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) ASC, a.project_name COLLATE NOCASE
            """,
            (today_iso, today_iso, today_iso),
        )
        expired_contracts = [
            row for row in renewal_rows
            if row["contract_end_date"] < today_iso
        ]
        ending_this_year = [
            row for row in renewal_rows
            if today_iso <= row["contract_end_date"] <= year_end
        ]
        if focus == "expired":
            ending_this_year = []
        elif focus == "year":
            expired_contracts = []
        elif focus == "90":
            expired_contracts = []
            ending_this_year = [row for row in ending_this_year if row["renewal_bucket"] in {"0-30 dias", "31-90 dias"}]
        renewal_metrics = {
            "expired": len(expired_contracts),
            "next_30_days": sum(1 for row in renewal_rows if row["renewal_bucket"] == "0-30 dias"),
            "next_90_days": sum(1 for row in renewal_rows if row["renewal_bucket"] == "31-90 dias"),
            "this_year": len(ending_this_year),
        }
        return render_template(
            "renewals.html",
            expired_contracts=expired_contracts,
            ending_this_year=ending_this_year,
            renewal_metrics=renewal_metrics,
            focus=focus,
            today_iso=today_iso,
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings() -> str:
        if request.method == "POST":
            excel_path = request.form.get("excel_path", "").strip()
            if not excel_path:
                flash("Indica o caminho do Excel.", "error")
                return redirect(url_for("settings"))
            excel_file = Path(excel_path)
            if not excel_file.exists() or not excel_file.is_file():
                flash("O ficheiro Excel indicado nao existe ou nao esta acessivel.", "error")
                return redirect(url_for("settings"))
            if excel_file.suffix.lower() not in {".xlsx", ".xlsm"}:
                flash("Indica um ficheiro Excel valido (.xlsx ou .xlsm).", "error")
                return redirect(url_for("settings"))

            app.config["EXCEL_PATH"] = excel_path
            backup_path = create_database_backup(Path(app.config["DATABASE"]), backup_dir)
            try:
                imported = import_excel_data(g.db, excel_file)
            except Exception as exc:
                flash(f"Falha ao importar o Excel: {exc}", "error")
                flash(f"A base de dados ficou salvaguardada no backup {backup_path.name}.", "warning")
                return redirect(url_for("settings"))
            flash(
                f"Importacao concluida. {imported['assets']} assets, {imported['monitoring']} linhas de monitorizacao e {imported['tickets']} tickets importados.",
                "success",
            )
            flash(
                f"Backup automatico criado antes da reimportacao: {backup_path.name}",
                "warning",
            )
            return redirect(url_for("settings"))

        db_info = {
            "assets": query_scalar(g.db, "SELECT COUNT(*) FROM assets"),
            "monitoring": query_scalar(g.db, "SELECT COUNT(*) FROM monitoring_records"),
            "tickets": query_scalar(g.db, "SELECT COUNT(*) FROM tickets"),
            "aliases": query_scalar(g.db, "SELECT COUNT(*) FROM asset_aliases"),
        }
        excel_path = app.config["EXCEL_PATH"]
        return render_template("settings.html", db_info=db_info, excel_path=excel_path)

