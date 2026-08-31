"""What the platform does on its own, and who can change it.

Two kinds of automation live here and the difference is the point of the
screen. The schedulers are environment variables read once at process start,
so this module reports their state and their last run and offers no control --
a toggle that quietly did nothing would be worse than no toggle. The
notification channel and its policies are database rows, so those really can be
switched, and switching them is audited.

The scheduler half of that lives in `system/automation_health.py` now. It grew
past a label and a timestamp: reporting a schedule as "a correr" because the
scheduler enqueued something is not the same claim as reporting that the work
succeeded, and this screen made that mistake for both of the failures found on
2026-08-31.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.contracts.service import scoped_asset_ids
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.models import (
    NOTIFICATION_ASSET_SCOPES,
    NOTIFICATION_SEVERITIES,
    DigestRun,
    NotificationChannel,
    NotificationEvent,
    NotificationPolicy,
)
from nemsei.providers.audit import record_operator_action
from nemsei.shared.clock import utc_now
from nemsei.system.automation_health import automations, listener_processes, scheduler_pulse


# What each scope means on screen. V1 wrote these as `only_o&m` and
# `only_active_contracts` in a settings dropdown; the wording here says which
# installations are addressed, not which flag is read.
ASSET_SCOPE_LABELS = {
    "all": "Todo o parque",
    "om": "Só O&M (inclui contratos expirados)",
    "om_active": "Só O&M com contrato em vigor",
}


def policy_scope_counts(session: Session) -> dict[int, int]:
    """How many open incidents each enabled policy currently has in scope.

    Deliberately an approximation, and labelled as one on screen: it applies
    the severity floor and the baseline, which is what an operator can reason
    about, but not the escalation timing or the prior-event checks that
    `_decide_for_policy` also weighs. Running the real decision would write
    rows, and a preview must never do that.
    """
    counts: dict[int, int] = {}
    for policy in session.scalars(select(NotificationPolicy)):
        floor = NOTIFICATION_SEVERITIES.index(policy.min_severity) if policy.min_severity in NOTIFICATION_SEVERITIES else 0
        in_scope = [severity for severity in NOTIFICATION_SEVERITIES[floor:]]
        statement = select(func.count(DiagnosticIncident.id)).where(
            DiagnosticIncident.status == "open",
            DiagnosticIncident.severity.in_(in_scope),
        )
        if policy.baseline_at is not None:
            statement = statement.where(DiagnosticIncident.opened_at >= policy.baseline_at)
        # The asset scope is part of what the operator can reason about, so it
        # belongs in the preview. Without it the column would report the whole
        # fleet's incidents for a policy that speaks only for the O&M portfolio.
        scoped = scoped_asset_ids(session, asset_scope=policy.asset_scope)
        if scoped is not None:
            if not scoped:
                counts[policy.id] = 0
                continue
            statement = statement.where(DiagnosticIncident.asset_id.in_(scoped))
        counts[policy.id] = int(session.scalar(statement) or 0)
    return counts


def automations_overview(session: Session) -> dict[str, Any]:
    channels = list(session.scalars(select(NotificationChannel).order_by(NotificationChannel.id)))
    policies = list(session.scalars(select(NotificationPolicy).order_by(NotificationPolicy.id)))
    event_counts = dict(
        session.execute(select(NotificationEvent.status, func.count(NotificationEvent.id)).group_by(NotificationEvent.status)).all()
    )
    rows = automations(session)
    return {
        "scheduled": rows,
        "pulse": scheduler_pulse(rows),
        "listeners": listener_processes(session),
        "channels": channels,
        "policies": policies,
        "channels_by_id": {channel.id: channel for channel in channels},
        "scope_counts": policy_scope_counts(session),
        "asset_scope_options": [(value, ASSET_SCOPE_LABELS[value]) for value in NOTIFICATION_ASSET_SCOPES],
        "asset_scope_labels": ASSET_SCOPE_LABELS,
        "event_counts": {str(status): int(count) for status, count in event_counts.items()},
        "digests": list(session.scalars(select(DigestRun).order_by(DigestRun.id.desc()).limit(5))),
        "open_incidents": int(session.scalar(select(func.count(DiagnosticIncident.id)).where(DiagnosticIncident.status == "open")) or 0),
    }


def set_channel_enabled(session: Session, *, channel_id: int, enabled: bool, actor: str) -> NotificationChannel:
    """Flip the structural kill switch, refusing a state that cannot work.

    Enabling a channel with nowhere to deliver is not a half-configured
    channel, it is a channel that will fail on every attempt -- so it is
    refused here rather than discovered later in a delivery error.
    """
    channel = session.get(NotificationChannel, channel_id)
    if channel is None:
        raise ValueError("Canal desconhecido.")
    if enabled and not (channel.target_chat_id or "").strip():
        raise ValueError("Este canal não tem destino configurado, por isso ativá-lo não entregaria nada.")
    if channel.enabled != enabled:
        channel.enabled = enabled
        channel.updated_at = utc_now()
        record_operator_action(
            session,
            actor_username=actor,
            action="automation_enabled" if enabled else "automation_disabled",
            entity_type="notification_channel",
            entity_id=channel.id,
        )
    return channel


def set_policy_enabled(session: Session, *, policy_id: int, enabled: bool, actor: str) -> NotificationPolicy:
    policy = session.get(NotificationPolicy, policy_id)
    if policy is None:
        raise ValueError("Política desconhecida.")
    if policy.enabled != enabled:
        policy.enabled = enabled
        policy.updated_at = utc_now()
        record_operator_action(
            session,
            actor_username=actor,
            action="automation_enabled" if enabled else "automation_disabled",
            entity_type="notification_policy",
            entity_id=policy.id,
        )
    return policy


def set_policy_asset_scope(session: Session, *, policy_id: int, asset_scope: str, actor: str) -> NotificationPolicy:
    """Point a policy at part of the fleet.

    Narrowing is always safe: it can only ever remove candidates. Widening is
    not a storm either, because `baseline_at` and the baseline snapshots
    already decide what counts as pre-existing history -- an installation
    entering scope with an incident older than the baseline stays quiet.
    """
    if asset_scope not in NOTIFICATION_ASSET_SCOPES:
        raise ValueError("Âmbito de centrais desconhecido.")
    policy = session.get(NotificationPolicy, policy_id)
    if policy is None:
        raise ValueError("Política desconhecida.")
    if policy.asset_scope != asset_scope:
        policy.asset_scope = asset_scope
        policy.updated_at = utc_now()
        record_operator_action(
            session,
            actor_username=actor,
            action="automation_scope_changed",
            entity_type="notification_policy",
            entity_id=policy.id,
            metadata={"asset_scope": asset_scope},
        )
    return policy


def digest_preview_window(*, interval_minutes: int) -> tuple[datetime, datetime]:
    """The window `build_digest_payload` would use for a run right now."""
    now = utc_now()
    return now - timedelta(minutes=interval_minutes), now
