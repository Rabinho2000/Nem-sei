"""Sigenergy energy-flow reads normalized to canonical monitoring evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.sigenergy.client import SigenergyClient, SigenergyClientError, SigenergyCredentials, SigenergyEndpoints, SigenergyTransport
from nemsei.integrations.sigenergy.request_control import SigenergyRequestController
from nemsei.integrations.sigenergy.service import credentials_for
from nemsei.monitoring.service import confirm_current_monitoring, record_current_monitoring_attempt
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sources.service import resolve_source_policy
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from nemsei.sync.service import finish_sync_run, record_health, start_sync_run


@dataclass(frozen=True)
class SigenergyMonitoringSample:
    external_id: str
    condition: str
    quality: str
    completeness: str
    raw_status_code: str | None
    raw_status_text: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SigenergyMonitoringResult:
    connection_id: int
    sync_run_id: int
    status: str
    completeness: str
    expected: int
    received: int
    accepted: int
    rejected: int
    error_code: str | None = None


class SigenergyMonitoringService:
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

    def sync_current_monitoring(self, connection_id: int) -> SigenergyMonitoringResult:
        connection, selected, findings = self._selected_mappings(connection_id)
        run = self._start_run(connection_id)
        if connection.provider_code != ProviderCode.SIGENERGY.value:
            return self._finish(run.id, connection_id, len(selected), 0, 0, findings, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not Sigenergy."))
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish(run.id, connection_id, len(selected), 0, 0, findings, ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish(run.id, connection_id, len(selected), 0, 0, findings, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        if not selected:
            error = ProviderError(ProviderErrorCode.CONFIGURATION, "No Sigenergy mapping is selected for monitoring.") if findings else None
            return self._finish(run.id, connection_id, 0, 0, 0, findings, error)
        try:
            credentials, endpoints = credentials_for(connection)
        except SigenergyClientError as exc:
            return self._finish(run.id, connection_id, len(selected), 0, 0, findings, exc.error)
        client = self._client_factory(credentials, endpoints, self._transport)
        _value, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="authentication", purpose="sigenergy_monitoring_authentication", operation=client.authenticate)
        if error:
            return self._finish(run.id, connection_id, len(selected), 0, 0, findings, error)
        self._record_attempts(selected)
        received = accepted = rejected = 0
        first_error: ProviderError | None = None
        incomplete = False
        for mapping in selected:
            flow, error = self._calls.call(connection_id=connection_id, sync_run_id=run.id, endpoint_family="current_monitoring", purpose="sigenergy_current_monitoring", operation=lambda mapping=mapping: client.get_energy_flow(mapping.external_id))
            if error:
                first_error = error
                break
            assert flow is not None
            received += 1
            try:
                sample = normalize_energy_flow(mapping.external_id, flow)
            except ValueError:
                rejected += 1
                continue
            accepted += 1
            incomplete = incomplete or sample.completeness != "complete"
            self._persist_sample(run.id, mapping, sample)
        status_error = first_error
        return self._finish(run.id, connection_id, len(selected), received, accepted, findings, status_error, rejected=rejected, incomplete=incomplete)

    def _selected_mappings(self, connection_id: int) -> tuple[ProviderConnection, list[AssetProviderMapping], int]:
        with self._sessions() as session:
            connection = ProviderRepository(session).connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            candidates = [mapping for mapping in ProviderRepository(session).current_mappings_for_connection(connection_id) if mapping.mapping_status == "active"]
            selected = []
            findings = 0
            on_date = datetime.now(ZoneInfo("Europe/Lisbon")).date()
            for mapping in candidates:
                try:
                    policy = resolve_source_policy(session, asset_id=mapping.asset_id, source_use="monitoring", on_date=on_date)
                except ValueError:
                    findings += 1
                    continue
                if policy.provider_mapping_id == mapping.id:
                    selected.append(mapping)
            session.expunge(connection)
            for mapping in selected:
                session.expunge(mapping)
            return connection, selected, findings

    def _start_run(self, connection_id: int) -> SyncRun:
        with self._sessions() as session:
            run = start_sync_run(session, provider_connection_id=connection_id, capability=ProviderCapability.CURRENT_MONITORING.value)
            session.commit()
            session.expunge(run)
            return run

    def _record_attempts(self, mappings: list[AssetProviderMapping]) -> None:
        with self._sessions() as session:
            record_current_monitoring_attempt(session, provider_mapping_ids=[mapping.id for mapping in mappings])
            session.commit()

    def _persist_sample(self, run_id: int, mapping: AssetProviderMapping, sample: SigenergyMonitoringSample) -> None:
        with self._sessions() as session:
            confirm_current_monitoring(session, asset_id=mapping.asset_id, provider_mapping_id=mapping.id, source_observation_key=f"sigenergy-current:{normalize_external_id(ProviderCode.SIGENERGY, sample.external_id)}", observed_at=utc_now(), condition=sample.condition, freshness="unknown", quality=sample.quality, completeness=sample.completeness, sync_run_id=run_id, raw_status_code=sample.raw_status_code, raw_status_text=sample.raw_status_text, metadata=sample.metadata, deduplicate_observed_at=True)
            session.commit()

    def _finish(self, run_id: int, connection_id: int, expected: int, received: int, accepted: int, findings: int, error: ProviderError | None, *, rejected: int = 0, incomplete: bool = False, deferred: bool = False) -> SigenergyMonitoringResult:
        if deferred:
            status, completeness = "deferred", "none"
        elif error and accepted:
            status, completeness = "partial", "partial"
        elif error and error.code is ProviderErrorCode.RATE_LIMITED:
            status, completeness = "rate_limited", "none"
        elif error:
            status, completeness = "failed", "none"
        elif rejected or findings or received != expected or accepted != expected or incomplete:
            status, completeness = "partial", "partial"
        else:
            status, completeness = "success", "complete"
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {"actual_provider_calls": int(session.scalar(select(func.count()).select_from(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == run_id, ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")))) or 0), "expected_items": expected, "items_received": received, "items_accepted": accepted, "items_rejected": rejected, "source_policy_findings": findings}
            if error and error.code is ProviderErrorCode.CONFIGURATION:
                health_values = {"auth_state": "not_configured", "access_state": "not_configured", "provider_state": "not_configured", "quota_state": "unknown"}
            else:
                health_values = {"auth_state": "healthy" if error is None else ("degraded" if error.code in {ProviderErrorCode.AUTHENTICATION, ProviderErrorCode.AUTHORIZATION} else "unknown"), "access_state": "healthy" if error is None else "unknown", "provider_state": "healthy" if error is None or error.code is ProviderErrorCode.RATE_LIMITED else "unavailable", "quota_state": "degraded" if error and error.code is ProviderErrorCode.RATE_LIMITED else "unknown"}
            record_health(session, provider_connection_id=connection_id, sync_state="healthy" if status == "success" else "degraded", partial=status == "partial", error=error, **health_values)
            finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
            session.commit()
        return SigenergyMonitoringResult(connection_id, run_id, status, completeness, expected, received, accepted, rejected, error.code.value if error else None)


def normalize_energy_flow(external_id: str, flow: dict[str, Any]) -> SigenergyMonitoringSample:
    raw = flow.get("status") or flow.get("systemStatus") or flow.get("runningStatus") or flow.get("state")
    raw_code = str(raw).strip() if raw not in (None, "") else None
    condition = normalize_status(raw_code)
    quality = "complete" if raw_code is not None else "missing"
    completeness = "complete" if raw_code is not None else "partial"
    metadata = {"observed_at_source": "ingested_at_no_provider_timestamp", "energy_fields_present": sorted(key for key in ("pvPower", "gridPower", "batteryPower", "batterySoc", "loadPower") if flow.get(key) not in (None, ""))}
    return SigenergyMonitoringSample(external_id, condition, quality, completeness, raw_code, raw_code, metadata)


def normalize_status(raw_status: str | None) -> str:
    normalized = " ".join((raw_status or "").strip().lower().replace("_", " ").replace("-", " ").split())
    if normalized in {"normal", "online", "running"}:
        return "operational"
    if normalized in {"fault", "error", "abnormal"}:
        return "fault"
    if normalized in {"offline", "disconnected"}:
        return "offline"
    return "unknown"
