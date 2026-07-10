from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from monitoring_board.services.api_rate_limit import ApiRateLimitError, ApiTransientError


@dataclass(frozen=True)
class RetryPolicy:
    delays_seconds: tuple[int, ...] = (15, 60, 180)


def retry_api_call(
    call: Callable[[], Any],
    *,
    allow_sleep: bool,
    policy: RetryPolicy = RetryPolicy(),
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    attempts = len(policy.delays_seconds) + 1
    for index in range(attempts):
        try:
            return call()
        except ApiRateLimitError:
            raise
        except ApiTransientError:
            if index >= attempts - 1 or not allow_sleep:
                raise
            sleeper(policy.delays_seconds[index])
        except requests.RequestException as exc:
            if index >= attempts - 1 or not allow_sleep:
                raise ApiTransientError(str(exc)) from exc
            sleeper(policy.delays_seconds[index])
    raise ApiTransientError("API call failed after retries.")


def http_retryable_status(status_code: int | None) -> bool:
    return bool(status_code and 500 <= status_code <= 599)


def http_rate_limited_status(status_code: int | None) -> bool:
    return status_code == 429
