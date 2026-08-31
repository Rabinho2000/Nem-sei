"""FusionSolar device-level current status, normalized into diagnostic facts.

M7 Fatia 2 (`docs/v2/DEVICE_TELEMETRY.md`). Reads exactly the four signals
that milestone scoped -- inverter state, active power, day energy, and last
communication -- via `getDevList` + `getDevRealKpi`. Both endpoints are
evidenced by V1's own working code
(`monitoring_board/services/fusionsolar_client.py`: `fetch_device_list`,
`fetch_device_realtime_map`), which produced the 51 289 historical rows Fatia
1 already imported, not by documentation this codebase has never seen.

**MPPT/string fields are never parsed here.** V1's own parser
(`parse_fusionsolar_pv_inputs`) reads `pv{n}_i`/`pv{n}_u` from this exact same
`dataItemMap` and found them empty in every one of those 51 289 rows -- the
column exists, the data never arrived, for this account. This module does not
even look at those keys, so they cannot leak in even by accident.

**Alarms are out of scope.** `getAlarmList` is a separate, unaudited contract;
"última comunicação" is answered by `observed_at`/`freshness` on the reading
itself, not by a live alarm feed.

**Active power's unit is not V1-evidenced.** V1 guessed it with a magnitude
heuristic (`normalize_power_to_kw`: divide by 1000 if the raw value exceeds
1000) instead of a verified unit -- precisely the kind of guess
`FusionSolarProductionContract` already refuses to make for `PVYield`. This
module refuses the same way: a connection must explicitly declare its
verified `active_power` and `day_energy` units before any HTTP request or
persistence. A missing verification is a safe configuration failure, not a
silently mis-scaled reading.

**Freshness is derived honestly, not defaulted to true.** V1's own realtime
sync (`app_factory.py`) reads `has_recent_data = True if realtime and
seen_dt is None else ...` -- when the response carries no parseable
timestamp, V1 silently assumed the reading was recent. This module does the
opposite: no timestamp means `freshness="unknown"`, never `"fresh"`. This is
a deliberate divergence from V1, not an oversight; it mirrors the plant-level
precedent in `monitoring.py` ("the verified current-state response has no
reliable provider observation time... it never interprets stale data as
offline"). `availability_status` is still classified from `inverter_state` in
every response row (`has_recent_data=True` is passed to the ported classifier
unconditionally) because the row was received *during this live call*, not
carried forward from a stale cache -- that is a different question from
whether the payload's own `collectTime` field lets us pin the instant more
precisely than "during this sync run".
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from itertools import islice

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.diagnostics.rules import classify_fusionsolar_inverter_availability
from nemsei.diagnostics.service import record_device_status
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.integrations.fusionsolar.service import credentials_for
from nemsei.integrations.huawei_scada.fusionsolar_discovery import (
    DiscoveredCollector,
    as_metadata as as_collector_metadata,
    collectors_in,
)
from nemsei.integrations.fusionsolar.session_cache import (
    FusionSolarSessionCache,
    authenticated_client,
    invalidate_session,
    is_session_expiry,
)
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run


# Ported verbatim from V1 (`monitoring_board/constants.py:
# FUSIONSOLAR_INVERTER_DEVICE_TYPE_IDS`), the only two `dev_type_id` values
# every one of the 325 imported V1 devices ever carried. Not re-derived: a
# provider's own device-type vocabulary is not something to guess at.
INVERTER_DEVICE_TYPE_IDS = frozenset({1, 38})

# A `collectTime` older than this is "stale", not "fresh". Chosen to be wider
# than any cadence this milestone proposes polling at (docs/v2/
# DEVICE_TELEMETRY.md), so a healthy poll never misclassifies its own reading.
_FRESH_WITHIN = timedelta(hours=3)

_ACTIVE_POWER_UNITS = ("kW", "W")


@dataclass(frozen=True)
class FusionSolarDeviceContract:
    active_power_unit: str
    day_energy_unit: str
    inverter_device_type_ids: frozenset[int] = INVERTER_DEVICE_TYPE_IDS


@dataclass(frozen=True)
class DiscoveredDeviceType:
    external_device_id: str
    device_type_id: int


@dataclass(frozen=True)
class DeviceStatusSample:
    external_device_id: str
    availability_status: str
    raw_inverter_state: str | None
    active_power_kw: Decimal | None
    day_energy_kwh: Decimal | None
    observed_at: datetime
    freshness: str
    quality: str
    completeness: str


@dataclass(frozen=True)
class DeviceStatusSyncResult:
    connection_id: int
    sync_run_id: int
    status: str
    completeness: str
    expected: int
    received: int
    accepted: int
    rejected: int
    error_code: str | None = None


def device_contract_for(connection: ProviderConnection) -> FusionSolarDeviceContract:
    """Load an operator-verified device contract without exposing secrets.

    Neither unit is established by V1 evidence -- V1 guessed active power's
    unit from its own magnitude. Requiring both as explicit configuration
    makes a missing verification a safe configuration failure rather than a
    silently mis-scaled fact, mirroring `production_contract_for`.
    """
    reference = connection.credential_reference or ""
    if not reference or not reference.replace("_", "").isalnum():
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar credential reference is not configured."))
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    power_unit = os.environ.get(f"{prefix}_DEVICE_POWER_UNIT", "").strip()
    energy_unit = os.environ.get(f"{prefix}_DEVICE_ENERGY_UNIT", "").strip()
    if power_unit not in _ACTIVE_POWER_UNITS or energy_unit != "kWh":
        raise FusionSolarClientError(ProviderError(
            ProviderErrorCode.CONFIGURATION,
            "FusionSolar device active-power unit (kW or W) and verified kWh day-energy unit must be configured.",
        ))
    return FusionSolarDeviceContract(active_power_unit=power_unit, day_energy_unit=energy_unit)


def normalize_device_type_row(row: dict) -> DiscoveredDeviceType | None:
    """Learn one device's `dev_type_id` from a `getDevList` row.

    `None` for a row this contract does not recognise as an identifiable,
    typed device -- never guessed at, exactly as V1's own `fetch_device_list`
    consumer skipped rows missing either field before filtering by type.

    Identity precedence (`devId, id, devDn, deviceDn, esnCode, sn`) is ported
    verbatim from V1's own `normalize_fusionsolar_device_identity` -- a live
    canary run (2026-08-20, asset 153) caught this function using the wrong
    order (`devDn` before `devId`/`id`), which silently matched zero devices
    against mappings keyed on V1's real `external_device_id` even though the
    response actually did contain a matching field. Fixed to match the
    field this module's own device mappings are keyed on, not guessed again.
    """
    external_id = str(
        row.get("devId") or row.get("id") or row.get("devDn") or row.get("deviceDn") or row.get("esnCode") or row.get("sn") or ""
    ).strip()
    dev_type_id = row.get("devTypeId") if "devTypeId" in row else row.get("dev_type_id")
    if not external_id or dev_type_id is None:
        return None
    try:
        return DiscoveredDeviceType(external_id, int(dev_type_id))
    except (TypeError, ValueError):
        return None


def normalize_device_realtime_row(
    row: dict,
    *,
    expected_external_ids: frozenset[str],
    contract: FusionSolarDeviceContract,
    ingested_at: datetime,
) -> DeviceStatusSample | None:
    """A `getDevRealKpi` row to one diagnostic sample, or `None` if unmatched.

    Only `inverter_state`, `active_power` and `day_cap`/`day_energy` are read
    from `dataItemMap` -- the exact three fields this milestone scoped, and
    the only three Fatia 1 proved V1's own account ever populated.
    """
    identity = None
    for key in ("devId", "id", "devDn", "deviceDn", "esnCode", "sn"):
        value = str(row.get(key) or "").strip()
        if value and value in expected_external_ids:
            identity = value
            break
    if identity is None:
        return None
    data_map = row.get("dataItemMap")
    data_map = data_map if isinstance(data_map, dict) else row

    raw_state = _first_present(data_map, "inverter_state", "inverterState")
    raw_power = _first_present(data_map, "active_power", "activePower")
    raw_energy = _first_present(data_map, "day_cap", "dayEnergy", "day_energy")

    source_timestamp = _parse_collect_time(_first_present(row, "collectTime", "collectedAt"))
    if source_timestamp is not None:
        age = ingested_at - source_timestamp
        freshness = "fresh" if timedelta(0) <= age <= _FRESH_WITHIN else "stale"
        observed_at = source_timestamp
    else:
        # No parseable provider timestamp: the reading is attributed to this
        # sync run's own ingestion instant, and freshness is honestly
        # "unknown" -- never defaulted to "fresh", unlike V1's own code.
        freshness = "unknown"
        observed_at = ingested_at

    availability_status = classify_fusionsolar_inverter_availability(raw_state, has_recent_data=True)

    active_power_kw = _scale_power(raw_power, contract.active_power_unit)
    day_energy_kwh = _decimal(raw_energy)

    present = [value is not None for value in (raw_state, active_power_kw, day_energy_kwh)]
    if all(present):
        quality, completeness = "complete", "complete"
    elif any(present):
        quality, completeness = "partial", "partial"
    else:
        quality, completeness = "missing", "missing"

    return DeviceStatusSample(
        external_device_id=identity,
        availability_status=availability_status,
        raw_inverter_state=str(raw_state).strip() if raw_state not in (None, "") else None,
        active_power_kw=active_power_kw,
        day_energy_kwh=day_energy_kwh,
        observed_at=observed_at,
        freshness=freshness,
        quality=quality,
        completeness=completeness,
    )


class FusionSolarDeviceStatusService:
    """Run a single account-level device-status sync for actively claimed devices.

    Selection deliberately mirrors `FusionSolarMonitoringService`, not
    `resolve_source_policy`: `SOURCE_USES` (`sources/models.py`) only knows
    `monitoring`/`production`, both plant-level ambiguity questions between
    competing connections. A device claim carries its own `device_id`
    (`ck_asset_provider_mappings_device_link`) and the partial unique index on
    `(provider_connection_id, resource_kind, normalized_external_id)` where
    `mapping_status='active'` already makes at most one connection able to
    claim a given device at a time -- there is no competing-source question
    left to resolve.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client_factory: Callable[[FusionSolarCredentials], FusionSolarClient] = FusionSolarClient,
        max_transient_retries: int = 1,
        session_cache: "FusionSolarSessionCache | None" = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._client_factory = client_factory
        self._calls = FusionSolarRequestController(session_factory, max_transient_retries=max_transient_retries)
        self._session_cache = session_cache or FusionSolarSessionCache()

    def sync_device_status(self, connection_id: int) -> DeviceStatusSyncResult:
        connection = self._connection(connection_id)
        run = self._start_run(connection_id)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            return self._finish(run.id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."))
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish(run.id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish(run.id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        try:
            contract = device_contract_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, 0, 0, 0, 0, exc.error)

        selected, station_codes_by_asset = self._selected_device_mappings(connection_id)
        if not selected:
            return self._finish(run.id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar device mapping is selected for device status."))
        station_codes = sorted({code for code in station_codes_by_asset.values() if code})
        if not station_codes:
            return self._finish(run.id, connection_id, len(selected), 0, 0, len(selected), ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar plant mapping resolves a station code for the selected devices."))

        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, len(selected), 0, 0, 0, exc.error)

        client, error = authenticated_client(
            calls=self._calls,
            connection_id=connection_id,
            sync_run_id=run.id,
            purpose="fusionsolar_device_status_authentication",
            credentials=credentials,
            client_factory=self._client_factory,
            cache=self._session_cache,
        )
        if error:
            if is_session_expiry(error):
                invalidate_session(credentials, cache=self._session_cache)
            return self._finish(run.id, connection_id, len(selected), 0, 0, 0, error)

        expected_ids = frozenset(mapping.external_id.strip() for mapping in selected)
        device_types: dict[str, int] = {}
        # Huawei collectors (SDongle, SmartLogger) seen in the same response.
        # This call is the only one FusionSolar lets through for this account
        # -- once per hour -- and it already carries every serial the Huawei
        # SCADA integration needs to pre-map a plant. Recording them costs
        # nothing and is never allowed to affect this sync.
        asset_by_station = {code: asset_id for asset_id, code in station_codes_by_asset.items() if code}
        collectors: list[DiscoveredCollector] = []
        received = 0
        for batch in _batches(station_codes, 100):
            rows, error = self._calls.call(
                connection_id=connection_id, sync_run_id=run.id, endpoint_family="device_discovery",
                purpose="fusionsolar_device_discovery", operation=lambda batch=batch: client.device_list_batch(batch),
            )
            if error:
                if is_session_expiry(error):
                    invalidate_session(credentials, cache=self._session_cache)
                return self._finish(run.id, connection_id, len(selected), 0, 0, 0, error)
            assert rows is not None
            try:
                collectors.extend(collectors_in(rows, asset_by_station=asset_by_station))
            except Exception:  # pragma: no cover - a bystander must never fail the sync
                pass
            for row in rows:
                discovered = normalize_device_type_row(row)
                if discovered and discovered.external_device_id in expected_ids and discovered.device_type_id in contract.inverter_device_type_ids:
                    device_types[discovered.external_device_id] = discovered.device_type_id

        by_type: dict[int, list[str]] = {}
        for external_id, dev_type_id in device_types.items():
            by_type.setdefault(dev_type_id, []).append(external_id)
        unresolved = len(expected_ids) - len(device_types)

        accepted = rejected = 0
        first_error: ProviderError | None = None
        for dev_type_id, ids in by_type.items():
            for batch in _batches(ids, 100):
                rows, error = self._calls.call(
                    connection_id=connection_id, sync_run_id=run.id, endpoint_family="device_current_monitoring",
                    purpose=f"fusionsolar_device_status_type_{dev_type_id}",
                    operation=lambda batch=batch, dev_type_id=dev_type_id: client.device_current_monitoring_batch(batch, device_type_id=dev_type_id),
                )
                if error:
                    if is_session_expiry(error):
                        invalidate_session(credentials, cache=self._session_cache)
                    first_error = error
                    break
                assert rows is not None
                received += len(rows)
                ingested_at = utc_now()
                samples: dict[str, DeviceStatusSample] = {}
                for row in rows:
                    sample = normalize_device_realtime_row(
                        row, expected_external_ids=frozenset(batch), contract=contract, ingested_at=ingested_at,
                    )
                    if sample is None:
                        rejected += 1
                        continue
                    samples[sample.external_device_id] = sample
                accepted += self._persist_samples(run.id, selected, samples)
                rejected += len(batch) - len(samples)
            if first_error:
                break

        complete = first_error is None and unresolved == 0 and accepted == len(selected)
        return self._finish(
            run.id, connection_id, len(selected), received, accepted, rejected + unresolved, first_error,
            partial=not complete, collectors=collectors,
        )

    def _selected_device_mappings(self, connection_id: int) -> tuple[list[AssetProviderMapping], dict[int, str]]:
        with self._sessions() as session:
            repository = ProviderRepository(session)
            device_mappings = [
                mapping for mapping in repository.current_device_mappings_for_connection(connection_id)
                if mapping.mapping_status == "active"
            ]
            plant_mappings = {
                mapping.asset_id: mapping.external_id
                for mapping in repository.current_mappings_for_connection(connection_id)
                if mapping.mapping_status == "active"
            }
            for mapping in device_mappings:
                session.expunge(mapping)
            return device_mappings, {mapping.asset_id: plant_mappings.get(mapping.asset_id) for mapping in device_mappings}

    def _persist_samples(
        self, sync_run_id: int, mappings: list[AssetProviderMapping], samples: dict[str, DeviceStatusSample],
    ) -> int:
        by_external_id = {mapping.external_id.strip(): mapping for mapping in mappings}
        written = 0
        with self._sessions() as session:
            for external_id, sample in samples.items():
                mapping = by_external_id.get(external_id)
                if mapping is None or mapping.device_id is None:
                    continue
                record_device_status(
                    session,
                    device_id=mapping.device_id,
                    asset_id=mapping.asset_id,
                    source_fact_key=f"fusionsolar-device-live:{mapping.normalized_external_id}",
                    observed_at=sample.observed_at,
                    availability_status=sample.availability_status,
                    active_power_kw=sample.active_power_kw,
                    day_energy_kwh=sample.day_energy_kwh,
                    source_kind="live_read",
                    freshness=sample.freshness,
                    quality=sample.quality,
                    completeness=sample.completeness,
                    sync_run_id=sync_run_id,
                    metadata={
                        "raw_inverter_state": sample.raw_inverter_state,
                        "observed_at_source": "provider_collect_time" if sample.freshness != "unknown" else "ingested_at_no_provider_timestamp",
                    },
                    # No independent provider timestamp means `observed_at`
                    # is this sync run's own ingestion instant, which differs
                    # on every poll by construction; without deduplicating on
                    # it, an unchanged device would mint a new revision every
                    # single poll forever. See `record_device_status`.
                    deduplicate_observed_at=sample.freshness == "unknown",
                )
                written += 1
            session.commit()
        return written

    def _connection(self, connection_id: int) -> ProviderConnection:
        with self._sessions() as session:
            connection = ProviderRepository(session).connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            session.expunge(connection)
            return connection

    def _start_run(self, connection_id: int) -> SyncRun:
        with self._sessions() as session:
            run = start_sync_run(session, provider_connection_id=connection_id, capability=ProviderCapability.DEVICE_MONITORING.value)
            session.commit()
            session.expunge(run)
            return run

    def _finish(
        self, run_id: int, connection_id: int, expected: int, received: int, accepted: int, rejected: int,
        error: ProviderError | None, *, deferred: bool = False, partial: bool = False,
        collectors: "list[DiscoveredCollector] | None" = None,
    ) -> DeviceStatusSyncResult:
        if deferred:
            status, completeness = "deferred", "none"
        elif error and accepted:
            status, completeness = "partial", "partial"
        elif error and error.code is ProviderErrorCode.RATE_LIMITED:
            status, completeness = "rate_limited", "none"
        elif error:
            status, completeness = "failed", "none"
        elif partial or rejected:
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
                **(as_collector_metadata(collectors) if collectors else {}),
            }
            record_health(session, provider_connection_id=connection_id, partial=status == "partial", error=error, **health_values_for_error(error, operation="sync"))
            finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
            session.commit()
        return DeviceStatusSyncResult(connection_id, run_id, status, completeness, expected, received, accepted, rejected, error.code.value if error else None)


def _first_present(source: dict, *keys: str):
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def _parse_collect_time(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, datetime_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _scale_power(value: object, unit: str) -> Decimal | None:
    """Scale a verified-unit raw power reading to kW -- never a magnitude guess.

    Unlike V1's `normalize_power_to_kw`, the divisor is decided by the
    connection's verified `active_power_unit`, not by whether the number
    happens to exceed 1000.
    """
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed / Decimal(1000) if unit == "W" else parsed


def _batches(values: list[str], size: int):
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def _calls(session: Session, run_id: int) -> int:
    return int(session.scalar(
        select(func.count()).select_from(ProviderRequestAttempt).where(
            ProviderRequestAttempt.sync_run_id == run_id,
            ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")),
        )
    ) or 0)
