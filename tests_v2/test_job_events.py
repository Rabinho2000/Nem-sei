from __future__ import annotations

from datetime import timedelta

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
