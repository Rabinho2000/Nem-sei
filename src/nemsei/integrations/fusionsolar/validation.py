"""Explicit, single-asset FusionSolar read-only validation orchestration.

This module is the only composition point for the operator-triggered validation
workflow.  It never changes mappings or source policies and it performs all
preflight/audit writes in short database units of work before handing control to
the existing provider services.  Those services keep network calls outside DB
transactions and persist their own SyncRun/request evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.discovery import DiscoveryResult, MappingValidation
from nemsei.integrations.fusionsolar.discovery import MappingValidationStatus as ValidationStatus
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.integrations.fusionsolar.service import FusionSolarDiscoveryService
from nemsei.providers.audit import record_operator_action
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.preflight import ActivationPreflight, activation_preflight
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.sources.service import source_policy_date_for_asset
from nemsei.sync.models import ProviderRequestAttempt


@dataclass(frozen=True)
class SingleAssetValidationResult:
    mapping_id: int
    status: str
    provider_calls: int
    findings: tuple[str, ...]
    current_preflight: ActivationPreflight
    production_preflight: ActivationPreflight
    discovery_status: str = "not_run"
    mapping_status: str = "not_run"
    monitoring_status: str = "not_run"
    production_status: str = "not_run"
    discovery_sync_run_id: int | None = None
    monitoring_sync_run_id: int | None = None
    production_sync_run_id: int | None = None


class FusionSolarSingleAssetValidation:
    """Run exactly one explicitly selected FusionSolar mapping validation."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        discovery_factory: Callable[..., FusionSolarDiscoveryService] = FusionSolarDiscoveryService,
        monitoring_factory: Callable[..., FusionSolarMonitoringService] = FusionSolarMonitoringService,
        production_factory: Callable[..., FusionSolarProductionService] = FusionSolarProductionService,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._discovery_factory = discovery_factory
        self._monitoring_factory = monitoring_factory
        self._production_factory = production_factory

    def run(self, mapping_id: int, *, actor_username: str, on_date: date | None = None) -> SingleAssetValidationResult:
        with self._sessions() as session:
            current = activation_preflight(
                session,
                settings=self._settings,
                mapping_id=mapping_id,
                capability=ProviderCapability.CURRENT_MONITORING,
                on_date=on_date,
            )
            production = activation_preflight(
                session,
                settings=self._settings,
                mapping_id=mapping_id,
                capability=ProviderCapability.PRODUCTION_HISTORY,
                on_date=on_date,
            )
            findings = list(dict.fromkeys(
                [item.code for item in current.blocking_findings]
                + [item.code for item in production.blocking_findings]
            ))
            mapping = session.get(AssetProviderMapping, mapping_id)
            connection = session.get(ProviderConnection, mapping.provider_connection_id) if mapping else None
            if connection is None or not mapping or connection.provider_code != ProviderCode.FUSIONSOLAR.value:
                findings.append("provider_not_fusionsolar")
            else:
                active = [item for item in ProviderRepository(session).current_mappings_for_connection(connection.id) if item.mapping_status == "active"]
                if len(active) != 1 or active[0].id != mapping.id:
                    findings.append("single_asset_scope_required")
            if findings:
                record_operator_action(
                    session,
                    actor_username=actor_username,
                    action="validation_requested",
                    entity_type="asset_provider_mapping",
                    entity_id=mapping_id,
                    metadata={
                        "mapping_id": mapping_id,
                        "capability": "fusionsolar_single_asset_validation",
                        "finding_codes": findings[:20],
                        "provider_call_count": 0,
                        "result_status": "blocked",
                    },
                )
                session.commit()
                return SingleAssetValidationResult(
                    mapping_id,
                    "blocked",
                    0,
                    tuple(findings),
                    current,
                    production,
                )
            assert connection is not None and mapping is not None
            source_day = on_date
            if source_day is None:
                source_day = source_policy_date_for_asset(session, asset_id=mapping.asset_id)
            record_operator_action(
                session,
                actor_username=actor_username,
                action="validation_requested",
                entity_type="asset_provider_mapping",
                entity_id=mapping_id,
                metadata={
                    "mapping_id": mapping_id,
                    "asset_id": mapping.asset_id,
                    "connection_id": connection.id,
                    "provider_code": connection.provider_code,
                    "capability": "fusionsolar_single_asset_validation",
                },
            )
            session.commit()
            connection_id = connection.id

        discovery = self._discovery_factory(self._sessions, self._settings)
        discovery_result: DiscoveryResult = discovery.validate_connection(connection_id)
        mapping_result: MappingValidation = discovery.validate_mapping(mapping_id, discovery=discovery_result)
        calls = self._attempt_count(discovery_result.sync_run_id)
        if mapping_result.status is not ValidationStatus.VALID:
            result = self._result(
                mapping_id=mapping_id,
                status="failed",
                calls=calls,
                findings=(f"discovery_{mapping_result.status.value}",),
                current=current,
                production=production,
                discovery_status=discovery_result.status,
                mapping_status=mapping_result.status.value,
                discovery_sync_run_id=discovery_result.sync_run_id,
            )
            self._record_result(actor_username, result)
            return result

        monitoring = self._monitoring_factory(self._sessions, self._settings).sync_current_monitoring(connection_id)
        calls += self._attempt_count(monitoring.sync_run_id)
        production_result = self._production_factory(self._sessions, self._settings).sync_incremental(
            connection_id,
            start_date=source_day,
            end_date=source_day,
        )
        calls += self._attempt_count(production_result.sync_run_id)
        overall = "success" if monitoring.status == "success" and production_result.status == "success" else "partial"
        result = self._result(
            mapping_id=mapping_id,
            status=overall,
            calls=calls,
            findings=(),
            current=current,
            production=production,
            discovery_status=discovery_result.status,
            mapping_status=mapping_result.status.value,
            monitoring_status=monitoring.status,
            production_status=production_result.status,
            discovery_sync_run_id=discovery_result.sync_run_id,
            monitoring_sync_run_id=monitoring.sync_run_id,
            production_sync_run_id=production_result.sync_run_id,
        )
        self._record_result(actor_username, result)
        return result

    def _record_result(self, actor_username: str, result: SingleAssetValidationResult) -> None:
        with self._sessions() as session:
            record_operator_action(
                session,
                actor_username=actor_username,
                action="validation_requested",
                entity_type="asset_provider_mapping",
                entity_id=result.mapping_id,
                metadata={
                    "mapping_id": result.mapping_id,
                    "capability": "fusionsolar_single_asset_validation",
                    "provider_call_count": result.provider_calls,
                    "result_status": result.status,
                    "finding_codes": list(result.findings[:20]),
                },
            )
            session.commit()

    def _attempt_count(self, sync_run_id: int) -> int:
        with self._sessions() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderRequestAttempt)
                    .where(ProviderRequestAttempt.sync_run_id == sync_run_id)
                )
                or 0
            )

    @staticmethod
    def _result(*, mapping_id: int, status: str, calls: int, findings: tuple[str, ...], current: ActivationPreflight, production: ActivationPreflight, discovery_status: str, mapping_status: str, monitoring_status: str = "not_run", production_status: str = "not_run", discovery_sync_run_id: int | None = None, monitoring_sync_run_id: int | None = None, production_sync_run_id: int | None = None) -> SingleAssetValidationResult:
        return SingleAssetValidationResult(mapping_id, status, calls, findings, current, production, discovery_status, mapping_status, monitoring_status, production_status, discovery_sync_run_id, monitoring_sync_run_id, production_sync_run_id)
