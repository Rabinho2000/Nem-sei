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
from nemsei.monitoring.repository import CanonicalFactRepository, canonical_facts
from nemsei.shared.clock import utc_now
from nemsei.reporting.rules.availability_source import KIND_CONTRACTUAL
from nemsei.web.charts import Point, bar_chart, coverage_calendar, dual_bar_chart, sparkline, stacked_bars
from nemsei.web.labels import availability_coverage, availability_kind, availability_source

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


# Period options for the "Produção × Consumo" chart. "Hoje" is one bar --
# `production_facts` has never carried anything finer than daily granularity
# (verified against the real table: every one of 221 997 rows is
# `granularity='day'`), so an hourly "today" chart would be inventing a
# resolution the data cannot support. One honest daily bar is what "hoje"
# means here.
PRODUCTION_CONSUMPTION_PERIODS = ("today", "week", "month", "year")
PERIOD_LABELS = {"today": "Hoje", "week": "7 dias", "month": "Mês", "year": "Ano"}


def dual_daily_series(
    session: Session, *, asset_id: int, days: int, production_kind: str = "production_energy", consumption_kind: str = "consumption_energy"
) -> dict[str, Any]:
    """Produção e consumo, dia a dia, na mesma escala -- ver `charts.dual_bar_chart`."""
    today = utc_now().date()
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    production_totals = _daily_totals(session, asset_id=asset_id, start=start, end=end, metric_kind=production_kind)
    consumption_totals = _daily_totals(session, asset_id=asset_id, start=start, end=end, metric_kind=consumption_kind)

    production_points, consumption_points = [], []
    for offset in range(days):
        day = start + timedelta(days=offset)
        label = day.strftime("%d/%m") if days <= 14 or offset % 7 == 0 else ""
        production_value = production_totals.get(day)
        consumption_value = consumption_totals.get(day)
        production_points.append(
            Point(label, production_value, hint=f"{day.isoformat()}: produção " + (f"{production_value:.1f} kWh" if production_value is not None else "sem leitura"))
        )
        consumption_points.append(
            Point(label, consumption_value, hint=f"{day.isoformat()}: consumo " + (f"{consumption_value:.1f} kWh" if consumption_value is not None else "sem leitura"))
        )
    return {
        "chart": dual_bar_chart(production_points, consumption_points, unit="kWh"),
        "production_total": sum(production_totals.values()) if production_totals else None,
        "consumption_total": sum(consumption_totals.values()) if consumption_totals else None,
        "days": days,
    }


def dual_monthly_series(
    session: Session, *, asset_id: int, months: int = 12, production_kind: str = "production_energy", consumption_kind: str = "consumption_energy"
) -> dict[str, Any]:
    """Produção e consumo, mês a mês, na mesma escala. Mesmo balde de meses
    que `monthly_series` usa, para que os dois nunca discordem sobre onde
    cai a fronteira de um mês."""
    today = utc_now().date()
    first = date(today.year, today.month, 1)
    starts: list[date] = []
    cursor = first
    for _ in range(months):
        starts.append(cursor)
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
    starts.reverse()
    window_end = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)

    production_totals = _daily_totals(session, asset_id=asset_id, start=starts[0], end=window_end, metric_kind=production_kind)
    consumption_totals = _daily_totals(session, asset_id=asset_id, start=starts[0], end=window_end, metric_kind=consumption_kind)

    names = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")

    def month_points(totals: dict[date, float]) -> list[Point]:
        points = []
        for month_start in starts:
            in_month = {day: value for day, value in totals.items() if (day.year, day.month) == (month_start.year, month_start.month)}
            total = sum(in_month.values()) if in_month else None
            label = names[month_start.month - 1]
            points.append(Point(label, total, hint=f"{label} {month_start.year}: " + (f"{total:.0f} kWh" if total is not None else "sem leituras")))
        return points

    return {
        "chart": dual_bar_chart(month_points(production_totals), month_points(consumption_totals), unit="kWh"),
        "production_total": sum(production_totals.values()) if production_totals else None,
        "consumption_total": sum(consumption_totals.values()) if consumption_totals else None,
        "months": months,
    }


def production_consumption_series(session: Session, *, asset_id: int, period: str) -> dict[str, Any]:
    """The one entry point the installation detail page calls -- picks the
    right granularity for one of `PRODUCTION_CONSUMPTION_PERIODS`."""
    if period not in PRODUCTION_CONSUMPTION_PERIODS:
        period = "week"
    if period == "today":
        return {**dual_daily_series(session, asset_id=asset_id, days=1), "period": period}
    if period == "week":
        return {**dual_daily_series(session, asset_id=asset_id, days=7), "period": period}
    if period == "month":
        today = utc_now().date()
        days_in_month = monthrange(today.year, today.month)[1]
        return {**dual_daily_series(session, asset_id=asset_id, days=days_in_month), "period": period}
    return {**dual_monthly_series(session, asset_id=asset_id, months=12), "period": period}


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



