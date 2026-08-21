from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from nemsei.assets.service import create_asset, create_device
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.diagnostics.service import record_device_status
from nemsei.jobs.models import Job
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.worker import Worker
from nemsei.shared.clock import as_utc, utc_now
from tests_v2.test_migrations import upgrade


def test_worker_executes_noop_and_persists_success(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system")
    assert Worker(settings, worker_id="test-worker").run_once()
    events = repo.events_for(job.id)
    assert events[-1].to_status == "success"


def test_worker_executes_a_real_incident_evaluation_end_to_end(settings, monkeypatch) -> None:
    """D1, proven through the real Job/Scheduler/Worker pipeline this
    milestone targets, not by calling evaluate_and_persist_incidents directly."""
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Worker Pipeline Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        # Recent, not a fixed historical date: the job runs against real
        # wall-clock `now`, and a reading from weeks ago would *also* trip
        # `stale_reading` alongside `device_unavailable` -- both correct, but
        # this test wants exactly the one finding it names.
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=datetime.now(timezone.utc) - timedelta(hours=1), availability_status="unavailable",
        )

    repo = JobRepository(engine, session_factory)
    job, created = repo.enqueue_due_incident_evaluation(interval_minutes=15)
    assert created and job is not None

    assert Worker(settings, worker_id="test-worker-incidents").run_once()
    events = repo.events_for(job.id)
    assert events[-1].to_status == "success"

    # `result_json` only ever keeps the small, allowlisted keys `safe_metadata`
    # recognises (`reason`, `result_status`, ...) -- the real proof of what
    # this job did is the persisted incident it created, checked below, the
    # same way device_status.poll's own richer result fields
    # (expected/received/accepted/rejected) are never asserted against
    # `result_json` either.
    with session_factory() as session:
        incidents = session.scalars(select(DiagnosticIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].rule_code == "device_unavailable"
    assert incidents[0].status == "open"


def test_worker_persists_partial_as_a_distinct_terminal_result(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system")
    monkeypatch.setattr(
        "nemsei.jobs.worker.execute",
        lambda *_args, **_kwargs: type("Outcome", (), {"status": "partial", "result": {}})(),
    )
    assert Worker(settings, worker_id="worker").run_once()
    with repo.session_factory() as session:
        assert session.get(Job, job.id).status == "partial"


def test_retry_delay_backoff_and_exhaustion_are_persisted(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system", max_attempts=3)
    first = repo.claim_next(worker_id="worker", lease_seconds=30)
    assert first
    assert repo.retry_or_fail(first, error_type="Failure", message="failed", delay_seconds=60)
    with repo.session_factory() as session:
        waiting = session.get(Job, job.id)
        assert waiting.status == "waiting"
        assert as_utc(waiting.available_at) >= utc_now() + timedelta(seconds=59)
    first_due = utc_now() + timedelta(seconds=61)
    assert repo.activate_due_waiting(now=first_due) == 1
    second = repo.claim_next(worker_id="worker", lease_seconds=30, now=first_due)
    assert second and second.attempt == 2
    assert repo.retry_or_fail(second, error_type="Failure", message="failed", delay_seconds=300)
    with repo.session_factory() as session:
        assert as_utc(session.get(Job, job.id).available_at) >= utc_now() + timedelta(seconds=299)
    second_due = utc_now() + timedelta(seconds=301)
    assert repo.activate_due_waiting(now=second_due) == 1
    third = repo.claim_next(worker_id="worker", lease_seconds=30, now=second_due)
    assert third and third.attempt == 3
    assert repo.retry_or_fail(third, error_type="Failure", message="failed")
    with repo.session_factory() as session:
        failed = session.scalar(select(Job).where(Job.id == job.id))
        assert failed.status == "failed"
        assert failed.attempt_count == 3
    assert [event.event_type for event in repo.events_for(job.id)][-2:] == ["claimed", "retry_exhausted"]


def test_stale_recovery_preserves_attempt_accounting(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system", max_attempts=2)
    first = repo.claim_next(worker_id="worker-a", lease_seconds=1)
    assert first and first.attempt == 1
    assert repo.recover_expired(now=utc_now() + timedelta(seconds=2)) == 1
    due = utc_now() + timedelta(seconds=2)
    assert repo.activate_due_waiting(now=due) == 1
    second = repo.claim_next(worker_id="worker-b", lease_seconds=30, now=due)
    assert second and second.attempt == 2
