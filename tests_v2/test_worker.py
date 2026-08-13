from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from nemsei.db import build_engine, build_session_factory
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
