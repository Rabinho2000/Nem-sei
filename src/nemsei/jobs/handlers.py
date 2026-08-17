from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.jobs.repository import ClaimedJob
from nemsei.system.noop_service import execute_noop


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict[str, Any]
    resume_payload: dict[str, Any] | None = None


class RetryableJobError(RuntimeError):
    pass


def execute(
    job: ClaimedJob,
    *,
    testing: bool,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> JobOutcome:
    if job.job_type == "system.noop":
        return JobOutcome(status="success", result=execute_noop(job.payload, testing=testing))
    if job.job_type in {"production.incremental", "production.reconciliation", "production.bounded_backfill"}:
        if settings is None or session_factory is None:
            raise ValueError("Production jobs require worker settings and sessions.")
        return _execute_production(job, settings=settings, session_factory=session_factory)
    raise ValueError(f"Unsupported V2 foundation job type: {job.job_type}")


def _date(payload: dict[str, Any], key: str, *, required: bool = False) -> date | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Production job requires ISO {key}.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Production job {key} is invalid.") from exc


def _execute_production(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Production job connection_id is invalid.")
    service = FusionSolarProductionService(session_factory, settings)
    if job.job_type == "production.incremental":
        result = service.sync_incremental(connection_id, start_date=_date(job.payload, "start_date"), end_date=_date(job.payload, "end_date"))
    elif job.job_type == "production.reconciliation":
        days = job.payload.get("source_days", 1)
        if not isinstance(days, int):
            raise ValueError("Production reconciliation source_days is invalid.")
        result = service.sync_reconciliation(connection_id, source_days=days)
    else:
        result = service.sync_bounded_backfill(
            connection_id,
            start_date=_date(job.payload, "start_date", required=True),
            end_date=_date(job.payload, "end_date", required=True),
            resume_from=_date(job.payload, "next_source_day"),
        )
    result_json = {"mode": result.mode, "result_status": result.status}
    if result.status in {"failed", "rate_limited", "deferred", "partial"}:
        raise RetryableJobError(f"Production {result.mode} stopped with {result.status}.")
    if result.next_source_day:
        payload = dict(job.payload)
        payload["next_source_day"] = result.next_source_day.isoformat()
        payload["mode"] = result.mode
        return JobOutcome(status="success", result=result_json, resume_payload=payload)
    return JobOutcome(status="success", result=result_json)