# ---------------------------------------------------------------------------
# WAT diária, ao nível da instalação.
# ---------------------------------------------------------------------------
# A disponibilidade só existia no fecho mensal e no portfolio, e é diária que
# um operador precisa dela: para saber, hoje, se o dia fechado ontem produziu
# um número contratual ou se ficou sem cobertura. Nada aqui calcula nada --
# lê `asset_availability_daily` através de
# `diagnostics.availability_service.asset_availability_series`, que já aplica
# `select_availability`. Renderizar uma página nunca faz uma chamada ao
# provider, e este caminho não tem por onde: o serviço que lê não importa
# nenhum cliente.

AVAILABILITY_WINDOW_DAYS = 60


def availability_panel(session: Session, *, asset_id: int, days: int = AVAILABILITY_WINDOW_DAYS) -> dict[str, Any]:
    """O KPI, o gráfico e a tabela da WAT diária de uma instalação.

    Três regras que a página não pode quebrar, e que por isso são resolvidas
    aqui e não no template:

    1. **Um dia sem valor é um buraco, nunca um zero.** O `Point` sem valor já
       desenha `c-void` em `macros/chart.html`; o que este módulo garante é
       que nenhum dia sem linha entra na série como `0.0`.
    2. **Uma figura amostrada nunca aparece rotulada como contratual.** Cada
       ponto e cada linha da tabela carrega o seu `source_kind`, vindo da
       linha guardada -- não de uma suposição sobre qual das duas fontes
       "deve" estar lá.
    3. **A ausência explica-se.** Sem valor no último dia fechado, o KPI diz o
       que faltou (cobertura, inversores observados de esperados) em vez de
       mostrar um traço mudo.
    """
    from nemsei.diagnostics.availability_service import asset_availability_series  # local: evita ciclo web -> diagnostics -> web

    today = utc_now().date()
    start = today - timedelta(days=days - 1)
    series = asset_availability_series(session, asset_id=asset_id, from_date=start, to_date=today)

    points = []
    for index, entry in enumerate(series):
        day = entry["date"]
        kind = availability_kind(entry["source_kind"])
        if entry["availability_pct"] is None:
            hint = f"{day.strftime('%d/%m/%Y')}: sem WAT · {availability_coverage(entry['coverage_status'])['label'].lower()}"
        else:
            hint = (
                f"{day.strftime('%d/%m/%Y')}: {entry['availability_pct']:.2f} % · {kind['label']}"
                f" · {entry['valid_sample_count']} slots válidos"
            )
        points.append(
            Point(
                label=day.strftime("%d/%m") if index % 7 == 0 else "",
                value=entry["availability_pct"],
                hint=hint,
            )
        )

    rows = [
        {
            "date": entry["date"],
            "availability_pct": entry["availability_pct"],
            "coverage": availability_coverage(entry["coverage_status"]),
            "kind": availability_kind(entry["source_kind"]),
            "source_label": availability_source(entry["source"]) if entry["source"] else "—",
            "valid_sample_count": entry["valid_sample_count"],
            "observed_device_count": entry["observed_device_count"],
            "expected_device_count": entry["expected_device_count"],
            # Both sources present for this day, so the table can say that a
            # sampled figure exists beside a contractual one that has no
            # percentage -- the case an operator most needs to tell apart.
            "sources_available": entry["sources_available"],
        }
        for entry in reversed(series)
    ]

    measured = [entry for entry in series if entry["availability_pct"] is not None]
    contractual_days = sum(1 for entry in measured if entry["source_kind"] == KIND_CONTRACTUAL)
    return {
        "kpi": _availability_kpi(series),
        "chart": bar_chart(points, unit="%"),
        "rows": rows,
        "window_days": days,
        "days_with_value": len(measured),
        "contractual_days": contractual_days,
        # A window whose only figures are sampled is a real and reportable
        # state: the contractual pipeline has produced nothing for this
        # installation yet. Named so the template does not have to infer it.
        "contractual_available": contractual_days > 0,
    }


