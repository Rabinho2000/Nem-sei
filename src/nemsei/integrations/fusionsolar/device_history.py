"""Closed-day device history ingestion: the contractual availability source.

Pulls `/thirdData/getDevHistoryKpi` for one closed day and persists every
5-minute reading as a `DeviceStatusFact` with `source_kind='history_read'`.
That is deliberately the *same* table the realtime poll writes to: a history
row carries the identical fields (`collectTime`, `active_power`,
`inverter_state`, `day_cap`), so it reuses
`device_status.normalize_device_realtime_row` verbatim, along with the
revision/supersession and sync-run provenance `record_device_status` already
provides. What separates the two engines is `source_kind`, not a second
family of tables.

Verified live against the real account (2026-09-06, asset 153, 2026-09-04):
HTTP 200, `success: true`, `failCode: 0`, 558 rows for two inverters --
5-minute cadence, 00:00 to 23:55, both `active_power` and `inverter_state`
populated. See `docs/v2/FUSIONSOLAR_DEVICE_HISTORY.md`.

Why a closed day only: the window is the whole day, so asking for today
returns a partial series that would compute a lower availability than the
day actually had, and would then have to be corrected. V1 refused the same
way (`sync_fusionsolar_inverter_availability_for_date` raised on
`target_date >= current_lisbon_date()`); this refuses in the same place, for
the same reason.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.device_status import (
    device_contract_for,
    normalize_device_realtime_row,
    normalize_device_type_row,
)
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.integrations.fusionsolar.service import credentials_for
from nemsei.integrations.fusionsolar.session_cache import (
    FusionSolarSessionCache,
    authenticated_client,
    invalidate_session,
    is_session_expiry,
)
from nemsei.diagnostics.availability_service import expected_device_mappings_for_date
from nemsei.diagnostics.service import record_device_status
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sync.models import SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run

# V1's own chunk size for this endpoint (`fusionsolar_client.device_history_kpi`).
# One day of one device is ~288 rows, so ten devices is already ~2 880 rows in
# a single response; V1 never established that a larger batch is accepted.
DEVICE_HISTORY_BATCH = 10

ENDPOINT_FAMILY = "device_history"


@dataclass(frozen=True)
class DeviceHistorySyncResult:
    sync_run_id: int
    connection_id: int
    target_date: date
    devices_expected: int
    rows_received: int
    facts_written: int
    api_calls: int
    status: str
    error: ProviderError | None


def history_timezone_for(connection: ProviderConnection) -> ZoneInfo:
    """The timezone whose midnight-to-midnight defines a contractual day.

    Required explicitly per connection, never guessed. V1 built this window
    in the *calling process's* local timezone -- an implicit contract that
    silently changed meaning with the container's `TZ`, and the exact class
    of hidden assumption `production_contract_for`/`device_contract_for`
    already refuse. A missing setting is a configuration error, not a
    default, because guessing here shifts every boundary day's number.
    """
    reference = connection.credential_reference or ""
    if not reference or not reference.replace("_", "").isalnum():
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar credential reference is not configured."))
    name = os.environ.get(f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}_DEVICE_HISTORY_TIMEZONE", "").strip()
    if not name:
        raise FusionSolarClientError(
            ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar device history timezone is not verified for this connection.")
        )
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - any zoneinfo failure is a configuration failure
        raise FusionSolarClientError(
            ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar device history timezone is not a valid IANA zone.")
        ) from exc


def day_window_ms(target_date: date, tz: ZoneInfo) -> tuple[int, int]:
    """`[00:00, 24:00)` of one day in `tz`, as epoch milliseconds.

    Ported from V1's `closed_day_window_ms` in shape (inclusive start,
    end-minus-1ms) but with the timezone made explicit instead of implicit.
    Computed from wall-clock midnight in `tz`, so a DST day is 23 or 25 hours
    wide rather than a fixed 24, the same correctness argument
    `availability_window.lisbon_day_bounds` already makes for the sampled
    engine.
    """
    start = datetime.combine(target_date, time.min, tzinfo=tz)
    end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


class FusionSolarDeviceHistoryService:
    """One closed-day device-history sync for one connection.

    Shares every coordination mechanism the other FusionSolar services use --
    `FusionSolarRequestController` (per-`endpoint_family` quota state, 407
    cooldown, transient retry), the ownership lease it enforces, the session
    cache, and sync runs -- rather than introducing a parallel budget. The
    new `endpoint_family="device_history"` is what gives this endpoint its
    own cooldown and call counter without a second quota system: V1's
    separate `WAT_HISTORY_AREA`/`FUSIONSOLAR_WAT_DAILY_BUDGET` has no V2
    analogue to reuse, because V2 expresses exactly that idea as a row in
    `provider_request_states`.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client_factory: Callable[[FusionSolarCredentials], FusionSolarClient] = FusionSolarClient,
        max_transient_retries: int = 1,
        session_cache: FusionSolarSessionCache | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._client_factory = client_factory
        self._calls = FusionSolarRequestController(session_factory, max_transient_retries=max_transient_retries)
        self._session_cache = session_cache or FusionSolarSessionCache()

    def sync_device_history(
        self, connection_id: int, target_date: date, *, today: date | None = None
    ) -> DeviceHistorySyncResult:
        connection = self._connection(connection_id)
        run = self._start_run(connection_id)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            return self._finish(run.id, connection_id, target_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."))
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish(run.id, connection_id, target_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish(run.id, connection_id, target_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        try:
            contract = device_contract_for(connection)
            tz = history_timezone_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, target_date, 0, 0, 0, 0, exc.error)

        # A day is only contractual once it is over, in its own timezone.
        reference_day = today or datetime.now(tz).date()
        if target_date >= reference_day:
            return self._finish(
                run.id, connection_id, target_date, 0, 0, 0, 0,
                ProviderError(ProviderErrorCode.CONFIGURATION, "Contractual availability requires a closed day."),
            )

        mappings, station_codes_by_asset = self._device_mappings_for_date(connection_id, target_date)
        if not mappings:
            return self._finish(run.id, connection_id, target_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar device mapping is valid on this date."))
        station_codes = sorted({code for code in station_codes_by_asset.values() if code})
        if not station_codes:
            return self._finish(run.id, connection_id, target_date, len(mappings), 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar plant mapping resolves a station code."))

        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, target_date, len(mappings), 0, 0, 0, exc.error)

        client, error = authenticated_client(
            calls=self._calls, connection_id=connection_id, sync_run_id=run.id,
            purpose="fusionsolar_device_history_authentication", credentials=credentials,
            client_factory=self._client_factory, cache=self._session_cache,
        )
        if error:
            if is_session_expiry(error):
                invalidate_session(credentials, cache=self._session_cache)
            return self._finish(run.id, connection_id, target_date, len(mappings), 0, 0, 0, error)

        expected_ids = frozenset(mapping.external_id.strip() for mapping in mappings)
        api_calls = 0

        # `getDevHistoryKpi` groups by `devTypeId`, which V2 does not persist,
        # so it is learned the same way the realtime device poll learns it.
        device_types: dict[str, int] = {}
        for batch in _chunks(station_codes, 100):
            rows, error = self._calls.call(
                connection_id=connection_id, sync_run_id=run.id, endpoint_family="device_discovery",
                purpose="fusionsolar_device_history_discovery",
                operation=lambda batch=batch: client.device_list_batch(batch),
            )
            api_calls += 1
            if error:
                if is_session_expiry(error):
                    invalidate_session(credentials, cache=self._session_cache)
                return self._finish(run.id, connection_id, target_date, len(mappings), 0, 0, api_calls, error)
            assert rows is not None
            for row in rows:
                discovered = normalize_device_type_row(row)
                if discovered and discovered.external_device_id in expected_ids and discovered.device_type_id in contract.inverter_device_type_ids:
                    device_types[discovered.external_device_id] = discovered.device_type_id

        by_type: dict[int, list[str]] = {}
        for external_id, dev_type_id in device_types.items():
            by_type.setdefault(dev_type_id, []).append(external_id)

        start_ms, end_ms = day_window_ms(target_date, tz)
        received = written = 0
        first_error: ProviderError | None = None
        for dev_type_id, ids in sorted(by_type.items()):
            for batch in _chunks(sorted(ids), DEVICE_HISTORY_BATCH):
                rows, error = self._calls.call(
                    connection_id=connection_id, sync_run_id=run.id, endpoint_family=ENDPOINT_FAMILY,
                    purpose=f"fusionsolar_device_history_type_{dev_type_id}",
                    operation=lambda batch=batch, dev_type_id=dev_type_id: client.device_history_batch(
                        batch, device_type_id=dev_type_id, start_time_ms=start_ms, end_time_ms=end_ms
                    ),
                )
                api_calls += 1
                if error:
                    if is_session_expiry(error):
                        invalidate_session(credentials, cache=self._session_cache)
                    first_error = error
                    break
                assert rows is not None
                received += len(rows)
                written += self._persist_rows(run.id, mappings, rows, batch=frozenset(batch), contract=contract)
            if first_error:
                break

        return self._finish(
            run.id, connection_id, target_date, len(mappings), received, written, api_calls, first_error,
            partial=first_error is not None and written > 0,
        )

    def _persist_rows(self, sync_run_id: int, mappings, rows, *, batch, contract) -> int:
        """One `DeviceStatusFact` per (device, provider timestamp).

        The fact key carries the reading's own instant, so re-fetching a day
        is idempotent when nothing changed (`record_device_status` returns the
        existing row) and mints a **revision** superseding the old one when
        the provider has since corrected a value -- late corrections without
        losing the audit trail, using the machinery already in place rather
        than a mechanism invented here.
        """
        by_external_id = {mapping.external_id.strip(): mapping for mapping in mappings}
        ingested_at = utc_now()
        written = 0
        with self._sessions() as session:
            for row in rows:
                sample = normalize_device_realtime_row(
                    row, expected_external_ids=batch, contract=contract, ingested_at=ingested_at
                )
                if sample is None:
                    continue
                mapping = by_external_id.get(sample.external_device_id)
                if mapping is None or mapping.device_id is None:
                    continue
                if sample.freshness == "unknown":
                    # A history row without a parseable `collectTime` cannot be
                    # placed on the day's timeline at all, and a slot engine
                    # keyed on time has nothing to do with it. Dropped rather
                    # than attributed to ingestion time, which would invent a
                    # slot the provider never reported.
                    continue
                _fact, created = record_device_status(
                    session,
                    device_id=mapping.device_id,
                    asset_id=mapping.asset_id,
                    source_fact_key=f"fusionsolar-device-history:{mapping.normalized_external_id}:{sample.observed_at.isoformat()}",
                    observed_at=sample.observed_at,
                    availability_status=sample.availability_status,
                    active_power_kw=sample.active_power_kw,
                    day_energy_kwh=sample.day_energy_kwh,
                    source_kind="history_read",
                    freshness=sample.freshness,
                    quality=sample.quality,
                    completeness=sample.completeness,
                    sync_run_id=sync_run_id,
                    metadata={"raw_inverter_state": sample.raw_inverter_state, "observed_at_source": "provider_collect_time"},
                )
                written += int(created)
            session.commit()
        return written

    def _device_mappings_for_date(self, connection_id: int, target_date: date):
        """Delegated to the domain: which inverters this plant had that day.

        Resolving that reads `devices`, and this adapter package may not
        depend on the asset domain (`test_architecture_boundaries`), so the
        question is asked rather than answered here.
        """
        with self._sessions() as session:
            return expected_device_mappings_for_date(session, connection_id=connection_id, target_date=target_date)

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
        self, run_id: int, connection_id: int, target_date: date, expected: int, received: int, written: int,
        api_calls: int, error: ProviderError | None, *, deferred: bool = False, partial: bool = False,
    ) -> DeviceHistorySyncResult:
        if deferred:
            status, completeness = "deferred", "none"
        elif error and partial:
            status, completeness = "partial", "partial"
        elif error and error.code is ProviderErrorCode.RATE_LIMITED:
            status, completeness = "rate_limited", "none"
        elif error:
            status, completeness = "failed", "none"
        else:
            status, completeness = "succeeded", "complete"
        with self._sessions() as session:
            finish_sync_run(session, sync_run_id=run_id, status=status, completeness=completeness, error=error)
            record_health(session, provider_connection_id=connection_id, **health_values_for_error(error))
            session.commit()
        return DeviceHistorySyncResult(
            sync_run_id=run_id, connection_id=connection_id, target_date=target_date, devices_expected=expected,
            rows_received=received, facts_written=written, api_calls=api_calls, status=status, error=error,
        )


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
