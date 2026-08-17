"""Sanitized, provider-neutral discovery values and deterministic reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from nemsei.providers.registry import ProviderCode, normalize_external_id


class ReconciliationStatus(StrEnum):
    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    INVALID_MAPPING = "invalid_mapping"
    INACCESSIBLE_UNKNOWN = "inaccessible_unknown"


class MappingValidationStatus(StrEnum):
    VALID = "valid"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    AMBIGUOUS_CONFLICT = "ambiguous_conflict"
    DEFERRED = "deferred"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DiscoveredPlant:
    provider: ProviderCode
    provider_connection_id: int
    external_id: str
    external_name: str | None
    metadata: dict[str, Any]
    discovered_at: datetime

    @property
    def normalized_external_id(self) -> str:
        return normalize_external_id(self.provider, self.external_id)


@dataclass(frozen=True)
class DiscoveryResult:
    connection_id: int
    plants: tuple[DiscoveredPlant, ...]
    duplicate_external_ids: frozenset[str]
    status: str
    completeness: str
    sync_run_id: int
    error_code: str | None = None


@dataclass(frozen=True)
class ReconciliationItem:
    external_id: str
    external_name: str | None
    status: ReconciliationStatus
    mapping_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class MappingValidation:
    mapping_id: int
    status: MappingValidationStatus
    sync_run_id: int | None


def plant_from_payload(*, connection_id: int, row: dict[str, Any], discovered_at: datetime) -> DiscoveredPlant:
    external_id = str(row.get("plantCode") or row.get("stationCode") or "").strip()
    if not external_id:
        raise ValueError("FusionSolar plant row has no stable ID.")
    external_name = str(row.get("plantName") or row.get("stationName") or "").strip() or None
    metadata: dict[str, Any] = {}
    if row.get("capacity") not in {None, ""}:
        metadata["capacity"] = str(row["capacity"])[:64]
    return DiscoveredPlant(ProviderCode.FUSIONSOLAR, connection_id, external_id, external_name, metadata, discovered_at)
