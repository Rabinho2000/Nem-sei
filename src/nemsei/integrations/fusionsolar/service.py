"""Read-only FusionSolar validation, discovery, and mapping reconciliation.

Every outbound request is reserved and durably recorded before it is made. The
HTTP call happens after that transaction commits; its result is persisted in a
separate short transaction. This service deliberately has no asset creation or
mapping mutation path.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings, read_secret_value
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.discovery import (
    DiscoveryResult,
    DiscoveredPlant,
    MappingValidation,
    MappingValidationStatus,
    ReconciliationItem,
    ReconciliationStatus,
    plant_from_payload,
)
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run


_REFERENCE = re.compile(r"^[A-Za-z0-9_]{1,80}$")


class FusionSolarDiscoveryService:
    """Orchestrates only live-read actions implemented in this first slice."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client_factory: Callable[[FusionSolarCredentials], FusionSolarClient] = FusionSolarClient,
        max_transient_retries: int = 1,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._client_factory = client_factory
        self._calls = FusionSolarRequestController(session_factory, max_transient_retries=max_transient_retries)

    def validate_connection(self, connection_id: int) -> DiscoveryResult:
        """Authenticate then read only page one, proving account access without a full scan."""
        return self.discover(connection_id, page_limit=1, capability=ProviderCapability.CONNECTION_VALIDATION.value)

    def discover(self, connection_id: int, *, page_limit: int | None = None, capability: str = ProviderCapability.DISCOVERY.value) -> DiscoveryResult:
        connection = self._connection(connection_id)
        run = self._start_run(connection_id, capability)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            return self._finish_without_call(run.id, connection_id, capability, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."), "failed")
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish_without_call(run.id, connection_id, capability, ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."), "failed")
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish_without_call(run.id, connection_id, capability, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), "deferred")
        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            self._record_configuration_failure(connection_id, exc.error)
            return self._finish_without_call(run.id, connection_id, capability, exc.error, "failed")

        client = self._client_factory(credentials)
        _value, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="authentication", purpose="fusionsolar_authentication", operation=client.authenticate)
        if error:
            return self._finish_discovery(run.id, connection_id, (), frozenset(), error, received=0, rejected=0, pages=0)

        plants: list[DiscoveredPlant] = []
        seen: set[str] = set()
        duplicate_ids: set[str] = set()
        rejected = 0
        page = 1
        expected_pages = 1
        while page <= expected_pages and (page_limit is None or page <= page_limit):
            rows, error = self._calls.call(
                connection_id=connection_id,
                sync_run_id=run.id,
                endpoint_family="discovery",
                purpose=f"fusionsolar_discovery_page_{page}",
                operation=lambda page=page: client.discover_page(page),
            )
            if error:
                return self._finish_discovery(run.id, connection_id, tuple(plants), frozenset(duplicate_ids), error, received=len(plants), rejected=rejected, pages=page - 1)
            assert rows is not None
            page_rows, expected_pages = rows
            for row in page_rows:
                try:
                    plant = plant_from_payload(connection_id=connection_id, row=row, discovered_at=utc_now())
                    normalized = plant.normalized_external_id
                except ValueError:
                    rejected += 1
                    continue
                if normalized in seen:
                    duplicate_ids.add(normalized)
                    continue
                seen.add(normalized)
                plants.append(plant)
            page += 1

        completeness = "complete" if page_limit is None or page_limit >= expected_pages else "partial"
        return self._finish_discovery(
            run.id,
            connection_id,
            tuple(plants),
            frozenset(duplicate_ids),
            None,
            received=len(plants),
            rejected=rejected,
            pages=min(page - 1, expected_pages),
            completeness=completeness,
            allow_partial_success=capability == ProviderCapability.CONNECTION_VALIDATION.value,
        )

    def reconcile(self, result: DiscoveryResult) -> list[ReconciliationItem]:
        with self._sessions() as session:
            mappings = ProviderRepository(session).current_mappings_for_connection(result.connection_id)
        by_identifier: dict[str, list[AssetProviderMapping]] = {}
        for mapping in mappings:
            by_identifier.setdefault(mapping.normalized_external_id, []).append(mapping)
        items: list[ReconciliationItem] = []
        for plant in result.plants:
            normalized = plant.normalized_external_id
            mappings = by_identifier.get(normalized, [])
            if normalized in result.duplicate_external_ids or len(mappings) > 1:
                status = ReconciliationStatus.DUPLICATE_CONFLICT
            elif not mappings:
                status = ReconciliationStatus.UNMAPPED
            elif mappings[0].mapping_status == "invalid":
                status = ReconciliationStatus.INVALID_MAPPING
            elif mappings[0].mapping_status == "active" and mappings[0].valid_to is None:
                status = ReconciliationStatus.MAPPED
            else:
                status = ReconciliationStatus.INVALID_MAPPING
            items.append(ReconciliationItem(plant.external_id, plant.external_name, status, tuple(mapping.id for mapping in mappings)))
        return items

    def validate_mapping(self, mapping_id: int, *, discovery: DiscoveryResult | None = None) -> MappingValidation:
        with self._sessions() as session:
            mapping = ProviderRepository(session).mapping(mapping_id)
            if mapping is None:
                raise ValueError("Unknown provider mapping.")
            connection_id = mapping.provider_connection_id
            external_id = mapping.normalized_external_id
            mapping_status = mapping.mapping_status
        if mapping_status == "invalid":
            return MappingValidation(mapping_id, MappingValidationStatus.NOT_FOUND, None)
        result = discovery if discovery is not None else self.discover(connection_id)
        if result.connection_id != connection_id:
            raise ValueError("Discovery result belongs to another provider connection.")
        matches = [plant for plant in result.plants if plant.normalized_external_id == external_id]
        if external_id in result.duplicate_external_ids or len(matches) > 1:
            return MappingValidation(mapping_id, MappingValidationStatus.AMBIGUOUS_CONFLICT, result.sync_run_id)
        if matches:
            return MappingValidation(mapping_id, MappingValidationStatus.VALID, result.sync_run_id)
        if result.status != "success":
            return MappingValidation(mapping_id, _validation_error(result.error_code, result.status), result.sync_run_id)
        if result.completeness != "complete":
            return MappingValidation(mapping_id, MappingValidationStatus.UNKNOWN, result.sync_run_id)
        return MappingValidation(mapping_id, MappingValidationStatus.NOT_FOUND, result.sync_run_id)

    def _connection(self, connection_id: int) -> ProviderConnection:
        with self._sessions() as session:
            connection = ProviderRepository(session).connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            session.expunge(connection)
            return connection

    def _start_run(self, connection_id: int, capability: str) -> SyncRun:
        with self._sessions() as session:
            run = start_sync_run(session, provider_connection_id=connection_id, capability=capability)
            session.commit()
            session.expunge(run)
            return run

    def _finish_without_call(self, run_id: int, connection_id: int, capability: str, error: ProviderError, status: str) -> DiscoveryResult:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            if error.code is ProviderErrorCode.CONFIGURATION:
                record_health(session, provider_connection_id=connection_id, auth_state="not_configured", access_state="not_configured", provider_state="not_configured", discovery_state="not_configured", error=error)
            finish_sync_run(session, run=run, status=status, completeness="none", error=error)
            session.commit()
        return DiscoveryResult(connection_id, (), frozenset(), status, "none", run_id, error.code.value)

    def _finish_discovery(
        self,
        run_id: int,
        connection_id: int,
        plants: tuple[DiscoveredPlant, ...],
        duplicates: frozenset[str],
        error: ProviderError | None,
        *,
        received: int,
        rejected: int,
        pages: int,
        completeness: str | None = None,
        allow_partial_success: bool = False,
    ) -> DiscoveryResult:
        if error is None:
            status = "success" if (completeness == "complete" or allow_partial_success) and rejected == 0 else "partial"
            effective_completeness = completeness or "complete"
        elif plants:
            status, effective_completeness = "partial", "partial"
        elif error.code is ProviderErrorCode.RATE_LIMITED:
            status, effective_completeness = "rate_limited", "none"
        elif error.code is ProviderErrorCode.NOT_SUPPORTED:
            status, effective_completeness = "deferred", "none"
        else:
            status, effective_completeness = "failed", "none"
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {"actual_provider_calls": _calls(session, run_id), "items_received": received, "items_accepted": len(plants), "items_rejected": rejected, "duplicate_ids": len(duplicates), "pages_completed": pages}
            health_values = health_values_for_error(error, operation="discovery")
            record_health(session, provider_connection_id=connection_id, partial=status == "partial", error=error, **health_values)
            finish_sync_run(session, run=run, status=status, completeness=effective_completeness, error=error)
            session.commit()
        return DiscoveryResult(connection_id, plants, duplicates, status, effective_completeness, run_id, error.code.value if error else None)

    def _record_configuration_failure(self, connection_id: int, error: ProviderError) -> None:
        with self._sessions() as session:
            record_health(session, provider_connection_id=connection_id, auth_state="not_configured", access_state="not_configured", provider_state="not_configured", discovery_state="not_configured", error=error)
            session.commit()

