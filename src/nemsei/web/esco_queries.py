"""The fleet-wide ESCO page: autoconsumo, poupança do cliente, receita Solcor.

"Instalação ESCO" here means the same thing the billing calculation itself
means by it: a resolved `AssetBillingConfig` whose `report_type` is
`"esco"` -- not `contracts.priority`'s `commercial_family` (whose money is
at risk, used to prioritise incident attention) and not
`reporting.commercial.report_type_for`'s guess from free-text contract
fields (used only where no billing configuration exists yet to ask
instead). Both of those answer a different question; this page follows
whichever field actually drives `calculate_billing`'s ESCO branch, so a
plant never shows ESCO revenue here that the billing engine itself would
not also compute.

The energy metrics come from `web.series.fleet_metric_totals` -- five fleet
queries, current revision only, instead of `energy_balance`'s five queries
*per asset*. `calculate_billing` itself is pure arithmetic once the metrics
are in hand, so this reduces N ESCO installations from 5N queries to 5.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.reporting.commercial import billing_config_from, resolve_billing_config_for_all
from nemsei.reporting.rules.billing import calculate_billing
from nemsei.reporting.rules.types import EnergyBreakdown
from nemsei.shared.clock import utc_now
from nemsei.web.series import fleet_metric_totals

_METRIC_KINDS = ("production_energy", "self_use_energy", "export_energy", "consumption_energy")


def _decimal(by_asset: dict[int, float], asset_id: int) -> Decimal | None:
    value = by_asset.get(asset_id)
    return Decimal(str(value)) if value is not None else None


def esco_page(session: Session, *, on: date | None = None) -> dict[str, Any]:
    today = on or utc_now().date()
    month_start = date(today.year, today.month, 1)
    end = today + timedelta(days=1)

    configs_by_asset = resolve_billing_config_for_all(session, on=today, report_type="esco")
    if not configs_by_asset:
        return {"rows": [], "totals": None, "asset_count": 0}

    asset_rows = session.execute(
        select(Asset.id, Asset.canonical_name, Organization.display_name)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(Asset.id.in_(configs_by_asset))
    ).all()

    metrics_by_kind = {
        kind: fleet_metric_totals(session, start=month_start, end=end, metric_kind=kind, asset_ids=list(configs_by_asset))
        for kind in _METRIC_KINDS
    }

    rows: list[dict[str, Any]] = []
    for asset_id, name, organization_name in asset_rows:
        production = _decimal(metrics_by_kind["production_energy"], asset_id)
        row: dict[str, Any] = {
            "asset_id": asset_id,
            "name": name,
            "organization_name": organization_name,
            "has_production_data": production is not None,
            "billing": None,
        }
        if production is not None:
            config = billing_config_from(configs_by_asset[asset_id])
            breakdown = EnergyBreakdown(
                production_kwh=production,
                self_use_kwh=_decimal(metrics_by_kind["self_use_energy"], asset_id) or Decimal("0"),
                export_kwh=_decimal(metrics_by_kind["export_energy"], asset_id) or Decimal("0"),
                consumption_kwh=_decimal(metrics_by_kind["consumption_energy"], asset_id) or Decimal("0"),
            )
            row["billing"] = calculate_billing(breakdown, config)
        rows.append(row)

    billed = [row for row in rows if row["billing"] is not None]
    rows.sort(key=lambda row: (row["billing"] is None, -(row["billing"].solcor_payment_eur if row["billing"] else Decimal("0"))))

    totals = None
    if billed:
        totals = {
            "production_kwh": sum((row["billing"].production_kwh for row in billed), Decimal("0")),
            "self_use_kwh": sum((row["billing"].self_use_kwh for row in billed), Decimal("0")),
            "savings_eur": sum((row["billing"].net_benefit_eur for row in billed), Decimal("0")),
            "solcor_revenue_eur": sum((row["billing"].solcor_payment_eur for row in billed), Decimal("0")),
        }

    return {
        "rows": rows,
        "totals": totals,
        "asset_count": len(rows),
        "billed_count": len(billed),
        "month": today.month,
    }
