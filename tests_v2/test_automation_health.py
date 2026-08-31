"""Scheduler health and execution health, and why they must not be one number.

Every scenario here is one the live deployment was in on 2026-08-31, and in
every one of them the previous screen said the automation was "a correr".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, ScheduleState
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sync.models import SyncRun
from nemsei.system.automation_health import (
    A_EXECUTAR,
    AGENDADA,
    ATRASADA,
    DEGRADADA,
    DESLIGADA,
    FALHOU,
    HEARTBEAT_KEY,
    NUNCA_EXECUTADA,
    OK,
    automations,
    scheduler_pulse,
    split_key,
)
from tests_v2.test_migrations import upgrade


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def schedule(session, key, *, next_run_at, last_enqueued_at=None):
    session.add(
        ScheduleState(
            schedule_key=key,
            next_run_at=next_run_at,
            last_enqueued_at=last_enqueued_at or next_run_at - timedelta(minutes=15),
            updated_at=NOW,
        )
    )


def heartbeat(session, *, alive=True):
    schedule(
        session,
        HEARTBEAT_KEY,
        next_run_at=NOW + timedelta(minutes=45) if alive else NOW - timedelta(hours=3),
    )


def job(session, key, *, status, started_at=None, finished_at=None, slot="a", error_type=None):
    session.add(
        Job(
            job_type=key.rpartition(":")[0] if key.rpartition(":")[2].isdigit() else key,
            status=status,
            payload_json={},
            dedupe_key=f"{key}:{slot}",
            priority=100,
            available_at=started_at or NOW,
            attempt_count=1,
            max_attempts=3,
            created_at=started_at or NOW,
            updated_at=finished_at or started_at or NOW,
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
        )
    )


def rows_for(factory, seed):
    with factory() as session:
        seed(session)
        session.commit()
    with factory() as session:
        return {row.schedule_key: row for row in automations(session, now=NOW)}


def test_a_connection_id_stays_in_the_key_so_two_providers_stay_two_rows(settings, monkeypatch) -> None:
    """`production.incremental:3` is FusionSolar; `:5` is Sigenergy.

    The previous screen matched both against one generic
    `production.incremental` definition and showed whichever it found first, so
    a week of FusionSolar failures was represented by a Sigenergy success.
    """
    assert split_key("production.incremental:3") == ("production.incremental", 3)
    assert split_key("system.noop.hourly") == ("system.noop.hourly", None)

    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        fusion = create_connection(
            session, provider_code="fusionsolar", connection_key="fs", display_name="FusionSolar principal",
            credential_reference="primary", enabled=True, configuration_status="configured",
        )
        sigen = create_connection(
            session, provider_code="sigenergy", connection_key="sg", display_name="Sigenergy live",
            credential_reference="primary", enabled=True, configuration_status="configured",
        )
        session.flush()
        for connection, status in ((fusion, "failed"), (sigen, "success")):
            key = f"production.incremental:{connection.id}"
            schedule(session, key, next_run_at=NOW + timedelta(hours=20))
            job(session, key, status=status, started_at=NOW - timedelta(hours=23),
                finished_at=NOW - timedelta(hours=23), error_type="RetryableJobError" if status == "failed" else None)
        session.flush()
        seed.ids = (fusion.id, sigen.id)

    rows = rows_for(factory, seed)
    fusion_id, sigen_id = seed.ids
    assert rows[f"production.incremental:{fusion_id}"].status == FALHOU
    assert rows[f"production.incremental:{sigen_id}"].status == OK
    assert rows[f"production.incremental:{fusion_id}"].provider_code == "fusionsolar"
    assert rows[f"production.incremental:{sigen_id}"].connection_label == "Sigenergy live"


def test_an_overdue_schedule_with_a_live_heartbeat_is_off_not_late(settings, monkeypatch) -> None:
    """The diagnostics regression, read off the data.

    `next_run_at` only moves when the scheduler executes that branch, so a
    scheduler that is demonstrably alive and has left this schedule behind is
    not enqueueing it on purpose -- a switch at false, or an override that fell
    out of a deploy. Reporting it as merely "late" would send someone looking
    for a slow worker.
    """
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session, alive=True)
        schedule(session, "diagnostics.evaluate_incidents", next_run_at=NOW - timedelta(hours=6))
        job(session, "diagnostics.evaluate_incidents", status="success",
            started_at=NOW - timedelta(hours=6), finished_at=NOW - timedelta(hours=6))

    rows = rows_for(factory, seed)
    row = rows["diagnostics.evaluate_incidents"]
    assert row.status == DESLIGADA
    # And the execution history is still there and still says it used to work,
    # which is exactly why the headline must not come from it.
    assert row.execution.state == OK
    assert row.execution.last_success_at is not None


def test_an_overdue_schedule_with_a_dead_heartbeat_blames_the_scheduler(settings, monkeypatch) -> None:
    """Nothing can be concluded about one automation when none of them ran."""
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session, alive=False)
        schedule(session, "diagnostics.evaluate_incidents", next_run_at=NOW - timedelta(hours=6))

    rows = rows_for(factory, seed)
    assert rows["diagnostics.evaluate_incidents"].status == ATRASADA
    assert rows["diagnostics.evaluate_incidents"].scheduler.scheduler_suspect is True

    with factory() as session:
        pulse = scheduler_pulse(automations(session, now=NOW))
    assert pulse.alive is False
    assert pulse.overdue_automations == 1


def test_a_schedule_that_fires_perfectly_into_a_failing_job_is_not_ok(settings, monkeypatch) -> None:
    """The FusionSolar production sync, 2026-08-24 to 2026-08-30.

    Seven scheduler ticks, seven jobs, seven failures, and a green row every
    single day because the only thing being read was that a job had been
    enqueued.
    """
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="fs", display_name="FusionSolar principal",
            credential_reference="primary", enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Central")
        session.flush()
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id,
                       external_id="FS-001", valid_from=date(2020, 1, 1))
        key = f"production.incremental:{connection.id}"
        schedule(session, key, next_run_at=NOW + timedelta(hours=1))
        for day in range(7):
            when = NOW - timedelta(days=day + 1)
            job(session, key, status="failed", started_at=when, finished_at=when,
                slot=when.isoformat(), error_type="RetryableJobError")
        run = SyncRun(
            provider_connection_id=connection.id, capability="production_history", status="rate_limited",
            started_at=NOW - timedelta(days=1), finished_at=NOW - timedelta(days=1),
            completeness="none", error_code="rate_limited",
            safe_detail="FusionSolar rate limited the request.", metadata_json={},
        )
        session.add(run)
        seed.key = key

    rows = rows_for(factory, seed)
    row = rows[seed.key]
    assert row.status == FALHOU
    assert row.scheduler.state == "ativa", "the scheduler was never the problem"
    assert row.execution.consecutive_failures == 7
    assert row.execution.last_success_at is None
    assert "rate_limited" in row.execution.failure_reason


def test_the_states_a_healthy_automation_moves_through(settings, monkeypatch) -> None:
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        # Scheduled, nothing queued and nothing ever run.
        schedule(session, "digests.generate", next_run_at=NOW + timedelta(hours=12))
        # Queued but not yet started.
        schedule(session, "sync_runs.sweep_abandoned", next_run_at=NOW + timedelta(minutes=10))
        job(session, "sync_runs.sweep_abandoned", status="queued")
        # Running right now.
        schedule(session, "notifications.process", next_run_at=NOW + timedelta(minutes=10))
        job(session, "notifications.process", status="running", started_at=NOW - timedelta(seconds=30))

    rows = rows_for(factory, seed)
    assert rows["digests.generate"].status == NUNCA_EXECUTADA
    assert rows["sync_runs.sweep_abandoned"].status == AGENDADA
    assert rows["notifications.process"].status == A_EXECUTAR
    assert rows["notifications.process"].execution.running_since is not None


def test_a_partial_provider_read_is_degraded_not_ok_and_not_failed(settings, monkeypatch) -> None:
    """The plant-state read: the job succeeds, two plants out of 134 do not.

    Marking the job failed would retry an account that answered fine, and
    marking the row ok would hide that the answer was incomplete.
    """
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="fs", display_name="FusionSolar principal",
            credential_reference="primary", enabled=True, configuration_status="configured",
        )
        session.flush()
        key = f"monitoring.current:{connection.id}"
        schedule(session, key, next_run_at=NOW + timedelta(minutes=10))
        job(session, key, status="success", started_at=NOW - timedelta(minutes=5), finished_at=NOW - timedelta(minutes=5))
        session.add(SyncRun(
            provider_connection_id=connection.id, capability="current_monitoring", status="partial",
            started_at=NOW - timedelta(minutes=5), finished_at=NOW - timedelta(minutes=5),
            completeness="partial", metadata_json={},
        ))
        seed.key = key

    rows = rows_for(factory, seed)
    assert rows[seed.key].status == DEGRADADA
    assert rows[seed.key].execution.last_result == "success", "the job really did succeed"


def test_a_schedule_the_catalogue_has_never_heard_of_still_gets_a_row(settings, monkeypatch) -> None:
    """An automation running invisibly is worse than one with a plain name.

    `monitoring.current:*` was in exactly this position: two schedules running
    every fifteen minutes and no row on the screen for either of them.
    """
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        schedule(session, "something.new:9", next_run_at=NOW + timedelta(minutes=5))

    rows = rows_for(factory, seed)
    assert "something.new:9" in rows
    assert rows["something.new:9"].label == "something.new:9"
    assert rows["something.new:9"].status == NUNCA_EXECUTADA


def test_the_failure_reason_never_repeats_a_raw_exception_message(settings, monkeypatch) -> None:
    """`jobs.error_message` can carry anything a stray exception put there.

    A database error carries a connection string, credentials and all. What is
    shown is the exception's class name and the sync run's own `safe_detail`,
    which is the field that exists to be repeated.
    """
    factory = factory_for(settings, monkeypatch)
    secret = "postgresql://nemsei:hunter2@db:5432/nemsei_v2"

    def seed(session):
        heartbeat(session)
        schedule(session, "diagnostics.evaluate_incidents", next_run_at=NOW + timedelta(minutes=10))
        session.add(
            Job(
                job_type="diagnostics.evaluate_incidents", status="failed", payload_json={},
                dedupe_key="diagnostics.evaluate_incidents:a", priority=150,
                available_at=NOW - timedelta(minutes=20), attempt_count=3, max_attempts=3,
                created_at=NOW - timedelta(minutes=20), updated_at=NOW - timedelta(minutes=19),
                started_at=NOW - timedelta(minutes=20), finished_at=NOW - timedelta(minutes=19),
                error_type="OperationalError",
                error_message=f"could not connect: {secret}",
            )
        )

    rows = rows_for(factory, seed)
    reason = rows["diagnostics.evaluate_incidents"].execution.failure_reason
    assert reason == "OperationalError"
    assert secret not in reason
    assert "hunter2" not in reason


def test_the_heartbeat_is_not_listed_among_the_automations_it_qualifies(settings, monkeypatch) -> None:
    factory = factory_for(settings, monkeypatch)

    def seed(session):
        heartbeat(session)
        schedule(session, "digests.generate", next_run_at=NOW + timedelta(hours=12))

    rows = rows_for(factory, seed)
    assert rows[HEARTBEAT_KEY].is_heartbeat is True
    assert [key for key, row in rows.items() if not row.is_heartbeat] == ["digests.generate"]
