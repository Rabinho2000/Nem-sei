"""HTTP adapter for ticket routes.

Routes are registered directly on the application to preserve the historical
endpoint names used by templates and external links during this migration.
"""
from __future__ import annotations

from flask import Flask, flash, g, redirect, render_template, request, url_for

from monitoring_board.domains.tickets import service


def register_ticket_routes(app: Flask) -> None:
    @app.route("/tickets", methods=["GET", "POST"])
    def tickets() -> str:
        if request.method == "POST":
            asset_id = service.create_ticket(g.db, request.form)
            if asset_id is None:
                flash("A intervencao precisa de um titulo.", "error")
                return redirect(url_for("tickets"))
            flash("Intervencao criada.", "success")
            return redirect(url_for("tickets", asset_id=asset_id))
        return render_template("tickets.html", **service.ticket_page_context(g.db, request.args))

    @app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
    def update_ticket(ticket_id: int):
        ticket = service.update_ticket(g.db, ticket_id, request.form)
        flash("Intervencao atualizada.", "success")
        if ticket:
            return redirect(url_for("tickets", asset_id=ticket["asset_id"]))
        return redirect(url_for("tickets"))

    @app.route("/tickets/<int:ticket_id>/visit", methods=["POST"])
    def add_visit(ticket_id: int):
        ticket = service.add_ticket_visit(g.db, ticket_id, request.form)
        flash("Visita registada.", "success")
        if ticket:
            return redirect(url_for("tickets", asset_id=ticket["asset_id"]))
        return redirect(url_for("tickets"))

    @app.route("/tickets/<int:ticket_id>/delete", methods=["POST"])
    def delete_ticket(ticket_id: int):
        ticket = service.delete_ticket(g.db, ticket_id)
        if ticket is None:
            flash("Intervencao nao encontrada.", "error")
            return redirect(url_for("tickets"))
        flash("Intervencao apagada.", "success")
        return redirect(url_for("tickets", asset_id=ticket["asset_id"]))
