from __future__ import annotations

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.worker import Worker
from tests_v2.test_migrations import upgrade


def test_worker_executes_noop_and_persists_success(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    repo = JobRepository(engine, build_session_factory(engine))
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system")
    assert Worker(settings, worker_id="test-worker").run_once()
    events = repo.events_for(job.id)
    assert events[-1].to_status == "success"
