"""FusionSolar daily production-history adapter.

This adapter deliberately implements only the frozen-V1-evidenced daily KPI
request.  A connection must explicitly declare its provider-day time zone and
the verified ``PVYield`` unit before any HTTP request or canonical persistence.
That keeps unverified account/provider semantics out of ``ProductionFact``.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.integrations.fusionsolar.service import credentials_for
from nemsei.integrations.fusionsolar.session_cache import (
    FusionSolarSessionCache,
    authenticated_client,
    invalidate_session,
    is_session_expiry,
)
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.sources.service import resolve_source_policy
from nemsei.sync.models import ProviderRequestAttempt, SyncCursor, SyncRun
from nemsei.sync.service import advance_cursor, finish_sync_run, health_values_for_error, record_health, start_sync_run


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
    # The instant the row says it describes. `getKpiStationDay` answers with one
    # row per day of the month, all carrying the same station code, so a row can
    # only be attributed to a source day by its own timestamp.
    source_timestamp: datetime | None = None


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
    mode: str = "incremental"
    next_source_day: date | None = None
    # Durable intra-day progress for a source day this run did not finish:
    # which provider-call batches already succeeded, so the next attempt can
    # resume at the first unfinished one instead of re-fetching from batch
    # zero. `None` means either the day completed or nothing was attempted.
    batch_checkpoint: dict[str, Any] | None = None


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
    collected_at = row.get("collectTime")
    source_timestamp: datetime | None = None
    if collected_at is not None:
        try:
            source_timestamp = datetime.fromtimestamp(int(collected_at) / 1000, datetime_timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError) as exc:
            raise ValueError("FusionSolar daily row has an invalid collectTime.") from exc
    values = row.get("dataItemMap")
    values = values if isinstance(values, dict) else {}
    raw_value = values.get("PVYield")
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return DailyProductionSample(external_id, None, "missing", "partial", source_timestamp)
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("FusionSolar daily PVYield is not numeric.") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("FusionSolar daily PVYield is invalid.")
    return DailyProductionSample(external_id, value, "complete", "complete", source_timestamp)


class FusionSolarProductionService:
    """Incremental daily history ingestion; it never discovers plants or schedules itself."""

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

    def sync_daily_production(
        self,
        connection_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        reconciliation_days: int = 1,
    ) -> ProductionSyncResult:
        """Compatibility name for the explicit incremental mode."""
        return self.sync_incremental(
            connection_id,
            start_date=start_date,
            end_date=end_date,
            reconciliation_days=reconciliation_days,
        )

    def sync_incremental(
        self,
        connection_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        reconciliation_days: int = 1,
    ) -> ProductionSyncResult:
        """Cover the outstanding window one chunk at a time, resuming after each.

        An explicitly bounded call (both dates given) is left alone: the caller
        already said how much to ask for, and a manual reconciliation of three
        named days should not silently become a resuming job.
        """
        explicit = start_date is not None and end_date is not None
        return self._sync_window(
            connection_id,
            start_date=start_date,
            end_date=end_date,
            reconciliation_days=reconciliation_days,
            mode="incremental",
            allow_cursor_advance=True,
            max_source_days=self._settings.production_max_source_days,
            chunk_days=None if explicit else self._settings.production_incremental_chunk_days,
        )

    def sync_reconciliation(
        self,
        connection_id: int,
        *,
        source_days: int = 1,
        as_of: datetime | None = None,
    ) -> ProductionSyncResult:
        """Explicit provider-local D-1 (or small recent-window) refresh."""
        return self._sync_window(
            connection_id,
            start_date=None,
            end_date=None,
            reconciliation_days=source_days,
            mode="reconciliation",
            allow_cursor_advance=False,
            max_source_days=self._settings.production_reconciliation_max_source_days,
            as_of=as_of,
        )

    def sync_bounded_backfill(
        self,
        connection_id: int,
        *,
        start_date: date | None,
        end_date: date | None,
        resume_from: date | None = None,
        batch_checkpoint: dict[str, Any] | None = None,
    ) -> ProductionSyncResult:
        """Process one chronological, resumable bounded-backfill chunk.

        `batch_checkpoint`, when given, is durable intra-day progress an
        earlier attempt at this same day left behind (see `_sync_day`). It is
        only ever applied to the day it names; a checkpoint for a different
        day is a caller bug, not something to guess around, so `_sync_window`
        ignores it rather than misapplying it.
        """
        if start_date is None or end_date is None:
            return self._sync_window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=0,
                mode="bounded_backfill",
                allow_cursor_advance=True,
                max_source_days=self._settings.production_backfill_chunk_days,
                require_explicit_bounds=True,
            )
        if end_date < start_date:
            return self._sync_window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=0,
                mode="bounded_backfill",
                allow_cursor_advance=True,
                max_source_days=self._settings.production_backfill_chunk_days,
                require_explicit_bounds=True,
            )
        if (end_date - start_date).days + 1 > self._settings.production_backfill_max_source_days:
            return self._sync_window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=0,
                mode="bounded_backfill",
                allow_cursor_advance=True,
                max_source_days=self._settings.production_backfill_chunk_days,
                require_explicit_bounds=True,
                force_window_error="Production backfill window exceeds the configured safety limit.",
            )
        chunk_start = resume_from or start_date
        if chunk_start < start_date or chunk_start > end_date:
            return self._sync_window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=0,
                mode="bounded_backfill",
                allow_cursor_advance=True,
                max_source_days=self._settings.production_backfill_chunk_days,
                require_explicit_bounds=True,
                force_window_error="Production backfill resume point is outside its requested bounds.",
            )
        chunk_end = min(end_date, chunk_start + timedelta(days=self._settings.production_backfill_chunk_days - 1))
        result = self._sync_window(
            connection_id,
            start_date=chunk_start,
            end_date=chunk_end,
            reconciliation_days=0,
            mode="bounded_backfill",
            allow_cursor_advance=True,
            max_source_days=self._settings.production_backfill_chunk_days,
            require_explicit_bounds=True,
            batch_checkpoint=batch_checkpoint,
        )
        if result.status == "success" and chunk_end < end_date:
            return replace(result, next_source_day=chunk_end + timedelta(days=1))
        return result

    def _sync_window(
        self,
        connection_id: int,
        *,
        start_date: date | None,
        end_date: date | None,
        reconciliation_days: int,
        mode: str,
        allow_cursor_advance: bool,
        max_source_days: int,
        as_of: datetime | None = None,
        require_explicit_bounds: bool = False,
        force_window_error: str | None = None,
        chunk_days: int | None = None,
        batch_checkpoint: dict[str, Any] | None = None,
    ) -> ProductionSyncResult:
        """Fetch a bounded daily window and advance only complete coverage.

        The cursor deliberately advances only when every requested source day
        has a complete selected-mapping response.  This conservative rule can
        re-fetch completed data after a partial failure, but can never skip a
        missing day; canonical idempotency makes that replay safe.

        `chunk_days` bounds what a single run asks for, without touching that
        rule. `max_source_days` still measures the *whole* outstanding gap, so
        a window too wide to be normal work is still refused rather than
        quietly chunked through; what chunking removes is the reason the gap
        kept growing. A run that stops short reports the next day to resume
        from, and the job re-queues itself after a pause.
        """
        if reconciliation_days < 0:
            raise ValueError("Reconciliation overlap cannot be negative.")
        connection = self._connection(connection_id)
        if connection.provider_code != ProviderCode.FUSIONSOLAR.value:
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not FusionSolar."), mode=mode)
        if not connection.enabled or connection.configuration_status != "configured":
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar connection is not enabled and configured."), mode=mode)
        if not self._settings.capabilities.get("provider_reads", False):
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True, mode=mode)
        try:
            contract = production_contract_for(connection)
            requested_from, requested_until = self._window(
                connection_id,
                start_date=start_date,
                end_date=end_date,
                reconciliation_days=reconciliation_days,
                contract=contract,
                mode=mode,
                max_source_days=max_source_days,
                as_of=as_of,
                require_explicit_bounds=require_explicit_bounds,
                force_window_error=force_window_error,
            )
        except (FusionSolarClientError, ValueError) as exc:
            error = exc.error if isinstance(exc, FusionSolarClientError) else ProviderError(ProviderErrorCode.CONFIGURATION, str(exc))
            run = self._start_run(connection_id)
            return self._finish(run.id, connection_id, start_date, end_date, 0, 0, 0, 0, error, mode=mode)
        # Clamped after `_window` has measured and vetted the full gap, so the
        # safety limit keeps applying to the real outstanding window rather
        # than to the slice this run happens to take from it.
        resume_from: date | None = None
        if chunk_days is not None and (requested_until - requested_from).days + 1 > chunk_days:
            requested_until = requested_from + timedelta(days=chunk_days - 1)
            resume_from = requested_until + timedelta(days=1)
        run = self._start_run(connection_id, requested_from, requested_until, contract)
        try:
            credentials = credentials_for(connection)
        except FusionSolarClientError as exc:
            return self._finish(run.id, connection_id, requested_from, requested_until, 0, 0, 0, 0, exc.error, mode=mode)

        selected_by_day, selection_findings = self._selected_mappings(connection_id, requested_from, requested_until)
        expected = sum(len(values) for values in selected_by_day.values())
        if expected == 0:
            error = ProviderError(ProviderErrorCode.CONFIGURATION, "No FusionSolar mapping is selected for production.")
            return self._finish(run.id, connection_id, requested_from, requested_until, 0, 0, 0, selection_findings, error, mode=mode)

        client, error = authenticated_client(
            calls=self._calls,
            connection_id=connection_id,
            sync_run_id=run.id,
            purpose="fusionsolar_production_authentication",
            credentials=credentials,
            client_factory=self._client_factory,
            cache=self._session_cache,
        )
        if error:
            if is_session_expiry(error):
                invalidate_session(credentials, cache=self._session_cache)
            return self._finish(run.id, connection_id, requested_from, requested_until, expected, 0, 0, selection_findings, error, mode=mode)

        received = accepted = rejected = out_of_window = 0
        first_error: ProviderError | None = None
        incomplete = bool(selection_findings)
        day_batch_checkpoint: dict[str, Any] | None = None
        for source_day in _days(requested_from, requested_until):
            selected = selected_by_day[source_day]
            if not selected:
                continue
            # A checkpoint only ever applies to the day it names. This window
            # is almost always exactly that one day (bounded-backfill jobs are
            # single-day payloads), but the guard is what makes misapplying a
            # stale or foreign checkpoint impossible rather than merely
            # unlikely.
            resume_checkpoint = (
                batch_checkpoint
                if batch_checkpoint is not None
                and batch_checkpoint.get("connection_id") == connection_id
                and batch_checkpoint.get("source_day") == source_day.isoformat()
                else None
            )
            outcome = self._sync_day(run.id, connection_id, client, source_day, contract, selected, resume_checkpoint=resume_checkpoint)
            received += outcome.received
            accepted += outcome.accepted
            rejected += outcome.rejected
            out_of_window += outcome.out_of_window
            incomplete = incomplete or outcome.partial
            if outcome.batch_checkpoint is not None:
                day_batch_checkpoint = outcome.batch_checkpoint
            if outcome.error:
                first_error = outcome.error
                break

        complete = first_error is None and not incomplete and rejected == 0
        result = self._finish(
            run.id,
            connection_id,
            requested_from,
            requested_until,
            expected,
            received,
            accepted,
            rejected + selection_findings,
            first_error,
            advance=complete and allow_cursor_advance,
            contract=contract,
            partial=incomplete,
            mode=mode,
        )
        if day_batch_checkpoint is not None:
            result = replace(result, batch_checkpoint=day_batch_checkpoint)
        # Only resume from a chunk that actually moved the cursor. A chunk that
        # ended short leaves the cursor where it was, so resuming would ask for
        # the very same days again -- which is the loop this change exists to
        # break, not to reproduce a chunk at a time.
        if resume_from is not None and result.cursor_advanced:
            return replace(result, next_source_day=resume_from)
        return result

    def _sync_day(
        self,
        run_id: int,
        connection_id: int,
        client: FusionSolarClient,
        source_day: date,
        contract: FusionSolarProductionContract,
        selected: list[AssetProviderMapping],
        *,
        resume_checkpoint: dict[str, Any] | None = None,
    ) -> "_DayOutcome":
        """Fetch one source day, one provider-call batch at a time, persisting
        each batch as it lands rather than waiting for the whole day.

        A day with more than `_MAX_BATCH` selected mappings needs more than
        one provider call, and a provider that allows roughly one call per
        cooldown can take many attempts to get through all of them. Without a
        durable batch-level checkpoint, every attempt restarted at batch one:
        the mappings batch one already covered got re-fetched (and re-spent
        the single call the provider allowed that cycle) while batch two never
        got its turn. Six reportable assets sat missing from every affected
        August day for exactly this reason.

        `resume_checkpoint`, when it names this exact source day, says which
        mapping ids already have a batch call that reached the provider
        (`completed_mapping_ids`) plus the running totals and per-mapping
        completeness an earlier attempt had gathered. Filtering everything
        through the *current* `selected` id set (rather than trusting a frozen
        order or index) is what keeps a mapping list that changed between
        attempts from corrupting an already-started sequence: a dropped
        mapping's old progress is simply excluded, and a newly-added one is
        simply still outstanding.
        """
        expected = {mapping.normalized_external_id: mapping for mapping in selected}
        selected_ids = {mapping.id for mapping in selected}
        checkpoint = resume_checkpoint or {}
        completed_ids = {mapping_id for mapping_id in checkpoint.get("completed_mapping_ids", []) if mapping_id in selected_ids}
        sample_completeness: dict[int, str] = {
            int(mapping_id): value
            for mapping_id, value in (checkpoint.get("sample_completeness") or {}).items()
            if int(mapping_id) in selected_ids
        }
        received = int(checkpoint.get("received_total", 0) or 0)
        rejected = int(checkpoint.get("rejected_total", 0) or 0)
        out_of_window = int(checkpoint.get("out_of_window_total", 0) or 0)

        remaining = [mapping for mapping in selected if mapping.id not in completed_ids]
        window_start = datetime.combine(source_day, time.min, tzinfo=contract.source_timezone)
        window_end = window_start + timedelta(days=1)
        for batch in _batches(remaining, _MAX_BATCH):
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
                if is_session_expiry(error):
                    invalidate_session(client.credentials, cache=self._session_cache)
                checkpoint_out = _batch_checkpoint(
                    connection_id, source_day, completed_ids, len(selected), received, rejected, out_of_window, sample_completeness,
                )
                return _DayOutcome(received, len(sample_completeness), rejected, True, error, out_of_window, checkpoint_out)
            assert rows is not None
            # Scoped to this batch's own mappings, not the whole day's: a row
            # claiming a station code from a batch that has not been asked for
            # yet must not be accepted, or it would mark that mapping's batch
            # complete without a call ever having been made for it.
            batch_expected = {mapping.normalized_external_id: mapping for mapping in batch}
            batch_samples: dict[str, DailyProductionSample] = {}
            for row in rows:
                received += 1
                try:
                    sample = normalize_daily_production_row(row)
                    normalized = normalize_external_id(ProviderCode.FUSIONSOLAR, sample.external_id)
                except ValueError:
                    rejected += 1
                    continue
                if normalized not in batch_expected:
                    rejected += 1
                    continue
                # One request answers with a row per day of the month, every row
                # carrying the same station code. A row belongs to this source
                # day only if its own timestamp says so; without one it cannot be
                # attributed at all.
                if sample.source_timestamp is None:
                    rejected += 1
                    continue
                if not window_start <= sample.source_timestamp < window_end:
                    out_of_window += 1
                    continue
                if normalized in batch_samples:
                    rejected += 1
                    continue
                batch_samples[normalized] = sample
            # Committed as its own unit of work before the checkpoint that
            # marks it done ever gets written. A crash in between replays this
            # batch's one call next time -- wasteful, never wrong, because
            # persistence is idempotent -- rather than losing it.
            self._persist_day(run_id, source_day, contract, expected, batch_samples)
            for normalized, sample in batch_samples.items():
                sample_completeness[expected[normalized].id] = sample.completeness
            completed_ids |= {mapping.id for mapping in batch}

        # Every batch this day needs has now succeeded, whether just now or on
        # an earlier attempt. Durable evidence decides completeness here, not
        # an in-memory tally that a resumed attempt never fully rebuilds.
        accepted = len(sample_completeness)
        partial = accepted != len(selected) or any(value != "complete" for value in sample_completeness.values())
        return _DayOutcome(received, accepted, rejected, partial, None, out_of_window, None)

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
                source_key = f"fusionsolar-daily-pvyield:{normalized}:{source_day.isoformat()}"
                record_production_fact(
                    session,
                    asset_id=mapping.asset_id,
                    provider_mapping_id=mapping.id,
                    source_fact_key=source_key,
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
        mode: str,
        max_source_days: int,
        as_of: datetime | None,
        require_explicit_bounds: bool,
        force_window_error: str | None,
    ) -> tuple[date, date]:
        if force_window_error:
            raise ValueError(force_window_error)
        with self._sessions() as session:
            cursor = session.scalar(select(SyncCursor).where(
                SyncCursor.provider_connection_id == connection_id,
                SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                SyncCursor.cursor_key == _CURSOR_KEY,
            ))
            checkpoint = dict(cursor.checkpoint_json) if cursor else {}
        if cursor is not None and checkpoint.get("source_timezone") != contract.source_timezone_name:
            raise ValueError("Production cursor timezone differs from the configured verified timezone; operator reconciliation or reset is required.")
        if mode == "reconciliation":
            if not 1 <= reconciliation_days <= max_source_days:
                raise ValueError("Production reconciliation window exceeds its configured safety limit.")
            local_now = (as_of or datetime.now(contract.source_timezone)).astimezone(contract.source_timezone)
            end_date = local_now.date() - timedelta(days=1)
            start_date = end_date - timedelta(days=reconciliation_days - 1)
            return start_date, end_date
        if require_explicit_bounds and (start_date is None or end_date is None):
            raise ValueError("Production bounded backfill requires explicit start and end dates.")
        default_end = datetime.now(contract.source_timezone).date() - timedelta(days=1)
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
        if mode == "bounded_backfill" and end_date > default_end:
            raise ValueError("Production backfill cannot request an incomplete or future provider-local day.")
        if (end_date - start_date).days + 1 > max_source_days:
            raise ValueError("Production sync window exceeds the configured normal-sync safety limit.")
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
        mode: str = "incremental",
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
                "production_mode": mode,
            }
            record_health(session, provider_connection_id=connection_id, partial=status == "partial", error=error, **health_values_for_error(error, operation="sync"))
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
        return ProductionSyncResult(connection_id, run_id, status, completeness, start, end, expected, received, accepted, rejected, cursor_updated, error.code.value if error else None, mode)


@dataclass(frozen=True)
class _DayOutcome:
    received: int
    accepted: int
    rejected: int
    partial: bool
    error: ProviderError | None
    # Rows for other days of the same month: legitimate provider data, not a defect.
    out_of_window: int = 0
    # Durable resume point for this day, or None when it is done (or nothing
    # was attempted). See `_sync_day` and `_batch_checkpoint`.
    batch_checkpoint: dict[str, Any] | None = None


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


def _batch_checkpoint(
    connection_id: int,
    source_day: date,
    completed_ids: set[int],
    mapping_count: int,
    received: int,
    rejected: int,
    out_of_window: int,
    sample_completeness: dict[int, str],
) -> dict[str, Any]:
    """The durable shape a job payload carries between attempts at one day.

    `completed_mapping_ids` is what makes resumption skip a provider call:
    every id in it already had its batch's HTTP round-trip succeed, in this
    attempt or an earlier one. Everything else here is running totals and
    per-mapping completeness so the day's eventual finish reflects the whole
    day, not just whichever attempt happened to finish it -- and observability
    fields (`mapping_count`, `batch_size`, `batch_count`, `batches_done`,
    `next_batch`) so an operator reading `job_events` can see the shape of the
    remaining work without a database query.
    """
    batch_count = -(-mapping_count // _MAX_BATCH) if mapping_count else 0
    batches_done = -(-len(completed_ids) // _MAX_BATCH) if completed_ids else 0
    return {
        "source_day": source_day.isoformat(),
        "connection_id": connection_id,
        "completed_mapping_ids": sorted(completed_ids),
        "received_total": received,
        "rejected_total": rejected,
        "out_of_window_total": out_of_window,
        "sample_completeness": {str(mapping_id): value for mapping_id, value in sample_completeness.items()},
        "mapping_count": mapping_count,
        "batch_size": _MAX_BATCH,
        "batch_count": batch_count,
        "batches_done": batches_done,
        "next_batch": batches_done,
    }


def _calls(session: Session, run_id: int) -> int:
    return int(session.scalar(
        select(func.count()).select_from(ProviderRequestAttempt).where(
            ProviderRequestAttempt.sync_run_id == run_id,
            ProviderRequestAttempt.status.in_(("succeeded", "failed", "rate_limited")),
        )
    ) or 0)