def credentials_for(connection: ProviderConnection) -> FusionSolarCredentials:
    reference = connection.credential_reference or ""
    if not _REFERENCE.fullmatch(reference):
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar credential reference is not configured."))
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    username = read_secret_value(value_name=f"{prefix}_USERNAME", file_name=f"{prefix}_USERNAME_FILE")
    password = read_secret_value(value_name=f"{prefix}_PASSWORD", file_name=f"{prefix}_PASSWORD_FILE")
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    if not username or not password or not base_url:
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar credentials or endpoint are not configured."))
    if not base_url.startswith(("https://", "http://")):
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar endpoint is invalid."))
    return FusionSolarCredentials(username=username, password=password, base_url=base_url)


def _calls(session: Session, run_id: int) -> int:
    return int(session.scalar(select(func.count()).select_from(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == run_id, ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")))) or 0)


def _validation_error(error_code: str | None, status: str) -> MappingValidationStatus:
    if status in {"deferred", "rate_limited"}:
        return MappingValidationStatus.DEFERRED if status == "deferred" else MappingValidationStatus.RATE_LIMITED
    if error_code == ProviderErrorCode.AUTHORIZATION.value:
        return MappingValidationStatus.ACCESS_DENIED
    if error_code == ProviderErrorCode.AUTHENTICATION.value:
        return MappingValidationStatus.CONNECTION_UNAVAILABLE
    if error_code in {ProviderErrorCode.UNAVAILABLE.value, ProviderErrorCode.TIMEOUT.value, ProviderErrorCode.TRANSPORT.value}:
        return MappingValidationStatus.PROVIDER_UNAVAILABLE
    return MappingValidationStatus.UNKNOWN
