"""FusionSolar daily production-history adapter.

This adapter deliberately implements only the frozen-V1-evidenced daily KPI
request.  A connection must explicitly declare its provider-day time zone and
the verified ``PVYield`` unit before any HTTP request or canonical persistence.
That keeps unverified account/provider semantics out of ``ProductionFact``.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.integrations.fusionsolar.service import credentials_for
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.sources.service import resolve_source_policy
from nemsei.sync.models import ProviderRequestAttempt, SyncCursor, SyncRun
from nemsei.sync.service import advance_cursor, finish_sync_run, record_health, start_sync_run


_CURSOR_KEY = "fusionsolar-daily-production"
_MAX_BATCH = 100


@dataclass(frozen=True)
class FusionSolarProductionContract:
    source_timezone: ZoneInfo
    source_timezone_name: str
    canonical_unit: str


@dataclass(frozen=True)
class DailyProductionSample:
    external_id: str
    value: Decimal | None
    quality: str
    completeness: str


@dataclass(frozen=True)
class ProductionSyncResult:
    connection_id: int
    sync_run_id: int
    status: str
    completeness: str
    requested_from: date | None
    requested_until: date | None
    expected: int
    received: int
    accepted: int
    rejected: int
    cursor_advanced: bool
    error_code: str | None = None


def production_contract_for(connection: ProviderConnection) -> FusionSolarProductionContract:
    """Load an operator-verified production contract without exposing secrets.

    The V1 endpoint behavior alone does not establish either field.  Requiring
    both values makes a missing verification a safe configuration failure rather
    than a silently shifted or wrongly-unitized fact.
    """
    reference = connection.credential_reference or ""
    if not reference or not reference.replace("_", "").isalnum():
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar credential reference is not configured."))
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    timezone_name = os.environ.get(f"{prefix}_PRODUCTION_TIMEZONE", "").strip()
    unit = os.environ.get(f"{prefix}_PRODUCTION_UNIT", "").strip()
    if not timezone_name or unit != "kWh":
        raise FusionSolarClientError(ProviderError(
            ProviderErrorCode.CONFIGURATION,
            "FusionSolar production timezone and verified kWh unit must be configured.",
        ))
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar production timezone is invalid.")) from exc
    return FusionSolarProductionContract(timezone, timezone_name, unit)


def normalize_daily_production_row(row: dict) -> DailyProductionSample:
    """Accept only the explicitly contracted daily energy signal, ``PVYield``."""
    external_id = str(row.get("stationCode") or row.get("plantCode") or "").strip()
    if not external_id:
        raise ValueError("FusionSolar daily production row has no station code.")
    values = row.get("dataItemMap")
    values = values if isinstance(values, dict) else {}
    raw_value = values.get("PVYield")
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return DailyProductionSample(external_id, None, "missing", "partial")
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("FusionSolar daily PVYield is not numeric.") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("FusionSolar daily PVYield is invalid.")
    return DailyProductionSample(external_id, value, "complete", "complete")


class FusionSolarProductionService:
    """Incremental daily history ingestion; it never discovers plants or schedules itself."""

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

    def sync_daily_production(
        self,
        connection_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        reconciliation_days: int = 1,
    ) -> ProductionSyncResult:
        """Fetch a bounded daily window and advance only complete coverage.

        The cursor deliberately advances only when every requested source day
        has a complete selected-mapping response.  This conservative rule can
        re-fetch completed data after a partial failure, but can never skip a
        missing day; canonical idempotency makes that replay safe.
        """
        if reconciliation_days < 0:
            raise ValueError("Reconciliation overlap cannot be negative.")
        connection = self._connection(connection_id)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."))
        if not connection.enabled or connection.configuration_status != "configured":
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        try:
            contract = production_contract_for(connection)
            requested_from, requested_until = self._window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=reconciliation_days,
                contract=contract,
            )
        except (FusionSolarClientError, ValueError) as exc:
            error = exc.error if isinstance(exc, FusionSolarClientError) else ProviderError(ProviderErrorCode.CONFIGURATION, str(exc))
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, error)
        run = self._start_run(connection_id, requested_from, requested_until, contract)
        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, requested_from, requested_until, 0, 0, 0, 0, exc.error)

        selected_by_day, selection_findings = self._selected_mappings(connection_id, requested_from, requested_until)
        expected = sum(len(values) for values in selected_by_day.values())
        if expected == 0:
            error = ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar mapping is selected for production.")
            return self._finish(run.id, connection_id, requested_from, requested_until, 0, 0, 0, selection_findings, error)

        client = self._client_factory(credentials)
        _value, error = self._calls.call(
            connection_id=connection_id,
            sync_run_id=run.id,
            endpoint_family="authentication",
            purpose="fusionsolar_production_authentication",
            operation=client.authenticate,
        )
        if error:
            return self._finish(run.id, connection_id, requested_from, requested_until, expected, 0, 0, selection_findings, error)

        received = accepted = rejected = 0
        first_error: ProviderError | None = None
        incomplete = bool(selection_findings)
        for source_day in _days(requested_from, requested_until):
            selected = selected_by_day[source_day]
            if not selected:
                continue
            outcome = self._sync_day(run.id, connection_id, client, source_day, contract, selected)
            received += outcome.received
            accepted += outcome.accepted
            rejected += outcome.rejected
            incomplete = incomplete or outcome.partial
            if outcome.error:
                first_error = outcome.error
                break

        complete = first_error is None and not incomplete and rejected == 0
        return self._finish(
            run.id,
            connection_id,
            requested_from,
            requested_until,
            expected,
            received,
            accepted,
            rejected + selection_findings,
            first_error,
            advance=complete,
            contract=contract,
            partial=incomplete,
        )

    def _sync_day(
        self,
        run_id: int,
        connection_id: int,
        client: FusionSolarClient,
        source_day: date,
        contract: FusionSolarProductionContract,
        selected: list[AssetProviderMapping],
    ) -> "_DayOutcome":
        expected = {mapping.normalized_external_id: mapping for mapping in selected}
        samples: dict[str, DailyProductionSample] = {}
        received = rejected = 0
        for batch in _batches(selected, _MAX_BATCH):
            rows, error = self._calls.call(
                connection_id=connection_id,
                sync_run_id=run_id,
                endpoint_family="production_history_daily",
                purpose=f"fusionsolar_daily_production_{source_day.isoformat()}",
                operation=lambda batch=batch: client.daily_production_batch(
                    [mapping.external_id for mapping in batch], source_day=source_day, source_timezone=contract.source_timezone
                ),
            )
            if error:
                accepted = self._persist_day(run_id, source_day, contract, expected, samples)
                return _DayOutcome(received, accepted, rejected, True, error)
            assert rows is not None
            for row in rows:
                received += 1
                try:
                    sample = normalize_daily_production_row(row)
                    normalized = normalize_external_id(ProviderCode.FUSIONSOLAR, sample.external_id)
                except ValueError:
                    rejected += 1
                    continue
                if normalized not in expected or normalized in samples:
                    rejected += 1
                    continue
                samples[normalized] = sample
        accepted = self._persist_day(run_id, source_day, contract, expected, samples)
        # A matching row with PVYield absent is persisted as an explicit missing
        # fact, but its source day remains incomplete and cannot advance cursor.
        partial = len(samples) != len(expected) or any(sample.completeness != "complete" for sample in samples.values())
        return _DayOutcome(received, accepted, rejected, partial, None)

    def _persist_day(
        self,
        run_id: int,
        source_day: date,
        contract: FusionSolarProductionContract,
        mappings: dict[str, AssetProviderMapping],
        samples: dict[str, DailyProductionSample],
    ) -> int:
        start = datetime.combine(source_day, time.min, tzinfo=contract.source_timezone)
        end = start + timedelta(days=1)
        metadata = {
            "source_period_timezone": contract.source_timezone_name,
            "source_period_date": source_day.isoformat(),
            "provider_value_field": "PVYield",
        }
        with self._sessions() as session:
            for normalized, sample in samples.items():
                mapping = mappings[normalized]
                record_production_fact(
                    session,
                    asset_id=mapping.asset_id,
                    provider_mapping_id=mapping.id,
                    source_fact_key=f"fusionsolar-daily-pvyield:{normalized}:{source_day.isoformat()}",
                    period_start=start,
                    period_end=end,
                    granularity="day",
                    value=sample.value,
                    unit=contract.canonical_unit,
                    quality=sample.quality,
                    completeness=sample.completeness,
                    sync_run_id=run_id,
                    metadata=metadata,
                )
            session.commit()
        return len(samples)

    def _selected_mappings(self, connection_id: int, start: date, end: date) -> tuple[dict[date, list[AssetProviderMapping]], int]:
        selected_by_day: dict[date, list[AssetProviderMapping]] = {}
        findings = 0
        with self._sessions() as session:
            repository = ProviderRepository(session)
            for source_day in _days(start, end):
                selected: list[AssetProviderMapping] = []
                for mapping in repository.mappings_for_connection_on_date(connection_id, source_day):
                    if mapping.mapping_status != "active":
                        continue
                    try:
                        policy = resolve_source_policy(session, asset_id=mapping.asset_id, source_use="production", on_date=source_day)
                    except ValueError:
                        findings += 1
                        continue
                    if policy.provider_mapping_id == mapping.id:
                        selected.append(mapping)
                for mapping in selected:
                    session.expunge(mapping)
                selected_by_day[source_day] = selected
        return selected_by_day, findings

    def _window(
        self,
        connection_id: int,
        *,
        start_date: date | None,
        end_date: date | None,
        reconciliation_days: int,
        contract: FusionSolarProductionContract,
    ) -> tuple[date, date]:
        default_end = datetime.now(contract.source_timezone).date() - timedelta(days=1)
        with self._sessions() as session:
            cursor = session.scalar(select(SyncCursor).where(
                SyncCursor.provider_connection_id == connection_id,
                SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                SyncCursor.cursor_key == _CURSOR_KEY,
            ))
            checkpoint = dict(cursor.checkpoint_json) if cursor else {}
        if start_date is None:
            last_day = checkpoint.get("last_completed_day")
            if not isinstance(last_day, str):
                raise ValueError("The first production sync requires an explicit start date.")
            try:
                start_date = date.fromisoformat(last_day) + timedelta(days=1 - reconciliation_days)
            except ValueError as exc:
                raise ValueError("Production cursor checkpoint is invalid.") from exc
        end_date = end_date or default_end
        if end_date < start_date:
            raise ValueError("Production sync window is invalid.")
        if (end_date - start_date).days + 1 > self._settings.production_max_source_days:
            raise ValueError("Production sync window exceeds the configured normal-sync safety limit.")
        source_timezone = checkpoint.get("source_timezone")
        if cursor is not None and source_timezone != contract.source_timezone_name:
            raise ValueError("Production cursor timezone differs from the configured verified timezone; operator reconciliation or reset is required.")
        return start_date, end_date

    def _connection(self, connection_id: int) -> ProviderConnection:
        with self._sessions() as session:
            connection = ProviderRepository(session).connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            session.expunge(connection)
            return connection

    def _start_run(
        self,
        connection_id: int,
        start: date | None = None,
        end: date | None = None,
        contract: FusionSolarProductionContract | None = None,
    ) -> SyncRun:
        with self._sessions() as session:
            run = start_sync_run(
                session,
                provider_connection_id=connection_id,
                capability=ProviderCapability.PRODUCTION_HISTORY.value,
                requested_from=datetime.combine(start, time.min, tzinfo=contract.source_timezone) if start and contract else None,
                requested_until=datetime.combine(end + timedelta(days=1), time.min, tzinfo=contract.source_timezone) if end and contract else None,
            )
            session.commit()
            session.expunge(run)
            return run

    def _finish(
        self,
        run_id: int,
        connection_id: int,
        start: date | None,
        end: date | None,
        expected: int,
        received: int,
        accepted: int,
        rejected: int,
        error: ProviderError | None,
        *,
        advance: bool = False,
        deferred: bool = False,
        contract: FusionSolarProductionContract | None = None,
        partial: bool = False,
    ) -> ProductionSyncResult:
        if deferred:
            status, completeness = "deferred", "none"
        elif error and accepted:
            status, completeness = "partial", "partial"
        elif error and error.code is ProviderErrorCode.RATE_LIMITED:
            status, completeness = "rate_limited", "none"
        elif error:
            status, completeness = "failed", "none"
        elif partial or rejected or accepted != expected:
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
                "source_period_start": start.isoformat() if start else None,
                "source_period_end": end.isoformat() if end else None,
                "source_period_timezone": contract.source_timezone_name if contract else None,
            }
            record_health(session, provider_connection_id=connection_id, partial=status == "partial", error=error, **_health_values(error))
            finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
            cursor_updated = False
            if advance:
                assert status == "success" and contract is not None
                assert start is not None and end is not None
                covered_through = datetime.combine(end + timedelta(days=1), time.min, tzinfo=contract.source_timezone)
                existing_cursor = session.scalar(select(SyncCursor).where(
                    SyncCursor.provider_connection_id == connection_id,
                    SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                    SyncCursor.cursor_key == _CURSOR_KEY,
                ).with_for_update())
                if _can_extend_cursor(existing_cursor, start=start, end=end, timezone_name=contract.source_timezone_name):
                    advance_cursor(
                        session,
                        run=run,
                        cursor_key=_CURSOR_KEY,
                        checkpoint={"last_completed_day": end.isoformat(), "source_timezone": contract.source_timezone_name},
                        covered_through=covered_through,
                    )
                    cursor_updated = True
            session.commit()
        return ProductionSyncResult(connection_id, run_id, status, completeness, start, end, expected, received, accepted, rejected, cursor_updated, error.code.value if error else None)


@dataclass(frozen=True)
class _DayOutcome:
    received: int
    accepted: int
    rejected: int
    partial: bool
    error: ProviderError | None


def _can_extend_cursor(
    cursor: SyncCursor | None,
    *,
    start: date,
    end: date,
    timezone_name: str,
) -> bool:
    """Allow only first coverage or a window contiguous with the safe day.

    A successful historical correction is intentionally not an advancement. A
    successful gap window may persist its immutable facts, but it cannot turn an
    uncovered interval into implied coverage.
    """
    if cursor is None:
        return True
    checkpoint = dict(cursor.checkpoint_json)
    if checkpoint.get("source_timezone") != timezone_name:
        return False
    last_completed = checkpoint.get("last_completed_day")
    if not isinstance(last_completed, str):
        return False
    try:
        last_day = date.fromisoformat(last_completed)
    except ValueError:
        return False
    if end <= last_day:
        return False
    return start <= last_day + timedelta(days=1)


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _batches(values: list[AssetProviderMapping], size: int):
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


def _health_values(error: ProviderError | None) -> dict[str, str]:
    if error is None:
        return {"auth_state": "healthy", "access_state": "healthy", "provider_state": "healthy", "quota_state": "unknown"}
    if error.code is ProviderErrorCode.AUTHENTICATION:
        return {"auth_state": "degraded", "access_state": "unknown", "provider_state": "healthy"}
    if error.code is ProviderErrorCode.AUTHORIZATION:
        return {"auth_state": "healthy", "access_state": "degraded", "provider_state": "healthy"}
    if error.code is ProviderErrorCode.RATE_LIMITED:
        return {"provider_state": "healthy", "quota_state": "degraded"}
    if error.code is ProviderErrorCode.CONFIGURATION:
        return {"provider_state": "not_configured"}
    return {"provider_state": "unavailable"}
