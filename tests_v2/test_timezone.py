from __future__ import annotations

from datetime import UTC

from nemsei.shared.clock import utc_now


def test_persisted_clock_boundary_is_utc_aware() -> None:
    assert utc_now().tzinfo is UTC
