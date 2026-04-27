from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
from typing import Any

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app
from flask import Flask, flash, g, redirect, render_template, request, send_file, url_for
from openpyxl import load_workbook
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


BASE_DIR = Path(__file__).resolve().parent


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_local_env()

DB_PATH = BASE_DIR / "monitoring_board.db"
DEFAULT_EXCEL_PATH = next(BASE_DIR.glob("*.xlsx"), None)
BACKUP_DIR = BASE_DIR / "backups"
CONTRACTS_DIR = BASE_DIR / "uploads" / "contracts"
INTEGRATION_PROVIDER_FUSIONSOLAR = "FusionSolar"
INTEGRATION_PROVIDER_OPTIONS = [INTEGRATION_PROVIDER_FUSIONSOLAR]
DEFAULT_FUSIONSOLAR_SYNC_HOURS = "08:00,14:00"
DEFAULT_FUSIONSOLAR_LOGIN_ENDPOINT = "/thirdData/login"
DEFAULT_FUSIONSOLAR_STATIONS_ENDPOINT = "/thirdData/stations"
DEFAULT_FUSIONSOLAR_REALTIME_ENDPOINT = "/thirdData/getStationRealKpi"
DEFAULT_FUSIONSOLAR_ALARMS_ENDPOINT = "/thirdData/getAlarmList"
DEFAULT_FUSIONSOLAR_ALARMS_LANGUAGE = "en_US"

STATUS_COLORS = {
    "Erro": "danger",
    "Desconectada": "warning",
    "Resolvido": "success",
    "Operacional": "success",
    "Aberto": "danger",
    "Em analise": "warning",
    "Agendado": "accent",
    "Em visita": "accent",
    "Fechado": "muted",
}

