"""FusionSolar current plant monitoring normalized into canonical observations.

The verified current-state response has no reliable provider observation time.
The canonical record therefore marks its timestamp source as ingestion time and
leaves freshness `unknown`; it never interprets stale data as offline.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.integrations.fusionsolar.service import credentials_for
from nemsei.monitoring.service import confirm_current_monitoring, record_current_monitoring_attempt
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sources.service import resolve_source_policy
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from nemsei.sync.service import finish_sync_run, record_health, start_sync_run


LISBON = ZoneInfo("Europe/Lisbon")


@dataclass(frozen=True)
class CurrentMonitoringSample:
    external_id: str
    raw_status_code: str | None
    condition: str
    quality: str
    completeness: str


@dataclass(frozen=True)
class MonitoringSyncResult:
    connection_id: int
    sync_run_id: int
    status: str
    completeness: str
    expected: int
    received: int
    accepted: int
    rejected: int
    error_code: str | None = None


def normalize_current_monitoring_row(row: dict) -> CurrentMonitoringSample:
    external_id = str(row.get("stationCode") or "").strip()
    if not external_id:
        raise ValueError("FusionSolar monitoring row has no station code.")
    values = row.get("dataItemMap")
    values = values if isinstance(values, dict) else {}
    raw = values.get("real_health_state")
    raw_code = str(raw).strip() if raw is not None and str(raw).strip() else None
    if raw_code in {"3", "3.0"}:
        return CurrentMonitoringSample(external_id, raw_code, "operational", "complete", "complete")
    if raw_code in {"2", "2.0"}:
        return CurrentMonitoringSample(external_id, raw_code, "fault", "complete", "complete")
    if raw_code in {"1", "1.0"}:
        return CurrentMonitoringSample(external_id, raw_code, "offline", "complete", "complete")
    if raw_code is None:
        return CurrentMonitoringSample(external_id, None, "unknown", "missing", "partial")
    # No warning code is verified for this endpoint. Unrecognized values are
    # explicitly unknown rather than inheriting V1's presentation heuristics.
    return CurrentMonitoringSample(external_id, raw_code, "unknown", "unknown", "complete")


class FusionSolarMonitoringService:
    """Run a single account-level current-monitoring sync for selected mappings."""

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

    def sync_current_monitoring(self, connection_id: int) -> MonitoringSyncResult:
        connection, selected, selection_findings = self._selected_mappings(connection_id)
        run = self._start_run(connection_id)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            return self._finish(run.id, connection_id, expected=0, received=0, accepted=0, rejected=0, selection_findings=selection_findings, error=ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."))
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish(run.id, connection_id, expected=0, received=0, accepted=0, rejected=0, selection_findings=selection_findings, error=ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish(run.id, connection_id, expected=len(selected), received=0, accepted=0, rejected=0, selection_findings=selection_findings, error=ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        if not selected:
            error = ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar mapping is selected for monitoring.") if selection_findings else None
            return self._finish(run.id, connection_id, expected=0, received=0, accepted=0, rejected=selection_findings, selection_findings=selection_findings, error=error)
        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, expected=len(selected), received=0, accepted=0, rejected=0, selection_findings=selection_findings, error=exc.error)

        client = self._client_factory(credentials)
        _value, error = self._calls.call(
            connection_id=connection_id,
            sync_run_id=run.id,
            endpoint_family="authentication",
            purpose="fusionsolar_monitoring_authentication",
            operation=client.authenticate,
        )
        if error:
            return self._finish(run.id, connection_id, expected=len(selected), received=0, accepted=0, rejected=0, selection_findings=selection_findings, error=error)

        by_external_id = {mapping.normalized_external_id: mapping for mapping in selected}
        received = accepted = rejected = 0
        first_error: ProviderError | None = None
        evaluated: set[str] = set()
        for batch in _batches(selected, 100):
            codes = [mapping.external_id for mapping in batch]
            self._record_attempts(batch)
            rows, error = self._calls.call(
                connection_id=connection_id,
                sync_run_id=run.id,
                endpoint_family="current_monitoring",
                purpose="fusionsolar_current_monitoring",
                operation=lambda codes=codes: client.current_monitoring_batch(codes),
            )
            if error:
                first_error = error
                break
            assert rows is not None
            batch_samples: dict[str, CurrentMonitoringSample] = {}
            for row in rows:
                received += 1
                try:
                    sample = normalize_current_monitoring_row(row)
                    normalized = normalize_external_id(ProviderCode.FUSIONSOLAR, sample.external_id)
                except ValueError:
                    rejected += 1
                    continue
                if normalized not in by_external_id or normalized in batch_samples:
                    rejected += 1
                    continue
                batch_samples[normalized] = sample
            accepted += self._persist_samples(run.id, by_external_id, batch_samples)
            rejected += len(batch) - len(batch_samples)
            evaluated.update(batch_samples)

        return self._finish(
            run.id,
            connection_id,
            expected=len(selected),
            received=received,
            accepted=accepted,
            rejected=rejected,
            selection_findings=selection_findings,
            error=first_error,
            evaluated=len(evaluated),
        )

    def _selected_mappings(self, connection_id: int) -> tuple[ProviderConnection, list[AssetProviderMapping], int]:
        with self._sessions() as session:
            repository = ProviderRepository(session)
            connection = repository.connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            candidates = [mapping for mapping in repository.current_mappings_for_connection(connection_id) if mapping.mapping_status == "active"]
            selected: list[AssetProviderMapping] = []
            findings = 0
            on_date = datetime.now(LISBON).date()
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

    def _persist_samples(
        self,
        sync_run_id: int,
        mappings: dict[str, AssetProviderMapping],
        samples: dict[str, CurrentMonitoringSample],
    ) -> int:
        with self._sessions() as session:
            for normalized, sample in samples.items():
                mapping = mappings[normalized]
                confirm_current_monitoring(
                    session,
                    asset_id=mapping.asset_id,
                    provider_mapping_id=mapping.id,
                    source_observation_key=f"fusionsolar-current:{normalized}",
                    # The endpoint response has no verified source timestamp.
                    observed_at=utc_now(),
                    condition=sample.condition,
                    freshness="unknown",
                    quality=sample.quality,
                    completeness=sample.completeness,
                    sync_run_id=sync_run_id,
                    raw_status_code=sample.raw_status_code,
                    metadata={"observed_at_source": "ingested_at_no_provider_timestamp"},
                    deduplicate_observed_at=True,
                )
            session.commit()
        return len(samples)

    def _record_attempts(self, mappings: list[AssetProviderMapping]) -> None:
        with self._sessions() as session:
            record_current_monitoring_attempt(
                session,
                provider_mapping_ids=[mapping.id for mapping in mappings],
            )
            session.commit()

    def _finish(
        self,
        run_id: int,
        connection_id: int,
        *,
        expected: int,
        received: int,
        accepted: int,
        rejected: int,
        selection_findings: int,
        error: ProviderError | None,
        evaluated: int = 0,
        deferred: bool = False,
    ) -> MonitoringSyncResult:
        if deferred:
            status, completeness = "deferred", "none"
        elif error and accepted:
            status, completeness = "partial", "partial"
        elif error and error.code is ProviderErrorCode.RATE_LIMITED:
            status, completeness = "rate_limited", "none"
        elif error:
            status, completeness = "failed", "none"
        elif rejected or evaluated != expected:
            status, completeness = "partial", "partial"
        else:
            status, completeness = "success", "complete"
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {
                "actual_provider_calls": _calls(session, run_id),
                "expected_items": expected,
                "items_received": received,
                "items_accepted": accepted,
                "items_rejected": rejected,
                "source_policy_findings": selection_findings,
            }
            health = _health_values(error)
            record_health(
                session,
                provider_connection_id=connection_id,
                partial=status == "partial",
                error=error,
                **health,
            )
            finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
            session.commit()
        return MonitoringSyncResult(connection_id, run_id, status, completeness, expected, received, accepted, rejected, error.code.value if error else None)


def _batches(values: list[AssetProviderMapping], size: int):
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def _calls(session: Session, run_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ProviderRequestAttempt)
            .where(
                ProviderRequestAttempt.sync_run_id == run_id,
                ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")),
            )
        )
        or 0
    )


def _health_values(error: ProviderError | None) -> dict[str, str]:
    if error is None:
        return {
            "auth_state": "healthy",
            "access_state": "healthy",
            "provider_state": "healthy",
            "quota_state": "unknown",
        }
    if error.code is ProviderErrorCode.AUTHENTICATION:
        return {"auth_state": "degraded", "access_state": "unknown", "provider_state": "healthy"}
    if error.code is ProviderErrorCode.AUTHORIZATION:
        return {"auth_state": "healthy", "access_state": "degraded", "provider_state": "healthy"}
    if error.code is ProviderErrorCode.RATE_LIMITED:
        return {"provider_state": "healthy", "quota_state": "degraded"}
    return {"provider_state": "unavailable"}
