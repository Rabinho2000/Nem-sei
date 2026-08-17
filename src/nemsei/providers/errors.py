"""Normalized, provider-neutral errors emitted by future adapters."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderErrorCode(StrEnum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_RESPONSE = "invalid_response"
    NOT_SUPPORTED = "not_supported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderError:
    code: ProviderErrorCode
    safe_message: str
    retry_after_seconds: int | None = None
    transient: bool = False

    def __post_init__(self) -> None:
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
