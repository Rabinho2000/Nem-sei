"""HTTP views for the persisted Telegram alert history."""
from __future__ import annotations

from typing import Any

from flask import Flask, g, render_template, request

from monitoring_board.db import query_all


def register_alert_routes(app: Flask) -> None:
    @app.route("/telegram-alerts")
    def telegram_alerts() -> str:
        status_filter = request.args.get("status", "").strip()
        asset_filter = request.args.get("asset_id", "").strip()
        alert_type_filter = request.args.get("alert_type", "").strip()
        blocked_reason_filter = request.args.get("blocked_reason", "").strip()
        conditions = []
        params: list[Any] = []
        if status_filter:
            conditions.append("ta.status = ?")
            params.append(status_filter)
        if asset_filter:
            conditions.append("ta.asset_id = ?")
            params.append(asset_filter)
        if alert_type_filter:
            conditions.append("ta.alert_type = ?")
            params.append(alert_type_filter)
        if blocked_reason_filter:
            conditions.append("ta.blocked_reason = ?")
            params.append(blocked_reason_filter)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = query_all(
            g.db,
            f"""
            SELECT ta.*, a.project_name
            FROM telegram_alerts ta
            LEFT JOIN assets a ON a.id = ta.asset_id
            {where_sql}
            ORDER BY ta.sent_at DESC, ta.id DESC
            LIMIT 250
            """,
            params,
        )
        alert_types = [row["alert_type"] for row in query_all(g.db, "SELECT DISTINCT alert_type FROM telegram_alerts ORDER BY alert_type")]
        blocked_reasons = [row["blocked_reason"] for row in query_all(g.db, "SELECT DISTINCT blocked_reason FROM telegram_alerts WHERE blocked_reason IS NOT NULL AND blocked_reason != '' ORDER BY blocked_reason")]
        assets_for_mapping = query_all(
            g.db,
            """
            SELECT DISTINCT a.id, a.project_name
            FROM assets a
            JOIN telegram_alerts ta ON ta.asset_id = a.id
            ORDER BY a.project_name COLLATE NOCASE
            """,
        )
        return render_template(
            "telegram_alerts.html",
            alerts=rows,
            status_filter=status_filter,
            asset_filter=asset_filter,
            alert_type_filter=alert_type_filter,
            blocked_reason_filter=blocked_reason_filter,
            alert_types=alert_types,
            blocked_reasons=blocked_reasons,
            assets_for_mapping=assets_for_mapping,
        )
