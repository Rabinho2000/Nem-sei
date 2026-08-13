"""The only executable foundation job; it has no external side effects."""
from __future__ import annotations

from typing import Any

from nemsei.shared.clock import utc_now


def execute_noop(payload: dict[str, Any], *, testing: bool) -> dict[str, str]:
    if payload.get("test_hold_seconds"):
        raise ValueError("test_hold_seconds is available only in the acceptance image")
    return {"message": "noop", "executed_at": utc_now().isoformat()}
