"""What the platform does on its own, and who can change it.

Two kinds of automation live here and the difference is the point of the
screen. The schedulers are environment variables read once at process start,
so this module reports their state and their last run and offers no control --
a toggle that quietly did nothing would be worse than no toggle. The
notification channel and its policies are database rows, so those really can be
switched, and switching them is audited.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.jobs.models import ScheduleState
from nemsei.notifications.models import (
    NOTIFICATION_SEVERITIES,
    DigestRun,
    NotificationChannel,
    NotificationEvent,
    NotificationPolicy,
)
from nemsei.providers.audit import record_operator_action
from nemsei.shared.clock import utc_now


@dataclass(frozen=True)
class ScheduledAutomation:
    """One background automation, described by what it actually did.

    Deliberately **not** described by `Settings`. The schedulers run in the
    `scheduler` container and their flags are that container's environment;
    the web process does not have them, so reading its own `Settings` here
    reported "Sincronização de produção: desligada" while the scheduler was
    running it on time. That was found against production, not in review.

    `schedule_state` is evidence the scheduler itself wrote: a row exists only
    because the schedule ran, and `next_run_at` is the scheduler's own
    statement of when it intends to run again. Reading those needs no
    knowledge of anyone else's environment and cannot disagree with reality.
    """

    key: str
    label: str
    purpose: str
    setting_name: str
    schedule_key: str | None
    next_run_at: datetime | None
    last_enqueued_at: datetime | None

    @property
    def state(self) -> str:
        """`unseen` (never ran), `overdue` (its own next run is past) or `running`."""
        if self.schedule_key is None or self.last_enqueued_at is None:
            return "unseen"
        # Five minutes of grace: the scheduler ticks on its own loop and a
        # schedule due seconds ago is not a stopped schedule.
        if self.next_run_at is not None and self.next_run_at < utc_now() - timedelta(minutes=5):
            return "overdue"
        return "running"


DEFINITIONS = (
    ("production.incremental", "Sincronização de produção", "Lê a produção diária do provider e escreve factos.", "NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED"),
    ("device_status.poll", "Sonda de dispositivos", "Lê o estado e a potência de cada inversor.", "NEMSEI_V2_DEVICE_STATUS_POLL_ENABLED"),
    ("diagnostics.evaluate_incidents", "Avaliação de incidentes", "Reavalia as regras e abre ou fecha incidentes. Não faz chamadas ao provider.", "NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_ENABLED"),
    ("notifications.process", "Processamento de notificações", "Decide e entrega notificações segundo as políticas.", "NEMSEI_V2_NOTIFICATION_PROCESSING_ENABLED"),
    ("digest.generate", "Digest periódico", "Resume os incidentes desde o digest anterior.", "NEMSEI_V2_DIGEST_GENERATION_ENABLED"),
    ("system.noop.hourly", "Pulsação do agendador", "Trabalho horário sem efeito, que prova que o agendador está vivo.", "—"),
)


def scheduled_automations(session: Session) -> list[ScheduledAutomation]:
    """Every known automation, matched to whatever `schedule_state` proves.

    Keys are matched by prefix because the two provider-bound schedules carry
    the connection id in the key (`production.incremental:3`), and the web
    process has no way to know which connection the scheduler was pointed at.
    """
    states = list(session.scalars(select(ScheduleState).order_by(ScheduleState.schedule_key)))
    out: list[ScheduledAutomation] = []
    for key, label, purpose, setting_name in DEFINITIONS:
        state = next((row for row in states if row.schedule_key == key or row.schedule_key.startswith(f"{key}:")), None)
        out.append(
            ScheduledAutomation(
                key=key,
                label=label,
                purpose=purpose,
                setting_name=setting_name,
                schedule_key=state.schedule_key if state else None,
                next_run_at=state.next_run_at if state else None,
                last_enqueued_at=state.last_enqueued_at if state else None,
            )
        )
    return out


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
        counts[policy.id] = int(session.scalar(statement) or 0)
    return counts


def automations_overview(session: Session) -> dict[str, Any]:
    channels = list(session.scalars(select(NotificationChannel).order_by(NotificationChannel.id)))
    policies = list(session.scalars(select(NotificationPolicy).order_by(NotificationPolicy.id)))
    event_counts = dict(
        session.execute(select(NotificationEvent.status, func.count(NotificationEvent.id)).group_by(NotificationEvent.status)).all()
    )
    return {
        "scheduled": scheduled_automations(session),
        "channels": channels,
        "policies": policies,
        "channels_by_id": {channel.id: channel for channel in channels},
        "scope_counts": policy_scope_counts(session),
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


def digest_preview_window(*, interval_minutes: int) -> tuple[datetime, datetime]:
    """The window `build_digest_payload` would use for a run right now."""
    now = utc_now()
    return now - timedelta(minutes=interval_minutes), now
