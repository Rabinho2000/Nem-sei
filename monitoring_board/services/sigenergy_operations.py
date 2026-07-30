from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from monitoring_board.repositories import sigenergy as repository
from monitoring_board.services.sigenergy_contracts import (
    AccessOutcome,
    AccessStatus,
    CredentialOutcome,
    CredentialStatus,
    CredentialTestResult,
    DiscoveryResult,
    DiscoveryStatus,
    FailureCategory,
    FailureClassification,
    OPERATION_ACCESS,
    OPERATION_CREDENTIALS,
    OPERATION_DISCOVERY,
    OPERATION_STATE_SYNC,
    SIGENERGY_PROVIDER,
    SyncBatchResult,
    SyncStatus,
    SystemAccessResult,
    SystemSyncResult,
    classify_sigenergy_failure,
    normalize_operational_status,
    scoped_error,
    validate_sigenergy_system_id,
)
from monitoring_board.services.sigenergy_models import (
    float_or_none,
    map_sigenergy_status,
    normalize_energy_flow,
    normalize_system,
    sanitize_payload,
)
from monitoring_board.services.production_api_queue import (
    ApiSlotUnavailableError,
)


SigenergyCall = Callable[[str, int, Callable[[], Any]], Any]
MonitoringChange = Callable[
    [int, str, dict[str, Any], str],
    None,
]


def _direct_call(_area: str, _priority: int, callback: Callable[[], Any]) -> Any:
    return callback()


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


