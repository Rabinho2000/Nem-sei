from __future__ import annotations

from datetime import datetime
from typing import Any

from nemsei.jobs.models import Job
from nemsei.jobs.repository import JobRepository


class JobService:
    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        actor_source: str,
        dedupe_key: str | None = None,
        available_at: datetime | None = None,
    ) -> tuple[Job, bool]:
        return self.repository.enqueue(
            job_type=job_type,
            payload=payload,
            actor_source=actor_source,
            dedupe_key=dedupe_key,
            available_at=available_at,
        )
