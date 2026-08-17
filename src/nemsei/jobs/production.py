"""Validated, non-secret payload constructors for production queue work."""
from __future__ import annotations

from datetime import date


def incremental_payload(connection_id: int, *, start_date: date | None = None, end_date: date | None = None) -> dict:
    return _payload("incremental", connection_id, start_date=start_date, end_date=end_date)


def reconciliation_payload(connection_id: int, *, source_days: int = 1) -> dict:
    if source_days <= 0:
        raise ValueError("Production reconciliation source_days must be positive.")
    return {"mode": "reconciliation", "connection_id": _connection(connection_id), "source_days": source_days}


def bounded_backfill_payload(connection_id: int, *, start_date: date, end_date: date) -> dict:
    if end_date < start_date:
        raise ValueError("Production backfill end date cannot precede start date.")
    return _payload("bounded_backfill", connection_id, start_date=start_date, end_date=end_date)


def _payload(mode: str, connection_id: int, *, start_date: date | None, end_date: date | None) -> dict:
    payload = {"mode": mode, "connection_id": _connection(connection_id)}
    if start_date:
        payload["start_date"] = start_date.isoformat()
    if end_date:
        payload["end_date"] = end_date.isoformat()
    return payload


def _connection(connection_id: int) -> int:
    if connection_id <= 0:
        raise ValueError("Production connection id must be positive.")
    return connection_id
