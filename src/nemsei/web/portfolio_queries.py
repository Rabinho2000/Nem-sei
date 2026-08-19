"""Read models for the portfolio screens.

Routes stay thin: everything a template needs is assembled here, from the
persisted `PortfolioDataset` rather than by recomputing anything. Country,
lifecycle status and provider arrive as **filters** over a portfolio's members;
they never split it into sub-portfolios.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.portfolios.datasets import assets_needing_attention, latest_dataset
from nemsei.portfolios.models import (
    Portfolio,
    PortfolioDataset,
    PortfolioDatasetMember,
    PortfolioMembership,
    PortfolioRule,
    PortfolioSnapshot,
)
from nemsei.portfolios.service import resolve_members
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.reporting.periods import monthly_period


SECTIONS = (
    ("overview", "Visão geral"),
    ("installations", "Instalações"),
    ("production", "Produção"),
    ("availability", "Disponibilidade"),
    ("financial", "Financeiro"),
    ("reports", "Relatórios"),
    ("settings", "Configuração"),
)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def portfolio_list(session: Session) -> dict[str, Any]:
    rows = []
    for portfolio in session.scalars(
        select(Portfolio).order_by(Portfolio.status, Portfolio.name)
    ):
        members = session.scalar(
            select(func.count(PortfolioMembership.id)).where(
                PortfolioMembership.portfolio_id == portfolio.id,
                PortfolioMembership.valid_to.is_(None),
            )
        )
        unresolved = session.scalar(
            select(func.count(PortfolioMembership.id)).where(
                PortfolioMembership.portfolio_id == portfolio.id,
                PortfolioMembership.valid_to.is_(None),
                PortfolioMembership.resolution_state != "resolved",
            )
        )
        owner = session.get(Organization, portfolio.owner_id) if portfolio.owner_id else None
        rows.append(
            {
                "portfolio": portfolio,
                "members": members or 0,
                "unresolved": unresolved or 0,
                "owner": owner.display_name if owner else None,
            }
        )
    return {"portfolios": rows}


def _member_rows(session: Session, dataset: PortfolioDataset | None, snapshot: PortfolioSnapshot | None) -> list[dict[str, Any]]:
    """One row per member, whether or not it has an installation or data."""
    metrics_by_asset: dict[int, PortfolioDatasetMember] = {}
    if dataset is not None:
        metrics_by_asset = {member.asset_id: member for member in dataset.members}

    rows: list[dict[str, Any]] = []
    for entry in (snapshot.members_json if snapshot else []):
        asset_id = entry.get("asset_id")
        asset = session.get(Asset, asset_id) if asset_id else None
        member = metrics_by_asset.get(asset_id) if asset_id else None
        metrics = (member.metrics_json if member else {}) or {}
        states = (member.states_json if member else {}) or {}
        provider = None
        if asset is not None:
            provider = session.scalar(
                select(ProviderConnection.provider_code)
                .join(AssetProviderMapping, AssetProviderMapping.provider_connection_id == ProviderConnection.id)
                .where(
                    AssetProviderMapping.asset_id == asset.id,
                    AssetProviderMapping.resource_kind == "plant",
                    AssetProviderMapping.mapping_status == "active",
                )
                .limit(1)
            )
        production = _number(metrics.get("production"))
        expected = _number(metrics.get("expected"))
        rows.append(
            {
                "asset_id": asset_id,
                "name": asset.canonical_name if asset else (entry.get("external_name") or "—"),
                "sub_account": entry.get("sub_account"),
                "tax_id": entry.get("tax_id"),
                "resolution_state": entry.get("resolution_state"),
                "origin": entry.get("origin"),
                "membership_id": entry.get("membership_id"),
                "country_code": asset.country_code if asset else None,
                "locality": asset.locality if asset else None,
                "lifecycle_status": asset.lifecycle_status if asset else None,
                "contract_type": asset.contract_type if asset else None,
                "provider_code": provider,
                "kwp": _number(asset.installed_dc_power_kw) if asset else None,
                "production_kwh": production,
                "expected_kwh": expected,
                "self_use_kwh": _number(metrics.get("self_use")),
                "export_kwh": _number(metrics.get("export")),
                "consumption_kwh": _number(metrics.get("consumption")),
                "grid_import_kwh": _number(metrics.get("grid_import")),
                "production_state": states.get("production", "missing"),
                "performance_pct": (
                    production / expected * 100 if production is not None and expected else None
                ),
            }
        )
    return rows


def _apply_filters(rows: list[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    """Country, status and provider narrow the view. They never split it."""
    result = rows
    for key in ("country_code", "lifecycle_status", "provider_code", "contract_type", "resolution_state"):
        wanted = (filters.get(key) or "").strip()
        if wanted:
            result = [row for row in result if (row.get(key) or "") == wanted]
    search = (filters.get("q") or "").strip().casefold()
    if search:
        result = [
            row
            for row in result
            if search in (row["name"] or "").casefold()
            or search in (row.get("sub_account") or "").casefold()
            or search in (row.get("tax_id") or "").casefold()
        ]
    if (filters.get("attention") or "").strip():
        result = [row for row in result if row["production_state"] != "measured"]
    return result


def _filter_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    def options(key: str) -> list[str]:
        return sorted({row[key] for row in rows if row.get(key)})

    return {
        "country_code": options("country_code"),
        "lifecycle_status": options("lifecycle_status"),
        "provider_code": options("provider_code"),
        "contract_type": options("contract_type"),
    }


def portfolio_detail(
    session: Session,
    *,
    portfolio_id: int,
    section: str,
    report_month: str,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    portfolio = session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Unknown portfolio.")
    period = monthly_period(report_month)
    dataset = latest_dataset(session, portfolio_id=portfolio_id, period_start=period.start)
    snapshot = session.get(PortfolioSnapshot, dataset.snapshot_id) if dataset else None

    if snapshot is None:
        # Nothing frozen for this period yet: show the live membership so the
        # screen is still useful, and say plainly that no dataset exists.
        live = resolve_members(session, portfolio_id=portfolio_id, on=period.start)
        snapshot_members = [member.as_dict() for member in live]
        snapshot = None
    else:
        snapshot_members = list(snapshot.members_json or [])

    rows = _member_rows(session, dataset, snapshot) if snapshot else _member_rows(
        session, dataset, type("S", (), {"members_json": snapshot_members})()
    )
    options = _filter_options(rows)
    filtered = _apply_filters(rows, filters or {})

    totals = (dataset.totals_json if dataset else {}) or {}
    coverage = (dataset.coverage_json if dataset else {}) or {}
    attention = [row for row in filtered if row["production_state"] != "measured"]

    return {
        "portfolio": portfolio,
        "owner": session.get(Organization, portfolio.owner_id) if portfolio.owner_id else None,
        "section": section,
        "sections": SECTIONS,
        "period": period,
        "report_month": report_month,
        "dataset": dataset,
        "snapshot": snapshot,
        "rows": filtered,
        "all_rows": rows,
        "filters": filters or {},
        "filter_options": options,
        "totals": totals,
        "coverage": coverage,
        "attention": attention,
        "rules": session.scalars(
            select(PortfolioRule).where(PortfolioRule.portfolio_id == portfolio_id).order_by(PortfolioRule.id)
        ).all(),
        "memberships": session.scalars(
            select(PortfolioMembership)
            .where(PortfolioMembership.portfolio_id == portfolio_id)
            .order_by(PortfolioMembership.sub_account, PortfolioMembership.id)
        ).all(),
        "ranked": assets_needing_attention(session, dataset) if dataset else [],
        "snapshots": session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
            .order_by(PortfolioSnapshot.period_start.desc())
            .limit(12)
        ).all(),
    }


def available_months(session: Session, portfolio_id: int) -> list[str]:
    rows = session.scalars(
        select(PortfolioDataset.period_start)
        .where(PortfolioDataset.portfolio_id == portfolio_id)
        .order_by(PortfolioDataset.period_start.desc())
        .distinct()
    ).all()
    return [row.strftime("%Y-%m") for row in rows]


def default_month(session: Session, portfolio_id: int) -> str:
    months = available_months(session, portfolio_id)
    return months[0] if months else date.today().strftime("%Y-%m")
