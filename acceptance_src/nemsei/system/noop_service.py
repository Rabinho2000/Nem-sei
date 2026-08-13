"""Acceptance-only overlay for exercising a worker crash with a held noop."""
from __future__ import annotations

import time
from typing import Any

from nemsei.shared.clock import utc_now


def execute_noop(payload: dict[str, Any], *, testing: bool) -> dict[str, str]:
    delay = payload.get("test_hold_seconds", 0)
    if delay and not testing:
        raise ValueError("test_hold_seconds is only available in an acceptance runtime")
    if delay:
        time.sleep(float(delay))
    return {"message": "noop", "executed_at": utc_now().isoformat()}
