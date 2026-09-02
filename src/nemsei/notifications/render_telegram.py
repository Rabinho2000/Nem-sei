"""Compact Telegram text -- pure, no database, no network (req 9).

Takes a `NotificationContext` (`notifications/enrichment.py`) plus the kind
of moment being rendered and produces the message text stored on
`NotificationEvent.message` and, separately, actually sent by
`telegram_client.py`. Density matches req 9's example; the exact layout does
not have to.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from nemsei.notifications.enrichment import NotificationContext

_FAMILY_LABELS = {
    "communication": "Sem comunicação",
    "fault": "Avaria",
    "coverage": "Cobertura insuficiente",
}

# Finer than the family label above, for the handful of `fault`-family
# rule_codes that are specifically a production shortfall rather than a
# hard equipment alarm -- `diagnostics.incident_categories` correctly files
# both under `fault` (a confirmed production loss is a real fault, not a
# fourth category), but "Perda de produção" reads better than "Avaria" in
# the message title for these specifically. A rule_code-level override, the
# same pattern `playbook.py` already uses for the same reason.
_RULE_CODE_LABELS = {
    "zero_power_while_peers_active": "Perda de produção",
    "power_disparity_among_peers": "Perda de produção",
    "daily_energy_disparity_among_peers": "Perda de produção",
    "zero_production_in_productive_window": "Perda de produção",
}

_OPEN_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
_ESCALATE_ICON = "⏫"
_REMINDER_ICON = "🔁"
_RESOLVE_ICON = "🟢"


def render_message(
    context: NotificationContext, *, kind: str, now: datetime, base_url: str | None = None
) -> str:
    if kind == "resolved":
        return _render_recovery(context, now=now, base_url=base_url)
    if kind == "reminder":
        return _render_followup(context, now=now, base_url=base_url, icon=_REMINDER_ICON, heading="Ainda aberto")
    if kind == "escalated":
        return _render_followup(context, now=now, base_url=base_url, icon=_ESCALATE_ICON, heading="Escalado")
    return _render_immediate(context, now=now, base_url=base_url)


def _installation_name(context: NotificationContext) -> str:
    return context.installation.display_name if context.installation else context.asset.canonical_name


def _family_label(context: NotificationContext) -> str:
    override = _RULE_CODE_LABELS.get(context.incident.rule_code)
    if override is not None:
        return override
    return _FAMILY_LABELS.get(context.episode.problem_family, context.episode.problem_family)


def _duration_label(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, remainder = divmod(minutes, 60)
    if hours:
        return f"{hours}h{remainder:02d}"
    return f"{minutes}min"


def _contract_badges(context: NotificationContext) -> str:
    badges = []
    if context.is_om:
        badges.append("O&M")
    if context.is_esco_priority:
        badges.append("ESCO")
    return " + ".join(badges) if badges else context.contract_family_label


def _impact_line(context: NotificationContext) -> str:
    kwh = context.energy_impact.lost_kwh
    eur = context.financial_impact.lost_eur
    if kwh is None:
        return "Impacto estimado: não calculável"
    kwh_text = f"{kwh:g} kWh"
    if eur is not None:
        return f"Impacto estimado: {kwh_text} / ~€{eur:.0f}"
    return f"Impacto estimado: {kwh_text} / € não calculável"


def _work_line(context: NotificationContext) -> str:
    if context.work_order is None:
        return "Trabalho aberto: não"
    label = f"WO-{context.work_order.id}"
    if context.work_order.planned_date:
        label += f" · visita {context.work_order.planned_date.strftime('%d/%m')}"
    return f"Trabalho aberto: {label}"


def _contact_block(context: NotificationContext) -> str:
    if context.contact_name is None:
        return "Contacto local: não registado"
    label = context.contact_name
    if context.contact_role:
        label = f"{label} · {context.contact_role}"
    lines = ["Contacto local:", label]
    if context.contact_phone:
        lines.append(context.contact_phone)
    return "\n".join(lines)


def _links_line(context: NotificationContext, *, base_url: str | None) -> str | None:
    if not base_url:
        return None
    root = base_url.rstrip("/")
    installation_url = f"{root}/diagnostics/assets/{context.asset.id}"
    incident_url = f"{root}/diagnostics/incidents/{context.incident.id}"
    return f"[Instalação]({installation_url}) [Incidente]({incident_url})"


def _render_immediate(context: NotificationContext, *, now: datetime, base_url: str | None) -> str:
    icon = _OPEN_ICON.get(context.episode.severity_peak, "🟠")
    lines = [f"{icon} {_installation_name(context)} — {_family_label(context)}", ""]

    age = now - context.episode.opened_at
    if context.episode.problem_family == "communication":
        lines.append(f"Offline há {_duration_label(age)}")
    else:
        lines.append(f"Aberto há {_duration_label(age)}")
    power = f"{context.asset.installed_dc_power_kw:g} kWp" if context.asset.installed_dc_power_kw else None
    badges = _contract_badges(context)
    lines.append(" · ".join(part for part in (power, badges) if part))
    lines.append("")
    lines.append(f"Últimos dados: {context.incident.last_observed_at.strftime('%H:%M')}")
    lines.append(_impact_line(context))
    lines.append(f"Incidente: #{context.incident.id}")
    lines.append(_work_line(context))
    lines.append("")
    lines.append("Ação sugerida:")
    lines.append(context.suggested_action)
    lines.append("")
    lines.append(_contact_block(context))

    links = _links_line(context, base_url=base_url)
    if links:
        lines.append("")
        lines.append(links)
    return "\n".join(lines)


def _render_followup(
    context: NotificationContext, *, now: datetime, base_url: str | None, icon: str, heading: str
) -> str:
    age = now - context.episode.opened_at
    lines = [
        f"{icon} {_installation_name(context)} — {_family_label(context)} ({heading.lower()}, há {_duration_label(age)})",
        "",
        _impact_line(context),
        f"Incidente: #{context.incident.id}",
        _work_line(context),
        "",
        "Ação sugerida:",
        context.suggested_action,
    ]
    links = _links_line(context, base_url=base_url)
    if links:
        lines.append("")
        lines.append(links)
    return "\n".join(lines)


def _render_recovery(context: NotificationContext, *, now: datetime, base_url: str | None) -> str:
    end = context.episode.closed_at or now
    duration = _duration_label(end - context.episode.opened_at)
    lines = [
        f"{_RESOLVE_ICON} {_installation_name(context)} — {_family_label(context)} recuperado",
        "",
        f"Durou {duration}" + (f" · {context.episode.flap_count} ocorrências" if context.episode.flap_count > 1 else ""),
        _impact_line(context),
        f"Incidente: #{context.incident.id}",
    ]
    links = _links_line(context, base_url=base_url)
    if links:
        lines.append("")
        lines.append(links)
    return "\n".join(lines)
