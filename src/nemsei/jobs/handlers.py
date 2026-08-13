from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nemsei.jobs.repository import ClaimedJob
from nemsei.system.noop_service import execute_noop


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict[str, Any]


class RetryableJobError(RuntimeError):
    pass


def execute(job: ClaimedJob, *, testing: bool) -> JobOutcome:
    if job.job_type == "system.noop":
        return JobOutcome(status="success", result=execute_noop(job.payload, testing=testing))
    raise ValueError(f"Unsupported V2 foundation job type: {job.job_type}")