class SigenergyIntegrationService:
    """Explicit Sigenergy operations with discovery-independent access paths."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        client: Any,
        execute_call: SigenergyCall | None = None,
        now: Callable[[], str] = _iso_now,
        today: Callable[[], date] = _today,
        monitoring_change: MonitoringChange | None = None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.execute_call = execute_call or _direct_call
        self.now = now
        self.today = today
        self.monitoring_change = monitoring_change
        repository.ensure_sigenergy_repository_schema(conn)

    def test_credentials(self) -> CredentialTestResult:
        attempted_at = self.now()
        try:
            self.execute_call(
                OPERATION_CREDENTIALS,
                1,
                self.client.authenticate,
            )
        except ApiSlotUnavailableError:
            raise
        except Exception as exc:
            failure = classify_sigenergy_failure(
                exc,
                operation=OPERATION_CREDENTIALS,
            )
            error = scoped_error(
                failure,
                operation=OPERATION_CREDENTIALS,
                external_id="",
                occurred_at=attempted_at,
            )
            if failure.category is FailureCategory.AUTH_FAILED:
                repository.record_scoped_error(
                    self.conn,
                    error,
                    state_status=CredentialStatus.INVALID.value,
                )
                return CredentialTestResult(
                    CredentialOutcome.AUTH_FAILED,
                    CredentialStatus.INVALID,
                    attempted_at,
                    message=failure.message,
                    error=error,
                )
            if failure.category is FailureCategory.RATE_LIMITED:
                repository.record_scoped_error(
                    self.conn,
                    error,
                    state_status=CredentialStatus.RATE_LIMITED.value,
                )
                return CredentialTestResult(
                    CredentialOutcome.RATE_LIMITED,
                    CredentialStatus.RATE_LIMITED,
                    attempted_at,
                    message=failure.message,
                    error=error,
                )
            repository.record_scoped_error(
                self.conn,
                error,
                state_status=CredentialStatus.ERROR.value,
            )
            return CredentialTestResult(
                CredentialOutcome.PROVIDER_ERROR,
                CredentialStatus.ERROR,
                attempted_at,
                message=failure.message,
                error=error,
            )

        repository.record_operation_result(
            self.conn,
            operation=OPERATION_CREDENTIALS,
            status=CredentialStatus.VALID.value,
            occurred_at=attempted_at,
            metadata={
                "outcome": CredentialOutcome.AUTHENTICATED.value,
            },
            succeeded=True,
        )
        return CredentialTestResult(
            CredentialOutcome.AUTHENTICATED,
            CredentialStatus.VALID,
            attempted_at,
        )

    def discover_systems(self, *, persist: bool = True) -> DiscoveryResult:
        attempted_at = self.now()
        try:
            raw_systems = self.execute_call(
                OPERATION_DISCOVERY,
                1,
                lambda: self.client.list_systems(allow_empty=True),
            )
            if not isinstance(raw_systems, (list, tuple)):
                raise ValueError(
                    "A descoberta Sigenergy devolveu um payload invalido."
                )
            validated_systems: list[dict[str, Any]] = []
            for row in raw_systems:
                if not isinstance(row, dict):
                    raise ValueError(
                        "A descoberta Sigenergy devolveu uma linha invalida."
                    )
                normalize_system(row)
                validated_systems.append(sanitize_payload(row))
            systems = tuple(validated_systems)
        except ApiSlotUnavailableError:
            raise
        except Exception as exc:
            failure = classify_sigenergy_failure(
                exc,
                operation=OPERATION_DISCOVERY,
            )
            error = scoped_error(
                failure,
                operation=OPERATION_DISCOVERY,
                external_id="",
                occurred_at=attempted_at,
            )
            if failure.category is FailureCategory.RESTRICTED:
                repository.record_scoped_error(
                    self.conn,
                    error,
                    state_status=DiscoveryStatus.RESTRICTED.value,
                )
                return DiscoveryResult(
                    DiscoveryStatus.RESTRICTED,
                    (),
                    attempted_at,
                    message=(
                        "A Sigenergy restringe a listagem global para esta "
                        "App Key. Sistemas conhecidos podem ser verificados "
                        "diretamente pelo System ID."
                    ),
                    error=error,
                )
            if failure.category is FailureCategory.RATE_LIMITED:
                repository.record_scoped_error(
                    self.conn,
                    error,
                    state_status=DiscoveryStatus.RATE_LIMITED.value,
                )
                return DiscoveryResult(
                    DiscoveryStatus.RATE_LIMITED,
                    (),
                    attempted_at,
                    message=failure.message,
                    error=error,
                    cooldown_until=failure.cooldown_until,
                )
            repository.record_scoped_error(
                self.conn,
                error,
                state_status=DiscoveryStatus.ERROR.value,
            )
            return DiscoveryResult(
                DiscoveryStatus.ERROR,
                (),
                attempted_at,
                message=failure.message,
                error=error,
            )

        status = (
            DiscoveryStatus.SUCCESS if systems else DiscoveryStatus.EMPTY
        )
        if persist and systems:
            repository.upsert_discovered_systems(
                self.conn,
                systems,
                discovered_at=attempted_at,
            )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_DISCOVERY,
            status=status.value,
            occurred_at=attempted_at,
            metadata={
                "system_count": len(systems),
                "external_ids": [
                    str(
                        row.get("systemId")
                        or row.get("id")
                        or row.get("stationId")
                        or row.get("plantId")
                        or ""
                    ).strip()
                    for row in systems
                ],
            },
            succeeded=True,
        )
        return DiscoveryResult(status, systems, attempted_at)

    def verify_system_access(
        self,
        system_id: str,
        *,
        external_name: str = "",
    ) -> SystemAccessResult:
        attempted_at = self.now()
        try:
            external_id = validate_sigenergy_system_id(system_id)
        except ValueError as exc:
            failure = classify_sigenergy_failure(
                exc,
                operation=OPERATION_ACCESS,
            )
            error = scoped_error(
                failure,
                operation=OPERATION_ACCESS,
                external_id=str(system_id or "").strip(),
                occurred_at=attempted_at,
            )
            repository.record_scoped_error(
                self.conn,
                error,
                state_status=AccessStatus.ERROR.value,
            )
            return SystemAccessResult(
                str(system_id or "").strip(),
                AccessOutcome.PROVIDER_ERROR,
                AccessStatus.ERROR,
                attempted_at,
                message=failure.message,
                error=error,
            )

        identity = self._system_identity(external_id, external_name)
        try:
            energy_flow = self.execute_call(
                OPERATION_ACCESS,
                2,
                lambda: self.client.get_energy_flow(external_id),
            )
            if not isinstance(energy_flow, dict):
                raise ValueError(
                    "O energyFlow Sigenergy devolveu um payload invalido."
                )
        except ApiSlotUnavailableError:
            raise
        except Exception as exc:
            result = self._access_failure(
                external_id,
                attempted_at,
                exc,
            )
            self._record_legacy_access_validation(result)
            return result

        raw_status = (
            energy_flow.get("systemStatus")
            or energy_flow.get("status")
            or energy_flow.get("runningStatus")
            or energy_flow.get("state")
            or identity.get("status")
            or identity.get("systemStatus")
            or identity.get("runningStatus")
            or identity.get("state")
        )
        operational_status = normalize_operational_status(raw_status)
        resolved_name = str(
            identity.get("systemName")
            or identity.get("name")
            or external_name
            or external_id
        ).strip()
        repository.upsert_accessible_system(
            self.conn,
            external_id=external_id,
            external_name=resolved_name,
            energy_flow=energy_flow,
            operational_status=operational_status,
            observed_at=attempted_at,
        )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_ACCESS,
            external_id=external_id,
            status=AccessOutcome.ACCESSIBLE.value,
            occurred_at=attempted_at,
            metadata={
                "validation_method": "direct_energy_flow",
                "energy_flow_fields": sorted(str(key) for key in energy_flow),
            },
            succeeded=True,
        )
        result = SystemAccessResult(
            external_id,
            AccessOutcome.ACCESSIBLE,
            AccessStatus.ACCESSIBLE,
            attempted_at,
            external_name=resolved_name,
            operational_status=operational_status,
            energy_flow=sanitize_payload(energy_flow),
            metadata=sanitize_payload(identity),
        )
        self._record_legacy_access_validation(result)
        return result

    def sync_system(
        self,
        system_id: str,
        *,
        mapping: dict[str, Any] | None = None,
        batch_id: int | None = None,
    ) -> SystemSyncResult:
        attempted_at = self.now()
        external_id = validate_sigenergy_system_id(system_id)
        mapping = mapping or repository.mapping_for_system(
            self.conn,
            external_id,
        )
        if mapping is None:
            status = SyncStatus.FAILED
            message = (
                "O System ID Sigenergy nao tem um mapping ativo e nao pode "
                "ser sincronizado."
            )
            error = scoped_error(
                classify_sigenergy_failure(
                    ValueError(message),
                    operation=OPERATION_STATE_SYNC,
                ),
                operation=OPERATION_STATE_SYNC,
                external_id=external_id,
                occurred_at=attempted_at,
            )
            repository.record_scoped_error(
                self.conn,
                error,
                state_status=status.value,
            )
            return SystemSyncResult(
                external_id,
                SyncStatus.FAILED,
                attempted_at,
                error=error,
            )

        repository.ensure_mapped_inventory(
            self.conn,
            external_id=external_id,
            external_name=str(
                mapping.get("external_name")
                or mapping.get("project_name")
                or external_id
            ),
            observed_at=attempted_at,
        )
        try:
            flow = self.execute_call(
                OPERATION_STATE_SYNC,
                2,
                lambda: self.client.get_energy_flow(external_id),
            )
            if not isinstance(flow, dict):
                raise ValueError(
                    "O energyFlow Sigenergy devolveu um payload invalido."
                )
        except ApiSlotUnavailableError:
            raise
        except Exception as exc:
            failure = classify_sigenergy_failure(
                exc,
                operation=OPERATION_STATE_SYNC,
            )
            access_status = {
                FailureCategory.UNAUTHORIZED: AccessStatus.UNAUTHORIZED,
                FailureCategory.NOT_FOUND: AccessStatus.NOT_FOUND,
            }.get(failure.category, AccessStatus.UNKNOWN)
            status = (
                SyncStatus.RATE_LIMITED
                if failure.category is FailureCategory.RATE_LIMITED
                else SyncStatus.FAILED
            )
            error = scoped_error(
                failure,
                operation=OPERATION_STATE_SYNC,
                external_id=external_id,
                occurred_at=attempted_at,
            )
            repository.record_scoped_error(
                self.conn,
                error,
                state_status=status.value,
            )
            repository.update_sync_failure(
                self.conn,
                external_id=external_id,
                sync_status=status,
                message=failure.message,
                attempted_at=attempted_at,
                access_status=access_status,
            )
            return SystemSyncResult(
                external_id,
                status,
                attempted_at,
                asset_id=int(mapping["asset_id"]),
                access_status=access_status,
                error=error,
                cooldown_until=failure.cooldown_until,
            )

        identity = self._system_identity(
            external_id,
            str(
                mapping.get("external_name")
                or mapping.get("project_name")
                or external_id
            ),
        )
        row = normalize_sigenergy_live_row(identity, flow)
        operational_status = normalize_operational_status(row["raw_status"])
        resolved_name = str(row["external_name"] or external_id)
        repository.upsert_accessible_system(
            self.conn,
            external_id=external_id,
            external_name=resolved_name,
            energy_flow=flow,
            operational_status=operational_status,
            observed_at=attempted_at,
        )
        snapshot_id = repository.insert_realtime_snapshot(
            self.conn,
            asset_id=int(mapping["asset_id"]),
            row=row,
            collected_at=attempted_at,
        )
        previous = self.conn.execute(
            """
            SELECT status, record_date, source
            FROM monitoring_records
            WHERE asset_id = ?
            ORDER BY record_date DESC, id DESC
            LIMIT 1
            """,
            (int(mapping["asset_id"]),),
        ).fetchone()
        record_date = self.today().isoformat()
        duplicate = (
            previous is not None
            and str(previous["status"]) == str(row["status"])
            and str(previous["record_date"]) == record_date
            and str(previous["source"]) == SIGENERGY_PROVIDER
        )
        if not duplicate:
            self.conn.execute(
                """
                INSERT INTO monitoring_records (
                    asset_id, status, record_date, notes, source, batch_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(mapping["asset_id"]),
                    row["status"],
                    record_date,
                    row["notes"],
                    SIGENERGY_PROVIDER,
                    batch_id,
                ),
            )
            if self.monitoring_change is not None:
                self.monitoring_change(
                    int(mapping["asset_id"]),
                    str(previous["status"]) if previous is not None else "",
                    row,
                    attempted_at,
                )
        repository.update_sync_success(
            self.conn,
            external_id=external_id,
            external_name=resolved_name,
            operational_status=operational_status,
            legacy_status=str(row["status"]),
            collected_at=attempted_at,
        )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_STATE_SYNC,
            external_id=external_id,
            status=SyncStatus.SUCCESS.value,
            occurred_at=attempted_at,
            metadata={"snapshot_id": snapshot_id, "asset_id": mapping["asset_id"]},
            succeeded=True,
        )
        return SystemSyncResult(
            external_id,
            SyncStatus.SUCCESS,
            attempted_at,
            asset_id=int(mapping["asset_id"]),
            snapshot_id=snapshot_id,
            access_status=AccessStatus.ACCESSIBLE,
            operational_status=operational_status,
            normalized_row=row,
        )

    def sync_all_mappings(
        self,
        *,
        target_external_ids: Iterable[str] | None = None,
        batch_id: int | None = None,
    ) -> SyncBatchResult:
        started_at = self.now()
        targets: list[str] | None = None
        if target_external_ids is not None:
            targets = list(
                dict.fromkeys(
                    validate_sigenergy_system_id(item)
                    for item in target_external_ids
                )
            )
        mappings = repository.list_enabled_mappings(
            self.conn,
            target_external_ids=targets,
        )
        by_id = {str(row["external_id"]): row for row in mappings}
        candidates: list[tuple[str, dict[str, Any] | None]]
        if targets is None:
            candidates = [
                (str(row["external_id"]), row) for row in mappings
            ]
        else:
            candidates = [
                (external_id, by_id.get(external_id))
                for external_id in targets
            ]

        results: list[SystemSyncResult] = []
        rate_limit_until: datetime | None = None
        for external_id, mapping in candidates:
            if rate_limit_until is not None:
                attempted_at = self.now()
                message = (
                    "Sync nao iniciado porque outra instalacao atingiu o "
                    "rate limit da conta Sigenergy."
                )
                error = scoped_error(
                    FailureClassification(
                        FailureCategory.RATE_LIMITED,
                        message,
                        http_status=429,
                        cooldown_until=rate_limit_until,
                    ),
                    operation=OPERATION_STATE_SYNC,
                    external_id=external_id,
                    occurred_at=attempted_at,
                )
                repository.record_scoped_error(
                    self.conn,
                    error,
                    state_status=SyncStatus.RATE_LIMITED.value,
                )
                results.append(
                    SystemSyncResult(
                        external_id,
                        SyncStatus.RATE_LIMITED,
                        attempted_at,
                        asset_id=(
                            int(mapping["asset_id"]) if mapping is not None else None
                        ),
                        error=error,
                        cooldown_until=rate_limit_until,
                    )
                )
                continue
            result = self.sync_system(
                external_id,
                mapping=mapping,
                batch_id=batch_id,
            )
            results.append(result)
            if (
                result.status is SyncStatus.RATE_LIMITED
                and result.cooldown_until is not None
            ):
                rate_limit_until = result.cooldown_until

        status = _batch_status(results)
        return SyncBatchResult(
            status,
            tuple(results),
            started_at,
            self.now(),
        )

    def _system_identity(
        self,
        external_id: str,
        external_name: str,
    ) -> dict[str, Any]:
        inventory = repository.inventory_identity(self.conn, external_id)
        metadata: dict[str, Any] = {}
        if inventory is not None:
            try:
                parsed = json.loads(inventory.get("metadata_json") or "{}")
                if isinstance(parsed, dict):
                    metadata = sanitize_payload(parsed)
            except json.JSONDecodeError:
                metadata = {}
        metadata["systemId"] = external_id
        metadata["systemName"] = (
            external_name
            or (
                str(inventory.get("external_name") or "")
                if inventory is not None
                else ""
            )
            or external_id
        )
        return metadata

    def _access_failure(
        self,
        external_id: str,
        attempted_at: str,
        exc: BaseException,
    ) -> SystemAccessResult:
        failure = classify_sigenergy_failure(
            exc,
            operation=OPERATION_ACCESS,
        )
        outcome = {
            FailureCategory.UNAUTHORIZED: AccessOutcome.UNAUTHORIZED,
            FailureCategory.NOT_FOUND: AccessOutcome.NOT_FOUND,
            FailureCategory.AUTH_FAILED: AccessOutcome.AUTH_FAILED,
            FailureCategory.RATE_LIMITED: AccessOutcome.RATE_LIMITED,
        }.get(failure.category, AccessOutcome.PROVIDER_ERROR)
        access_status = {
            AccessOutcome.UNAUTHORIZED: AccessStatus.UNAUTHORIZED,
            AccessOutcome.NOT_FOUND: AccessStatus.NOT_FOUND,
            AccessOutcome.AUTH_FAILED: AccessStatus.ERROR,
            AccessOutcome.RATE_LIMITED: AccessStatus.ERROR,
            AccessOutcome.PROVIDER_ERROR: AccessStatus.ERROR,
        }[outcome]
        error = scoped_error(
            failure,
            operation=OPERATION_ACCESS,
            external_id=external_id,
            occurred_at=attempted_at,
        )
        repository.record_scoped_error(
            self.conn,
            error,
            state_status=access_status.value,
        )
        repository.update_access_failure_if_known(
            self.conn,
            external_id=external_id,
            access_status=access_status,
            observed_at=attempted_at,
        )
        return SystemAccessResult(
            external_id,
            outcome,
            access_status,
            attempted_at,
            message=failure.message,
            error=error,
            cooldown_until=failure.cooldown_until,
        )

    def _record_legacy_access_validation(
        self,
        result: SystemAccessResult,
    ) -> None:
        details = {
            "source": "direct_energy_flow",
            "discovery_returned": False,
            "energy_flow_fields": sorted(str(key) for key in result.energy_flow),
            "canonical_event_store": "provider_operation_events",
        }
        self.conn.execute(
            """
            INSERT INTO sigenergy_access_validations (
                system_id, validation_method, discovery_returned, outcome,
                status_code, sanitized_error, details_json, created_at
            ) VALUES (?, 'direct_energy_flow', 0, ?, ?, ?, ?, ?)
            """,
            (
                result.external_id,
                (
                    "available"
                    if result.outcome is AccessOutcome.ACCESSIBLE
                    else result.outcome.value
                ),
                result.error.http_status if result.error is not None else None,
                result.message,
                json.dumps(details, ensure_ascii=True, sort_keys=True),
                result.attempted_at,
            ),
        )


