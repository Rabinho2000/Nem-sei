from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from nemsei.shared.clock import utc_now
from tests_v2.test_jobs import repository


def test_expired_running_job_is_recovered_with_sanitized_audit_event(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(job_type="system.noop", payload={"password": "never-audit"}, actor_source="system")
    claimed = repo.claim_next(worker_id="worker-a", lease_seconds=1)
    assert claimed
    assert repo.recover_expired(now=utc_now() + timedelta(seconds=2)) == 1
    event = repo.events_for(job.id)[-1]
    assert (event.actor_source, event.from_status, event.to_status) == ("recovery", "running", "waiting")
    assert "password" not in event.metadata_json
    assert repo.activate_due_waiting(now=utc_now() + timedelta(seconds=2)) == 1
    assert repo.events_for(job.id)[-1].to_status == "queued"


def test_job_events_are_database_enforced_append_only(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="system")
    event_id = repo.events_for(job.id)[0].id
    with repo.engine.begin() as connection:
        with pytest.raises(Exception, match="append-only"):
            connection.execute(text("UPDATE job_events SET event_type = 'changed' WHERE id = :id"), {"id": event_id})
    with repo.engine.begin() as connection:
        with pytest.raises(Exception, match="append-only"):
            connection.execute(text("DELETE FROM job_events WHERE id = :id"), {"id": event_id})
