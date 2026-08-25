"""Chart-ready series for one installation, read from canonical facts only.

Every function here goes through `CanonicalFactRepository.
current_production_facts_for_asset`, which reduces the append-only
`production_facts` table to its newest revision per source fact. Summing the
raw rows would add a corrected value to the value it was meant to replace --
that defect produced 129.28 kWh for a day that made 59.56, and it is not one to
rediscover from a chart.

Coverage is computed alongside every total, never afterwards. A month with four
days of readings and a month with thirty are not the same number, and a chart
that draws them the same height is worse than no chart.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.monitoring.models import ProductionFact
from nemsei.monitoring.repository import CanonicalFactRepository
from nemsei.shared.clock import utc_now
from nemsei.web.charts import Point, bar_chart, coverage_calendar, sparkline, stacked_bars

METRIC_LABELS = {
    "production_energy": "Produção",
    "self_use_energy": "Autoconsumo",
    "export_energy": "Injeção na rede",
    "consumption_energy": "Consumo",
    "grid_import_energy": "Importação da rede",
}


def _moment(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _daily_totals(
    session: Session, *, asset_id: int, start: date, end: date, metric_kind: str = "production_energy"
) -> dict[date, float]:
    """Day -> kWh, current revision only, days without a fact simply absent."""
    facts = CanonicalFactRepository(session).current_production_facts_for_asset(
        asset_id=asset_id,
        period_start=_moment(start),
        period_end=_moment(end),
        metric_kind=metric_kind,
    )
    totals: dict[date, float] = {}
    for fact in facts:
        if fact.value is None:
            continue
        day = fact.period_start.astimezone(timezone.utc).date()
        totals[day] = totals.get(day, 0.0) + float(fact.value)
    return totals


def daily_series(session: Session, *, asset_id: int, days: int = 60, metric_kind: str = "production_energy") -> dict[str, Any]:
    """The last `days` days, one column each, gaps left as gaps."""
    today = utc_now().date()
    start = today - timedelta(days=days - 1)
    totals = _daily_totals(session, asset_id=asset_id, start=start, end=today + timedelta(days=1), metric_kind=metric_kind)
    points = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        value = totals.get(day)
        points.append(
            Point(
                label=day.strftime("%d/%m") if offset % 7 == 0 else "",
                value=value,
                hint=f"{day.isoformat()}: " + (f"{value:.1f} kWh" if value is not None else "sem leitura"),
            )
        )
    return {
        "chart": bar_chart(points, unit="kWh"),
        "total": sum(totals.values()),
        "days_with_data": len(totals),
        "days": days,
        "metric_label": METRIC_LABELS.get(metric_kind, metric_kind),
    }


def monthly_series(session: Session, *, asset_id: int, months: int = 12, metric_kind: str = "production_energy") -> dict[str, Any]:
    """Monthly totals, each carrying the share of its own days that reported."""
    today = utc_now().date()
    first = date(today.year, today.month, 1)
    starts: list[date] = []
    cursor = first
    for _ in range(months):
        starts.append(cursor)
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
    starts.reverse()

    window_end = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    totals = _daily_totals(session, asset_id=asset_id, start=starts[0], end=window_end, metric_kind=metric_kind)

    names = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")
    points = []
    for month_start in starts:
        days_in_month = monthrange(month_start.year, month_start.month)[1]
        # The current month is only expected to have reported up to today.
        expected = today.day if (month_start.year, month_start.month) == (today.year, today.month) else days_in_month
        in_month = {day: value for day, value in totals.items() if (day.year, day.month) == (month_start.year, month_start.month)}
        total = sum(in_month.values()) if in_month else None
        points.append(
            Point(
                label=f"{names[month_start.month - 1]}",
                value=total,
                coverage=(len(in_month) / expected) if expected else 0.0,
                hint=(
                    f"{names[month_start.month - 1]} {month_start.year}: "
                    + (f"{total:.0f} kWh" if total is not None else "sem leituras")
                    + f" · {len(in_month)} de {expected} dias"
                ),
            )
        )
    return {
        "chart": bar_chart(points, unit="kWh"),
        "months": months,
        "metric_label": METRIC_LABELS.get(metric_kind, metric_kind),
    }


def month_calendar(session: Session, *, asset_id: int, year: int, month: int) -> dict[str, Any]:
    """One square per day of a month, so a gap is a shape and not a footnote."""
    days_in_month = monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    totals = _daily_totals(session, asset_id=asset_id, start=start, end=end)
    values: dict[int, float | None] = {day: totals.get(date(year, month, day)) for day in range(1, days_in_month + 1)}
    return {
        "calendar": coverage_calendar(values, year=year, month=month),
        "with_data": sum(1 for value in values.values() if value is not None),
        "days_in_month": days_in_month,
        "year": year,
        "month": month,
    }


def energy_balance(session: Session, *, asset_id: int, start: date, end: date) -> dict[str, Any]:
    """Production split into self-use and export; consumption into self-use and import.

    Self-use appears in both columns deliberately -- it is the same energy seen
    from the two sides, and showing it twice is what makes the balance legible.
    """
    metrics = {
        name: sum(_daily_totals(session, asset_id=asset_id, start=start, end=end, metric_kind=name).values())
        for name in ("production_energy", "self_use_energy", "export_energy", "consumption_energy", "grid_import_energy")
    }
    columns = [
        ("Produção", [("Autoconsumo", metrics["self_use_energy"], 1), ("Injeção", metrics["export_energy"], 2)]),
        ("Consumo", [("Autoconsumo", metrics["self_use_energy"], 1), ("Importação", metrics["grid_import_energy"], 3)]),
    ]
    return {
        "stack": stacked_bars(columns),
        "metrics": metrics,
        "self_use_share": (metrics["self_use_energy"] / metrics["production_energy"]) if metrics["production_energy"] else None,
    }


def headline(session: Session, *, asset_id: int, days: int = 30) -> dict[str, Any]:
    """The numbers at the top of an installation, each with its own coverage."""
    today = utc_now().date()
    start = today - timedelta(days=days - 1)
    totals = _daily_totals(session, asset_id=asset_id, start=start, end=today + timedelta(days=1))
    ordered = [totals.get(start + timedelta(days=offset)) for offset in range(days)]
    known = [value for value in ordered if value is not None]
    newest = session.scalar(
        select(func.max(ProductionFact.period_start)).where(ProductionFact.asset_id == asset_id)
    )
    return {
        "window_days": days,
        "total_kwh": sum(known) if known else None,
        "days_with_data": len(known),
        "coverage": len(known) / days if days else 0.0,
        "best_day": max(known) if known else None,
        "spark": sparkline(ordered),
        "latest_fact_on": newest.astimezone(timezone.utc).date() if newest else None,
        "stale_days": (today - newest.astimezone(timezone.utc).date()).days if newest else None,
    }


def portfolio_monthly_series(session: Session, *, months: int = 12, total_assets: int | None = None) -> dict[str, Any]:
    """Monthly production across the whole portfolio, with how much of it reported.

    One query rather than 266, but the same reduction: `DISTINCT ON
    (provider_mapping_id, source_fact_key) ... ORDER BY source_revision DESC`
    is exactly what `current_production_facts_for_asset` does per asset, and
    skipping it here would add every corrected value to the value it replaced.

    Coverage is installations reporting over installations that exist, which is
    the honest denominator: a month where 2 of 266 plants reported is not a
    collapse in production, and the chart has to be able to say that.
    """
    today = utc_now().date()
    first = date(today.year, today.month, 1)
    starts: list[date] = []
    cursor = first
    for _ in range(months):
        starts.append(cursor)
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
    starts.reverse()

    current = (
        select(
            ProductionFact.asset_id,
            ProductionFact.period_start,
            ProductionFact.value,
        )
        .where(
            ProductionFact.metric_kind == "production_energy",
            ProductionFact.period_start >= _moment(starts[0]),
        )
        .distinct(ProductionFact.provider_mapping_id, ProductionFact.source_fact_key)
        .order_by(
            ProductionFact.provider_mapping_id,
            ProductionFact.source_fact_key,
            ProductionFact.source_revision.desc(),
        )
        .subquery()
    )
    bucket = func.date_trunc("month", current.c.period_start).label("bucket")
    rows = session.execute(
        select(bucket, func.sum(current.c.value), func.count(func.distinct(current.c.asset_id)))
        .where(current.c.value.isnot(None))
        .group_by(bucket)
    ).all()
    by_month = {
        (row[0].astimezone(timezone.utc).year, row[0].astimezone(timezone.utc).month): (float(row[1] or 0), int(row[2]))
        for row in rows
    }

    if total_assets is None:
        total_assets = int(session.scalar(select(func.count(Asset.id))) or 0)
    names = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")
    points = []
    for month_start in starts:
        total, reporting = by_month.get((month_start.year, month_start.month), (None, 0))
        points.append(
            Point(
                label=names[month_start.month - 1],
                value=(total / 1000.0) if total is not None else None,
                coverage=(reporting / total_assets) if total_assets else 0.0,
                hint=(
                    f"{names[month_start.month - 1]} {month_start.year}: "
                    + (f"{total / 1000.0:.1f} MWh" if total is not None else "sem leituras")
                    + f" · {reporting} de {total_assets} centrais"
                ),
            )
        )
    return {"chart": bar_chart(points, unit="MWh"), "total_assets": total_assets, "months": months}
