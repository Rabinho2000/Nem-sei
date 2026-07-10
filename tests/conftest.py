from __future__ import annotations

from typing import Any

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def isolate_fusionsolar_runtime_state() -> Any:
    app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
    app_module.FUSIONSOLAR_SESSION_CACHE.clear()
    try:
        yield
    finally:
        app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
        app_module.FUSIONSOLAR_SESSION_CACHE.clear()
