"""Periodic diagnostic digests (D6): a summary, never a second finding.

`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` -- this module never calls
`diagnostics/findings.py`, never writes a `DiagnosticIncident` (D1), and
never writes a `NotificationEvent` (D3, immediate per-incident alerts). It
only reads already-persisted incidents (via `portfolios/diagnostics.py`,
itself read-only) and produces one `DigestRun` row per window:

    facts -> findings -> incidents -> digest decision -> delivery

`generate_digest` is the decision step (deterministic, idempotent, no
external call). `deliver_digest` is the only step that ever calls a
`TelegramClient`, and only ever touches a `pending` digest -- mirrors D3's
decide/deliver split exactly, for the same reason: a digest with nothing to
deliver it yet is a different question from whether delivery succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nemsei.assets.models import Asset
from nemsei.config import external_capability_enabled
from nemsei.contracts.service import scoped_asset_ids
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.notifications.eligibility import eligible_for_recovery_digest
from nemsei.notifications.enrichment import NotificationContext, build_context
from nemsei.notifications.models import DIGEST_KINDS, DigestRun, NotificationChannel, NotificationEpisode
from nemsei.notifications.problem_families import CATEGORIES
from nemsei.notifications.render_telegram import duration_label, family_label
from nemsei.notifications.telegram_client import TelegramClient, default_client_factory
from nemsei.portfolios.diagnostics import (
    asset_ids_for_portfolio,
    portfolio_diagnostics_summary,
    portfolio_installation_rows,
)
from nemsei.portfolios.models import Portfolio
from nemsei.shared.clock import utc_now


# How many installations the digest names individually per portfolio --
# "não quero listar centenas de incidentes": the digest is a summary, the
# full worst-first ranking already exists at /portfolios/<id>/diagnostics
# (D5) for whoever needs the rest.
TOP_INSTALLATIONS_PER_PORTFOLIO = 3


def _default_client_factory(channel: NotificationChannel) -> TelegramClient:
    # One decision point for the whole process: telegram_client.py returns the
    # real client only when a bot token is configured, and the mock otherwise.
    return default_client_factory(channel)


# --- decide: build the window's content, deterministically --------------------


def _window_incident_facts(
    session: Session, *, asset_ids: list[int], window_start: datetime, window_end: datetime
) -> tuple[list[DiagnosticIncident], list[DiagnosticIncident]]:
    """Every incident opened, and every incident resolved, inside this exact
    window -- regardless of current status, so an incident that opened and
    resolved within the same window is honestly reported as both."""
    if not asset_ids:
        return [], []
    opened = list(
        session.scalars(
            select(DiagnosticIncident).where(
                DiagnosticIncident.asset_id.in_(asset_ids),
                DiagnosticIncident.opened_at >= window_start,
                DiagnosticIncident.opened_at < window_end,
            )
        )
    )
    resolved = list(
        session.scalars(
            select(DiagnosticIncident).where(
                DiagnosticIncident.asset_id.in_(asset_ids),
                DiagnosticIncident.resolved_at.is_not(None),
                DiagnosticIncident.resolved_at >= window_start,
                DiagnosticIncident.resolved_at < window_end,
            )
        )
    )
    return opened, resolved


def _portfolio_payload(session: Session, *, portfolio: Portfolio, window_start: datetime, window_end: datetime, now: datetime) -> dict[str, Any]:
    summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio.id, on=window_end.date(), now=now)
    installations = portfolio_installation_rows(session, portfolio_id=portfolio.id, on=window_end.date(), now=now)
    asset_ids = asset_ids_for_portfolio(session, portfolio_id=portfolio.id, on=window_end.date())
    opened, resolved = _window_incident_facts(session, asset_ids=asset_ids, window_start=window_start, window_end=window_end)

    new_by_asset = {incident.asset_id for incident in opened}
    for row in installations:
        row["is_new"] = row["asset_id"] in new_by_asset

    # Priority order for "top installations": novidades first, then
    # críticos, then anything with an open incident at all -- never
    # alphabetical, never just "most incidents".
    ranked = sorted(
        (row for row in installations if row["incident_count"] > 0),
        key=lambda row: (not row["is_new"], -row["critical_count"], -row["warning_count"], row["name"] or ""),
    )

    # Dominant backlog rule_codes -- "backlog dominante: stale/unknown
    # histórico" from real counts, not a guess.
    backlog_rule_counts: dict[str, int] = {}
    if asset_ids:
        for row_incident in session.scalars(
            select(DiagnosticIncident).where(
                DiagnosticIncident.asset_id.in_(asset_ids),
                DiagnosticIncident.status == "open",
                DiagnosticIncident.opened_at < window_start,
            )
        ):
            backlog_rule_counts[row_incident.rule_code] = backlog_rule_counts.get(row_incident.rule_code, 0) + 1

    return {
        "portfolio_id": portfolio.id,
        "portfolio_name": portfolio.name,
        "total_installations": summary.total_installations,
        "installations_with_incidents": summary.installations_with_incidents,
        "installations_no_devices": summary.installations_no_devices,
        "installations_full_coverage": summary.installations_full_coverage,
        "incidents_critical": summary.incidents_critical,
        "incidents_warning": summary.incidents_warning,
        "incidents_info": summary.incidents_info,
        "new_count": len(opened),
        "new_critical_count": sum(1 for incident in opened if incident.severity == "critical"),
        "resolved_count": len(resolved),
        # "Incidentes antigos ainda abertos": a real incident count (every
        # open incident whose opened_at predates this window), not an
        # installation count -- exactly the same set backlog_dominant_rules
        # is drawn from, so the two numbers can never disagree with each
        # other.
        "persistent_count": sum(backlog_rule_counts.values()),
        "backlog_dominant_rules": sorted(backlog_rule_counts.items(), key=lambda item: -item[1])[:3],
        "top_installations": [
            {
                "asset_id": row["asset_id"],
                "name": row["name"],
                "critical_count": row["critical_count"],
                "warning_count": row["warning_count"],
                "info_count": row["info_count"],
                "is_new": row["is_new"],
                "oldest_incident_age_days": row["oldest_incident_age_days"],
            }
            for row in ranked[:TOP_INSTALLATIONS_PER_PORTFOLIO]
        ],
    }


def build_digest_payload(session: Session, *, window_start: datetime, window_end: datetime, now: datetime | None = None) -> dict[str, Any]:
    """Everything `render_digest_text` needs -- computed once, stored in
    `DigestRun.summary_json`, never re-derived from a window that has
    already closed."""
    now_value = now or utc_now()
    portfolios = list(session.scalars(select(Portfolio).where(Portfolio.status == "active").order_by(Portfolio.name)))
    per_portfolio = [
        _portfolio_payload(session, portfolio=portfolio, window_start=window_start, window_end=window_end, now=now_value)
        for portfolio in portfolios
    ]
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "portfolios": per_portfolio,
        "totals": {
            "portfolios_included": len(per_portfolio),
            "portfolios_with_incidents": sum(1 for entry in per_portfolio if entry["installations_with_incidents"] > 0),
            "installations_with_incidents": sum(entry["installations_with_incidents"] for entry in per_portfolio),
            "incidents_critical": sum(entry["incidents_critical"] for entry in per_portfolio),
            "incidents_warning": sum(entry["incidents_warning"] for entry in per_portfolio),
            "incidents_info": sum(entry["incidents_info"] for entry in per_portfolio),
            "new_count": sum(entry["new_count"] for entry in per_portfolio),
            "new_critical_count": sum(entry["new_critical_count"] for entry in per_portfolio),
            "resolved_count": sum(entry["resolved_count"] for entry in per_portfolio),
        },
    }


def render_digest_text(payload: dict[str, Any]) -> str:
    """Plain text -- what a Telegram message (D4, not this slice) would
    carry verbatim. Priority order throughout: novidades, críticos, mudanças
    desde o último digest, depois os maiores problemas persistentes -- never
    a flat dump of every incident.
    """
    window_end = datetime.fromisoformat(payload["window_end"])
    window_start = datetime.fromisoformat(payload["window_start"])
    totals = payload["totals"]
    lines = [f"Diagnóstico — {window_end.strftime('%d/%m/%Y %H:%M')}"]

    nothing_changed = totals["new_count"] == 0 and totals["resolved_count"] == 0
    if nothing_changed:
        lines.append(f"Sem alterações desde o último digest ({window_start.strftime('%d/%m/%Y %H:%M')}).")
    else:
        lines.append(
            f"{totals['new_count']} novo(s) · {totals['resolved_count']} resolvido(s) "
            f"desde {window_start.strftime('%d/%m/%Y %H:%M')}."
        )

    lines.append("")
    lines.append("Prioridade:")
    if totals["new_critical_count"] > 0:
        lines.append(f"- {totals['new_critical_count']} ocorrência(s) crítica(s) nova(s)")
    else:
        lines.append("- nenhuma ocorrência crítica nova")
    if totals["new_count"] > totals["new_critical_count"]:
        lines.append(f"- {totals['new_count'] - totals['new_critical_count']} outro(s) incidente(s) novo(s)")
    if totals["resolved_count"] > 0:
        lines.append(f"- {totals['resolved_count']} incidente(s) resolvido(s)")
    dominant: dict[str, int] = {}
    for entry in payload["portfolios"]:
        for rule, count in entry["backlog_dominant_rules"]:
            dominant[rule] = dominant.get(rule, 0) + count
    if dominant:
        top_rules = ", ".join(f"{rule} ({count})" for rule, count in sorted(dominant.items(), key=lambda item: -item[1])[:3])
        lines.append(f"- backlog persistente dominante: {top_rules}")

    for entry in payload["portfolios"]:
        if entry["total_installations"] == 0:
            continue
        lines.append("")
        lines.append(entry["portfolio_name"])
        lines.append(
            f"  {entry['installations_with_incidents']}/{entry['total_installations']} instalações com incidentes"
        )
        lines.append(f"  {entry['new_count']} novos · {entry['resolved_count']} resolvidos")
        severity_bits = []
        if entry["incidents_critical"]:
            severity_bits.append(f"{entry['incidents_critical']} críticos")
        if entry["incidents_warning"]:
            severity_bits.append(f"{entry['incidents_warning']} avisos")
        if entry["incidents_info"]:
            severity_bits.append(f"{entry['incidents_info']} info")
        if severity_bits:
            lines.append(f"  {' · '.join(severity_bits)}")
        if entry["installations_no_devices"]:
            lines.append(f"  {entry['installations_no_devices']} instalações sem dispositivos")
        if entry["top_installations"]:
            lines.append("  Prioritárias:")
            for row in entry["top_installations"]:
                tag = "novo" if row["is_new"] else "persistente"
                counts = f"{row['critical_count']}c/{row['warning_count']}w/{row['info_count']}i"
                age = f", {row['oldest_incident_age_days']:.0f}d" if row["oldest_incident_age_days"] is not None else ""
                lines.append(f"    - {row['name']} ({tag}, {counts}{age})")

    return "\n".join(lines)


# --- decide: recoveries digest (req 13) ----------------------------------------


def build_recovery_digest_payload(
    session: Session, *, window_start: datetime, window_end: datetime, now: datetime | None = None
) -> dict[str, Any]:
    """Episodes that recovered inside this window but do not justify an
    immediate push -- `notifications.eligibility.eligible_for_recovery_digest`
    is the one place that decides which (the same hard floor as the
    immediate path: never one that was never notified, never one already
    sent immediately as a `resolved` `NotificationEvent`). Scoped to
    O&M-active installations, same as every other operational surface (req
    1) -- a recovery at an installation Solcor does not operate is not this
    channel's concern.
    """
    scoped = scoped_asset_ids(session, asset_scope="om_active", on=window_end.date())
    asset_ids = sorted(scoped) if scoped else []
    if not asset_ids:
        return {"window_start": window_start.isoformat(), "window_end": window_end.isoformat(), "recoveries": []}

    closed = session.scalars(
        select(NotificationEpisode).where(
            NotificationEpisode.status == "closed",
            NotificationEpisode.asset_id.in_(asset_ids),
            NotificationEpisode.closed_at.is_not(None),
            NotificationEpisode.closed_at >= window_start,
            NotificationEpisode.closed_at < window_end,
        )
    )
    rows: list[dict[str, Any]] = []
    for episode in closed:
        if not eligible_for_recovery_digest(episode):
            continue
        asset = session.get(Asset, episode.asset_id)
        installation = session.get(Installation, asset.installation_id) if asset and asset.installation_id else None
        name = installation.display_name if installation else (asset.canonical_name if asset else f"asset #{episode.asset_id}")
        duration_minutes = round((episode.closed_at - episode.opened_at).total_seconds() / 60)
        rows.append(
            {
                "asset_id": episode.asset_id,
                "name": name,
                "duration_minutes": duration_minutes,
                "problem_family": episode.problem_family,
            }
        )
    rows.sort(key=lambda row: row["name"] or "")
    return {"window_start": window_start.isoformat(), "window_end": window_end.isoformat(), "recoveries": rows}


def render_recovery_digest_text(payload: dict[str, Any]) -> str:
    window_end = datetime.fromisoformat(payload["window_end"])
    window_start = datetime.fromisoformat(payload["window_start"])
    rows = payload["recoveries"]
    hours = round((window_end - window_start).total_seconds() / 3600)
    if not rows:
        return f"Recuperações — últimas {hours}h\n\nNenhuma recuperação nesta janela."
    lines = [f"Recuperações — últimas {hours}h", "", f"{len(rows)} instalações recuperaram:"]
    for row in rows:
        lines.append(f"- {row['name']} — {duration_label(timedelta(minutes=row['duration_minutes']))}")
    return "\n".join(lines)


# --- decide: morning O&M briefing (reqs 10-12) ----------------------------------

# Worst-first tiebreak when one installation carries more than one open
# episode: the briefing represents an installation once, by its single
# worst problem -- "o que precisa de atenção primeiro *nesta instalação*",
# not a second finding per episode. communication (whole plant dark) outranks
# a confirmed fault, which outranks a coverage gap -- the same ordering
# req 12 asks the categories themselves to respect.
_FAMILY_RANK = {"communication": 0, "fault": 1, "coverage": 2}
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _pick_representative(episodes: list[NotificationEpisode]) -> NotificationEpisode:
    return min(
        episodes,
        key=lambda episode: (
            _FAMILY_RANK.get(episode.problem_family, 1),
            _SEVERITY_RANK.get(episode.severity_peak, 1),
            episode.opened_at,
        ),
    )


def _briefing_row(context: NotificationContext, *, now: datetime) -> dict[str, Any]:
    duration_minutes = round((now - context.episode.opened_at).total_seconds() / 60)
    return {
        "asset_id": context.asset.id,
        "name": context.installation.display_name if context.installation else context.asset.canonical_name,
        "problem_family": context.episode.problem_family,
        "rule_code": context.incident.rule_code,
        "category": context.category,
        "duration_minutes": duration_minutes,
        "installed_dc_power_kw": float(context.asset.installed_dc_power_kw) if context.asset.installed_dc_power_kw else None,
        "energy_impact_kwh": float(context.energy_impact.lost_kwh) if context.energy_impact.lost_kwh is not None else None,
        "financial_impact_eur": float(context.financial_impact.lost_eur) if context.financial_impact.lost_eur is not None else None,
        "priority_score": context.priority.score,
        "priority_bucket": context.priority.bucket,
        "priority_reasons": context.priority.reasons,
        "recurrence_count_24h": context.recurrence_count_24h,
        "has_work_order": context.work_order is not None,
        "work_order_label": f"WO-{context.work_order.id}" if context.work_order else None,
        "work_planned_or_in_progress_today": context.work_planned_or_in_progress_today,
        "is_esco_priority": context.is_esco_priority,
        "contact_name": context.contact_name,
        "contact_role": context.contact_role,
        "contact_phone": context.contact_phone,
        "suggested_action": context.suggested_action,
        "incident_id": context.incident.id,
    }


def build_morning_briefing_payload(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """A snapshot of the O&M-active fleet's current state, ranked by
    `notifications.priority.score_episode` -- the exact same engine and the
    exact same `NotificationContext` (`notifications/enrichment.py::
    build_context`) an immediate Telegram alert is rendered from. Never a
    second ranking: the briefing's "PRIORIDADE ALTA" and an immediate
    critical alert can never disagree about which installation matters more,
    because they are the same function call.

    This is a point-in-time snapshot, not a window aggregation like the
    diagnostics/recoveries digests -- "what does the fleet look like right
    now" has no meaningful "since the last briefing" delta the way "what
    happened in the last 2h" does. `window_start`/`window_end` are still
    recorded (by `generate_digest`'s own chaining, unchanged) purely so this
    kind's `DigestRun` rows stay comparable/orderable with the other two,
    not because the payload summarises the interval between them.

    Known, documented gap (not solved here): an O&M-active asset with *no*
    device and *no* plant reading ever is never evaluated by
    `diagnostics/incidents.py` at all, so it can never have an episode and
    reads as "operational" below -- indistinguishable, with today's data,
    from a genuinely healthy installation. Every asset with at least one
    historical reading already surfaces through `coverage`
    (`stale_reading`/`device_unknown_status`/...), which is the common case;
    a true zero-evidence asset is the one case this briefing cannot yet
    tell apart from "fine". Revisit if that ever turns out to matter in
    practice.
    """
    now_value = now or utc_now()
    empty_counts = {category: 0 for category in CATEGORIES}
    scoped = scoped_asset_ids(session, asset_scope="om_active", on=now_value.date())
    asset_ids = sorted(scoped) if scoped else []
    if not asset_ids:
        return {
            "generated_at": now_value.isoformat(), "om_active_count": 0, "operational_count": 0,
            "category_counts": empty_counts, "high": [], "to_check": [], "recurring": [],
            "work_planned_count": 0, "no_action_count": 0, "prioritized_today_count": 0,
        }

    open_episodes = list(
        session.scalars(
            select(NotificationEpisode).where(
                NotificationEpisode.status == "open", NotificationEpisode.asset_id.in_(asset_ids)
            )
        )
    )
    by_asset: dict[int, list[NotificationEpisode]] = {}
    for episode in open_episodes:
        by_asset.setdefault(episode.asset_id, []).append(episode)

    rows = [
        _briefing_row(build_context(session, episode=_pick_representative(episodes), now=now_value), now=now_value)
        for episodes in by_asset.values()
    ]
    rows.sort(key=lambda row: -row["priority_score"])

    high = [row for row in rows if row["priority_bucket"] == "HIGH"]
    to_check = [row for row in rows if row["priority_bucket"] != "HIGH"]
    # >=3 episodes/24h, req 5's own recurrence threshold -- read straight off
    # `recurrence_count_24h`, never re-derived with a different number here.
    recurring = sorted((row for row in rows if row["recurrence_count_24h"] >= 3), key=lambda row: -row["recurrence_count_24h"])

    category_counts = dict(empty_counts)
    for row in rows:
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1

    return {
        "generated_at": now_value.isoformat(),
        "om_active_count": len(asset_ids),
        "operational_count": len(asset_ids) - len(rows),
        "category_counts": category_counts,
        "high": high,
        "to_check": to_check,
        "recurring": recurring,
        "work_planned_count": sum(1 for row in rows if row["work_planned_or_in_progress_today"]),
        "no_action_count": sum(1 for row in high if not row["has_work_order"]),
        "prioritized_today_count": len(high),
    }


def _impact_text(row: dict[str, Any]) -> str:
    kwh = row["energy_impact_kwh"]
    if kwh is None:
        return "não calculável"
    eur = row["financial_impact_eur"]
    return f"{kwh:g} kWh / ~€{eur:.0f}" if eur is not None else f"{kwh:g} kWh / € não calculável"


def render_morning_briefing_text(payload: dict[str, Any]) -> str:
    generated_at = datetime.fromisoformat(payload["generated_at"])
    lines = ["O&M — Estado do parque", f"{generated_at.strftime('%d/%m/%Y')} · {generated_at.strftime('%H:%M')}"]

    high = payload["high"]
    if high:
        lines.append("")
        lines.append("PRIORIDADE ALTA")
        lines.append("")
        for index, row in enumerate(high, start=1):
            lines.append(f"{index}. {row['name']}")
            state = family_label(problem_family=row["problem_family"], rule_code=row["rule_code"])
            lines.append(f"   {state} há {duration_label(timedelta(minutes=row['duration_minutes']))}")
            if row["installed_dc_power_kw"] is not None:
                lines.append(f"   {row['installed_dc_power_kw']:g} kWp")
            lines.append(f"   Impacto estimado: {_impact_text(row)}")
            lines.append(f"   Trabalho: {row['work_order_label']}" if row["work_order_label"] else "   Sem trabalho aberto")
            if row["suggested_action"]:
                lines.append(f"   Ação: {row['suggested_action'].splitlines()[0]}")
            if row["contact_name"]:
                contact = row["contact_name"] + (f" · {row['contact_role']}" if row["contact_role"] else "")
                lines.append(f"   Contacto: {contact}")
            if index != len(high):
                lines.append("")

    if payload["to_check"]:
        lines.append("")
        lines.append("A VERIFICAR")
        lines.append("")
        for row in payload["to_check"]:
            state = family_label(problem_family=row["problem_family"], rule_code=row["rule_code"])
            lines.append(f"- {row['name']} — {state.lower()} {duration_label(timedelta(minutes=row['duration_minutes']))}")

    if payload["recurring"]:
        lines.append("")
        lines.append("RECORRENTES")
        lines.append("")
        for row in payload["recurring"]:
            lines.append(f"- {row['name']} — {row['recurrence_count_24h']} falhas/24h")

    lines.append("")
    lines.append("RESUMO")
    lines.append("")
    counts = payload["category_counts"]
    lines.append(f"{payload['om_active_count']} contratos O&M ativos")
    lines.append(f"{payload['operational_count']} operacionais")
    lines.append(f"{counts.get('communication_issue', 0)} offline")
    lines.append(f"{counts.get('operational_fault', 0)} fault")
    lines.append(f"{counts.get('monitoring_coverage', 0)} sem dados suficientes")
    lines.append("")
    lines.append(f"{payload['prioritized_today_count']} prioritários hoje")
    lines.append(f"{payload['work_planned_count']} já têm trabalho planeado")
    lines.append(f"{payload['no_action_count']} problema(s) sem ação")

    return "\n".join(lines)


# --- generate: idempotent, restart-safe, one row per window -------------------


# Payload builder + renderer per kind (Fatia 4 adds the last two) -- one
# dispatch table, not three copies of `generate_digest`'s own machinery.
# Every builder takes the same `(session, *, window_start, window_end, now)`
# shape even though `morning_briefing`'s payload is a snapshot *at*
# `window_end` rather than an aggregation *over* the window -- see
# `build_morning_briefing_payload`'s own docstring for why that is still the
# right window semantics to store, not a schema mismatch.
def _builders_for(kind: str) -> tuple[Callable[..., dict[str, Any]], Callable[[dict[str, Any]], str]]:
    if kind == "recoveries":
        return build_recovery_digest_payload, render_recovery_digest_text
    if kind == "morning_briefing":
        return (lambda session, *, window_start, window_end, now: build_morning_briefing_payload(session, now=now)), render_morning_briefing_text
    return build_digest_payload, render_digest_text


def generate_digest(
    session: Session, *, window_end: datetime, interval_minutes: int, kind: str = "diagnostics", now: datetime | None = None
) -> DigestRun | None:
    """One digest of the given `kind` for the window ending at `window_end`.

    `window_start` is the previous `DigestRun` *of the same kind*'s
    `window_end` when one exists -- windows chain per kind, so "since the
    last digest" is always literally true for that kind, never blended with
    another kind's cadence (the diagnostics digest, the 2h recovery
    grouping, and the daily briefing all chain independently). With no
    previous digest of this kind, the window is bootstrapped to exactly one
    cadence interval, so the very first digest is shaped the same as every
    one after it.

    Idempotent by construction, and checked in the right order: asking for
    the *same* `(kind, window_end)` twice must return the *same* row,
    without ever trying to chain a new window off of it -- re-deriving
    `window_start` from "the most recent `DigestRun` of this kind" before
    checking whether `window_end` itself was already generated would find
    that same row as its own "previous" digest on the second call and
    compute an empty or negative window. So the idempotency check comes
    first, keyed on `(kind, window_end)` alone (what the caller is actually
    asking to generate), and only a genuinely new `window_end` ever reaches
    the chaining logic below. A concurrent attempt for a new window still
    loses a `SAVEPOINT`-guarded unique-constraint race gracefully (same
    pattern as D1/D3) rather than aborting the caller's transaction.
    """
    if kind not in DIGEST_KINDS:
        raise ValueError(f"Unknown digest kind: {kind!r}")
    now_value = now or utc_now()

    already = session.scalar(
        select(DigestRun)
        .where(DigestRun.kind == kind, DigestRun.window_end == window_end)
        .order_by(DigestRun.id.desc())
        .limit(1)
    )
    if already is not None:
        return already

    previous = session.scalar(
        select(DigestRun).where(DigestRun.kind == kind).order_by(DigestRun.window_end.desc()).limit(1)
    )
    window_start = previous.window_end if previous is not None else window_end - timedelta(minutes=interval_minutes)
    if window_end <= window_start:
        return None

    build_payload, render_text = _builders_for(kind)
    payload = build_payload(session, window_start=window_start, window_end=window_end, now=now_value)
    text = render_text(payload)
    digest = DigestRun(
        kind=kind,
        window_start=window_start,
        window_end=window_end,
        generated_at=now_value,
        summary_json=payload,
        rendered_text=text,
        delivery_status="pending",
        delivery_attempt_count=0,
        created_at=now_value,
        updated_at=now_value,
    )
    try:
        with session.begin_nested():
            session.add(digest)
            session.flush()
    except IntegrityError:
        # A concurrent generator won this exact window first.
        return session.scalar(
            select(DigestRun).where(
                DigestRun.kind == kind, DigestRun.window_start == window_start, DigestRun.window_end == window_end
            )
        )
    return digest


# --- deliver: mock-only, D3's exact discipline ---------------------------------


@dataclass(frozen=True)
class DigestDeliveryResult:
    attempted: bool
    delivered: bool


def deliver_digest(
    session_factory: sessionmaker[Session],
    *,
    digest_run_id: int,
    now: datetime | None = None,
    client_factory: Callable[[NotificationChannel], TelegramClient] = _default_client_factory,
    notifications_enabled: bool | None = None,
) -> DigestDeliveryResult:
    """Attempt delivery for one digest, one transaction, committed
    immediately -- the same reasoning as D3's `deliver_pending_notifications`:
    a real send is an external side effect, so a crash must never be able to
    replay it. No channel configured (today's real state) or a disabled one
    means this never reaches a client at all, mock or otherwise.
    """
    # The same global kill switch `deliver_pending_notifications` honours, and
    # for the same reason: a digest is an outbound Telegram message like any
    # other. Nothing is attempted, so the digest stays `pending` and is
    # delivered if and when the switch comes back on.
    if notifications_enabled is None:
        notifications_enabled = external_capability_enabled("notifications")
    if not notifications_enabled:
        return DigestDeliveryResult(attempted=False, delivered=False)

    now_value = now or utc_now()
    with session_factory() as session, session.begin():
        digest = session.get(DigestRun, digest_run_id)
        if digest is None or digest.delivery_status not in ("pending", "failed"):
            return DigestDeliveryResult(attempted=False, delivered=False)
        if digest.channel_id is None:
            return DigestDeliveryResult(attempted=False, delivered=False)
        channel = session.get(NotificationChannel, digest.channel_id)
        if channel is None or not channel.enabled:
            return DigestDeliveryResult(attempted=False, delivered=False)

        client = client_factory(channel)
        digest.delivery_attempt_count += 1
        digest.updated_at = now_value
        result = client.send_message(chat_id=channel.target_chat_id or "", text=digest.rendered_text)
        if result.delivered:
            digest.delivery_status = "delivered"
            digest.delivered_at = now_value
            digest.last_error = None
            return DigestDeliveryResult(attempted=True, delivered=True)
        digest.delivery_status = "failed"
        digest.last_error = result.error
        return DigestDeliveryResult(attempted=True, delivered=False)
