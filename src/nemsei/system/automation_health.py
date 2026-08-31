"""What each automation is actually doing, as two separate questions.

The screen this feeds used to answer one question and present it as the other.
It read `schedule_state.last_enqueued_at` and `next_run_at` -- which is the
scheduler saying "I put a job on the queue" -- and rendered that as **a
correr**. Whether the job then ran, and whether it worked, was never asked. So
the FusionSolar production sync could fail every single day from 2026-08-24 to
2026-08-30 while the row stayed green, and the diagnostic-incident evaluator
could be dropped from a deploy entirely and take hours to notice.

So there are two dimensions here and they are never merged:

* **Scheduler health** -- is anything still enqueueing this? Evidence:
  `schedule_state`, written by the scheduler itself.
* **Execution health** -- did the work succeed? Evidence: the `jobs` rows the
  schedule produced, and the `sync_runs` behind them.

They fail independently and they are fixed differently. A schedule that stopped
is a deploy or configuration problem; a schedule that fires perfectly into a
job that fails every time is a provider or data problem.

The heartbeat makes one more distinction possible. `system.noop.hourly` exists
to prove the scheduler loop is alive, so an overdue schedule means one of two
very different things: if the heartbeat is fresh, the scheduler is running and
deliberately not enqueueing *this* one -- which is what a switch left off, or
an override dropped from a deploy, looks like from the outside. If the
heartbeat is stale too, nothing can be concluded about any individual
automation, and saying so is more useful than blaming each of them in turn.

Rows are derived from the `schedule_state` rows that exist, not from a list
written here. A catalogue supplies wording for keys it recognises, and an
unrecognised key still gets a row -- named after itself -- because the
alternative is a real automation running invisibly. That is also why the
connection id stays in the key: `production.incremental:3` (FusionSolar) and
`production.incremental:5` (Sigenergy) are two automations with two accounts,
two rate limits and two failure modes, and folding them into one
"Sincronização de produção" threw away which of them was broken.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.jobs.models import Job, ScheduleState
from nemsei.providers.models import ProviderConnection
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sync.models import SyncRun


HEARTBEAT_KEY = "system.noop.hourly"

# The scheduler ticks on its own loop, so a schedule due seconds ago is not a
# stopped schedule. Five minutes is the same grace the previous version used.
SCHEDULE_GRACE = timedelta(minutes=5)

# Terminal job statuses, and the subset that counts as a failure when walking
# back through history. `partial` is terminal but not a failure: it means some
# of the work landed, which is a degradation, not a stop.
TERMINAL_STATUSES = ("success", "partial", "failed", "cancelled")
FAILURE_STATUSES = ("failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running", "waiting")


@dataclass(frozen=True)
class ScheduleDescription:
    label: str
    purpose: str
    setting_name: str
    # The `sync_runs.capability` this schedule drives, when it drives one.
    # Purely local work (incident evaluation, digests, the sweep) has none.
    capability: str | None = None


# Wording for the keys this codebase knows how to produce. Everything here is
# matched on the part of the key before the connection id, so a new connection
# needs no entry.
CATALOGUE: dict[str, ScheduleDescription] = {
    "production.incremental": ScheduleDescription(
        "Sincronização de produção",
        "Lê a produção diária do provider e escreve factos.",
        "NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED",
        capability="production_history",
    ),
    "monitoring.current": ScheduleDescription(
        "Estado das centrais",
        "Lê o estado corrente de cada central e regista a observação.",
        "NEMSEI_V2_CURRENT_MONITORING_SCHEDULER_ENABLED",
        capability="current_monitoring",
    ),
    "device_status.poll": ScheduleDescription(
        "Sonda de dispositivos",
        "Lê o estado e a potência de cada inversor.",
        "NEMSEI_V2_DEVICE_STATUS_POLL_ENABLED",
        capability="device_monitoring",
    ),
    "diagnostics.evaluate_incidents": ScheduleDescription(
        "Avaliação de incidentes",
        "Reavalia as regras e abre ou fecha incidentes. Não faz chamadas ao provider.",
        "NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_ENABLED",
    ),
    "notifications.process": ScheduleDescription(
        "Processamento de notificações",
        "Decide e entrega notificações segundo as políticas.",
        "NEMSEI_V2_NOTIFICATION_PROCESSING_ENABLED",
    ),
    "digests.generate": ScheduleDescription(
        "Digest periódico",
        "Resume os incidentes desde o digest anterior.",
        "NEMSEI_V2_DIGEST_GENERATION_ENABLED",
    ),
    "sync_runs.sweep_abandoned": ScheduleDescription(
        "Limpeza de sincronizações abandonadas",
        "Fecha corridas cujo processo dono já não pode terminá-las.",
        "NEMSEI_V2_SYNC_RUN_SWEEP_ENABLED",
    ),
    "huawei_scada.rollup": ScheduleDescription(
        "Consolidação Huawei SCADA",
        "Converte as amostras recebidas do dongle em energia diária.",
        "NEMSEI_V2_HUAWEI_SCADA_ROLLUP_ENABLED",
    ),
    "huawei_scada.retention": ScheduleDescription(
        "Retenção Huawei SCADA",
        "Apaga amostras de dias cuja energia já está fechada.",
        "NEMSEI_V2_HUAWEI_SCADA_RETENTION_ENABLED",
    ),
    HEARTBEAT_KEY: ScheduleDescription(
        "Pulsação do agendador",
        "Trabalho horário sem efeito, que prova que o agendador está vivo.",
        "—",
    ),
}


# Headline statuses. The vocabulary is deliberately small and each value means
# one thing an operator can act on.
DESLIGADA = "desligada"
NUNCA_EXECUTADA = "nunca_executada"
AGENDADA = "agendada"
OK = "ok"
A_EXECUTAR = "a_executar"
DEGRADADA = "degradada"
FALHOU = "falhou"
ATRASADA = "atrasada"

STATUS_LABELS = {
    DESLIGADA: "desligada",
    NUNCA_EXECUTADA: "nunca executada",
    AGENDADA: "agendada",
    OK: "ok",
    A_EXECUTAR: "a executar",
    DEGRADADA: "degradada",
    FALHOU: "falhou",
    ATRASADA: "atrasada",
}

# How each status renders. `warning` is "look at this", `danger` is "this is
# broken now", `muted` is "nothing is claimed".
STATUS_TONES = {
    DESLIGADA: "muted",
    NUNCA_EXECUTADA: "muted",
    AGENDADA: "muted",
    OK: "success",
    A_EXECUTAR: "success",
    DEGRADADA: "warning",
    FALHOU: "danger",
    ATRASADA: "warning",
}


@dataclass(frozen=True)
class SchedulerHealth:
    """Is anything still enqueueing this, and can that even be answered."""

    state: str  # "ativa" | "atrasada" | "sem_registo"
    next_run_at: datetime | None
    last_enqueued_at: datetime | None
    overdue_by: timedelta | None
    # True when the heartbeat is stale, so an overdue schedule says nothing
    # about this automation in particular.
    scheduler_suspect: bool


@dataclass(frozen=True)
class ExecutionHealth:
    """Did the work run, and did it work."""

    state: str  # "ok" | "degradada" | "falhou" | "a_executar" | "nunca_executada" | "agendada"
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_result: str | None
    consecutive_failures: int
    running_since: datetime | None
    queued: bool
    failure_reason: str | None


@dataclass(frozen=True)
class Automation:
    schedule_key: str
    label: str
    purpose: str
    setting_name: str
    connection_id: int | None
    connection_label: str | None
    provider_code: str | None
    scheduler: SchedulerHealth
    execution: ExecutionHealth
    status: str
    is_heartbeat: bool

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def tone(self) -> str:
        return STATUS_TONES[self.status]


def split_key(schedule_key: str) -> tuple[str, int | None]:
    """`production.incremental:3` -> (`production.incremental`, 3).

    Only a trailing all-digits segment is a connection id; `system.noop.hourly`
    keeps its whole name.
    """
    prefix, separator, suffix = schedule_key.rpartition(":")
    if separator and suffix.isdigit():
        return prefix, int(suffix)
    return schedule_key, None


def describe(schedule_key: str) -> ScheduleDescription:
    """Wording for a key, inventing nothing for one this codebase does not know.

    An unknown key is still a real automation that is really running, so it
    gets a row named after itself rather than being dropped -- which is how
    `monitoring.current:3` and `monitoring.current:5` stayed off this screen
    while both ran every fifteen minutes.
    """
    prefix, _connection_id = split_key(schedule_key)
    known = CATALOGUE.get(prefix)
    if known is not None:
        return known
    return ScheduleDescription(schedule_key, "Agendamento sem descrição nesta versão do código.", "—")


def _safe_reason(job: Job | None, run: SyncRun | None) -> str | None:
    """A summary of the last failure that cannot carry a secret.

    `jobs.error_message` is never shown. Most of them are code-authored
    sentences, but the column also catches arbitrary exceptions, and an
    exception from the database layer can carry a connection string. What is
    shown instead is the exception's class name -- always safe -- and the sync
    run's `error_code` and `safe_detail`, which exist precisely because
    somebody already decided what is safe to repeat about a provider failure.
    """
    parts: list[str] = []
    if run is not None and run.error_code:
        parts.append(run.error_code)
    if run is not None and run.safe_detail:
        parts.append(run.safe_detail)
    if not parts and job is not None and job.error_type:
        parts.append(job.error_type)
    return " · ".join(parts) or None


def _jobs_for(session: Session, schedule_key: str, *, limit: int = 40) -> list[Job]:
    """Every schedule stamps its key into the job's dedupe key, so this is exact.

    Matching on `job_type` alone would merge the two production connections
    back together, which is the collapse this module exists to undo.
    """
    return list(
        session.scalars(
            select(Job)
            .where(Job.dedupe_key.startswith(f"{schedule_key}:"))
            .order_by(Job.id.desc())
            .limit(limit)
        )
    )


def _latest_run(session: Session, *, connection_id: int, capability: str) -> SyncRun | None:
    return session.scalars(
        select(SyncRun)
        .where(SyncRun.provider_connection_id == connection_id, SyncRun.capability == capability)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()


def scheduler_health(state: ScheduleState, *, now: datetime, heartbeat_stale: bool) -> SchedulerHealth:
    next_run = as_utc(state.next_run_at) if state.next_run_at else None
    overdue_by = None
    schedule_state = "ativa"
    if next_run is None:
        schedule_state = "sem_registo"
    elif next_run < now - SCHEDULE_GRACE:
        schedule_state = "atrasada"
        overdue_by = now - next_run
    return SchedulerHealth(
        state=schedule_state,
        next_run_at=next_run,
        last_enqueued_at=as_utc(state.last_enqueued_at) if state.last_enqueued_at else None,
        overdue_by=overdue_by,
        scheduler_suspect=heartbeat_stale and schedule_state == "atrasada",
    )


def execution_health(jobs: list[Job], run: SyncRun | None) -> ExecutionHealth:
    """Read the job history newest-first, which is the order it is decided in."""
    running = next((job for job in jobs if job.status == "running"), None)
    queued = any(job.status in ("queued", "waiting") for job in jobs)
    terminal = [job for job in jobs if job.status in TERMINAL_STATUSES]
    attempted = [job for job in jobs if job.started_at is not None]

    last_attempt_at = as_utc(attempted[0].started_at) if attempted else None
    successful = next((job for job in terminal if job.status == "success"), None)
    last_success_at = as_utc(successful.finished_at) if successful and successful.finished_at else None
    last_result = terminal[0].status if terminal else None

    # Consecutive failures: walk back from the newest terminal job and stop at
    # anything that was not a failure. A `partial` breaks the streak because it
    # is not a stop -- it is counted as a degradation instead.
    consecutive = 0
    for job in terminal:
        if job.status in FAILURE_STATUSES:
            consecutive += 1
        else:
            break

    failed = next((job for job in terminal if job.status in FAILURE_STATUSES), None)
    reason = _safe_reason(failed, run) if consecutive or (run is not None and run.error_code) else None

    if running is not None:
        state = A_EXECUTAR
    elif not terminal:
        state = AGENDADA if queued else NUNCA_EXECUTADA
    elif consecutive:
        state = FALHOU
    elif last_result == "partial" or (run is not None and run.status in ("partial", "rate_limited", "deferred")):
        state = DEGRADADA
    else:
        state = OK

    return ExecutionHealth(
        state=state,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
        last_result=last_result,
        consecutive_failures=consecutive,
        running_since=as_utc(running.started_at) if running and running.started_at else None,
        queued=queued,
        failure_reason=reason,
    )


def headline(scheduler: SchedulerHealth, execution: ExecutionHealth) -> str:
    """One status, from the two dimensions, with the scheduler asked first.

    Order matters and it is not arbitrary. An automation nothing is enqueueing
    has no execution health worth reporting -- its last run may have been a
    perfect success three weeks ago, and reporting **ok** for that is exactly
    the failure this module was written to end.

    `desligada` versus `atrasada` is the heartbeat's distinction: with a live
    scheduler, an overdue schedule is a switch that is off (or an override
    dropped from a deploy). With a stale heartbeat, nothing about this
    automation in particular can be concluded, and `atrasada` says that without
    accusing it.
    """
    if scheduler.state == "sem_registo":
        return DESLIGADA
    if scheduler.state == "atrasada":
        return ATRASADA if scheduler.scheduler_suspect else DESLIGADA
    return execution.state


def automations(session: Session, *, now: datetime | None = None) -> list[Automation]:
    """Every schedule that exists, described by what it did rather than by a list."""
    now_value = now or utc_now()
    states = list(session.scalars(select(ScheduleState).order_by(ScheduleState.schedule_key)))

    heartbeat = next((state for state in states if state.schedule_key == HEARTBEAT_KEY), None)
    heartbeat_stale = heartbeat is None or (
        heartbeat.next_run_at is not None and as_utc(heartbeat.next_run_at) < now_value - SCHEDULE_GRACE
    )

    connections = {
        connection.id: connection
        for connection in session.scalars(select(ProviderConnection))
    }

    rows: list[Automation] = []
    for state in states:
        prefix, connection_id = split_key(state.schedule_key)
        description = describe(state.schedule_key)
        connection = connections.get(connection_id) if connection_id is not None else None
        run = (
            _latest_run(session, connection_id=connection_id, capability=description.capability)
            if connection_id is not None and description.capability
            else None
        )
        scheduler = scheduler_health(state, now=now_value, heartbeat_stale=heartbeat_stale)
        execution = execution_health(_jobs_for(session, state.schedule_key), run)
        rows.append(
            Automation(
                schedule_key=state.schedule_key,
                label=description.label,
                purpose=description.purpose,
                setting_name=description.setting_name,
                connection_id=connection_id,
                connection_label=connection.display_name if connection is not None else None,
                provider_code=connection.provider_code if connection is not None else None,
                scheduler=scheduler,
                execution=execution,
                status=headline(scheduler, execution),
                is_heartbeat=state.schedule_key == HEARTBEAT_KEY,
            )
        )
    # The heartbeat is about the scheduler, not about the fleet, so it is not
    # sorted in among the automations it qualifies.
    rows.sort(key=lambda row: (row.is_heartbeat, row.label, row.connection_id or 0))
    return rows


@dataclass(frozen=True)
class SchedulerPulse:
    """The scheduler's own health, kept apart from every automation's."""

    alive: bool
    last_enqueued_at: datetime | None
    next_run_at: datetime | None
    stale_by: timedelta | None
    overdue_automations: int


def scheduler_pulse(rows: list[Automation]) -> SchedulerPulse:
    """Read off the rows, which were already decided against a single `now`."""
    heartbeat = next((row for row in rows if row.is_heartbeat), None)
    if heartbeat is None:
        return SchedulerPulse(False, None, None, None, sum(1 for row in rows if row.scheduler.state == "atrasada"))
    return SchedulerPulse(
        alive=heartbeat.scheduler.state == "ativa",
        last_enqueued_at=heartbeat.scheduler.last_enqueued_at,
        next_run_at=heartbeat.scheduler.next_run_at,
        stale_by=heartbeat.scheduler.overdue_by,
        overdue_automations=sum(1 for row in rows if not row.is_heartbeat and row.scheduler.state == "atrasada"),
    )


def listener_processes(session: Session, *, now: datetime | None = None) -> list[dict]:
    """Continuous processes that are not schedules at all.

    The Huawei SCADA listener has no `schedule_state` row and never will: the
    dongle dials in and the listener answers, so there is nothing periodic to
    enqueue. It is on this screen anyway because "is it receiving?" is the same
    operational question every other row answers, and because it is the only
    automation whose failure mode is silence rather than a failed job.
    """
    from nemsei.integrations.huawei_scada.models import HuaweiScadaSession

    now_value = now or utc_now()
    grouped = session.execute(
        select(
            HuaweiScadaSession.provider_connection_id,
            func.count(HuaweiScadaSession.id).filter(HuaweiScadaSession.closed_at.is_(None)),
            func.max(HuaweiScadaSession.last_seen_at),
        ).group_by(HuaweiScadaSession.provider_connection_id)
    ).all()
    out = []
    for connection_id, open_sessions, last_seen in grouped:
        connection = session.get(ProviderConnection, connection_id) if connection_id else None
        last_seen_at = as_utc(last_seen) if last_seen else None
        # The listener's own poll interval bounds this: a dongle that has not
        # been heard from in fifteen minutes has stopped, whatever the socket
        # thinks.
        silent = last_seen_at is None or last_seen_at < now_value - timedelta(minutes=15)
        out.append({
            "label": "Escuta Huawei SCADA",
            "connection_label": connection.display_name if connection else None,
            "open_sessions": int(open_sessions or 0),
            "last_seen_at": last_seen_at,
            "status": DEGRADADA if silent else OK,
            "status_label": STATUS_LABELS[DEGRADADA if silent else OK],
            "tone": STATUS_TONES[DEGRADADA if silent else OK],
        })
    return out
