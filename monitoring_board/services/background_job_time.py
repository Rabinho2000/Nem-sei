from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
LISBON = ZoneInfo("Europe/Lisbon")


def background_job_utc_now() -> datetime:
    return datetime.now(UTC)


def as_background_job_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def serialize_background_job_timestamp(value: datetime | None = None) -> str:
    normalized = as_background_job_utc(value or background_job_utc_now())
    return normalized.isoformat(timespec="seconds")


def parse_background_job_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return as_background_job_utc(parsed)


def background_job_timestamp_is_due(
    value: Any,
    *,
    now: datetime | None = None,
) -> bool:
    parsed = parse_background_job_timestamp(value)
    return parsed is None or parsed <= as_background_job_utc(
        now or background_job_utc_now()
    )


def background_job_timestamp_to_lisbon(
    value: Any,
    *,
    timespec: str = "seconds",
) -> str:
    parsed = parse_background_job_timestamp(value)
    if parsed is None:
        return ""
    return parsed.astimezone(LISBON).isoformat(timespec=timespec)