TICKET_STATUSES = ["Aberto", "Em analise", "Agendado", "Em visita", "Resolvido", "Fechado"]
TICKET_URGENCIES = ["Baixa", "Media", "Alta", "Critica"]
MONITORING_SOURCES = ["Solar Fusion", "Sigenergy", "Manual / Outro"]
RENEWAL_STATUSES = ["Por contactar", "Email enviado", "Em negociacao", "Renovado", "Sem interesse"]
INTEGRATION_STATUS_COLORS = {
    "success": "success",
    "error": "danger",
    "warning": "warning",
    "pending": "accent",
}
EXPORT_DATASETS = {
    "assets": {
        "label": "Instalacoes / centrais",
        "columns": [
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("address", "Morada"),
            ("contact_phone", "Contacto"),
            ("contact_name", "Nome"),
            ("access_type", "Acesso"),
            ("coverage_type", "Tipo de cobertura"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("company_name", "Empresa"),
            ("contact_email", "Email"),
        ],
    },
    "monitoring": {
        "label": "Monitorizacao filtrada",
        "columns": [
            ("record_date", "Data"),
            ("imported_at", "Importado em"),
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("status", "Estado"),
            ("notes", "Notas"),
            ("source", "Origem"),
        ],
    },
    "tickets": {
        "label": "Tickets / corretivas",
        "columns": [
            ("project_name", "Central"),
            ("location", "Localizacao"),
            ("contract_type", "Contrato"),
            ("active_contract", "O&M"),
            ("title", "Titulo"),
            ("status", "Estado"),
            ("urgency", "Urgencia"),
            ("installation_ref", "Referencia"),
            ("next_action", "Proxima acao"),
            ("notes", "Notas"),
            ("created_at", "Criado em"),
            ("updated_at", "Atualizado em"),
        ],
    },
}

GROUP_INHERITED_FIELDS = [
    "company_name",
    "location",
    "address",
    "contract_type",
    "contact_name",
    "contact_role",
    "contact_email",
    "contact_phone",
    "access_type",
    "coverage_type",
]

SCHEDULER: BackgroundScheduler | None = None
FUSIONSOLAR_SESSION_CACHE: dict[str, Any] = {}
FUSIONSOLAR_SESSION_LOCK = threading.Lock()
FUSIONSOLAR_SYNC_LOCK = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "monitoring-board-local-secret")
    app.config["DATABASE"] = str(DB_PATH)
    app.config["EXCEL_PATH"] = str(DEFAULT_EXCEL_PATH) if DEFAULT_EXCEL_PATH else ""

    ensure_database(app.config["DATABASE"])
    with closing(get_db(app.config["DATABASE"])) as bootstrap_conn:
        populate_missing_installation_groups(bootstrap_conn)
        populate_missing_group_metadata(bootstrap_conn)
        sync_all_contract_statuses(bootstrap_conn)
        ensure_integration_seed_data(bootstrap_conn)
        bootstrap_conn.commit()
    start_integration_scheduler(app)

    @app.before_request
    def before_request() -> None:
        g.db = get_db(app.config["DATABASE"])

    @app.teardown_request
    def teardown_request(exception: BaseException | None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "today_iso": date.today().isoformat(),
            "ticket_statuses": TICKET_STATUSES,
            "ticket_urgencies": TICKET_URGENCIES,
            "status_colors": STATUS_COLORS,
            "monitoring_sources": MONITORING_SOURCES,
            "renewal_statuses": RENEWAL_STATUSES,
            "integration_status_colors": INTEGRATION_STATUS_COLORS,
            "om_status_label": om_status_label,
            "format_date_pt": format_date_pt,
        }

    @app.route("/")
    def dashboard() -> str:
        stats = fetch_dashboard_stats(g.db)
        monitoring_by_day = query_all(
            g.db,
            """
            SELECT record_date, COUNT(*) AS total
            FROM monitoring_records
            GROUP BY record_date
            ORDER BY record_date DESC
            LIMIT 7
            """,
        )
        critical_assets = query_all(
            g.db,
            """
            SELECT
                a.id,
                a.project_name,
                a.active_contract,
                lm.status,
                lm.record_date,
                COUNT(t.id) AS open_tickets
            FROM assets a
            LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
            LEFT JOIN tickets t ON t.asset_id = a.id AND t.status != 'Fechado'
            WHERE a.active_contract = 'yes'
              AND (lm.status IN ('Erro', 'Desconectada') OR t.id IS NOT NULL)
            GROUP BY a.id, a.project_name, a.active_contract, lm.status, lm.record_date
            ORDER BY
                CASE lm.status WHEN 'Erro' THEN 1 WHEN 'Desconectada' THEN 2 ELSE 3 END,
                open_tickets DESC,
                a.project_name COLLATE NOCASE
            LIMIT 12
            """,
        )
        potential_assets = query_all(
            g.db,
            """
            SELECT
                a.id,
                a.project_name,
                a.active_contract,
                lm.status,
                lm.record_date
            FROM assets a
            LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
            WHERE COALESCE(a.active_contract, '') != 'yes'
              AND lm.status IN ('Erro', 'Desconectada')
            ORDER BY
                CASE lm.status WHEN 'Erro' THEN 1 WHEN 'Desconectada' THEN 2 ELSE 3 END,
                a.project_name COLLATE NOCASE
            LIMIT 10
            """,
        )
        open_ticket_assets = query_all(
            g.db,
            """
            SELECT
                a.id,
                a.project_name,
                a.active_contract,
                COUNT(t.id) AS ticket_count,
                SUM(CASE WHEN t.urgency = 'Critica' THEN 1 ELSE 0 END) AS critical_count,
                MAX(t.updated_at) AS last_update
            FROM assets a
            JOIN tickets t ON t.asset_id = a.id
            WHERE t.status != 'Fechado' AND a.active_contract = 'yes'
            GROUP BY a.id, a.project_name, a.active_contract
            ORDER BY critical_count DESC, ticket_count DESC, a.project_name COLLATE NOCASE
            LIMIT 10
            """,
        )
        renewal_focus = query_all(
            g.db,
            """
            SELECT
                a.id,
                a.project_name,
                a.installation_group,
                a.company_name,
                COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) AS contract_end_date,
                COALESCE(oc.renewal_status, 'Por contactar') AS renewal_status
            FROM assets a
            LEFT JOIN om_contracts oc ON oc.asset_id = a.id
            WHERE (a.maintenance = 'yes' OR oc.id IS NOT NULL)
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) NOT IN ('', '-')
              AND (
                  COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) < ?
                  OR substr(COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')), 1, 4) = ?
              )
            ORDER BY COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) ASC, a.project_name COLLATE NOCASE
            LIMIT 10
            """,
            (date.today().isoformat(), str(date.today().year)),
        )
        return render_template(
            "dashboard.html",
            stats=stats,
            monitoring_by_day=monitoring_by_day,
            critical_assets=critical_assets,
            potential_assets=potential_assets,
            open_ticket_assets=open_ticket_assets,
            renewal_focus=renewal_focus,
        )

    @app.route("/assets", methods=["GET", "POST"])
    def assets() -> str:
        if request.method == "POST":
            project_name = request.form.get("project_name", "").strip()
            installation_group = request.form.get("installation_group", "").strip()
            company_name = request.form.get("company_name", "").strip()
            location = request.form.get("location", "").strip()
            address = request.form.get("address", "").strip()
            kwp = request.form.get("kwp", "").strip()
            contract_type = request.form.get("contract_type", "").strip()
            maintenance = request.form.get("maintenance", "").strip()
            active_contract = request.form.get("active_contract", "").strip()
            start_contract = request.form.get("start_contract", "").strip()
            end_contract = request.form.get("end_contract", "").strip()
            contact_name = request.form.get("contact_name", "").strip()
            contact_email = request.form.get("contact_email", "").strip()
            contact_phone = request.form.get("contact_phone", "").strip()
            notes = request.form.get("notes", "").strip()

            if not project_name:
                flash("O nome da instalacao/central e obrigatorio.", "error")
                return redirect(url_for("assets"))

            existing = query_one("SELECT id FROM assets WHERE project_name = ?", (project_name,))
            if existing is not None:
                flash("Ja existe um asset com esse nome.", "error")
                return redirect(url_for("assets"))

            final_group = installation_group or infer_installation_group(project_name)
            inherited_payload = apply_group_defaults(
                g.db,
                {
                    "company_name": company_name,
                    "location": location,
                    "address": address,
                    "contract_type": contract_type,
                    "contact_name": contact_name,
                    "contact_email": contact_email,
                    "contact_phone": contact_phone,
                },
                final_group,
            )
            company_name = inherited_payload["company_name"]
            location = inherited_payload["location"]
            address = inherited_payload["address"]
            contract_type = inherited_payload["contract_type"]
            contact_name = inherited_payload["contact_name"]
            contact_email = inherited_payload["contact_email"]
            contact_phone = inherited_payload["contact_phone"]
            start_contract = normalize_date_value(start_contract)
            end_contract = normalize_date_value(end_contract)
            active_contract = derive_active_contract(end_contract, active_contract)
            cursor = g.db.execute(
                """
                INSERT INTO assets (
                    project_name, installation_group, company_name, location, address, kwp, contract_type,
                    maintenance, active_contract, start_contract, end_contract, contact_name,
                    contact_email, contact_phone, notes, alias_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_name,
                    final_group,
                    company_name,
                    location,
                    address,
                    kwp,
                    contract_type,
                    maintenance,
                    active_contract,
                    start_contract,
                    end_contract,
                    contact_name,
                    contact_email,
                    contact_phone,
                    notes,
                    project_name,
                ),
            )
            asset_id = int(cursor.lastrowid)
            normalized_name = normalize_name(project_name)
            if normalized_name:
                g.db.execute(
                    "INSERT OR IGNORE INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
                    (asset_id, project_name, normalized_name, "manual-create"),
                )
            g.db.commit()
            rebuild_asset_alias_blob(g.db, asset_id)
            flash("Instalacao criada com sucesso.", "success")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        search = request.args.get("search", "").strip()
        contract_filter = request.args.get("contract_type", "").strip()
        om_filter = request.args.get("active_contract", "").strip()

        conditions = []
        params: list[Any] = []
        if search:
            conditions.append(
                "(a.project_name LIKE ? OR a.company_name LIKE ? OR a.location LIKE ? OR a.alias_blob LIKE ?)"
            )
            wildcard = f"%{search}%"
            params.extend([wildcard, wildcard, wildcard, wildcard])
        if contract_filter:
            conditions.append("a.contract_type = ?")
            params.append(contract_filter)
        if om_filter:
            conditions.append("a.active_contract = ?")
            params.append(om_filter)

        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        assets_rows = query_all(
            g.db,
            f"""
            SELECT
                a.*,
                lm.status AS latest_status,
                lm.record_date AS latest_status_date,
                (
                    SELECT COUNT(*)
                    FROM tickets t
                    WHERE t.asset_id = a.id AND t.status != 'Fechado'
                ) AS open_tickets
            FROM assets a
            LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
            {where_sql}
            ORDER BY a.installation_group COLLATE NOCASE, a.project_name COLLATE NOCASE
            """,
            params,
        )
        contract_types = [
            row["contract_type"]
            for row in query_all(
                g.db,
                "SELECT DISTINCT contract_type FROM assets WHERE contract_type != '' ORDER BY contract_type",
            )
        ]
        return render_template(
            "assets.html",
            assets=assets_rows,
            contract_types=contract_types,
            search=search,
            contract_filter=contract_filter,
            om_filter=om_filter,
        )

    @app.route("/asset/<int:asset_id>")
    def asset_detail(asset_id: int) -> str:
        asset = query_one("SELECT * FROM assets WHERE id = ?", (asset_id,))
        if asset is None:
            flash("Asset nao encontrado.", "error")
            return redirect(url_for("assets"))

        om_contract = query_one(
            """
            SELECT *
            FROM om_contracts
            WHERE asset_id = ?
            """,
            (asset_id,),
        )

        monitoring_history = query_all(
            g.db,
            """
            SELECT id, record_date, status, notes, source
            FROM monitoring_records
            WHERE asset_id = ?
            ORDER BY record_date DESC, id DESC
            LIMIT 100
            """,
            (asset_id,),
        )
        tickets = query_all(
            g.db,
            """
            SELECT *
            FROM tickets
            WHERE asset_id = ?
            ORDER BY updated_at DESC, created_at DESC
            """,
            (asset_id,),
        )
        aliases = query_all(
            g.db,
            """
            SELECT id, alias_name
            FROM asset_aliases
            WHERE asset_id = ?
            ORDER BY alias_name COLLATE NOCASE
            """,
            (asset_id,),
        )
        visits_by_ticket = build_visits_by_ticket(
            query_all(
                g.db,
                """
                SELECT *
                FROM ticket_visits
                WHERE ticket_id IN (
                    SELECT id FROM tickets WHERE asset_id = ?
                )
                ORDER BY visit_date DESC, id DESC
                """,
                (asset_id,),
            )
        )
        return render_template(
            "asset_detail.html",
            asset=asset,
            om_contract=om_contract,
            monitoring_history=monitoring_history,
            tickets=tickets,
            aliases=aliases,
            visits_by_ticket=visits_by_ticket,
        )

    @app.route("/asset/<int:asset_id>/installation-group", methods=["POST"])
    def update_asset_installation_group(asset_id: int):
        asset = query_one("SELECT id, project_name FROM assets WHERE id = ?", (asset_id,))
        if asset is None:
            flash("Asset nao encontrado.", "error")
            return redirect(url_for("assets"))

        group_name = request.form.get("installation_group", "").strip()
        if not group_name:
            group_name = infer_installation_group(asset["project_name"])

        g.db.execute("UPDATE assets SET installation_group = ? WHERE id = ?", (group_name, asset_id))
        apply_group_defaults_to_asset(g.db, asset_id, group_name)
        g.db.commit()
        flash("Grupo de instalacao atualizado.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/asset/<int:asset_id>/update", methods=["POST"])
    def update_asset(asset_id: int):
        asset = query_one("SELECT id FROM assets WHERE id = ?", (asset_id,))
        if asset is None:
            flash("Asset nao encontrado.", "error")
            return redirect(url_for("assets"))

        payload = {
            "project_name": request.form.get("project_name", "").strip(),
            "installation_group": request.form.get("installation_group", "").strip(),
            "company_name": request.form.get("company_name", "").strip(),
            "location": request.form.get("location", "").strip(),
            "address": request.form.get("address", "").strip(),
            "contract_type": request.form.get("contract_type", "").strip(),
            "maintenance": request.form.get("maintenance", "").strip(),
            "active_contract": request.form.get("active_contract", "").strip(),
            "start_contract": request.form.get("start_contract", "").strip(),
            "end_contract": request.form.get("end_contract", "").strip(),
            "contact_name": request.form.get("contact_name", "").strip(),
            "contact_email": request.form.get("contact_email", "").strip(),
            "contact_phone": request.form.get("contact_phone", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
        if not payload["project_name"]:
            flash("O nome da central e obrigatorio.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        if not payload["installation_group"]:
            payload["installation_group"] = infer_installation_group(payload["project_name"])
        payload = apply_group_defaults(g.db, payload, payload["installation_group"], exclude_asset_id=asset_id)
        payload["start_contract"] = normalize_date_value(payload["start_contract"])
        payload["end_contract"] = normalize_date_value(payload["end_contract"])
        payload["active_contract"] = derive_active_contract(payload["end_contract"], payload["active_contract"])

        g.db.execute(
            """
            UPDATE assets
            SET project_name = ?, installation_group = ?, company_name = ?, location = ?, address = ?,
                contract_type = ?, maintenance = ?, active_contract = ?, start_contract = ?, end_contract = ?,
                contact_name = ?, contact_email = ?, contact_phone = ?, notes = ?
            WHERE id = ?
            """,
            (
                payload["project_name"],
                payload["installation_group"],
                payload["company_name"],
                payload["location"],
                payload["address"],
                payload["contract_type"],
                payload["maintenance"],
                payload["active_contract"],
                payload["start_contract"],
                payload["end_contract"],
                payload["contact_name"],
                payload["contact_email"],
                payload["contact_phone"],
                payload["notes"],
                asset_id,
            ),
        )
        g.db.commit()
        flash("Asset atualizado.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/asset/<int:asset_id>/delete", methods=["POST"])
    def delete_asset(asset_id: int):
        asset = query_one("SELECT id, project_name FROM assets WHERE id = ?", (asset_id,))
        if asset is None:
            flash("Asset nao encontrado.", "error")
            return redirect(url_for("assets"))

        contract = query_one("SELECT pdf_path FROM om_contracts WHERE asset_id = ?", (asset_id,))
        if contract and contract["pdf_path"]:
            contract_path = BASE_DIR / contract["pdf_path"]
            if contract_path.exists():
                contract_path.unlink()

        g.db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        g.db.commit()
        flash(f"Asset '{asset['project_name']}' apagado.", "success")
        return redirect(url_for("assets"))

    @app.route("/asset/<int:asset_id>/contract", methods=["POST"])
    def update_asset_contract(asset_id: int):
        asset = query_one("SELECT id, project_name FROM assets WHERE id = ?", (asset_id,))
        if asset is None:
            flash("Asset nao encontrado.", "error")
            return redirect(url_for("assets"))

        start_date = normalize_date_value(request.form.get("contract_start_date", "").strip())
        end_date = normalize_date_value(request.form.get("contract_end_date", "").strip())
        annual_value_raw = request.form.get("annual_value", "").strip()
        contract_notes = request.form.get("contract_notes", "").strip()
        uploaded_file = request.files.get("contract_pdf")

        annual_value = None
        if annual_value_raw:
            normalized_value = annual_value_raw.replace(" ", "").replace(",", ".")
            try:
                annual_value = float(normalized_value)
            except ValueError:
                flash("O valor anual do contrato nao e valido.", "error")
                return redirect(url_for("asset_detail", asset_id=asset_id))

        existing_contract = query_one(
            """
            SELECT *
            FROM om_contracts
            WHERE asset_id = ?
            """,
            (asset_id,),
        )

        stored_path = existing_contract["pdf_path"] if existing_contract else ""
        original_filename = existing_contract["original_filename"] if existing_contract else ""
        if uploaded_file and uploaded_file.filename:
            suffix = Path(uploaded_file.filename).suffix.lower()
            if suffix != ".pdf":
                flash("O contrato tem de ser um ficheiro PDF.", "error")
                return redirect(url_for("asset_detail", asset_id=asset_id))
            CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
            safe_stem = normalize_name(asset["project_name"]).replace(" ", "-") or f"asset-{asset_id}"
            filename = f"{asset_id}_{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            target_path = CONTRACTS_DIR / filename
            uploaded_file.save(target_path)
            if stored_path:
                old_path = BASE_DIR / stored_path
                if old_path.exists() and old_path != target_path:
                    old_path.unlink()
            stored_path = str(target_path.relative_to(BASE_DIR))
            original_filename = uploaded_file.filename

        if existing_contract:
            g.db.execute(
                """
                UPDATE om_contracts
                SET contract_start_date = ?, contract_end_date = ?, annual_value = ?, notes = ?,
                    pdf_path = ?, original_filename = ?, updated_at = ?
                WHERE asset_id = ?
                """,
                (
                    start_date,
                    end_date,
                    annual_value,
                    contract_notes,
                    stored_path,
                    original_filename,
                    datetime.now().isoformat(timespec="seconds"),
                    asset_id,
                ),
            )
        else:
            g.db.execute(
                """
                INSERT INTO om_contracts (
                    asset_id, contract_start_date, contract_end_date, annual_value, notes, pdf_path,
                    original_filename, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    start_date,
                    end_date,
                    annual_value,
                    contract_notes,
                    stored_path,
                    original_filename,
                    datetime.now().isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        sync_asset_contract_status(g.db, asset_id, start_date, end_date)
        g.db.commit()
        flash("Contrato O&M atualizado.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/asset/<int:asset_id>/contract/open")
    def open_asset_contract(asset_id: int):
        contract = query_one(
            """
            SELECT pdf_path
            FROM om_contracts
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
        if contract is None or not contract["pdf_path"]:
            flash("Esta central ainda nao tem contrato associado.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        contract_path = BASE_DIR / contract["pdf_path"]
        if not contract_path.exists():
            flash("O ficheiro do contrato nao foi encontrado no projeto.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        return send_file(contract_path, mimetype="application/pdf", as_attachment=False)

    @app.route("/asset/<int:asset_id>/alias", methods=["POST"])
    def add_alias(asset_id: int):
        alias_name = request.form.get("alias_name", "").strip()
        if not alias_name:
            flash("Indica um nome alternativo para guardar o alias.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        normalized = normalize_name(alias_name)
        if not normalized:
            flash("O alias indicado nao e valido.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        existing = query_one("SELECT id FROM asset_aliases WHERE normalized_alias = ?", (normalized,))
        if existing:
            flash("Esse alias ja esta associado a outra instalacao.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        g.db.execute(
            "INSERT INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
            (asset_id, alias_name, normalized, "manual"),
        )
        g.db.commit()
        rebuild_asset_alias_blob(g.db, asset_id)
        flash("Alias guardado com sucesso.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/asset/<int:asset_id>/alias/<int:alias_id>/update", methods=["POST"])
    def update_alias(asset_id: int, alias_id: int):
        alias_name = request.form.get("alias_name", "").strip()
        if not alias_name:
            flash("Indica um nome valido para o alias.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        alias_row = query_one(
            "SELECT * FROM asset_aliases WHERE id = ? AND asset_id = ?",
            (alias_id, asset_id),
        )
        if alias_row is None:
            flash("Alias nao encontrado.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        normalized = normalize_name(alias_name)
        existing = query_one("SELECT id, asset_id FROM asset_aliases WHERE normalized_alias = ?", (normalized,))
        if existing and (existing["id"] != alias_id or existing["asset_id"] != asset_id):
            flash("Esse alias ja esta associado a outra instalacao.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        g.db.execute(
            """
            UPDATE asset_aliases
            SET alias_name = ?, normalized_alias = ?, source = ?
            WHERE id = ? AND asset_id = ?
            """,
            (alias_name, normalized, "manual-edit", alias_id, asset_id),
        )
        g.db.commit()
        rebuild_asset_alias_blob(g.db, asset_id)
        flash("Alias atualizado.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/asset/<int:asset_id>/alias/<int:alias_id>/delete", methods=["POST"])
    def delete_alias(asset_id: int, alias_id: int):
        alias_row = query_one(
            "SELECT * FROM asset_aliases WHERE id = ? AND asset_id = ?",
            (alias_id, asset_id),
        )
        if alias_row is None:
            flash("Alias nao encontrado.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))

        g.db.execute("DELETE FROM asset_aliases WHERE id = ? AND asset_id = ?", (alias_id, asset_id))
        g.db.commit()
        rebuild_asset_alias_blob(g.db, asset_id)
        flash("Alias apagado.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    @app.route("/monitoring", methods=["GET", "POST"])
    def monitoring() -> str:
        if request.method == "POST":
            record_date = request.form.get("record_date", date.today().isoformat())
            pasted_table = request.form.get("pasted_table", "")
            default_notes = request.form.get("default_notes", "").strip()
            platform_source = request.form.get("platform_source", "Manual / Outro").strip() or "Manual / Outro"
            import_scope = request.form.get("import_scope", "complete").strip() or "complete"
            result = import_daily_monitoring(
                g.db,
                pasted_table,
                record_date,
                default_notes,
                platform_source,
                import_scope,
            )
            flash(
                f"Importacao concluida: {result.imported} registos, {result.matched} associados automaticamente, {result.unmatched} por mapear, {result.auto_resolved} resolvidos automaticamente.",
                "success" if result.imported else "warning",
            )
            if result.batch_id is not None:
                return redirect(url_for("monitoring", batch_id=result.batch_id))
            return redirect(url_for("monitoring"))

        search = request.args.get("search", "").strip()
        asset_filter = request.args.get("asset_id", "").strip()
        status_filter = request.args.get("status", "").strip()
        issue_only = request.args.get("issue_only", "no").strip()
        start_date = request.args.get("start_date", "").strip()
        end_date = request.args.get("end_date", "").strip()
        om_only = request.args.get("om_only", "yes").strip()
        batch_id = request.args.get("batch_id", "").strip()

        latest_conditions = []
        latest_params: list[Any] = []
        if search:
            wildcard = f"%{search}%"
            latest_conditions.append("(a.project_name LIKE ? OR a.alias_blob LIKE ? OR a.company_name LIKE ?)")
            latest_params.extend([wildcard, wildcard, wildcard])
        if asset_filter:
            latest_conditions.append("a.id = ?")
            latest_params.append(asset_filter)
        if status_filter:
            latest_conditions.append("lm.status = ?")
            latest_params.append(status_filter)
        elif issue_only == "yes":
            latest_conditions.append("lm.status IN ('Erro', 'Desconectada')")
        if om_only == "yes":
            latest_conditions.append("a.active_contract = 'yes'")
        if start_date:
            latest_conditions.append("lm.record_date >= ?")
            latest_params.append(start_date)
        if end_date:
            latest_conditions.append("lm.record_date <= ?")
            latest_params.append(end_date)

        latest_where_sql = f"WHERE {' AND '.join(latest_conditions)}" if latest_conditions else ""
        latest_rows = query_all(
            g.db,
            f"""
            SELECT
                a.id AS asset_id,
                a.project_name,
                a.installation_group,
                a.location,
                a.contract_type,
                a.active_contract,
                lm.status,
                lm.record_date,
                lm.notes,
                (
                    SELECT COUNT(*)
                    FROM monitoring_records mr
                    WHERE mr.asset_id = a.id
                ) AS history_count
            FROM latest_monitoring_view lm
            JOIN assets a ON a.id = lm.asset_id
            {latest_where_sql}
            ORDER BY
                CASE a.active_contract WHEN 'yes' THEN 1 ELSE 2 END,
                CASE lm.status
                    WHEN 'Erro' THEN 1
                    WHEN 'Desconectada' THEN 2
                    ELSE 3
                END,
                a.project_name COLLATE NOCASE
            """,
            latest_params,
        )

        filter_sql = []
        params: list[Any] = []
        if search:
            wildcard = f"%{search}%"
            filter_sql.append("(a.project_name LIKE ? OR a.alias_blob LIKE ? OR a.company_name LIKE ?)")
            params.extend([wildcard, wildcard, wildcard])
        if asset_filter:
            filter_sql.append("a.id = ?")
            params.append(asset_filter)
        if status_filter:
            filter_sql.append("mr.status = ?")
            params.append(status_filter)
        elif issue_only == "yes":
            filter_sql.append("mr.status IN ('Erro', 'Desconectada')")
        if om_only == "yes":
            filter_sql.append("a.active_contract = 'yes'")
        if start_date:
            filter_sql.append("mr.record_date >= ?")
            params.append(start_date)
        if end_date:
            filter_sql.append("mr.record_date <= ?")
            params.append(end_date)

        where_sql = f"WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        history_rows = query_all(
            g.db,
            f"""
            SELECT
                mr.id,
                mr.record_date,
                mr.status,
                mr.notes,
                mr.source,
                mr.batch_id,
                a.id AS asset_id,
                a.project_name,
                a.installation_group,
                a.location,
                a.contract_type,
                a.active_contract,
                mib.imported_at
            FROM monitoring_records mr
            JOIN assets a ON a.id = mr.asset_id
            LEFT JOIN monitoring_import_batches mib ON mib.id = mr.batch_id
            {where_sql}
            ORDER BY mr.record_date DESC, a.project_name COLLATE NOCASE, mr.id DESC
            LIMIT 250
            """,
            params,
        )

        selected_asset = None
        asset_history = []
        asset_problem_periods = []
        selected_group_assets = []
        if asset_filter:
            selected_asset = query_one(
                """
                SELECT
                    a.*,
                    lm.status AS latest_status,
                    lm.record_date AS latest_status_date
                FROM assets a
                LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
                WHERE a.id = ?
                """,
                (asset_filter,),
            )
            asset_history = query_all(
                g.db,
                """
                SELECT mr.id, mr.record_date, mr.status, mr.notes, mr.source, mr.batch_id, mib.imported_at
                FROM monitoring_records mr
                LEFT JOIN monitoring_import_batches mib ON mib.id = mr.batch_id
                WHERE mr.asset_id = ?
                ORDER BY mr.record_date DESC, mr.id DESC
                LIMIT 200
                """,
                (asset_filter,),
            )
            asset_problem_periods = build_problem_periods(g.db, int(asset_filter))
            selected_group_assets = query_all(
                g.db,
                """
                SELECT
                    a.id,
                    a.project_name,
                    a.installation_group,
                    lm.status,
                    lm.record_date
                FROM assets a
                LEFT JOIN latest_monitoring_view lm ON lm.asset_id = a.id
                WHERE a.installation_group = ?
                ORDER BY a.project_name COLLATE NOCASE
                """,
                (selected_asset["installation_group"] or selected_asset["project_name"],),
            )

        unresolved = query_all(
            g.db,
            """
            SELECT mu.*, mib.imported_at
            FROM monitoring_unmatched mu
            LEFT JOIN monitoring_import_batches mib ON mib.id = mu.batch_id
            ORDER BY record_date DESC, original_name COLLATE NOCASE
            LIMIT 50
            """
        )
        import_batches = query_all(
            g.db,
            """
            SELECT
                mib.*,
                (
                    SELECT COUNT(*)
                    FROM monitoring_records mr
                    WHERE mr.batch_id = mib.id
                ) AS resolved_rows
            FROM monitoring_import_batches mib
            ORDER BY mib.imported_at DESC, mib.id DESC
            LIMIT 30
            """
        )
        assets_for_mapping = query_all(
            g.db,
            "SELECT id, project_name FROM assets ORDER BY project_name COLLATE NOCASE",
        )
        monitoring_statuses = [
            row["status"]
            for row in query_all(
                g.db,
                "SELECT DISTINCT status FROM monitoring_records ORDER BY status",
            )
        ]
        monitoring_stats = {
            "history_records": len(history_rows),
            "centrals_in_filter": len(latest_rows),
            "installations_in_filter": len(group_latest_rows_by_installation(latest_rows)),
            "current_errors": sum(1 for row in latest_rows if row["status"] == "Erro"),
            "current_disconnected": sum(1 for row in latest_rows if row["status"] == "Desconectada"),
            "current_active_om": sum(1 for row in latest_rows if row["active_contract"] == "yes"),
        }
        grouped_latest_rows = group_latest_rows_by_installation(latest_rows)
        batch_insight = build_batch_insight(g.db, int(batch_id)) if batch_id else None

        return render_template(
            "monitoring.html",
            latest_rows=latest_rows,
            grouped_latest_rows=grouped_latest_rows,
            unresolved=unresolved,
            assets_for_mapping=assets_for_mapping,
            monitoring_statuses=monitoring_statuses,
            monitoring_stats=monitoring_stats,
            history_rows=history_rows,
            selected_asset=selected_asset,
            selected_group_assets=selected_group_assets,
            asset_history=asset_history,
            asset_problem_periods=asset_problem_periods,
            import_batches=import_batches,
            search=search,
            asset_filter=asset_filter,
            status_filter=status_filter,
            issue_only=issue_only,
            start_date=start_date,
            end_date=end_date,
            om_only=om_only,
            batch_insight=batch_insight,
        )

    @app.route("/monitoring/record/<int:record_id>/update", methods=["POST"])
    def update_monitoring_record(record_id: int):
        record = query_one("SELECT asset_id FROM monitoring_records WHERE id = ?", (record_id,))
        if record is None:
            flash("Registo de monitorizacao nao encontrado.", "error")
            return redirect(url_for("monitoring"))

        record_date = request.form.get("record_date", "").strip()
        status = normalize_status(request.form.get("status", "").strip())
        notes = request.form.get("notes", "").strip()
        if not record_date or not status:
            flash("Data e estado sao obrigatorios para atualizar o registo.", "error")
            return redirect(url_for("monitoring", asset_id=record["asset_id"]))

        g.db.execute(
            """
            UPDATE monitoring_records
            SET record_date = ?, status = ?, notes = ?
            WHERE id = ?
            """,
            (record_date, status, notes, record_id),
        )
        g.db.commit()
        flash("Registo de monitorizacao atualizado.", "success")
        return redirect(url_for("monitoring", asset_id=record["asset_id"]))

    @app.route("/monitoring/record/<int:record_id>/delete", methods=["POST"])
    def delete_monitoring_record(record_id: int):
        record = query_one("SELECT asset_id FROM monitoring_records WHERE id = ?", (record_id,))
        if record is None:
            flash("Registo de monitorizacao nao encontrado.", "error")
            return redirect(url_for("monitoring"))

        g.db.execute("DELETE FROM monitoring_records WHERE id = ?", (record_id,))
        g.db.commit()
        flash("Registo de monitorizacao apagado.", "success")
        return redirect(url_for("monitoring", asset_id=record["asset_id"]))

    @app.route("/monitoring/unmatched/<int:row_id>/resolve", methods=["POST"])
    def resolve_unmatched(row_id: int):
        asset_id = int(request.form["asset_id"])
        unmatched = query_one("SELECT * FROM monitoring_unmatched WHERE id = ?", (row_id,))
        if unmatched is None:
            flash("Linha pendente nao encontrada.", "error")
            return redirect(url_for("monitoring"))

        alias_name = unmatched["original_name"]
        normalized = normalize_name(alias_name)
        existing = query_one("SELECT id FROM asset_aliases WHERE normalized_alias = ?", (normalized,))
        if existing and existing["id"]:
            flash("Esse nome ja esta usado como alias.", "error")
            return redirect(url_for("monitoring"))

        g.db.execute(
            "INSERT INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
            (asset_id, alias_name, normalized, "resolved"),
        )
        g.db.execute(
            """
            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                unmatched["status"],
                unmatched["record_date"],
                unmatched["notes"],
                "resolved-unmatched",
                unmatched["batch_id"],
            ),
        )
        g.db.execute("DELETE FROM monitoring_unmatched WHERE id = ?", (row_id,))
        g.db.commit()
        rebuild_asset_alias_blob(g.db, asset_id)
        flash("Linha associada e importada com sucesso.", "success")
        return redirect(url_for("monitoring", asset_id=asset_id))

    @app.route("/tickets", methods=["GET", "POST"])
    def tickets() -> str:
        if request.method == "POST":
            asset_id = int(request.form["asset_id"])
            title = request.form.get("title", "").strip()
            urgency = request.form.get("urgency", "Media")
            status = request.form.get("status", "Aberto")
            installation_ref = request.form.get("installation_ref", "").strip()
            notes = request.form.get("notes", "").strip()
            next_action = request.form.get("next_action", "").strip()
            if not title:
                flash("O ticket precisa de um titulo.", "error")
                return redirect(url_for("tickets"))

            g.db.execute(
                """
                INSERT INTO tickets (
                    asset_id, title, urgency, status, installation_ref, notes, next_action, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    title,
                    urgency,
                    status,
                    installation_ref,
                    notes,
                    next_action,
                    datetime.now().isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            g.db.commit()
            flash("Ticket criado.", "success")
            return redirect(url_for("tickets", asset_id=asset_id))

        search = request.args.get("search", "").strip()
        asset_filter = request.args.get("asset_id", "").strip()
        status_filter = request.args.get("status", "").strip()
        urgency_filter = request.args.get("urgency", "").strip()
        scope = request.args.get("scope", "").strip()
        om_only = request.args.get("om_only", "yes").strip()

        conditions = []
        params: list[Any] = []
        if search:
            wildcard = f"%{search}%"
            conditions.append(
                "(a.project_name LIKE ? OR a.alias_blob LIKE ? OR t.title LIKE ? OR COALESCE(t.notes, '') LIKE ?)"
            )
            params.extend([wildcard, wildcard, wildcard, wildcard])
        if asset_filter:
            conditions.append("a.id = ?")
            params.append(asset_filter)
        if status_filter:
            conditions.append("t.status = ?")
            params.append(status_filter)
        if urgency_filter:
            conditions.append("t.urgency = ?")
            params.append(urgency_filter)
        if scope == "open":
            conditions.append("t.status != 'Fechado'")
        if om_only == "yes":
            conditions.append("a.active_contract = 'yes'")

        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        ticket_rows = query_all(
            g.db,
            f"""
            SELECT
                t.*,
                a.project_name,
                a.location,
                a.active_contract,
                a.contract_type
            FROM tickets t
            JOIN assets a ON a.id = t.asset_id
            {where_sql}
            ORDER BY
                CASE a.active_contract WHEN 'yes' THEN 1 ELSE 2 END,
                a.project_name COLLATE NOCASE,
                CASE t.status
                    WHEN 'Aberto' THEN 1
                    WHEN 'Em analise' THEN 2
                    WHEN 'Agendado' THEN 3
                    WHEN 'Em visita' THEN 4
                    WHEN 'Resolvido' THEN 5
                    ELSE 6
                END,
                CASE t.urgency
                    WHEN 'Critica' THEN 1
                    WHEN 'Alta' THEN 2
                    WHEN 'Media' THEN 3
                    ELSE 4
                END,
                t.updated_at DESC,
                t.id DESC
            """,
            params,
        )

        assets_rows = query_all(
            g.db,
            "SELECT id, project_name FROM assets ORDER BY project_name COLLATE NOCASE",
        )
        visits_by_ticket = build_visits_by_ticket(
            query_all(
                g.db,
                """
                SELECT *
                FROM ticket_visits
                ORDER BY visit_date DESC, id DESC
                """,
            )
        )
        grouped_tickets = group_tickets_by_asset(ticket_rows)

        selected_asset = None
        central_history = []
        central_summary = None
        if asset_filter:
            selected_asset = query_one("SELECT * FROM assets WHERE id = ?", (asset_filter,))
            central_history = query_all(
                g.db,
                """
                SELECT
                    t.*,
                    (
                        SELECT COUNT(*)
                        FROM ticket_visits tv
                        WHERE tv.ticket_id = t.id
                    ) AS visit_count
                FROM tickets t
                WHERE t.asset_id = ?
                ORDER BY t.updated_at DESC, t.id DESC
                """,
                (asset_filter,),
            )
            central_summary = {
                "total": len(central_history),
                "open": sum(1 for ticket in central_history if ticket["status"] != "Fechado"),
                "critical": sum(1 for ticket in central_history if ticket["urgency"] == "Critica" and ticket["status"] != "Fechado"),
                "visits": sum(ticket["visit_count"] for ticket in central_history),
            }

        ticket_stats = {
            "centrals": len(grouped_tickets),
            "tickets": len(ticket_rows),
            "open": sum(1 for ticket in ticket_rows if ticket["status"] != "Fechado"),
            "critical": sum(1 for ticket in ticket_rows if ticket["urgency"] == "Critica" and ticket["status"] != "Fechado"),
        }

        return render_template(
            "tickets.html",
            tickets=ticket_rows,
            grouped_tickets=grouped_tickets,
            assets=assets_rows,
            visits_by_ticket=visits_by_ticket,
            selected_asset=selected_asset,
            central_history=central_history,
            central_summary=central_summary,
            ticket_stats=ticket_stats,
            search=search,
            asset_filter=asset_filter,
            status_filter=status_filter,
            urgency_filter=urgency_filter,
            scope=scope,
            om_only=om_only,
        )

    @app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
    def update_ticket(ticket_id: int):
        ticket = query_one("SELECT asset_id FROM tickets WHERE id = ?", (ticket_id,))
        status = request.form.get("status", "Aberto")
        urgency = request.form.get("urgency", "Media")
        next_action = request.form.get("next_action", "").strip()
        notes = request.form.get("notes", "").strip()
        g.db.execute(
            """
            UPDATE tickets
            SET status = ?, urgency = ?, next_action = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, urgency, next_action, notes, datetime.now().isoformat(timespec="seconds"), ticket_id),
        )
        g.db.commit()
        flash("Ticket atualizado.", "success")
        if ticket:
            return redirect(url_for("tickets", asset_id=ticket["asset_id"]))
        return redirect(url_for("tickets"))

    @app.route("/tickets/<int:ticket_id>/visit", methods=["POST"])
    def add_visit(ticket_id: int):
        ticket = query_one("SELECT asset_id FROM tickets WHERE id = ?", (ticket_id,))
        visit_date = request.form.get("visit_date", date.today().isoformat())
        technician = request.form.get("technician", "").strip()
        result = request.form.get("result", "").strip()
        notes = request.form.get("notes", "").strip()
        next_action = request.form.get("next_action", "").strip()

        g.db.execute(
            """
            INSERT INTO ticket_visits (ticket_id, visit_date, technician, result, notes, next_action)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, visit_date, technician, result, notes, next_action),
        )
        if next_action:
            g.db.execute(
                "UPDATE tickets SET next_action = ?, updated_at = ? WHERE id = ?",
                (next_action, datetime.now().isoformat(timespec="seconds"), ticket_id),
            )
        g.db.commit()
        flash("Visita registada.", "success")
        if ticket:
            return redirect(url_for("tickets", asset_id=ticket["asset_id"]))
        return redirect(url_for("tickets"))

    @app.route("/tickets/<int:ticket_id>/delete", methods=["POST"])
    def delete_ticket(ticket_id: int):
        ticket = query_one("SELECT asset_id FROM tickets WHERE id = ?", (ticket_id,))
        if ticket is None:
            flash("Ticket nao encontrado.", "error")
            return redirect(url_for("tickets"))

        g.db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        g.db.commit()
        flash("Ticket apagado.", "success")
        return redirect(url_for("tickets", asset_id=ticket["asset_id"]))

    @app.route("/exports", methods=["GET", "POST"])
    def exports() -> str:
        selected_dataset = request.values.get("dataset", "monitoring").strip()
        if selected_dataset not in EXPORT_DATASETS:
            selected_dataset = "monitoring"

        if request.method == "POST":
            action = request.form.get("action", "download")
            export_format = request.form.get("export_format", "xlsx").strip().lower()
            columns = request.form.getlist("columns") or [column[0] for column in EXPORT_DATASETS[selected_dataset]["columns"]]
            filters = extract_export_filters(request.form, selected_dataset)

            if action == "save_template":
                template_name = request.form.get("template_name", "").strip()
                if not template_name:
                    flash("Indica um nome para o template.", "error")
                    return redirect(url_for("exports", dataset=selected_dataset))
                g.db.execute(
                    """
                    INSERT INTO export_templates (name, dataset, export_format, columns_json, filters_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template_name,
                        selected_dataset,
                        export_format,
                        json.dumps(columns, ensure_ascii=True),
                        json.dumps(filters, ensure_ascii=True),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                g.db.commit()
                flash("Template de exportacao guardado.", "success")
                return redirect(url_for("exports", dataset=selected_dataset))

            if action == "download":
                rows, headers = build_export_dataset(g.db, selected_dataset, filters, columns)
                filename = f"{selected_dataset}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                return export_rows_file(rows, headers, filename, export_format)

        template_id = request.args.get("template_id", "").strip()
        active_template = None
        template_filters = {}
        template_columns = [column[0] for column in EXPORT_DATASETS[selected_dataset]["columns"]]
        template_format = "xlsx"
        if template_id:
            active_template = query_one("SELECT * FROM export_templates WHERE id = ?", (template_id,))
            if active_template:
                selected_dataset = active_template["dataset"]
                template_filters = json.loads(active_template["filters_json"])
                template_columns = json.loads(active_template["columns_json"])
                template_format = active_template["export_format"]

        templates_rows = query_all(
            g.db,
            "SELECT * FROM export_templates ORDER BY created_at DESC, id DESC",
        )
        preview_filters = template_filters or extract_export_filters(request.args, selected_dataset, for_query=True)
        preview_columns = template_columns if active_template else request.args.getlist("columns") or [column[0] for column in EXPORT_DATASETS[selected_dataset]["columns"]]
        preview_rows, preview_headers = build_export_dataset(g.db, selected_dataset, preview_filters, preview_columns, limit=20)

        return render_template(
            "exports.html",
            datasets=EXPORT_DATASETS,
            selected_dataset=selected_dataset,
            templates_rows=templates_rows,
            active_template=active_template,
            preview_rows=preview_rows,
            preview_headers=preview_headers,
            selected_columns=preview_columns,
            template_filters=template_filters,
            template_format=template_format,
            assets_for_mapping=query_all(g.db, "SELECT id, project_name FROM assets ORDER BY project_name COLLATE NOCASE"),
        )

    @app.route("/integrations", methods=["GET", "POST"])
    def integrations() -> str:
        provider = INTEGRATION_PROVIDER_FUSIONSOLAR
        if request.method == "POST":
            action = request.form.get("action", "").strip()
            if action == "save_config":
                sync_hours = normalize_sync_hours(request.form.get("sync_hours", DEFAULT_FUSIONSOLAR_SYNC_HOURS))
                auto_sync_enabled = 1 if request.form.get("auto_sync_enabled") == "on" else 0
                enabled = 1 if request.form.get("enabled") == "on" else 0
                g.db.execute(
                    """
                    UPDATE integration_configs
                    SET username = ?, password = ?, base_url = ?, login_endpoint = ?, plants_endpoint = ?,
                        real_time_endpoint = ?, alarms_endpoint = ?, enabled = ?, auto_sync_enabled = ?, sync_hours = ?, updated_at = ?
                    WHERE provider = ?
                    """,
                    (
                        request.form.get("username", "").strip(),
                        request.form.get("password", "").strip(),
                        request.form.get("base_url", "").strip(),
                        request.form.get("login_endpoint", "").strip(),
                        request.form.get("plants_endpoint", "").strip(),
                        request.form.get("real_time_endpoint", "").strip(),
                        request.form.get("alarms_endpoint", "").strip(),
                        enabled,
                        auto_sync_enabled,
                        sync_hours,
                        datetime.now().isoformat(timespec="seconds"),
                        provider,
                    ),
                )
                g.db.commit()
                refresh_integration_scheduler(app)
                flash("Configuracao FusionSolar guardada.", "success")
                return redirect(url_for("integrations"))

            if action == "test_connection":
                try:
                    result = run_fusionsolar_check(g.db, provider, dry_run=True)
                    flash(
                        f"Ligacao FusionSolar validada: {result['station_count']} centrais, {result['realtime_count']} respostas realtime e {result['alarm_count']} alarmes ativos.",
                        "success",
                    )
                except Exception as exc:
                    flash(f"Falha no teste de ligacao FusionSolar: {exc}", "error")
                return redirect(url_for("integrations"))

            if action == "sync_now":
                try:
                    result = run_fusionsolar_sync(g.db, provider, trigger_type="manual")
                    flash(
                        f"Sync FusionSolar concluido: {result['matched']} associados, {result['unresolved']} por resolver, {result['auto_resolved']} resolvidos.",
                        "success",
                    )
                except Exception as exc:
                    flash(f"Falha ao sincronizar FusionSolar: {exc}", "error")
                return redirect(url_for("integrations"))

            if action == "resolve_unresolved":
                unresolved_id = int(request.form["unresolved_id"])
                asset_id = int(request.form["asset_id"])
                resolve_fusionsolar_unresolved(g.db, unresolved_id, asset_id)
                flash("Entrada FusionSolar associada ao asset.", "success")
                return redirect(url_for("integrations"))

            if action == "create_asset_from_unresolved":
                unresolved_id = int(request.form["unresolved_id"])
                asset_id = create_asset_from_unresolved(g.db, unresolved_id)
                flash("Asset criado a partir da entrada FusionSolar por resolver.", "success")
                return redirect(url_for("asset_detail", asset_id=asset_id))

            if action == "ignore_unresolved":
                unresolved_id = int(request.form["unresolved_id"])
                ignore_fusionsolar_unresolved(g.db, unresolved_id)
                flash("Entrada FusionSolar marcada como ignorada.", "success")
                return redirect(url_for("integrations"))

        config = get_integration_config(g.db, provider)
        sync_runs = query_all(
            g.db,
            """
            SELECT *
            FROM integration_sync_runs
            WHERE provider = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 20
            """,
            (provider,),
        )
        unresolved_rows = query_all(
            g.db,
            """
            SELECT *
            FROM integration_unresolved
            WHERE provider = ? AND resolution_status = 'pending'
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (provider,),
        )
        mapped_assets = query_all(
            g.db,
            """
            SELECT ai.*, a.project_name, a.installation_group
            FROM asset_integrations ai
            JOIN assets a ON a.id = ai.asset_id
            WHERE ai.provider = ?
            ORDER BY a.installation_group COLLATE NOCASE, a.project_name COLLATE NOCASE
            """,
            (provider,),
        )
        assets_for_mapping = query_all(g.db, "SELECT id, project_name FROM assets ORDER BY project_name COLLATE NOCASE")
        return render_template(
            "integrations.html",
            provider=provider,
            config=config,
            sync_runs=sync_runs,
            unresolved_rows=unresolved_rows,
            mapped_assets=mapped_assets,
            assets_for_mapping=assets_for_mapping,
        )

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
            backup_path = create_database_backup(Path(app.config["DATABASE"]))
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

    return app


def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database_backup(db_path: Path) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{db_path.stem}_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


def ensure_database(path: str) -> None:
    with closing(get_db(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_number TEXT,
                project_name TEXT NOT NULL,
                installation_group TEXT,
                company_name TEXT,
                nif TEXT,
                address TEXT,
                location TEXT,
                panels TEXT,
                kwp TEXT,
                contract_type TEXT,
                sell_to TEXT,
                duration TEXT,
                start_contract TEXT,
                maintenance TEXT,
                coverage_type TEXT,
                access_type TEXT,
                maintenance_comment TEXT,
                status_detail TEXT,
                contact_name TEXT,
                contact_role TEXT,
                contact_email TEXT,
                contact_phone TEXT,
                end_contract TEXT,
                active_contract TEXT,
                notes TEXT,
                asset_type TEXT,
                source_payload TEXT,
                alias_blob TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS asset_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                alias_name TEXT NOT NULL,
                normalized_alias TEXT NOT NULL UNIQUE,
                source TEXT,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS monitoring_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                record_date TEXT NOT NULL,
                notes TEXT,
                source TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS monitoring_unmatched (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                status TEXT NOT NULL,
                record_date TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS monitoring_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_date TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                source TEXT NOT NULL,
                default_notes TEXT,
                raw_input TEXT,
                imported_count INTEGER DEFAULT 0,
                matched_count INTEGER DEFAULT 0,
                unmatched_count INTEGER DEFAULT 0,
                auto_resolved_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS export_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                dataset TEXT NOT NULL,
                export_format TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                filters_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS om_contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL UNIQUE,
                contract_start_date TEXT,
                contract_end_date TEXT,
                annual_value REAL,
                notes TEXT,
                pdf_path TEXT,
                original_filename TEXT,
                renewal_status TEXT,
                last_contact_date TEXT,
                renewal_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS integration_configs (
                provider TEXT PRIMARY KEY,
                username TEXT,
                password TEXT,
                base_url TEXT,
                login_endpoint TEXT,
                plants_endpoint TEXT,
                real_time_endpoint TEXT,
                alarms_endpoint TEXT,
                enabled INTEGER DEFAULT 0,
                auto_sync_enabled INTEGER DEFAULT 0,
                sync_hours TEXT,
                last_sync_at TEXT,
                last_sync_status TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS asset_integrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                external_id TEXT,
                external_name TEXT,
                enabled INTEGER DEFAULT 1,
                last_sync_at TEXT,
                last_status TEXT,
                last_error TEXT,
                UNIQUE(provider, external_id),
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS integration_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                trigger_type TEXT,
                status TEXT,
                matched_count INTEGER DEFAULT 0,
                unresolved_count INTEGER DEFAULT 0,
                auto_resolved_count INTEGER DEFAULT 0,
                error_message TEXT,
                summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS integration_unresolved (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                sync_run_id INTEGER,
                external_id TEXT,
                external_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                external_status TEXT,
                payload_json TEXT,
                suggested_asset_id INTEGER,
                resolution_status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_notes TEXT,
                FOREIGN KEY (sync_run_id) REFERENCES integration_sync_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (suggested_asset_id) REFERENCES assets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                urgency TEXT NOT NULL,
                status TEXT NOT NULL,
                installation_ref TEXT,
                notes TEXT,
                next_action TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ticket_visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                visit_date TEXT NOT NULL,
                technician TEXT,
                result TEXT,
                notes TEXT,
                next_action TEXT,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
            );

            CREATE VIEW IF NOT EXISTS latest_monitoring_view AS
            SELECT mr.asset_id, mr.status, mr.record_date, mr.notes
            FROM monitoring_records mr
            JOIN (
                SELECT asset_id, MAX(record_date || 'T' || printf('%09d', id)) AS marker
                FROM monitoring_records
                GROUP BY asset_id
            ) latest
              ON latest.asset_id = mr.asset_id
             AND latest.marker = mr.record_date || 'T' || printf('%09d', mr.id);
            """
        )
        ensure_column(conn, "monitoring_records", "batch_id INTEGER")
        ensure_column(conn, "monitoring_unmatched", "batch_id INTEGER")
        ensure_column(conn, "assets", "installation_group TEXT")
        ensure_column(conn, "om_contracts", "renewal_status TEXT")
        ensure_column(conn, "om_contracts", "last_contact_date TEXT")
        ensure_column(conn, "om_contracts", "renewal_notes TEXT")
        ensure_column(conn, "integration_configs", "real_time_endpoint TEXT")
        populate_missing_installation_groups(conn)
        populate_missing_group_metadata(conn)
        ensure_predefined_export_templates(conn)
        conn.commit()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_definition: str) -> None:
    column_name = column_definition.split()[0]
    existing_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in existing_columns:
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def query_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return g.db.execute(sql, params).fetchone()


def query_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def fetch_dashboard_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    latest_status_counts = {
        row["status"]: row["total"]
        for row in query_all(
            conn,
            """
            SELECT lm.status, COUNT(*) AS total
            FROM latest_monitoring_view lm
            GROUP BY lm.status
            """,
        )
    }
    return {
        "assets": query_scalar(conn, "SELECT COUNT(*) FROM assets"),
        "active_om_assets": query_scalar(conn, "SELECT COUNT(*) FROM assets WHERE active_contract = 'yes'"),
        "pipeline_assets": query_scalar(conn, "SELECT COUNT(*) FROM assets WHERE COALESCE(active_contract, '') != 'yes'"),
        "monitoring_today": query_scalar(
            conn,
            "SELECT COUNT(*) FROM monitoring_records WHERE record_date = ?",
            (date.today().isoformat(),),
        ),
        "open_tickets": query_scalar(conn, "SELECT COUNT(*) FROM tickets WHERE status != 'Fechado'"),
        "open_tickets_active_om": query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM tickets t
            JOIN assets a ON a.id = t.asset_id
            WHERE t.status != 'Fechado' AND a.active_contract = 'yes'
            """,
        ),
        "critical_tickets": query_scalar(
            conn,
            "SELECT COUNT(*) FROM tickets WHERE urgency = 'Critica' AND status != 'Fechado'",
        ),
        "critical_active_issues": query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM latest_monitoring_view lm
            JOIN assets a ON a.id = lm.asset_id
            WHERE a.active_contract = 'yes' AND lm.status IN ('Erro', 'Desconectada')
            """,
        ),
        "expired_renewals": query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM assets a
            LEFT JOIN om_contracts oc ON oc.asset_id = a.id
            WHERE (a.maintenance = 'yes' OR oc.id IS NOT NULL)
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) NOT IN ('', '-')
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) < ?
            """,
            (date.today().isoformat(),),
        ),
        "renewals_next_90_days": query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM assets a
            LEFT JOIN om_contracts oc ON oc.asset_id = a.id
            WHERE (a.maintenance = 'yes' OR oc.id IS NOT NULL)
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) NOT IN ('', '-')
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) BETWEEN ? AND ?
            """,
            (date.today().isoformat(), (date.today() + timedelta(days=90)).isoformat()),
        ),
        "renewals_this_year": query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM assets a
            LEFT JOIN om_contracts oc ON oc.asset_id = a.id
            WHERE (a.maintenance = 'yes' OR oc.id IS NOT NULL)
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) NOT IN ('', '-')
              AND COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) BETWEEN ? AND ?
            """,
            (date.today().isoformat(), f"{date.today().year}-12-31"),
        ),
        "status_counts": latest_status_counts,
    }


def normalize_name(value: str) -> str:
    lowered = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    cleaned = "".join(char if char.isalnum() else " " for char in lowered)
    return " ".join(cleaned.split())


def infer_installation_group(project_name: str) -> str:
    name = (project_name or "").strip()
    if not name:
        return ""
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    return stripped or name


def parse_date_value(value: str | None) -> date | None:
    if value in (None, "", "-"):
        return None
    raw_value = str(value).strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw_value, date_format).date()
        except ValueError:
            continue
    return None


def normalize_date_value(value: str | None) -> str:
    parsed = parse_date_value(value)
    return parsed.isoformat() if parsed else (str(value).strip() if value else "")


def derive_active_contract(end_date: str | None, current_value: str = "") -> str:
    parsed_end_date = parse_date_value(end_date)
    if parsed_end_date is None:
        return current_value
    return "yes" if parsed_end_date >= date.today() else "no"


def om_status_label(value: str | None) -> str:
    return "O&M ativo" if value == "yes" else "Sem contrato"


def format_date_pt(value: str | None) -> str:
    parsed = parse_date_value(value)
    return parsed.strftime("%d/%m/%Y") if parsed else (value or "-")


def contract_end_sql() -> str:
    return "COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))"


def apply_group_defaults(
    conn: sqlite3.Connection,
    payload: dict[str, str],
    installation_group: str,
    exclude_asset_id: int | None = None,
) -> dict[str, str]:
    if not installation_group:
        return payload
    available_fields = [field for field in GROUP_INHERITED_FIELDS if field in payload]
    if not available_fields:
        return payload

    conditions = ["installation_group = ?"]
    params: list[Any] = [installation_group]
    if exclude_asset_id is not None:
        conditions.append("id != ?")
        params.append(exclude_asset_id)

    sources = conn.execute(
        f"""
        SELECT {", ".join(available_fields)}
        FROM assets
        WHERE {" AND ".join(conditions)}
          AND ({ " OR ".join(f"NULLIF({field}, '') IS NOT NULL" for field in available_fields) })
        ORDER BY id ASC
        """,
        params,
    ).fetchall()

    for source in sources:
        for field in available_fields:
            if not payload.get(field) and source[field]:
                payload[field] = source[field]
        if all(payload.get(field) for field in available_fields):
            break
    return payload


def apply_group_defaults_to_asset(conn: sqlite3.Connection, asset_id: int, installation_group: str) -> None:
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return
    payload = {field: asset[field] or "" for field in GROUP_INHERITED_FIELDS}
    updated_payload = apply_group_defaults(conn, payload, installation_group, exclude_asset_id=asset_id)
    changed_fields = [field for field in GROUP_INHERITED_FIELDS if (asset[field] or "") != updated_payload.get(field, "")]
    if not changed_fields:
        return
    assignments = ", ".join(f"{field} = ?" for field in changed_fields)
    values = [updated_payload[field] for field in changed_fields]
    conn.execute(f"UPDATE assets SET {assignments} WHERE id = ?", values + [asset_id])


def populate_missing_group_metadata(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, installation_group
        FROM assets
        WHERE installation_group IS NOT NULL AND TRIM(installation_group) != ''
        ORDER BY installation_group COLLATE NOCASE, id
        """
    ).fetchall()
    for row in rows:
        apply_group_defaults_to_asset(conn, row["id"], row["installation_group"])