def normalize_sigenergy_live_row(
    system_row: dict[str, Any],
    energy_flow: dict[str, Any],
) -> dict[str, Any]:
    normalized_system = normalize_system(system_row)
    normalized_flow = normalize_energy_flow(energy_flow)
    raw_status = (
        energy_flow.get("status")
        or energy_flow.get("systemStatus")
        or energy_flow.get("runningStatus")
        or energy_flow.get("state")
        or system_row.get("status")
        or system_row.get("systemStatus")
        or system_row.get("runningStatus")
        or system_row.get("state")
        or ""
    )
    status = map_sigenergy_status(raw_status)
    combined = {**normalized_system, **normalized_flow}
    notes = build_monitoring_notes(combined)
    debug = [f"system_status={raw_status or 'unknown'}"]
    for key in (
        "pvPower",
        "gridPower",
        "batteryPower",
        "batterySoc",
        "loadPower",
    ):
        if energy_flow.get(key) not in (None, ""):
            debug.append(f"{key}={energy_flow[key]}")
    return {
        **normalized_system,
        **normalized_flow,
        "external_id": normalized_system["external_id"],
        "external_name": normalized_system["external_name"],
        "status": status,
        "raw_status": str(raw_status or "unknown"),
        "notes": f"{notes} | {'; '.join(debug)}",
        "fetch_status": "ok",
        "fetch_error": "",
        "payload": {
            "system": sanitize_payload(system_row),
            "energy_flow": sanitize_payload(energy_flow),
        },
    }


