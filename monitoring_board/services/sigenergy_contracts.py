from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from monitoring_board.services.api_rate_limit import (
    ApiRateLimitError,
    ApiTransientError,
)
from monitoring_board.services.sigenergy_errors import (
    SigenergyApiError,
    SigenergyAuthError,
)
from monitoring_board.services.sigenergy_models import sanitize_sigenergy_error


SIGENERGY_PROVIDER = "Sigenergy"
SIGENERGY_DISCOVERY_RESTRICTED_CODE = "1201"
SIGENERGY_SYSTEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

OPERATION_CREDENTIALS = "credentials"
OPERATION_DISCOVERY = "discovery"
OPERATION_ACCESS = "access"
OPERATION_STATE_SYNC = "state_sync"
OPERATION_HISTORY = "history"
OPERATION_ONBOARDING = "onboarding"
OPERATION_MAPPING = "mapping"
OPERATION_LEGACY_UNKNOWN = "legacy_unknown"


class CredentialStatus(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    INVALID = "invalid"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class CredentialOutcome(StrEnum):
    AUTHENTICATED = "authenticated"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"


class DiscoveryStatus(StrEnum):
    NEVER_RUN = "never_run"
    SUCCESS = "success"
    EMPTY = "empty"
    RESTRICTED = "restricted"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class AccessStatus(StrEnum):
    UNKNOWN = "unknown"
    ACCESSIBLE = "accessible"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    ERROR = "error"


class AccessOutcome(StrEnum):
    ACCESSIBLE = "accessible"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"


class OperationalStatus(StrEnum):
    OPERATIONAL = "operational"
    WARNING = "warning"
    ERROR = "error"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class SyncStatus(StrEnum):
    NEVER_SYNCED = "never_synced"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


class DataQuality(StrEnum):
    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"
    INVALID = "invalid"


class MappingStatus(StrEnum):
    UNASSOCIATED = "unassociated"
    ASSOCIATED = "associated"
    DISABLED = "disabled"


class OnboardingStatus(StrEnum):
    REQUESTED = "requested"
    PROVIDER_PENDING = "provider_pending"
    PROVIDER_APPROVED = "provider_approved"
    PROVIDER_REJECTED = "provider_rejected"
    ACCESS_CONFIRMED_INDEPENDENTLY = "access_confirmed_independently"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class BackfillDayStatus(StrEnum):
    COMPLETE = "complete"
    QUEUED = "queued"
    REUSED = "reused"


class FailureCategory(StrEnum):
    RESTRICTED = "restricted"
    AUTH_FAILED = "auth_failed"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    message: str
    http_status: int | None = None
    api_code: str = ""
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class ScopedProviderError:
    provider: str
    operation: str
    external_id: str
    occurred_at: str
    category: str
    message: str
    http_status: int | None = None
    api_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CredentialTestResult:
    outcome: CredentialOutcome
    credential_status: CredentialStatus
    attempted_at: str
    message: str = ""
    error: ScopedProviderError | None = None

    @property
    def authenticated(self) -> bool:
        return self.outcome is CredentialOutcome.AUTHENTICATED

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    status: DiscoveryStatus
    systems: tuple[dict[str, Any], ...]
    attempted_at: str
    message: str = ""
    error: ScopedProviderError | None = None
    cooldown_until: datetime | None = None

    @property
    def station_count(self) -> int:
        return len(self.systems)

    @property
    def available_system_ids(self) -> list[str]:
        return [
            str(
                row.get("systemId")
                or row.get("id")
                or row.get("stationId")
                or row.get("plantId")
                or ""
            ).strip()
            for row in self.systems
            if str(
                row.get("systemId")
                or row.get("id")
                or row.get("stationId")
                or row.get("plantId")
                or ""
            ).strip()
        ]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.cooldown_until is not None:
            result["cooldown_until"] = self.cooldown_until.isoformat(
                timespec="seconds"
            )
        result["station_count"] = self.station_count
        result["available_system_ids"] = self.available_system_ids
        return result


@dataclass(frozen=True)
class SystemAccessResult:
    external_id: str
    outcome: AccessOutcome
    access_status: AccessStatus
    attempted_at: str
    validation_method: str = "direct_energy_flow"
    external_name: str = ""
    operational_status: OperationalStatus = OperationalStatus.UNKNOWN
    energy_flow: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: ScopedProviderError | None = None
    cooldown_until: datetime | None = None

    @property
    def accessible(self) -> bool:
        return self.outcome is AccessOutcome.ACCESSIBLE

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.cooldown_until is not None:
            result["cooldown_until"] = self.cooldown_until.isoformat(
                timespec="seconds"
            )
        return result


@dataclass(frozen=True)
class SystemSyncResult:
    external_id: str
    status: SyncStatus
    attempted_at: str
    asset_id: int | None = None
    snapshot_id: int | None = None
    access_status: AccessStatus = AccessStatus.UNKNOWN
    operational_status: OperationalStatus = OperationalStatus.UNKNOWN
    normalized_row: dict[str, Any] = field(default_factory=dict)
    error: ScopedProviderError | None = None
    cooldown_until: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is SyncStatus.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.cooldown_until is not None:
            result["cooldown_until"] = self.cooldown_until.isoformat(
                timespec="seconds"
            )
        return result


@dataclass(frozen=True)
class SyncBatchResult:
    status: SyncStatus
    systems: tuple[SystemSyncResult, ...]
    started_at: str
    finished_at: str

    @property
    def success_count(self) -> int:
        return sum(1 for row in self.systems if row.succeeded)

    @property
    def failed_count(self) -> int:
        return len(self.systems) - self.success_count

    @property
    def snapshot_count(self) -> int:
        return sum(1 for row in self.systems if row.snapshot_id is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "systems": [row.as_dict() for row in self.systems],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "snapshot_count": self.snapshot_count,
        }


@dataclass(frozen=True)
class HistoryResult:
    external_id: str
    target_date: str
    status: SyncStatus
    attempted_at: str
    asset_id: int | None = None
    fact_id: int | None = None
    data_quality: DataQuality = DataQuality.MISSING
    error: ScopedProviderError | None = None
    cooldown_until: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.cooldown_until is not None:
            result["cooldown_until"] = self.cooldown_until.isoformat(
                timespec="seconds"
            )
        return result


@dataclass(frozen=True)
class OnboardingResult:
    external_id: str
    status: OnboardingStatus
    attempted_at: str
    request_id: int
    provider_code: str = ""
    message: str = ""
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackfillDayResult:
    target_date: str
    status: BackfillDayStatus
    job_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackfillPlanResult:
    external_id: str
    asset_id: int
    date_from: str
    date_to: str
    days: tuple[BackfillDayResult, ...]

    @property
    def complete_count(self) -> int:
        return sum(
            1
            for day in self.days
            if day.status is BackfillDayStatus.COMPLETE
        )

    @property
    def queued_count(self) -> int:
        return sum(
            1 for day in self.days if day.status is BackfillDayStatus.QUEUED
        )

    @property
    def reused_count(self) -> int:
        return sum(
            1 for day in self.days if day.status is BackfillDayStatus.REUSED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "asset_id": self.asset_id,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "days": [day.as_dict() for day in self.days],
            "complete_count": self.complete_count,
            "queued_count": self.queued_count,
            "reused_count": self.reused_count,
        }


def validate_sigenergy_system_id(raw_system_id: str) -> str:
    system_id = str(raw_system_id or "").strip()
    if not SIGENERGY_SYSTEM_ID_PATTERN.fullmatch(system_id):
        raise ValueError(
            "Indica um unico System ID valido (letras, numeros, _ ou -)."
        )
    return system_id


def normalize_system_id_for_compare(system_id: str) -> str:
    return str(system_id or "").strip().casefold()


def normalize_operational_status(raw_status: Any) -> OperationalStatus:
    normalized = " ".join(
        str(raw_status or "")
        .strip()
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )
    if normalized in {"normal", "online", "running", "operational"}:
        return OperationalStatus.OPERATIONAL
    if normalized in {"warning", "alarm", "degraded"}:
        return OperationalStatus.WARNING
    if normalized in {"fault", "error", "abnormal"}:
        return OperationalStatus.ERROR
    if normalized in {"offline", "disconnected"}:
        return OperationalStatus.OFFLINE
    return OperationalStatus.UNKNOWN


def classify_sigenergy_failure(
    exc: BaseException,
    *,
    operation: str,
) -> FailureClassification:
    message = sanitize_sigenergy_error(exc) or "Pedido Sigenergy falhou."
    status_code = getattr(exc, "status_code", None)
    api_code = str(getattr(exc, "api_code", "") or "").strip()

    if isinstance(exc, ApiRateLimitError):
        return FailureClassification(
            FailureCategory.RATE_LIMITED,
            message,
            http_status=429,
            api_code=api_code,
            cooldown_until=exc.cooldown_until,
        )
    if (
        operation == OPERATION_DISCOVERY
        and isinstance(exc, SigenergyApiError)
        and api_code == SIGENERGY_DISCOVERY_RESTRICTED_CODE
    ):
        return FailureClassification(
            FailureCategory.RESTRICTED,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if isinstance(exc, SigenergyAuthError) or status_code == 401:
        return FailureClassification(
            FailureCategory.AUTH_FAILED,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if operation == OPERATION_CREDENTIALS and status_code == 403:
        return FailureClassification(
            FailureCategory.AUTH_FAILED,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if status_code == 403:
        return FailureClassification(
            FailureCategory.UNAUTHORIZED,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if status_code == 404:
        return FailureClassification(
            FailureCategory.NOT_FOUND,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if status_code == 429:
        return FailureClassification(
            FailureCategory.RATE_LIMITED,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    if isinstance(exc, ApiTransientError):
        return FailureClassification(
            FailureCategory.PROVIDER_ERROR,
            message,
            http_status=status_code,
            api_code=api_code,
        )
    return FailureClassification(
        FailureCategory.PROVIDER_ERROR,
        message,
        http_status=status_code,
        api_code=api_code,
    )


def scoped_error(
    classification: FailureClassification,
    *,
    operation: str,
    external_id: str,
    occurred_at: str,
) -> ScopedProviderError:
    return ScopedProviderError(
        provider=SIGENERGY_PROVIDER,
        operation=operation,
        external_id=external_id,
        occurred_at=occurred_at,
        category=classification.category.value,
        message=classification.message,
        http_status=classification.http_status,
        api_code=classification.api_code,
    )
