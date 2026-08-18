"""Sigenergy system discovery and mapping reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.sigenergy.client import SigenergyClient, SigenergyClientError, SigenergyCredentials, SigenergyEndpoints, SigenergyTransport
from nemsei.integrations.sigenergy.request_control import SigenergyRequestController
from nemsei.integrations.sigenergy.service import credentials_for
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run


@dataclass(frozen=True)
class SigenergyPlant:
    provider_connection_id: int
    external_id: str
    external_name: str | None
    raw_status: str | None
    metadata: dict[str, Any]
    discovered_at: datetime

    @property
    def normalized_external_id(self) -> str:
        return normalize_external_id(ProviderCode.SIGENERGY, self.external_id)


@dataclass(frozen=True)
class SigenergyDiscoveryResult:
    connection_id: int
    plants: tuple[SigenergyPlant, ...]
    duplicate_external_ids: frozenset[str]
    status: str
    completeness: str
    sync_run_id: int
    error_code: str | None = None


class SigenergyDiscoveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client_factory: Callable[[SigenergyCredentials, SigenergyEndpoints, SigenergyTransport | None], SigenergyClient] = lambda credentials, endpoints, transport: SigenergyClient(endpoints, credentials, transport=transport),
        transport: SigenergyTransport | None = None,
        max_transient_retries: int = 1,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._client_factory = client_factory
        self._transport = transport
        self._calls = SigenergyRequestController(session_factory, max_transient_retries=max_transient_retries)

    def validate_connection(self, connection_id: int) -> SigenergyDiscoveryResult:
        connection = self._connection(connection_id)
        run = self._start_run(connection_id, ProviderCapability.CONNECTION_VALIDATION.value)
        if not self._preflight(connection):
            return self._failed_preflight(run.id, connection)
        try:
            credentials, endpoints = credentials_for(connection)
        except SigenergyClientError as exc:
            return self._finish(run.id, connection_id, (), frozenset(), exc.error, status="failed", completeness="none")
        client = self._client_factory(credentials, endpoints, self._transport)
        _value, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="authentication", purpose="sigenergy_authentication", operation=client.authenticate)
        if error:
            return self._finish(run.id, connection_id, (), frozenset(), error, status="failed" if error.code is not ProviderErrorCode.RATE_LIMITED else "rate_limited", completeness="none")
        return self._finish(run.id, connection_id, (), frozenset(), None, status="success", completeness="complete")

    def discover(self, connection_id: int) -> SigenergyDiscoveryResult:
        connection = self._connection(connection_id)
        run = self._start_run(connection_id, ProviderCapability.DISCOVERY.value)
        if not self._preflight(connection):
            return self._failed_preflight(run.id, connection)
        try:
            credentials, endpoints = credentials_for(connection)
        except SigenergyClientError as exc:
            return self._finish(run.id, connection_id, (), frozenset(), exc.error, status="failed", completeness="none")
        client = self._client_factory(credentials, endpoints, self._transport)
        _value, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="authentication", purpose="sigenergy_authentication", operation=client.authenticate)
        if error:
            return self._finish(run.id, connection_id, (), frozenset(), error, status="failed" if error.code is not ProviderErrorCode.RATE_LIMITED else "rate_limited", completeness="none")
        rows, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="discovery", purpose="sigenergy_discovery", operation=client.discover_systems)
        if error:
            return self._finish(run.id, connection_id, (), frozenset(), error, status="failed" if error.code is not ProviderErrorCode.RATE_LIMITED else "rate_limited", completeness="none")
        plants: list[SigenergyPlant] = []
        seen: set[str] = set()
        duplicates: set[str] = set()
        rejected = 0
        for row in rows or []:
            try:
                plant = plant_from_payload(connection_id, row)
                normalized = plant.normalized_external_id
            except ValueError:
                rejected += 1
                continue
            if normalized in seen:
                duplicates.add(normalized)
                continue
            seen.add(normalized)
            plants.append(plant)
        status = "success" if rejected == 0 and not duplicates else "partial"
        return self._finish(run.id, connection_id, tuple(plants), frozenset(duplicates), None, status=status, completeness="complete" if status == "success" else "partial", metadata={"items_received": len(rows or []), "items_accepted": len(plants), "items_rejected": rejected, "duplicate_ids": len(duplicates)})

    def reconcile(self, result: SigenergyDiscoveryResult) -> list[tuple[str, str | None, str, tuple[int, ...]]]:
        with self._sessions() as session:
            mappings = ProviderRepository(session).current_mappings_for_connection(result.connection_id)
        by_identifier: dict[str, list[AssetProviderMapping]] = {}
        for mapping in mappings:
            by_identifier.setdefault(mapping.normalized_external_id, []).append(mapping)
        values = []
        for plant in result.plants:
            matches = by_identifier.get(plant.normalized_external_id, [])
            if plant.normalized_external_id in result.duplicate_external_ids or len(matches) > 1:
                state = "conflict"
            elif not matches:
                state = "unmapped"
            elif matches[0].mapping_status == "active" and matches[0].valid_to is None:
                state = "mapped"
            else:
                state = "invalid"
            values.append((plant.external_id, plant.external_name, state, tuple(mapping.id for mapping in matches)))
        return values

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

    def _preflight(self, connection: ProviderConnection) -> bool:
        return connection.provider_code == ProviderCode.SIGENERGY.value and connection.enabled and connection.configuration_status == "configured" and self._settings.capabilities.get("provider_reads", False)

    def _failed_preflight(self, run_id: int, connection: ProviderConnection) -> SigenergyDiscoveryResult:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            reads_disabled = not self._settings.capabilities.get("provider_reads", False)
            error = ProviderError(ProviderErrorCode.NOT_SUPPORTED if reads_disabled else ProviderErrorCode.CONFIGURATION, "Sigenergy operation was deferred by provider-read safety policy." if reads_disabled else "Sigenergy connection is not enabled and configured.")
            if not reads_disabled:
                record_health(session, provider_connection_id=connection.id, auth_state="not_configured", access_state="not_configured", provider_state="not_configured", discovery_state="not_configured", error=error)
            if run is not None and run.status == "running":
                finish_sync_run(session, run=run, status="deferred" if reads_disabled else "failed", completeness="none", error=error)
                session.commit()
        status = "deferred" if reads_disabled else "failed"
        return SigenergyDiscoveryResult(connection.id, (), frozenset(), status, "none", run_id, error.code.value)

    def _finish(self, run_id: int, connection_id: int, plants: tuple[SigenergyPlant, ...], duplicates: frozenset[str], error: ProviderError | None, *, status: str, completeness: str, metadata: dict[str, Any] | None = None) -> SigenergyDiscoveryResult:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {"actual_provider_calls": int(session.scalar(select(func.count()).select_from(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == run_id, ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")))) or 0), **(metadata or {})}
            health_values = health_values_for_error(error, operation="discovery")
            record_health(session, provider_connection_id=connection_id, error=error, partial=status == "partial", **health_values)
            finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
            session.commit()
        return SigenergyDiscoveryResult(connection_id, plants, duplicates, status, completeness, run_id, error.code.value if error else None)


def plant_from_payload(connection_id: int, row: dict[str, Any]) -> SigenergyPlant:
    external_id = str(row.get("systemId") or "").strip()
    if not external_id or len(external_id) > 255:
        raise ValueError("Sigenergy discovery row has no stable system identifier.")
    external_name = str(row.get("systemName") or "").strip() or None
    raw_status = row.get("status") or row.get("systemStatus") or row.get("runningStatus") or row.get("state")
    metadata = {}
    for key in ("pvCapacity", "batteryCapacity"):
        if row.get(key) not in (None, ""):
            metadata[key] = str(row[key])[:64]
    return SigenergyPlant(connection_id, external_id, external_name, str(raw_status).strip() if raw_status not in (None, "") else None, metadata, utc_now())