def build_monitoring_notes(row: dict[str, Any]) -> str:
    battery_capacity = float_or_none(row.get("battery_capacity_kwh"))
    has_battery = battery_capacity is not None and battery_capacity > 0
    battery = (
        _format_kw(row.get("battery_power_kw")) if has_battery else "N/A"
    )
    soc = _format_pct(row.get("battery_soc_pct")) if has_battery else "N/A"
    return " | ".join(
        (
            f"PV: {_format_kw(row.get('pv_power_kw'))}",
            f"Carga: {_format_kw(row.get('load_power_kw'))}",
            f"Rede: {_format_kw(row.get('grid_power_kw_raw'))}",
            f"Bateria: {battery}",
            f"SOC: {soc}",
        )
    )


def _format_kw(value: Any) -> str:
    parsed = float_or_none(value)
    return f"{parsed:g} kW" if parsed is not None else "N/A"


def _format_pct(value: Any) -> str:
    parsed = float_or_none(value)
    return f"{parsed:g}%" if parsed is not None else "N/A"


def _batch_status(results: list[SystemSyncResult]) -> SyncStatus:
    if not results:
        return SyncStatus.SUCCESS
    successes = sum(1 for result in results if result.succeeded)
    if successes == len(results):
        return SyncStatus.SUCCESS
    if successes:
        return SyncStatus.PARTIAL
    if any(result.status is SyncStatus.RATE_LIMITED for result in results):
        return SyncStatus.RATE_LIMITED
    return SyncStatus.FAILED
