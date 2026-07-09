from __future__ import annotations

from typing import Any


class FusionSolarApiError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        status_code: int | None = None,
        error_type: str = "api",
    ) -> None:
        super().__init__(message)
        self.payload = payload or {}
        self.status_code = status_code
        self.error_type = error_type


class FusionSolarRateLimitError(FusionSolarApiError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None, status_code: int | None = None) -> None:
        super().__init__(message, payload=payload, status_code=status_code, error_type="rate_limit")


class FusionSolarSessionExpiredError(FusionSolarApiError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message, payload=payload, error_type="session_expired")


class FusionSolarCredentialsError(FusionSolarApiError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message, payload=payload, error_type="credentials")