def _availability_kpi(series: list[dict[str, Any]]) -> dict[str, Any]:
    """O último dia fechado com WAT -- ou o que faltou para o haver.

    Qual é esse dia é decidido por
    `availability_service.latest_measured_entry`, não aqui: a regra ("um dia
    materializado que saiu `indeterminate` não serve de KPI") tem de valer
    igual em qualquer sítio que pergunte, e uma segunda cópia era o começo
    de duas respostas diferentes para a mesma pergunta.

    Quando não há nenhuma, devolve na mesma a cobertura do dia mais recente
    que chegou a ser materializado, para a página poder dizer *porquê* em
    vez de só mostrar um traço.
    """
    from nemsei.diagnostics.availability_service import latest_measured_entry  # local: mesmo ciclo que acima

    measured = latest_measured_entry(series)
    if measured is not None:
        return {
            "available": True,
            "date": measured["date"],
            "availability_pct": measured["availability_pct"],
            "kind": availability_kind(measured["source_kind"]),
            "source_label": availability_source(measured["source"]),
            "valid_sample_count": measured["valid_sample_count"],
            "observed_device_count": measured["observed_device_count"],
            "expected_device_count": measured["expected_device_count"],
        }
    for entry in reversed(series):
        if entry["source"] is not None:
            return {
                "available": False,
                "date": entry["date"],
                "availability_pct": None,
                "coverage": availability_coverage(entry["coverage_status"]),
                "kind": availability_kind(entry["source_kind"]),
                "source_label": availability_source(entry["source"]),
                "valid_sample_count": entry["valid_sample_count"],
                "observed_device_count": entry["observed_device_count"],
                "expected_device_count": entry["expected_device_count"],
            }
    return {
        "available": False,
        "date": None,
        "availability_pct": None,
        "coverage": availability_coverage("missing"),
        "kind": availability_kind(None),
        "source_label": "—",
        "valid_sample_count": 0,
        "observed_device_count": 0,
        "expected_device_count": 0,
    }


def portfolio_monthly_series(
    session: Session,
    *,
    months: int = 12,
    total_assets: int | None = None,
    asset_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Monthly production across the whole portfolio, with how much of it reported.

    One query rather than 266, and the same reduction:
    `monitoring.repository.canonical_facts` picks the newest revision *and*
    the source the policy selects, which is exactly what
    `current_production_facts_for_asset` does per asset. Doing it by hand here
    is how this chart came to add a fallback's reading to the primary's for
    the same day, on top of the corrected-revision defect it already handled.

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

    current = canonical_facts(
        metric_kind="production_energy",
        period_start=_moment(starts[0]),
        asset_ids=asset_ids,
    ).subquery()
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
        total_assets = len(asset_ids) if asset_ids is not None else int(session.scalar(select(func.count(Asset.id))) or 0)
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


def fleet_metric_totals(
    session: Session, *, start: date, end: date, metric_kind: str = "production_energy", asset_ids: list[int] | None = None
) -> dict[int, float]:
    """One metric, one period, every asset, in one query -- the same
    `canonical_facts` reduction `portfolio_monthly_series` uses for its
    monthly bucket, grouped by asset instead. The batched sibling of
    `_daily_totals`/`energy_balance`, which run one query per asset per
    metric -- fine for one installation's page, too many for a fleet page
    covering ~267 of them.

    Sharing the reduction is the point: a fleet total and an installation
    chart that disagree are two wrong answers, not one right one.
    """
    current = canonical_facts(
        metric_kind=metric_kind,
        period_start=_moment(start),
        period_end=_moment(end),
        asset_ids=asset_ids,
    ).subquery()
    rows = session.execute(
        select(current.c.asset_id, func.sum(current.c.value))
        .where(current.c.value.isnot(None))
        .group_by(current.c.asset_id)
    ).all()
    return {int(asset_id): float(total or 0) for asset_id, total in rows}


def ranked_installations(rows: list[dict[str, Any]], *, limit: int = 12) -> dict[str, Any]:
    """The period's production per installation, biggest first.

    Reads the dataset rows the portfolio report already built rather than
    querying again: a chart that disagreed with the table beside it would be
    worse than no chart. An installation with no measurement stays a gap, so a
    plant that reported nothing never looks like a plant that produced nothing.
    """
    named = [row for row in rows if row.get("asset_id")]
    ordered = sorted(named, key=lambda row: -(float(row["production_kwh"]) if row.get("production_kwh") else -1))
    points = [
        Point(
            label=(row["name"][:11] + "…") if len(row["name"]) > 12 else row["name"],
            value=float(row["production_kwh"]) if row.get("production_kwh") else None,
            hint=f"{row['name']}: " + (f"{float(row['production_kwh']):.0f} kWh" if row.get("production_kwh") else "sem medição"),
        )
        for row in ordered[:limit]
    ]
    return {"chart": bar_chart(points, width=760, plot_height=140, unit="kWh"), "shown": len(points), "total": len(named)}


def portfolio_balance(totals: dict[str, Any]) -> dict[str, Any]:
    """The portfolio's energy balance from its own frozen totals."""

    def value(key: str) -> float:
        entry = totals.get(key) or {}
        raw = entry.get("value")
        return float(raw) if raw else 0.0

    production, self_use = value("production"), value("self_use")
    columns = [
        ("Produção", [("Autoconsumo", self_use, 1), ("Excedente", value("export"), 2)]),
        ("Consumo", [("Autoconsumo", self_use, 1), ("Rede", value("grid_import"), 3)]),
    ]
    return {
        "stack": stacked_bars(columns),
        "self_use_share": (self_use / production) if production else None,
    }
