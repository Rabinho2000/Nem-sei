"""The fleet-wide Produção page: today, this month, and who is off-target.

Reuses the same current-revision-only reduction `web/series.py` already
proves against real regressions (129.28 kWh reported for a day that made
59.56, from summing raw rows instead of the newest revision per source
fact) -- but at fleet scale, so it stays one query per figure instead of
looping `energy_balance` over ~267 assets. `portfolio_monthly_series`
already does exactly this for the twelve-month trend; this module is the
same reduction for "hoje" and for "esperado vs real" this month, plus a
per-asset breakdown for the ranked table.

Expected values come from `FinancialModelMonth.expected_production_kwh` on
the confirmed model, exactly as `installation_queries._expected_vs_actual`
reads it for one installation -- never a second definition of "expected"
at fleet scale.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.reporting.models import FinancialModel, FinancialModelMonth
from nemsei.shared.clock import utc_now
from nemsei.web.series import fleet_metric_totals, portfolio_monthly_series


def fleet_daily_total(session: Session, *, on: date | None = None) -> dict[str, Any]:
    """Today's fleet production, and how many of the fleet's centrais it comes from."""
    day = on or utc_now().date()
    end = day + timedelta(days=1)
    by_asset = fleet_metric_totals(session, start=day, end=end)
    total_assets = int(session.scalar(select(func.count(Asset.id))) or 0)
    return {
        "date": day,
        "total_kwh": sum(by_asset.values()) if by_asset else None,
        "reporting_assets": len(by_asset),
        "total_assets": total_assets,
    }


def fleet_month_expected_vs_actual(session: Session, *, on: date | None = None) -> dict[str, Any]:
    """This month's fleet production against the sum of confirmed models'
    expectation for that month -- the same honesty rule as the
    per-installation version: no confirmed model for an asset means that
    asset is simply absent from the expected side, not zero."""
    today = on or utc_now().date()
    month_start = date(today.year, today.month, 1)
    by_asset = fleet_metric_totals(session, start=month_start, end=today + timedelta(days=1))

    confirmed = (
        select(FinancialModel.id, FinancialModel.asset_id)
        .where(FinancialModel.status == "confirmed")
        .distinct(FinancialModel.asset_id)
        .order_by(FinancialModel.asset_id, FinancialModel.version.desc())
        .subquery()
    )
    expected_rows = session.execute(
        select(confirmed.c.asset_id, FinancialModelMonth.expected_production_kwh)
        .join(FinancialModelMonth, FinancialModelMonth.financial_model_id == confirmed.c.id)
        .where(FinancialModelMonth.month == today.month, FinancialModelMonth.expected_production_kwh.isnot(None))
    ).all()
    expected_by_asset = {int(asset_id): Decimal(value) for asset_id, value in expected_rows}

    actual_total = sum(by_asset.values()) if by_asset else None
    expected_total = sum(expected_by_asset.values()) if expected_by_asset else None
    deviation_pct = (
        (Decimal(str(actual_total)) - expected_total) / expected_total * 100
        if actual_total is not None and expected_total
        else None
    )
    return {
        "month": today.month,
        "actual_total_kwh": actual_total,
        "expected_total_kwh": expected_total,
        "modelled_assets": len(expected_by_asset),
        "deviation_pct": deviation_pct,
        "by_asset": by_asset,
        "expected_by_asset": expected_by_asset,
    }


def production_ranking(
    session: Session, *, by_asset: dict[int, float], expected_by_asset: dict[int, Decimal], limit: int = 20
) -> list[dict[str, Any]]:
    """Installations this month, worst shortfall against their own confirmed
    model first -- an asset with no model is not a shortfall, it is simply
    unranked by deviation and shown after the ones that can be judged."""
    asset_ids = set(by_asset) | set(expected_by_asset)
    if not asset_ids:
        return []
    rows = session.execute(
        select(Asset.id, Asset.canonical_name, Organization.display_name)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(Asset.id.in_(asset_ids))
    ).all()
    entries: list[dict[str, Any]] = []
    for asset_id, name, organization_name in rows:
        actual = by_asset.get(asset_id)
        expected = expected_by_asset.get(asset_id)
        deviation_pct = (
            (Decimal(str(actual)) - expected) / expected * 100 if actual is not None and expected else None
        )
        entries.append(
            {
                "asset_id": asset_id,
                "name": name,
                "organization_name": organization_name,
                "actual_kwh": actual,
                "expected_kwh": expected,
                "deviation_pct": deviation_pct,
            }
        )
    entries.sort(key=lambda row: (row["deviation_pct"] is None, row["deviation_pct"] if row["deviation_pct"] is not None else 0))
    return entries[:limit]


def production_page(session: Session) -> dict[str, Any]:
    today = utc_now().date()
    daily = fleet_daily_total(session, on=today)
    month = fleet_month_expected_vs_actual(session, on=today)
    trend = portfolio_monthly_series(session, months=12, total_assets=daily["total_assets"])
    return {
        "today": daily,
        "month": month,
        "trend": trend,
        "ranking": production_ranking(
            session, by_asset=month["by_asset"], expected_by_asset=month["expected_by_asset"]
        ),
    }