def sync_asset_contract_status(
    conn: sqlite3.Connection,
    asset_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    asset = conn.execute("SELECT active_contract, start_contract, end_contract FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return
    contract = conn.execute(
        "SELECT contract_start_date, contract_end_date FROM om_contracts WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    final_start = normalize_date_value(start_date or (contract["contract_start_date"] if contract else "") or asset["start_contract"])
    final_end = normalize_date_value(end_date or (contract["contract_end_date"] if contract else "") or asset["end_contract"])
    active_contract = derive_active_contract(final_end, asset["active_contract"] or "")
    conn.execute(
        """
        UPDATE assets
        SET maintenance = CASE WHEN ? = 'yes' THEN 'yes' ELSE maintenance END,
            active_contract = ?,
            start_contract = CASE WHEN ? != '' THEN ? ELSE start_contract END,
            end_contract = CASE WHEN ? != '' THEN ? ELSE end_contract END
        WHERE id = ?
        """,
        (active_contract, active_contract, final_start, final_start, final_end, final_end, asset_id),
    )


def sync_all_contract_statuses(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT a.id, a.start_contract, a.end_contract, oc.contract_start_date, oc.contract_end_date
        FROM assets a
        LEFT JOIN om_contracts oc ON oc.asset_id = a.id
        WHERE COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        sync_asset_contract_status(
            conn,
            row["id"],
            row["contract_start_date"] or row["start_contract"],
            row["contract_end_date"] or row["end_contract"],
        )


def populate_missing_installation_groups(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, project_name
        FROM assets
        WHERE installation_group IS NULL OR TRIM(installation_group) = ''
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE assets SET installation_group = ? WHERE id = ?",
            (infer_installation_group(row["project_name"]), row["id"]),
        )


def status_rank(status: str) -> int:
    order = {
        "Erro": 1,
        "Desconectada": 2,
        "Aberto": 3,
        "Em analise": 4,
        "Agendado": 5,
        "Em visita": 6,
        "Resolvido": 7,
        "Operacional": 8,
        "ok": 8,
    }
    return order.get(status or "", 99)


def group_latest_rows_by_installation(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        installation_group = row["installation_group"] or row["project_name"]
        bucket = grouped.setdefault(
            installation_group,
            {
                "installation_group": installation_group,
                "location": row["location"],
                "active_contract": row["active_contract"],
                "record_date": row["record_date"],
                "members": [],
            },
        )
        bucket["members"].append(row)
        if not bucket["location"] and row["location"]:
            bucket["location"] = row["location"]
        if row["active_contract"] == "yes":
            bucket["active_contract"] = "yes"
        if (row["record_date"] or "") > (bucket["record_date"] or ""):
            bucket["record_date"] = row["record_date"]

    grouped_rows = []
    for bucket in grouped.values():
        members = sorted(bucket["members"], key=lambda item: (status_rank(item["status"]), item["project_name"].lower()))
        bucket["members"] = members
        bucket["group_status"] = members[0]["status"] if members else ""
        bucket["member_count"] = len(members)
        bucket["history_count"] = sum(int(member["history_count"]) for member in members)
        grouped_rows.append(bucket)

    grouped_rows.sort(
        key=lambda item: (
            0 if item["active_contract"] == "yes" else 1,
            status_rank(item["group_status"]),
            item["installation_group"].lower(),
        )
    )
    return grouped_rows


def normalize_status(value: str) -> str:
    lookup = {
        "erro": "Erro",
        "desconectada": "Desconectada",
        "operacional": "Operacional",
        "resolvido": "Resolvido",
        "aberto": "Aberto",
        "em analise": "Em analise",
        "agendado": "Agendado",
        "em visita": "Em visita",
        "fechado": "Fechado",
        "on": "Resolvido",
        "off": "Aberto",
        "faulty": "Em analise",
    }
    normalized = normalize_name(value)
    return lookup.get(normalized, value.strip())


def excel_date_to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value in (None, "", "-"):
        return ""
    return str(value)


def row_value(row: tuple[Any, ...], index: int) -> str:
    value = row[index] if index < len(row) else None
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def get_sheet(workbook, expected_name: str):
    normalized_expected = normalize_name(expected_name)
    for sheet_name in workbook.sheetnames:
        if normalize_name(sheet_name) == normalized_expected:
            return workbook[sheet_name]
    return None


def find_asset_id(conn: sqlite3.Connection, name: str) -> int | None:
    normalized = normalize_name(name)
    row = conn.execute(
        """
        SELECT asset_id
        FROM asset_aliases
        WHERE normalized_alias = ?
        """,
        (normalized,),
    ).fetchone()
    return int(row["asset_id"]) if row else None


def rebuild_asset_alias_blob(conn: sqlite3.Connection, asset_id: int) -> None:
    aliases = [row["alias_name"] for row in query_all(conn, "SELECT alias_name FROM asset_aliases WHERE asset_id = ?", (asset_id,))]
    conn.execute("UPDATE assets SET alias_blob = ? WHERE id = ?", (" | ".join(aliases), asset_id))
    conn.commit()


def import_excel_data(conn: sqlite3.Connection, excel_path: Path) -> dict[str, int]:
    workbook = load_workbook(excel_path, data_only=True)
    excel_batch_id = create_monitoring_batch(
        conn,
        record_date=date.today().isoformat(),
        default_notes="Sincronizacao a partir do Excel.",
        raw_input="",
        source="excel-import",
    )

    assets_by_name: dict[str, int] = {}
    project_sheet = get_sheet(workbook, "Project Overview")
    if project_sheet is None:
        raise ValueError("Folha 'Project Overview' nao encontrada no Excel.")

    for row in project_sheet.iter_rows(min_row=2, values_only=True):
        project_name = row_value(row, 1)
        if not project_name:
            continue
        payload = {
            "project_number": row_value(row, 0),
            "project_name": project_name,
            "company_name": row_value(row, 2),
            "nif": row_value(row, 3),
            "address": row_value(row, 4),
            "location": row_value(row, 5),
            "panels": row_value(row, 6),
            "kwp": row_value(row, 7),
            "contract_type": row_value(row, 8),
            "sell_to": row_value(row, 9),
            "duration": row_value(row, 10),
            "start_contract": row_value(row, 12),
            "maintenance": row_value(row, 13),
            "coverage_type": row_value(row, 14),
            "access_type": row_value(row, 15),
            "maintenance_comment": row_value(row, 16),
            "status_detail": row_value(row, 17),
            "contact_name": row_value(row, 18),
            "contact_role": row_value(row, 19),
            "contact_email": row_value(row, 20),
            "contact_phone": row_value(row, 21),
            "end_contract": row_value(row, 22),
            "active_contract": row_value(row, 24),
            "notes": row_value(row, 25),
            "asset_type": row_value(row, 27),
        }
        asset_id = upsert_asset_from_excel(conn, payload)
        assets_by_name[project_name] = asset_id
        alias_names = {project_name, payload["company_name"]}
        for alias_name in alias_names:
            if alias_name:
                normalized = normalize_name(alias_name)
                if normalized:
                    conn.execute(
                        "INSERT OR IGNORE INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
                        (asset_id, alias_name, normalized, "excel"),
                    )

    monitoring_imported = 0
    monitoring_sheet = get_sheet(workbook, "Monotorizacao")
    if monitoring_sheet is not None:
        for row in monitoring_sheet.iter_rows(min_row=3, values_only=True):
            display_name = row_value(row, 0)
            status = normalize_status(row_value(row, 1))
            record_date = excel_date_to_iso(row[4] if len(row) > 4 else None)
            notes = row_value(row, 5)
            original_name = row_value(row, 6) or display_name
            if not status or not original_name:
                continue
            asset_id = find_asset_id(conn, original_name) or find_asset_id(conn, display_name)
            if asset_id:
                record_date_value = record_date or date.today().isoformat()
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM monitoring_records
                    WHERE asset_id = ? AND status = ? AND record_date = ? AND source = 'excel'
                    LIMIT 1
                    """,
                    (asset_id, status, record_date_value),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """
                        INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (asset_id, status, record_date_value, notes, "excel", excel_batch_id),
                    )
                    monitoring_imported += 1
                for alias_candidate in {display_name, original_name}:
                    normalized = normalize_name(alias_candidate)
                    if normalized:
                        conn.execute(
                            "INSERT OR IGNORE INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
                            (asset_id, alias_candidate, normalized, "excel-monitoring"),
                        )

    tickets_imported = 0
    corrective_sheet = get_sheet(workbook, "Corretivas")
    if corrective_sheet is not None:
        carried_asset_name = ""
        carried_notes = ""
        for row in corrective_sheet.iter_rows(min_row=2, values_only=True):
            asset_name = row_value(row, 0) or carried_asset_name
            installation_ref = row_value(row, 1)
            contract_type = row_value(row, 2)
            created_at = excel_date_to_iso(row[3])
            status = normalize_status(row_value(row, 4))
            notes = row_value(row, 5)
            next_action = row_value(row, 6)

            if row_value(row, 0):
                carried_asset_name = row_value(row, 0)
            if notes:
                carried_notes = notes
            elif not row_value(row, 0) and carried_notes:
                notes = carried_notes

            if not asset_name or not (status or notes or next_action):
                continue

            asset_id = find_asset_id(conn, asset_name)
            if not asset_id:
                continue

            urgency = "Alta" if status in {"Aberto", "Em analise"} else "Media"
            title = next_action.splitlines()[0][:120] if next_action else f"Corretiva - {asset_name}"
            created_at_value = created_at or date.today().isoformat()
            existing = conn.execute(
                """
                SELECT 1
                FROM tickets
                WHERE asset_id = ? AND title = ? AND created_at = ?
                LIMIT 1
                """,
                (asset_id, title, created_at_value),
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO tickets (
                        asset_id, title, urgency, status, installation_ref, notes, next_action, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        title,
                        urgency,
                        status or "Aberto",
                        installation_ref or contract_type,
                        notes,
                        next_action,
                        created_at_value,
                        created_at_value,
                    ),
                )
                tickets_imported += 1

    for asset_id_row in query_all(conn, "SELECT id FROM assets"):
        rebuild_asset_alias_blob(conn, int(asset_id_row["id"]))

    conn.execute(
        """
        UPDATE monitoring_import_batches
        SET imported_count = ?, matched_count = ?, unmatched_count = 0, auto_resolved_count = 0
        WHERE id = ?
        """,
        (monitoring_imported, monitoring_imported, excel_batch_id),
    )
    conn.commit()
    return {"assets": len(assets_by_name), "monitoring": monitoring_imported, "tickets": tickets_imported}


def upsert_asset_from_excel(conn: sqlite3.Connection, payload: dict[str, str]) -> int:
    project_name = payload["project_name"]
    existing = conn.execute(
        """
        SELECT id, installation_group
        FROM assets
        WHERE project_name = ?
        LIMIT 1
        """,
        (project_name,),
    ).fetchone()
    asset_id = int(existing["id"]) if existing else (find_asset_id(conn, project_name) or 0)
    installation_group = (
        (existing["installation_group"] if existing and existing["installation_group"] else "")
        or infer_installation_group(payload["project_name"])
    )
    payload["start_contract"] = normalize_date_value(payload["start_contract"])
    payload["end_contract"] = normalize_date_value(payload["end_contract"])
    payload["active_contract"] = derive_active_contract(payload["end_contract"], payload["active_contract"])
    payload = apply_group_defaults(conn, payload, installation_group, exclude_asset_id=asset_id or None)

    values = (
        payload["project_number"],
        payload["project_name"],
        installation_group,
        payload["company_name"],
        payload["nif"],
        payload["address"],
        payload["location"],
        payload["panels"],
        payload["kwp"],
        payload["contract_type"],
        payload["sell_to"],
        payload["duration"],
        payload["start_contract"],
        payload["maintenance"],
        payload["coverage_type"],
        payload["access_type"],
        payload["maintenance_comment"],
        payload["status_detail"],
        payload["contact_name"],
        payload["contact_role"],
        payload["contact_email"],
        payload["contact_phone"],
        payload["end_contract"],
        payload["active_contract"],
        payload["notes"],
        payload["asset_type"],
        json.dumps(payload, ensure_ascii=True),
    )

    if asset_id:
        conn.execute(
            """
            UPDATE assets
            SET
                project_number = ?, project_name = ?, installation_group = ?, company_name = ?, nif = ?, address = ?, location = ?,
                panels = ?, kwp = ?, contract_type = ?, sell_to = ?, duration = ?, start_contract = ?,
                maintenance = ?, coverage_type = ?, access_type = ?, maintenance_comment = ?, status_detail = ?,
                contact_name = ?, contact_role = ?, contact_email = ?, contact_phone = ?, end_contract = ?,
                active_contract = ?, notes = ?, asset_type = ?, source_payload = ?
            WHERE id = ?
            """,
            values + (asset_id,),
        )
        return asset_id

    cursor = conn.execute(
        """
        INSERT INTO assets (
            project_number, project_name, installation_group, company_name, nif, address, location, panels, kwp,
            contract_type, sell_to, duration, start_contract, maintenance, coverage_type,
            access_type, maintenance_comment, status_detail, contact_name, contact_role,
            contact_email, contact_phone, end_contract, active_contract, notes, asset_type,
            source_payload, alias_blob
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values + (project_name,),
    )
    return int(cursor.lastrowid)


@dataclass
class MonitoringImportResult:
    imported: int = 0
    matched: int = 0
    unmatched: int = 0
    auto_resolved: int = 0
    batch_id: int | None = None


def parse_monitoring_lines(pasted_table: str) -> list[tuple[str, str]]:
    lines = []
    for raw_line in pasted_table.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = normalize_name(line)
        if lowered in {"instalacao estado", "instalacao", "estado"}:
            continue
        if "\t" in line:
            parts = [part.strip() for part in line.split("\t") if part.strip()]
            if len(parts) >= 2:
                first_part = normalize_name(parts[0])
                second_part = normalize_name(parts[1])
                if first_part in {"instalacao", "instalacao estado"} or second_part == "estado":
                    continue
                lines.append((parts[0], normalize_status(parts[1])))
                continue
        for marker in (" Erro", " Desconectada", " Operacional", " Resolvido"):
            if line.endswith(marker):
                lines.append((line[: -len(marker)].strip(), normalize_status(marker.strip())))
                break
        else:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                lines.append((parts[0].strip(), normalize_status(parts[1].strip())))
    return lines


def import_daily_monitoring(
    conn: sqlite3.Connection,
    pasted_table: str,
    record_date: str,
    default_notes: str,
    platform_source: str,
    import_scope: str = "complete",
) -> MonitoringImportResult:
    result = MonitoringImportResult()
    parsed_lines = parse_monitoring_lines(pasted_table)
    if not parsed_lines:
        return result
    batch_id = create_monitoring_batch(conn, record_date, default_notes, pasted_table, platform_source)
    result.batch_id = batch_id
    imported_asset_ids: set[int] = set()
    for original_name, status in parsed_lines:
        asset_id = find_asset_id(conn, original_name)
        if asset_id:
            duplicate = conn.execute(
                """
                SELECT 1
                FROM monitoring_records
                WHERE asset_id = ? AND status = ? AND record_date = ? AND source = 'manual-paste'
                LIMIT 1
                """,
                (asset_id, status, record_date),
            ).fetchone()
            if duplicate:
                continue
            result.imported += 1
            imported_asset_ids.add(asset_id)
            conn.execute(
                """
                INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (asset_id, status, record_date, default_notes, "manual-paste", batch_id),
            )
            result.matched += 1
        else:
            normalized_name = normalize_name(original_name)
            duplicate_unmatched = conn.execute(
                """
                SELECT 1
                FROM monitoring_unmatched
                WHERE normalized_name = ? AND status = ? AND record_date = ?
                LIMIT 1
                """,
                (normalized_name, status, record_date),
            ).fetchone()
            if duplicate_unmatched:
                continue
            result.imported += 1
            conn.execute(
                """
                INSERT INTO monitoring_unmatched (original_name, normalized_name, status, record_date, notes, batch_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (original_name, normalized_name, status, record_date, default_notes, batch_id),
            )
            result.unmatched += 1

    if import_scope == "complete":
        latest_problem_assets = query_all(
            conn,
            """
            SELECT lm.asset_id
            FROM latest_monitoring_view lm
            WHERE lm.status IN ('Erro', 'Desconectada')
            """,
        )
        for row in latest_problem_assets:
            asset_id = int(row["asset_id"])
            if asset_id in imported_asset_ids:
                continue
            existing_today = conn.execute(
                """
                SELECT 1
                FROM monitoring_records
                WHERE asset_id = ? AND record_date = ?
                LIMIT 1
                """,
                (asset_id, record_date),
            ).fetchone()
            if existing_today:
                continue
            conn.execute(
                """
                INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    "Resolvido",
                    record_date,
                    "Resolvido automaticamente por nao constar na lista diaria.",
                    "auto-resolved",
                    batch_id,
                ),
            )
            result.auto_resolved += 1
    conn.execute(
        """
        UPDATE monitoring_import_batches
        SET imported_count = ?, matched_count = ?, unmatched_count = ?, auto_resolved_count = ?
        WHERE id = ?
        """,
        (result.imported, result.matched, result.unmatched, result.auto_resolved, batch_id),
    )
    conn.commit()
    return result


def create_monitoring_batch(
    conn: sqlite3.Connection,
    record_date: str,
    default_notes: str,
    raw_input: str,
    source: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO monitoring_import_batches (record_date, imported_at, source, default_notes, raw_input)
        VALUES (?, ?, ?, ?, ?)
        """,
        (record_date, datetime.now().isoformat(timespec="seconds"), source, default_notes, raw_input),
    )
    return int(cursor.lastrowid)


def build_problem_periods(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    rows = query_all(
        conn,
        """
        SELECT mr.record_date, mr.status, mr.notes, mr.source, mib.imported_at
        FROM monitoring_records mr
        LEFT JOIN monitoring_import_batches mib ON mib.id = mr.batch_id
        WHERE mr.asset_id = ?
        ORDER BY mr.record_date ASC, mr.id ASC
        """,
        (asset_id,),
    )
    problem_statuses = {"Erro", "Desconectada"}
    periods: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in rows:
        status = row["status"]
        if status in problem_statuses:
            if current is None:
                current = {
                    "status": status,
                    "started_on": row["record_date"],
                    "started_at": row["imported_at"],
                    "start_notes": row["notes"],
                    "last_problem_on": row["record_date"],
                    "last_problem_status": status,
                }
            else:
                current["last_problem_on"] = row["record_date"]
                current["last_problem_status"] = status
            continue

        if current is not None:
            current["resolved_on"] = row["record_date"]
            current["resolved_at"] = row["imported_at"]
            current["resolution_status"] = status
            current["resolution_notes"] = row["notes"]
            periods.append(current)
            current = None

    if current is not None:
        current["resolved_on"] = None
        current["resolved_at"] = None
        current["resolution_status"] = "Ainda ativo"
        current["resolution_notes"] = ""
        periods.append(current)

    periods.reverse()
    return periods


def build_batch_insight(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    batch = conn.execute(
        """
        SELECT *
        FROM monitoring_import_batches
        WHERE id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None

    rows = query_all(
        conn,
        """
        SELECT
            mr.id,
            mr.asset_id,
            mr.status,
            mr.record_date,
            a.project_name,
            a.active_contract
        FROM monitoring_records mr
        JOIN assets a ON a.id = mr.asset_id
        WHERE mr.batch_id = ?
        ORDER BY mr.id ASC
        """,
        (batch_id,),
    )

    problem_statuses = {"Erro", "Desconectada"}
    new_problem_assets: list[dict[str, Any]] = []
    persistent_problem_assets: list[dict[str, Any]] = []
    resolved_assets: list[dict[str, Any]] = []

    for row in rows:
        previous = conn.execute(
            """
            SELECT mr.status, mr.record_date
            FROM monitoring_records mr
            WHERE mr.asset_id = ? AND mr.id < ?
            ORDER BY mr.id DESC
            LIMIT 1
            """,
            (row["asset_id"], row["id"]),
        ).fetchone()
        previous_status = previous["status"] if previous else ""
        current_status = row["status"]
        item = {
            "asset_id": row["asset_id"],
            "project_name": row["project_name"],
            "current_status": current_status,
            "previous_status": previous_status or "-",
            "record_date": row["record_date"],
        }

        if row["active_contract"] == "yes" and current_status in problem_statuses:
            if previous_status not in problem_statuses:
                new_problem_assets.append(item)
            else:
                persistent_problem_assets.append(item)
        elif row["active_contract"] == "yes" and current_status in {"Resolvido", "Operacional"} and previous_status in problem_statuses:
            resolved_assets.append(item)

    return {
        "batch": batch,
        "new_problem_assets": new_problem_assets,
        "persistent_problem_assets": persistent_problem_assets,
        "resolved_assets": resolved_assets,
    }


def extract_export_filters(source: Any, dataset: str, for_query: bool = False) -> dict[str, str]:
    get_value = source.get
    filters = {
        "search": get_value("search", "").strip(),
        "asset_id": get_value("asset_id", "").strip(),
        "om_only": get_value("om_only", "yes").strip(),
    }
    if dataset == "monitoring":
        filters.update(
            {
                "status": get_value("status", "").strip(),
                "start_date": get_value("start_date", "").strip(),
                "end_date": get_value("end_date", "").strip(),
            }
        )
    else:
        filters.update(
            {
                "status": get_value("status", "").strip(),
                "urgency": get_value("urgency", "").strip(),
            }
        )
    if for_query:
        return filters
    return {key: value for key, value in filters.items() if value}


def build_export_dataset(
    conn: sqlite3.Connection,
    dataset: str,
    filters: dict[str, str],
    columns: list[str],
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    headers = [column for column in EXPORT_DATASETS[dataset]["columns"] if column[0] in columns]
    if not headers:
        headers = EXPORT_DATASETS[dataset]["columns"]

    if dataset == "assets":
        filter_sql = []
        params: list[Any] = []
        if filters.get("search"):
            wildcard = f"%{filters['search']}%"
            filter_sql.append("(a.project_name LIKE ? OR a.alias_blob LIKE ? OR a.company_name LIKE ? OR a.location LIKE ?)")
            params.extend([wildcard, wildcard, wildcard, wildcard])
        if filters.get("asset_id"):
            filter_sql.append("a.id = ?")
            params.append(filters["asset_id"])
        if filters.get("om_only", "yes") == "yes":
            filter_sql.append("a.active_contract = 'yes'")
        where_sql = f"WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        limit_sql = f"LIMIT {limit}" if limit else ""
        rows = query_all(
            conn,
            f"""
            SELECT
                a.project_name,
                a.location,
                a.address,
                a.contact_phone,
                a.contact_name,
                a.access_type,
                a.coverage_type,
                a.contract_type,
                a.active_contract,
                a.company_name,
                a.contact_email
            FROM assets a
            {where_sql}
            ORDER BY
                CASE a.active_contract WHEN 'yes' THEN 1 ELSE 2 END,
                a.project_name COLLATE NOCASE
            {limit_sql}
            """,
            params,
        )
    elif dataset == "monitoring":
        filter_sql = []
        params: list[Any] = []
        if filters.get("search"):
            wildcard = f"%{filters['search']}%"
            filter_sql.append("(a.project_name LIKE ? OR a.alias_blob LIKE ? OR a.company_name LIKE ?)")
            params.extend([wildcard, wildcard, wildcard])
        if filters.get("asset_id"):
            filter_sql.append("a.id = ?")
            params.append(filters["asset_id"])
        if filters.get("status"):
            filter_sql.append("mr.status = ?")
            params.append(filters["status"])
        if filters.get("om_only", "yes") == "yes":
            filter_sql.append("a.active_contract = 'yes'")
        if filters.get("start_date"):
            filter_sql.append("mr.record_date >= ?")
            params.append(filters["start_date"])
        if filters.get("end_date"):
            filter_sql.append("mr.record_date <= ?")
            params.append(filters["end_date"])
        where_sql = f"WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        limit_sql = f"LIMIT {limit}" if limit else ""
        rows = query_all(
            conn,
            f"""
            SELECT
                mr.record_date,
                mib.imported_at,
                a.project_name,
                a.location,
                a.contract_type,
                a.active_contract,
                mr.status,
                mr.notes,
                mr.source
            FROM monitoring_records mr
            JOIN assets a ON a.id = mr.asset_id
            LEFT JOIN monitoring_import_batches mib ON mib.id = mr.batch_id
            {where_sql}
            ORDER BY mr.record_date DESC, a.project_name COLLATE NOCASE, mr.id DESC
            {limit_sql}
            """,
            params,
        )
    else:
        filter_sql = []
        params = []
        if filters.get("search"):
            wildcard = f"%{filters['search']}%"
            filter_sql.append("(a.project_name LIKE ? OR a.alias_blob LIKE ? OR t.title LIKE ? OR COALESCE(t.notes, '') LIKE ?)")
            params.extend([wildcard, wildcard, wildcard, wildcard])
        if filters.get("asset_id"):
            filter_sql.append("a.id = ?")
            params.append(filters["asset_id"])
        if filters.get("status"):
            filter_sql.append("t.status = ?")
            params.append(filters["status"])
        if filters.get("urgency"):
            filter_sql.append("t.urgency = ?")
            params.append(filters["urgency"])
        if filters.get("om_only", "yes") == "yes":
            filter_sql.append("a.active_contract = 'yes'")
        where_sql = f"WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        limit_sql = f"LIMIT {limit}" if limit else ""
        rows = query_all(
            conn,
            f"""
            SELECT
                a.project_name,
                a.location,
                a.contract_type,
                a.active_contract,
                t.title,
                t.status,
                t.urgency,
                t.installation_ref,
                t.next_action,
                t.notes,
                t.created_at,
                t.updated_at
            FROM tickets t
            JOIN assets a ON a.id = t.asset_id
            {where_sql}
            ORDER BY
                CASE a.active_contract WHEN 'yes' THEN 1 ELSE 2 END,
                a.project_name COLLATE NOCASE,
                t.updated_at DESC
            {limit_sql}
            """,
            params,
        )

    normalized_rows = [dict(row) for row in rows]
    return normalized_rows, headers


def export_rows_file(
    rows: list[dict[str, Any]],
    headers: list[tuple[str, str]],
    filename: str,
    export_format: str,
):
    if export_format == "pdf":
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
        styles = getSampleStyleSheet()
        data = [[header[1] for header in headers]]
        for row in rows:
            data.append([str(row.get(header[0], "") or "-") for header in headers])
        table = Table(data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f6b5c")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c9d3d7")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.HexColor("#eef4f2")]),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        doc.build([Paragraph(filename, styles["Heading2"]), Spacer(1, 12), table])
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name=f"{filename}.pdf", mimetype="application/pdf")

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Export"
    worksheet.append([header[1] for header in headers])
    for row in rows:
        worksheet.append([row.get(header[0], "") for header in headers])
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{filename}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def build_visits_by_ticket(visits: list[sqlite3.Row]) -> dict[int, list[sqlite3.Row]]:
    visits_by_ticket: dict[int, list[sqlite3.Row]] = {}
    for visit in visits:
        visits_by_ticket.setdefault(visit["ticket_id"], []).append(visit)
    return visits_by_ticket


def group_tickets_by_asset(tickets: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for ticket in tickets:
        asset_id = int(ticket["asset_id"])
        bucket = grouped.setdefault(
            asset_id,
            {
                "asset_id": asset_id,
                "project_name": ticket["project_name"],
                "location": ticket["location"],
                "active_contract": ticket["active_contract"],
                "contract_type": ticket["contract_type"],
                "tickets": [],
            },
        )
        bucket["tickets"].append(ticket)

    ordered = []
    for asset_id, bucket in grouped.items():
        tickets_list = bucket["tickets"]
        bucket["open_count"] = sum(1 for ticket in tickets_list if ticket["status"] != "Fechado")
        bucket["critical_count"] = sum(
            1 for ticket in tickets_list if ticket["urgency"] == "Critica" and ticket["status"] != "Fechado"
        )
        bucket["last_update"] = max(ticket["updated_at"] for ticket in tickets_list)
        ordered.append(bucket)

    ordered.sort(
        key=lambda item: (
            0 if item["active_contract"] == "yes" else 1,
            -item["critical_count"],
            -item["open_count"],
            item["project_name"].lower(),
        )
    )
    return ordered


def ensure_predefined_export_templates(conn: sqlite3.Connection) -> None:
    predefined_templates = [
        {
            "name": "Preventiva - O&M ativo",
            "dataset": "assets",
            "export_format": "xlsx",
            "columns": [
                "project_name",
                "location",
                "address",
                "contact_phone",
                "contact_name",
                "access_type",
                "coverage_type",
            ],
            "filters": {
                "om_only": "yes",
            },
        }
    ]

    for template in predefined_templates:
        existing = conn.execute(
            "SELECT id FROM export_templates WHERE name = ? LIMIT 1",
            (template["name"],),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO export_templates (name, dataset, export_format, columns_json, filters_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                template["name"],
                template["dataset"],
                template["export_format"],
                json.dumps(template["columns"], ensure_ascii=True),
                json.dumps(template["filters"], ensure_ascii=True),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def get_fusionsolar_env_config() -> dict[str, str]:
    return {
        "username": os.environ.get("FUSIONSOLAR_USERNAME", "").strip(),
        "password": os.environ.get("FUSIONSOLAR_PASSWORD", "").strip(),
        "base_url": os.environ.get("FUSIONSOLAR_BASE_URL", "").strip(),
        "login_endpoint": os.environ.get("FUSIONSOLAR_LOGIN_ENDPOINT", DEFAULT_FUSIONSOLAR_LOGIN_ENDPOINT).strip(),
        "plants_endpoint": os.environ.get("FUSIONSOLAR_STATIONS_ENDPOINT", DEFAULT_FUSIONSOLAR_STATIONS_ENDPOINT).strip(),
        "real_time_endpoint": os.environ.get(
            "FUSIONSOLAR_REALTIME_ENDPOINT",
            DEFAULT_FUSIONSOLAR_REALTIME_ENDPOINT,
        ).strip(),
        "alarms_endpoint": os.environ.get("FUSIONSOLAR_ALARMS_ENDPOINT", DEFAULT_FUSIONSOLAR_ALARMS_ENDPOINT).strip(),
        "sync_hours": os.environ.get("FUSIONSOLAR_SYNC_HOURS", DEFAULT_FUSIONSOLAR_SYNC_HOURS).strip(),
    }


def ensure_integration_seed_data(conn: sqlite3.Connection) -> None:
    env_config = get_fusionsolar_env_config()
    existing = conn.execute(
        "SELECT * FROM integration_configs WHERE provider = ?",
        (INTEGRATION_PROVIDER_FUSIONSOLAR,),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE integration_configs
            SET username = CASE WHEN COALESCE(username, '') = '' THEN ? ELSE username END,
                password = CASE WHEN COALESCE(password, '') = '' THEN ? ELSE password END,
                base_url = CASE WHEN COALESCE(base_url, '') = '' THEN ? ELSE base_url END,
                login_endpoint = CASE WHEN COALESCE(login_endpoint, '') = '' THEN ? ELSE login_endpoint END,
                plants_endpoint = CASE WHEN COALESCE(plants_endpoint, '') = '' THEN ? ELSE plants_endpoint END,
                real_time_endpoint = CASE WHEN COALESCE(real_time_endpoint, '') = '' THEN ? ELSE real_time_endpoint END,
                alarms_endpoint = CASE WHEN COALESCE(alarms_endpoint, '') = '' THEN ? ELSE alarms_endpoint END,
                sync_hours = CASE WHEN COALESCE(sync_hours, '') = '' THEN ? ELSE sync_hours END,
                updated_at = ?
            WHERE provider = ?
            """,
            (
                env_config["username"],
                env_config["password"],
                env_config["base_url"],
                env_config["login_endpoint"],
                env_config["plants_endpoint"],
                env_config["real_time_endpoint"],
                env_config["alarms_endpoint"],
                env_config["sync_hours"],
                datetime.now().isoformat(timespec="seconds"),
                INTEGRATION_PROVIDER_FUSIONSOLAR,
            ),
        )
        return

    conn.execute(
        """
        INSERT INTO integration_configs (
            provider, username, password, base_url, login_endpoint, plants_endpoint, real_time_endpoint, alarms_endpoint,
            enabled, auto_sync_enabled, sync_hours, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            INTEGRATION_PROVIDER_FUSIONSOLAR,
            env_config["username"],
            env_config["password"],
            env_config["base_url"],
            env_config["login_endpoint"],
            env_config["plants_endpoint"],
            env_config["real_time_endpoint"],
            env_config["alarms_endpoint"],
            0,
            0,
            env_config["sync_hours"],
            datetime.now().isoformat(timespec="seconds"),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def get_integration_config(conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM integration_configs WHERE provider = ?", (provider,)).fetchone()


def normalize_sync_hours(raw_value: str) -> str:
    candidates = [item.strip() for item in raw_value.split(",") if item.strip()]
    normalized: list[str] = []
    for item in candidates[:2]:
        if re.fullmatch(r"\d{2}:\d{2}", item):
            hour, minute = item.split(":")
            if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
                normalized.append(item)
    if not normalized:
        normalized = DEFAULT_FUSIONSOLAR_SYNC_HOURS.split(",")
    if len(normalized) == 1:
        normalized.append("14:00" if normalized[0] != "14:00" else "08:00")
    return ",".join(normalized[:2])


def start_integration_scheduler(app: Flask) -> None:
    global SCHEDULER
    if SCHEDULER is not None:
        return
    SCHEDULER = BackgroundScheduler(timezone="Europe/Lisbon")
    SCHEDULER.start()
    refresh_integration_scheduler(app)


def refresh_integration_scheduler(app: Flask) -> None:
    global SCHEDULER
    if SCHEDULER is None:
        return
    for job in list(SCHEDULER.get_jobs()):
        if job.id.startswith("fusionsolar-sync-"):
            SCHEDULER.remove_job(job.id)

    with closing(get_db(app.config["DATABASE"])) as conn:
        config = get_integration_config(conn, INTEGRATION_PROVIDER_FUSIONSOLAR)
    if config is None or not config["enabled"] or not config["auto_sync_enabled"]:
        return

    for index, item in enumerate(normalize_sync_hours(config["sync_hours"] or DEFAULT_FUSIONSOLAR_SYNC_HOURS).split(","), start=1):
        hour, minute = item.split(":")
        SCHEDULER.add_job(
            func=run_scheduled_fusionsolar_sync,
            trigger="cron",
            hour=int(hour),
            minute=int(minute),
            args=[app],
            id=f"fusionsolar-sync-{index}",
            replace_existing=True,
        )


def run_scheduled_fusionsolar_sync(app: Flask) -> None:
    with app.app_context():
        with closing(get_db(app.config["DATABASE"])) as conn:
            try:
                run_fusionsolar_sync(conn, INTEGRATION_PROVIDER_FUSIONSOLAR, trigger_type="scheduled")
            except Exception:
                pass


def build_provider_url(base_url: str, endpoint: str) -> str:
    if not base_url or not endpoint:
        raise ValueError("Configura a base URL e os endpoints da API FusionSolar antes de sincronizar.")
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def get_fusionsolar_endpoint_config(config: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    return {
        "base_url": str(config["base_url"] or "").strip(),
        "login_endpoint": str(config["login_endpoint"] or DEFAULT_FUSIONSOLAR_LOGIN_ENDPOINT).strip() or DEFAULT_FUSIONSOLAR_LOGIN_ENDPOINT,
        "plants_endpoint": str(config["plants_endpoint"] or DEFAULT_FUSIONSOLAR_STATIONS_ENDPOINT).strip() or DEFAULT_FUSIONSOLAR_STATIONS_ENDPOINT,
        "real_time_endpoint": str(config["real_time_endpoint"] or DEFAULT_FUSIONSOLAR_REALTIME_ENDPOINT).strip() or DEFAULT_FUSIONSOLAR_REALTIME_ENDPOINT,
        "alarms_endpoint": str(config["alarms_endpoint"] or DEFAULT_FUSIONSOLAR_ALARMS_ENDPOINT).strip() or DEFAULT_FUSIONSOLAR_ALARMS_ENDPOINT,
    }


def extract_fusionsolar_xsrf_token(response: requests.Response, session: requests.Session) -> str:
    for key, value in response.headers.items():
        if key.lower() == "xsrf-token" and value:
            return value.strip()
    for cookie_name in ("XSRF-TOKEN", "xsrf-token"):
        cookie_value = session.cookies.get(cookie_name)
        if cookie_value:
            return str(cookie_value).strip()
    raise ValueError("O login FusionSolar respondeu sem XSRF-TOKEN no header/cookies.")


def get_fusionsolar_session(config: sqlite3.Row | dict[str, Any], *, force_login: bool = False) -> tuple[requests.Session, str]:
    endpoints = get_fusionsolar_endpoint_config(config)
    base_url = endpoints["base_url"]
    username = str(config["username"] or "").strip()
    password = str(config["password"] or "").strip()

    if not username or not password:
        raise ValueError("Preenche username e password do FusionSolar.")
    if not base_url:
        raise ValueError("Preenche a Base URL do FusionSolar.")

    cache_key = f"{base_url}|{username}"
    now = datetime.now()

    with FUSIONSOLAR_SESSION_LOCK:
        cached = FUSIONSOLAR_SESSION_CACHE.get(cache_key)
        if cached and not force_login and cached["expires_at"] > now:
            return cached["session"], cached["xsrf_token"]

        session = requests.Session()
        login_response = session.post(
            build_provider_url(base_url, endpoints["login_endpoint"]),
            json={"userName": username, "systemCode": password},
            headers={"Content-Type": "application/json", "Accept": "application/json, */*"},
            timeout=30,
        )
        login_response.raise_for_status()
        payload = login_response.json()
        if payload.get("success") is not True or int(payload.get("failCode") or 0) != 0:
            message = payload.get("message") or "Login FusionSolar falhou."
            raise ValueError(f"{message} (failCode={payload.get('failCode')})")

        xsrf_token = extract_fusionsolar_xsrf_token(login_response, session)
        session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, */*",
                "XSRF-TOKEN": xsrf_token,
            }
        )
        FUSIONSOLAR_SESSION_CACHE[cache_key] = {
            "session": session,
            "xsrf_token": xsrf_token,
            "expires_at": now + timedelta(minutes=25),
        }
        return session, xsrf_token


def invalidate_fusionsolar_session(config: sqlite3.Row | dict[str, Any]) -> None:
    endpoints = get_fusionsolar_endpoint_config(config)
    cache_key = f"{endpoints['base_url']}|{str(config['username'] or '').strip()}"
    with FUSIONSOLAR_SESSION_LOCK:
        FUSIONSOLAR_SESSION_CACHE.pop(cache_key, None)


def post_fusionsolar_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    expected_message: str,
) -> dict[str, Any]:
    response = session.post(url, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("success") is not True or int(data.get("failCode") or 0) != 0:
        message = data.get("message") or expected_message
        raise ValueError(f"{message} (failCode={data.get('failCode')})")
    return data


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_fusionsolar_stations(session: requests.Session, *, base_url: str, endpoint: str) -> list[dict[str, Any]]:
    url = build_provider_url(base_url, endpoint)
    stations: list[dict[str, Any]] = []
    page_no = 1
    page_count = 1

    while page_no <= page_count:
        payload = post_fusionsolar_json(
            session,
            url,
            {"pageNo": page_no},
            expected_message="Falha ao obter a lista de centrais FusionSolar.",
        )
        page_data = payload.get("data") or {}
        page_list = page_data.get("list") or []
        if not isinstance(page_list, list):
            raise ValueError("A resposta FusionSolar da lista de centrais nao trouxe data.list.")
        stations.extend([item for item in page_list if isinstance(item, dict)])
        page_count = int(page_data.get("pageCount") or 1)
        page_no += 1

    return stations


def fetch_fusionsolar_realtime_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
) -> dict[str, dict[str, Any]]:
    url = build_provider_url(base_url, endpoint)
    real_time_map: dict[str, dict[str, Any]] = {}

    for group in chunked(station_codes, 100):
        payload = post_fusionsolar_json(
            session,
            url,
            {"stationCodes": ",".join(group)},
            expected_message="Falha ao obter os dados realtime das centrais FusionSolar.",
        )
        data_rows = payload.get("data") or []
        if not isinstance(data_rows, list):
            raise ValueError("A resposta FusionSolar realtime nao trouxe uma lista em data.")
        for row in data_rows:
            if not isinstance(row, dict):
                continue
            station_code = str(row.get("stationCode") or "").strip()
            if station_code:
                real_time_map[station_code] = row

    return real_time_map


def fetch_fusionsolar_alarm_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
) -> dict[str, list[dict[str, Any]]]:
    url = build_provider_url(base_url, endpoint)
    alarm_map: dict[str, list[dict[str, Any]]] = {}
    now_ms = int(datetime.now().timestamp() * 1000)

    for group in chunked(station_codes, 100):
        payload = post_fusionsolar_json(
            session,
            url,
            {
                "stationCodes": ",".join(group),
                "beginTime": 0,
                "endTime": now_ms,
                "language": DEFAULT_FUSIONSOLAR_ALARMS_LANGUAGE,
            },
            expected_message="Falha ao obter alarmes ativos FusionSolar.",
        )
        alarm_rows = payload.get("data") or []
        if not isinstance(alarm_rows, list):
            raise ValueError("A resposta FusionSolar de alarmes nao trouxe uma lista em data.")
        for row in alarm_rows:
            if not isinstance(row, dict):
                continue
            station_code = str(row.get("stationCode") or "").strip()
            if not station_code:
                continue
            alarm_map.setdefault(station_code, []).append(row)

    return alarm_map


def map_fusionsolar_status(raw_status: Any) -> str:
    raw_value = "" if raw_status is None else str(raw_status).strip()
    if raw_value in {"1", "1.0"}:
        return "Desconectada"
    if raw_value in {"2", "2.0"}:
        return "Erro"
    if raw_value in {"3", "3.0"}:
        return "Operacional"

    normalized = normalize_name(raw_value)
    if normalized in {"fault", "alarm", "error", "critical", "faulty"}:
        return "Erro"
    if normalized in {"offline", "disconnected", "no signal", "communication lost"}:
        return "Desconectada"
    if normalized in {"running", "normal", "online", "ok", "healthy"}:
        return "Operacional"
    return normalize_status(raw_value or "Operacional")


def describe_fusionsolar_health_state(raw_status: Any) -> str:
    raw_value = "" if raw_status is None else str(raw_status).strip()
    if raw_value in {"1", "1.0"}:
        return "disconnected"
    if raw_value in {"2", "2.0"}:
        return "faulty"
    if raw_value in {"3", "3.0"}:
        return "healthy"
    return raw_value or "unknown"


def normalize_fusionsolar_plant_row(
    station_row: dict[str, Any],
    realtime_row: dict[str, Any] | None = None,
    alarms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    external_id = str(station_row.get("plantCode") or station_row.get("stationCode") or "").strip()
    external_name = str(station_row.get("plantName") or station_row.get("stationName") or "").strip()
    if not external_name:
        raise ValueError("A resposta FusionSolar nao trouxe nome de central numa das linhas.")

    data_item_map = (realtime_row or {}).get("dataItemMap") or {}
    health_raw = data_item_map.get("real_health_state")
    raw_status = describe_fusionsolar_health_state(health_raw)
    status = map_fusionsolar_status(health_raw)

    active_alarms = alarms or []
    alarm_levels = sorted({str(item.get("lev")) for item in active_alarms if item.get("lev") is not None})
    notes_parts = [f"health_state={raw_status}"]
    if active_alarms:
        notes_parts.append(f"active_alarms={len(active_alarms)}")
        if alarm_levels:
            notes_parts.append(f"levels={','.join(alarm_levels)}")

    return {
        "external_id": external_id,
        "external_name": external_name,
        "status": status,
        "raw_status": raw_status,
        "health_state": raw_status,
        "alarm_count": len(active_alarms),
        "alarm_levels": ",".join(alarm_levels),
        "notes": "; ".join(notes_parts),
        "payload": {
            "station": station_row,
            "realtime": realtime_row or {},
            "alarms": active_alarms,
        },
    }


def find_suggested_asset_id(conn: sqlite3.Connection, external_name: str) -> int | None:
    normalized_name = normalize_name(external_name)
    exact = find_asset_id(conn, external_name)
    if exact:
        return exact
    candidate = conn.execute(
        """
        SELECT id
        FROM assets
        WHERE REPLACE(LOWER(project_name), ' ', '') LIKE ?
        ORDER BY project_name COLLATE NOCASE
        LIMIT 1
        """,
        (f"%{normalized_name.replace(' ', '')}%",),
    ).fetchone()
    return int(candidate["id"]) if candidate else None


def run_fusionsolar_check(conn: sqlite3.Connection, provider: str, dry_run: bool = False) -> dict[str, Any]:
    config = get_integration_config(conn, provider)
    if config is None:
        raise ValueError("Configuracao FusionSolar nao encontrada.")
    endpoints = get_fusionsolar_endpoint_config(config)
    if not endpoints["base_url"]:
        raise ValueError("Falta a Base URL do FusionSolar.")

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            session, _ = get_fusionsolar_session(config, force_login=attempt == 1)
            stations = fetch_fusionsolar_stations(
                session,
                base_url=endpoints["base_url"],
                endpoint=endpoints["plants_endpoint"],
            )
            if not stations:
                raise ValueError("A API FusionSolar nao devolveu centrais para esta conta.")

            station_codes = [
                str(row.get("plantCode") or row.get("stationCode") or "").strip()
                for row in stations
                if str(row.get("plantCode") or row.get("stationCode") or "").strip()
            ]
            realtime_map = fetch_fusionsolar_realtime_map(
                session,
                base_url=endpoints["base_url"],
                endpoint=endpoints["real_time_endpoint"],
                station_codes=station_codes,
            )
            alarm_map: dict[str, list[dict[str, Any]]] = {}
            alarm_error = ""
            try:
                alarm_map = fetch_fusionsolar_alarm_map(
                    session,
                    base_url=endpoints["base_url"],
                    endpoint=endpoints["alarms_endpoint"],
                    station_codes=station_codes,
                )
            except Exception as exc:
                alarm_error = str(exc)
            normalized_rows = [
                normalize_fusionsolar_plant_row(
                    station_row,
                    realtime_map.get(str(station_row.get("plantCode") or station_row.get("stationCode") or "").strip()),
                    alarm_map.get(str(station_row.get("plantCode") or station_row.get("stationCode") or "").strip(), []),
                )
                for station_row in stations
            ]
            break
        except Exception as exc:
            invalidate_fusionsolar_session(config)
            last_error = exc
            if attempt == 1:
                raise
    else:
        raise last_error or ValueError("Falha desconhecida no FusionSolar.")

    if not dry_run:
        conn.execute(
            """
            UPDATE integration_configs
            SET last_sync_status = ?, last_error = ?, updated_at = ?
            WHERE provider = ?
            """,
            ("success", "", datetime.now().isoformat(timespec="seconds"), provider),
        )
        conn.commit()
    return {
        "rows": normalized_rows,
        "station_count": len(stations),
        "realtime_count": len(realtime_map),
        "alarm_count": sum(len(items) for items in alarm_map.values()),
        "alarm_error": alarm_error,
    }


def create_integration_run(conn: sqlite3.Connection, provider: str, trigger_type: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO integration_sync_runs (provider, started_at, trigger_type, status)
        VALUES (?, ?, ?, ?)
        """,
        (provider, datetime.now().isoformat(timespec="seconds"), trigger_type, "running"),
    )
    return int(cursor.lastrowid)


def finalize_integration_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    matched_count: int,
    unresolved_count: int,
    auto_resolved_count: int,
    error_message: str = "",
    summary_json: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE integration_sync_runs
        SET finished_at = ?, status = ?, matched_count = ?, unresolved_count = ?,
            auto_resolved_count = ?, error_message = ?, summary_json = ?
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            status,
            matched_count,
            unresolved_count,
            auto_resolved_count,
            error_message,
            json.dumps(summary_json or {}, ensure_ascii=True),
            run_id,
        ),
    )


def create_or_update_asset_integration(
    conn: sqlite3.Connection,
    asset_id: int,
    provider: str,
    external_id: str,
    external_name: str,
    status: str,
) -> None:
    existing = None
    if external_id:
        existing = conn.execute(
            "SELECT id FROM asset_integrations WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE asset_integrations
            SET asset_id = ?, external_name = ?, enabled = 1, last_sync_at = ?, last_status = ?, last_error = ''
            WHERE id = ?
            """,
            (asset_id, external_name, datetime.now().isoformat(timespec="seconds"), status, existing["id"]),
        )
        return

    candidate = conn.execute(
        """
        SELECT id
        FROM asset_integrations
        WHERE provider = ? AND asset_id = ?
        LIMIT 1
        """,
        (provider, asset_id),
    ).fetchone()
    if candidate:
        conn.execute(
            """
            UPDATE asset_integrations
            SET external_id = ?, external_name = ?, enabled = 1, last_sync_at = ?, last_status = ?, last_error = ''
            WHERE id = ?
            """,
            (external_id or None, external_name, datetime.now().isoformat(timespec="seconds"), status, candidate["id"]),
        )
        return

    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled, last_sync_at, last_status, last_error)
        VALUES (?, ?, ?, ?, 1, ?, ?, '')
        """,
        (
            asset_id,
            provider,
            external_id or None,
            external_name,
            datetime.now().isoformat(timespec="seconds"),
            status,
        ),
    )


def upsert_integration_unresolved(
    conn: sqlite3.Connection,
    *,
    provider: str,
    run_id: int,
    external_id: str,
    external_name: str,
    status: str,
    payload: dict[str, Any],
) -> None:
    normalized_name = normalize_name(external_name)
    suggested_asset_id = find_suggested_asset_id(conn, external_name)
    existing = conn.execute(
        """
        SELECT id
        FROM integration_unresolved
        WHERE provider = ? AND normalized_name = ? AND resolution_status = 'pending'
        LIMIT 1
        """,
        (provider, normalized_name),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE integration_unresolved
            SET sync_run_id = ?, external_id = ?, external_status = ?, payload_json = ?, suggested_asset_id = ?, created_at = ?
            WHERE id = ?
            """,
            (
                run_id,
                external_id or None,
                status,
                json.dumps(payload, ensure_ascii=True),
                suggested_asset_id,
                datetime.now().isoformat(timespec="seconds"),
                existing["id"],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO integration_unresolved (
            provider, sync_run_id, external_id, external_name, normalized_name, external_status,
            payload_json, suggested_asset_id, resolution_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            provider,
            run_id,
            external_id or None,
            external_name,
            normalized_name,
            status,
            json.dumps(payload, ensure_ascii=True),
            suggested_asset_id,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def run_fusionsolar_sync(conn: sqlite3.Connection, provider: str, trigger_type: str = "manual") -> dict[str, Any]:
    with FUSIONSOLAR_SYNC_LOCK:
        config = get_integration_config(conn, provider)
        if config is None:
            raise ValueError("Configuracao FusionSolar nao encontrada.")
        if not config["enabled"]:
            raise ValueError("A integracao FusionSolar esta desativada.")

        run_id = create_integration_run(conn, provider, trigger_type)
        batch_id = create_monitoring_batch(
            conn,
            record_date=date.today().isoformat(),
            default_notes=f"Sync FusionSolar ({trigger_type})",
            raw_input="",
            source="FusionSolar API",
        )

        try:
            result = run_fusionsolar_check(conn, provider, dry_run=True)
            rows = result["rows"]
            matched = 0
            unresolved = 0
            auto_resolved = 0
            synced_asset_ids: set[int] = set()

            for row in rows:
                external_id = row["external_id"]
                external_name = row["external_name"]
                status = row["status"]

                mapped_asset = None
                if external_id:
                    mapped_asset = conn.execute(
                        """
                        SELECT ai.asset_id
                        FROM asset_integrations ai
                        WHERE ai.provider = ? AND ai.external_id = ? AND ai.enabled = 1
                        LIMIT 1
                        """,
                        (provider, external_id),
                    ).fetchone()
                if mapped_asset is None:
                    mapped_asset = conn.execute(
                        """
                        SELECT ai.asset_id
                        FROM asset_integrations ai
                        WHERE ai.provider = ? AND ai.external_name = ? AND ai.enabled = 1
                        LIMIT 1
                        """,
                        (provider, external_name),
                    ).fetchone()
                asset_id = int(mapped_asset["asset_id"]) if mapped_asset else (find_asset_id(conn, external_name) or 0)

                if asset_id:
                    synced_asset_ids.add(asset_id)
                    duplicate = conn.execute(
                        """
                        SELECT 1
                        FROM monitoring_records
                        WHERE asset_id = ? AND status = ? AND record_date = ? AND source = 'fusion-solar-sync'
                        LIMIT 1
                        """,
                        (asset_id, status, date.today().isoformat()),
                    ).fetchone()
                    if not duplicate:
                        conn.execute(
                            """
                            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                asset_id,
                                status,
                                date.today().isoformat(),
                                f"Sync {provider}: {row['notes']}",
                                "fusion-solar-sync",
                                batch_id,
                            ),
                        )
                    create_or_update_asset_integration(conn, asset_id, provider, external_id, external_name, status)
                    matched += 1
                else:
                    upsert_integration_unresolved(
                        conn,
                        provider=provider,
                        run_id=run_id,
                        external_id=external_id,
                        external_name=external_name,
                        status=status,
                        payload=row["payload"],
                    )
                    unresolved += 1

            mapped_assets = query_all(
                conn,
                """
                SELECT ai.asset_id
                FROM asset_integrations ai
                JOIN latest_monitoring_view lm ON lm.asset_id = ai.asset_id
                WHERE ai.provider = ? AND ai.enabled = 1 AND lm.status IN ('Erro', 'Desconectada')
                """,
                (provider,),
            )
            for row in mapped_assets:
                asset_id = int(row["asset_id"])
                if asset_id in synced_asset_ids:
                    continue
                existing_today = conn.execute(
                    """
                    SELECT 1
                    FROM monitoring_records
                    WHERE asset_id = ? AND record_date = ? AND source = 'fusion-solar-sync'
                    LIMIT 1
                    """,
                    (asset_id, date.today().isoformat()),
                ).fetchone()
                if existing_today:
                    continue
                conn.execute(
                    """
                    INSERT INTO monitoring_records (asset_id, status, record_date, notes, source, batch_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        "Resolvido",
                        date.today().isoformat(),
                        "Resolvido automaticamente por ausencia no sync FusionSolar.",
                        "fusion-solar-sync",
                        batch_id,
                    ),
                )
                auto_resolved += 1

            conn.execute(
                """
                UPDATE integration_configs
                SET last_sync_at = ?, last_sync_status = 'success', last_error = '', updated_at = ?
                WHERE provider = ?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                    provider,
                ),
            )
            conn.execute(
                """
                UPDATE monitoring_import_batches
                SET imported_count = ?, matched_count = ?, unmatched_count = ?, auto_resolved_count = ?
                WHERE id = ?
                """,
                (matched + unresolved, matched, unresolved, auto_resolved, batch_id),
            )
            finalize_integration_run(
                conn,
                run_id,
                status="success",
                matched_count=matched,
                unresolved_count=unresolved,
                auto_resolved_count=auto_resolved,
                summary_json={
                    "provider_rows": len(rows),
                    "alarm_rows": result.get("alarm_count", 0),
                    "alarm_error": result.get("alarm_error", ""),
                    "station_rows": result.get("station_count", len(rows)),
                    "realtime_rows": result.get("realtime_count", len(rows)),
                },
            )
            conn.commit()
            return {"matched": matched, "unresolved": unresolved, "auto_resolved": auto_resolved}
        except Exception as exc:
            conn.execute(
                """
                UPDATE integration_configs
                SET last_sync_status = 'error', last_error = ?, updated_at = ?
                WHERE provider = ?
                """,
                (str(exc), datetime.now().isoformat(timespec="seconds"), provider),
            )
            finalize_integration_run(
                conn,
                run_id,
                status="error",
                matched_count=0,
                unresolved_count=0,
                auto_resolved_count=0,
                error_message=str(exc),
            )
            conn.commit()
            raise


def resolve_fusionsolar_unresolved(conn: sqlite3.Connection, unresolved_id: int, asset_id: int) -> None:
    row = conn.execute(
        "SELECT * FROM integration_unresolved WHERE id = ? AND resolution_status = 'pending'",
        (unresolved_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Entrada FusionSolar por resolver nao encontrada.")
    payload = json.loads(row["payload_json"] or "{}")
    create_or_update_asset_integration(
        conn,
        asset_id,
        row["provider"],
        str(row["external_id"] or ""),
        row["external_name"],
        row["external_status"] or "Operacional",
    )
    conn.execute(
        """
        UPDATE integration_unresolved
        SET resolution_status = 'resolved', resolved_at = ?, resolution_notes = ?
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            f"Associado ao asset {asset_id}",
            unresolved_id,
        ),
    )
    conn.commit()


def create_asset_from_unresolved(conn: sqlite3.Connection, unresolved_id: int) -> int:
    row = conn.execute(
        "SELECT * FROM integration_unresolved WHERE id = ? AND resolution_status = 'pending'",
        (unresolved_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Entrada FusionSolar por resolver nao encontrada.")

    project_name = row["external_name"]
    installation_group = infer_installation_group(project_name)
    cursor = conn.execute(
        """
        INSERT INTO assets (project_name, installation_group, active_contract, notes, alias_blob)
        VALUES (?, ?, 'no', ?, ?)
        """,
        (
            project_name,
            installation_group,
            f"Criado a partir do provider {row['provider']}.",
            project_name,
        ),
    )
    asset_id = int(cursor.lastrowid)
    normalized_name = normalize_name(project_name)
    if normalized_name:
        conn.execute(
            "INSERT OR IGNORE INTO asset_aliases (asset_id, alias_name, normalized_alias, source) VALUES (?, ?, ?, ?)",
            (asset_id, project_name, normalized_name, "integration-create"),
        )
    resolve_fusionsolar_unresolved(conn, unresolved_id, asset_id)
    rebuild_asset_alias_blob(conn, asset_id)
    return asset_id


def ignore_fusionsolar_unresolved(conn: sqlite3.Connection, unresolved_id: int) -> None:
    conn.execute(
        """
        UPDATE integration_unresolved
        SET resolution_status = 'ignored', resolved_at = ?, resolution_notes = 'Ignorado manualmente'
        WHERE id = ?
        """,
        (datetime.now().isoformat(timespec="seconds"), unresolved_id),
    )
    conn.commit()


app = create_app()


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitoring Board local app")
    parser.add_argument("--host", default="127.0.0.1", help="Host/IP onde a app vai escutar")
    parser.add_argument("--port", type=int, default=5000, help="Porta onde a app vai arrancar")
    parser.add_argument("--debug", action="store_true", help="Ativa debug do Flask")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_cli_args()
    if DEFAULT_EXCEL_PATH and not DB_PATH.exists():
        with closing(get_db(str(DB_PATH))) as conn:
            import_excel_data(conn, DEFAULT_EXCEL_PATH)
    elif DEFAULT_EXCEL_PATH:
        with closing(get_db(str(DB_PATH))) as conn:
            if query_scalar(conn, "SELECT COUNT(*) FROM assets") == 0:
                import_excel_data(conn, DEFAULT_EXCEL_PATH)
    app.run(host=args.host, port=args.port, debug=args.debug)
