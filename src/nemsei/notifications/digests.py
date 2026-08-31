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

from nemsei.config import external_capability_enabled
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import DigestRun, NotificationChannel
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


# --- generate: idempotent, restart-safe, one row per window -------------------


def generate_digest(session: Session, *, window_end: datetime, interval_minutes: int, now: datetime | None = None) -> DigestRun | None:
    """One digest for the window ending at `window_end`.

    `window_start` is the previous `DigestRun.window_end` when one exists --
    windows chain, so "since the last digest" is always literally true, not
    approximated. With no previous digest, the window is bootstrapped to
    exactly one cadence interval, so the very first digest is shaped the
    same as every one after it, not an arbitrarily different size.

    Idempotent by construction, and checked in the right order: asking for
    the *same* `window_end` twice must return the *same* row, without ever
    trying to chain a new window off of it -- re-deriving `window_start`
    from "the most recent `DigestRun`" before checking whether `window_end`
    itself was already generated would find that same row as its own
    "previous" digest on the second call and compute an empty or negative
    window. So the idempotency check comes first, keyed on `window_end`
    alone (what the caller is actually asking to generate), and only a
    genuinely new `window_end` ever reaches the chaining logic below. A
    concurrent attempt for a new window still loses a `SAVEPOINT`-guarded
    unique-constraint race gracefully (same pattern as D1/D3) rather than
    aborting the caller's transaction.
    """
    now_value = now or utc_now()

    already = session.scalar(
        select(DigestRun).where(DigestRun.window_end == window_end).order_by(DigestRun.id.desc()).limit(1)
    )
    if already is not None:
        return already

    previous = session.scalar(select(DigestRun).order_by(DigestRun.window_end.desc()).limit(1))
    window_start = previous.window_end if previous is not None else window_end - timedelta(minutes=interval_minutes)
    if window_end <= window_start:
        return None

    payload = build_digest_payload(session, window_start=window_start, window_end=window_end, now=now_value)
    text = render_digest_text(payload)
    digest = DigestRun(
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
            select(DigestRun).where(DigestRun.window_start == window_start, DigestRun.window_end == window_end)
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
