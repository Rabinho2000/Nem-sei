"""Sigenergy daily production history, on V2's canonical terms.

The wire contract is V1's, derived from its working implementation rather than
from documentation (`monitoring_board/services/sigenergy_history.py` and
`energy_facts.parse_sigenergy_daily_history`): one GET per system per day,
`level=Day`, `date=YYYY-MM-DD`, returning cumulative counters for that day.

Three of V1's rules are carried over deliberately, because each exists for a
reason its own code records:

  * **Unit is verified, never assumed.** The history payload does not carry a
    unit in every response, so a value counts only when the payload says kWh
    or an operator has confirmed kWh for this account.
  * **Preferred field, then legacy.** Each metric has a `...Kwh` field and an
    older bare name; the newer one wins when present, and V1 learned this the
    hard way.
  * **`powerOneself`, not `powerSelfConsumption`.** V1's own comment: the
    former is load green-power consumption and is the value that balances the
    reports. The generation-side counter is a different number.

Two of V2's rules are added, and they are the reason this module could not
simply be copied:

  * **The source day is resolved in an operator-verified timezone.** V1 sends
    `date.today()` from the server and accepts what comes back, which assumes
    the provider's day is the server's day. That has never been checked, so
    here a missing timezone is a refusal to run rather than a quiet guess.
  * **Battery counters are not persisted.** `production_facts.metric_kind`
    has no vocabulary for charge/discharge, and inventing one to hold a number
    nothing reads would be schema debt, not capability. They stay in the
    fact's metadata, where the evidence survives without pretending to be a
    canonical metric.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.integrations.sigenergy.client import SigenergyClient, SigenergyClientError, SigenergyTransport
from nemsei.integrations.sigenergy.request_control import SigenergyRequestController
from nemsei.integrations.sigenergy.service import credentials_for, production_contract_for
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now
from nemsei.sources.service import resolve_source_policy
from nemsei.jobs.ownership import OwnershipFence, OwnershipLost, assert_ownership
from nemsei.sync.collection_models import CollectionRun
from nemsei.sync.collection_service import (
    CollectionEvidence,
    finalize_collection_run,
    mark_collection_run_lost_ownership,
    start_collection_run,
)
from nemsei.sync.models import SyncCursor, SyncRun
from nemsei.sync.scope_lock import SCOPE_KIND_CONNECTION, acquire_collection_scope_lock
from nemsei.sync.service import advance_cursor, finish_sync_run, health_values_for_error, record_health, start_sync_run

# target metric -> (preferred payload field, legacy payload field)
FIELD_MAP: dict[str, tuple[str, str]] = {
    "production_energy": ("powerGenerationKwh", "powerGeneration"),
    "consumption_energy": ("powerUseKwh", "powerUse"),
    "self_use_energy": ("powerOneselfKwh", "powerOneself"),
    "export_energy": ("powerToGridKwh", "powerToGrid"),
    "grid_import_energy": ("powerFromGridKwh", "powerFromGrid"),
}
# Kept as evidence in metadata only -- see the module docstring.
BATTERY_FIELDS: dict[str, tuple[str, str]] = {
    "battery_charge_kwh": ("esChargingKwh", "esCharging"),
    "battery_discharge_kwh": ("esDischargingKwh", "esDischarging"),
}
CORE_METRICS = tuple(FIELD_MAP)
_CURSOR_KEY = "sigenergy-daily-production"


class SigenergyHistoryUnitError(ValueError):
    """The payload's unit was neither stated as kWh nor operator-confirmed."""


@dataclass(frozen=True)
class ParsedDay:
    values: dict[str, float | None]
    battery: dict[str, float | None]
    quality: str
    completeness: str
    source_unit: str


def _energy(raw: Any, field: str) -> float | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{field} is not a numeric energy value.")
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a numeric energy value.") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} is not a valid kWh reading.")
    return parsed


def _pick(payload: dict[str, Any], preferred: str, legacy: str) -> tuple[Any, str]:
    if payload.get(preferred) not in (None, ""):
        return payload[preferred], preferred
    return payload.get(legacy), legacy


def parse_daily_history(payload: dict[str, Any], *, confirmed_unit: str) -> ParsedDay:
    """One day's counters, or a refusal. Never a silently converted value."""
    if not isinstance(payload, dict):
        raise ValueError("Sigenergy history returned an invalid payload.")
    payload_unit = str(payload.get("unit") or "").strip()
    source_unit = payload_unit or confirmed_unit.strip()
    if source_unit.casefold() != "kwh":
        raise SigenergyHistoryUnitError("Sigenergy history unit is not confirmed as kWh.")

    values: dict[str, float | None] = {}
    for metric, (preferred, legacy) in FIELD_MAP.items():
        raw, field = _pick(payload, preferred, legacy)
        values[metric] = _energy(raw, field)
    battery: dict[str, float | None] = {}
    for name, (preferred, legacy) in BATTERY_FIELDS.items():
        raw, field = _pick(payload, preferred, legacy)
        battery[name] = _energy(raw, field)

    present = [value for value in values.values() if value is not None]
    if not present:
        quality, completeness = "missing", "missing"
    elif all(values[metric] is not None for metric in CORE_METRICS):
        quality, completeness = "complete", "complete"
    else:
        quality, completeness = "partial", "partial"
    return ParsedDay(values, battery, quality, completeness, payload_unit or confirmed_unit)


@dataclass
class SigenergyProductionResult:
    """What one run was obliged to collect, and what it actually got.

    `days_requested`/`days_accepted` are kept under their original names
    because callers and stored job results use them, but both now count the
    same unit -- **mapping-days**, one obligation per system per day. They
    previously counted different things (`len(days)` against a per-mapping
    tally), so a two-system account could report accepting five of three.
    """

    status: str
    days_requested: int
    days_accepted: int
    facts_written: int
    provider_calls: int
    error_code: str | None = None
    days_rejected: int = 0
    #: The run this result came from, so a rate-limited outcome can be
    #: deferred against the persisted cooldown instead of spending the job's
    #: retry budget on a call nobody made.
    sync_run_id: int = 0


class SigenergyProductionService:
    """Daily history for the mappings a source policy actually selects."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client_factory: Any = None,
        transport: SigenergyTransport | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._transport = transport
        self._client_factory = client_factory or (
            lambda credentials, endpoints, transport: SigenergyClient(endpoints, credentials, transport=transport)
        )
        self._calls = SigenergyRequestController(session_factory)

    def sync_incremental(
        self, connection_id: int, *, max_days: int = 7, fence: OwnershipFence | None = None
    ) -> SigenergyProductionResult:
        """Resume from wherever the cursor got to, bounded.

        The cursor is this provider's own (`sigenergy-daily-production`) and
        advances only on a clean run, the same conservative rule FusionSolar
        uses: replaying a completed day is safe because the facts are keyed
        idempotently, while skipping one is not recoverable without noticing.

        `max_days` is a safety bound, not a budget. Unlike FusionSolar's
        31-day cap this one does **not** refuse when the gap is larger -- it
        takes the oldest `max_days` and leaves the rest for the next tick,
        because a cap that refuses is a cap that gets stuck the moment the gap
        outgrows it, which is exactly the trap FusionSolar fell into.

        The window itself is resolved **inside** the run, after the contract
        is known, and never here. It used to be computed from `utc_now()`
        before anything had established which day the provider is in, so a run
        at 23:30 UTC in September asked Lisbon for a day that had another hour
        to go and stored the counter it was holding at that moment as the
        day's total. V1 refused `target_date >= today` for exactly this
        reason; the refusal is back, against the source's own calendar.
        """
        if max_days <= 0:
            raise ValueError("Sigenergy production window must span at least one day.")
        return self._sync(connection_id, start_date=None, end_date=None, max_days=max_days, fence=fence)

    def sync_daily_production(
        self, connection_id: int, *, start_date: date, end_date: date, fence: OwnershipFence | None = None
    ) -> SigenergyProductionResult:
        if end_date < start_date:
            raise ValueError("Sigenergy production window is invalid.")
        return self._sync(
            connection_id, start_date=start_date, end_date=end_date, max_days=None, fence=fence
        )

    def _sync(
        self,
        connection_id: int,
        *,
        start_date: date | None,
        end_date: date | None,
        max_days: int | None,
        fence: OwnershipFence | None = None,
    ) -> SigenergyProductionResult:
        with self._sessions() as session:
            connection = ProviderRepository(session).connection(connection_id)
            if connection is None:
                raise ValueError("Unknown provider connection.")
            session.expunge(connection)

        run_id = self._start_run(connection_id)
        if connection.provider_code != ProviderCode.SIGENERGY.value:
            return self._finish(run_id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Connection is not Sigenergy."))
        if not connection.enabled or connection.configuration_status != "configured":
            return self._finish(run_id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy connection is not enabled and configured."))
        if not self._settings.capabilities.get("provider_reads", False):
            return self._finish(run_id, connection_id, 0, 0, 0, 0, ProviderError(ProviderErrorCode.NOT_SUPPORTED, "Provider reads are disabled by policy."), deferred=True)
        try:
            contract = production_contract_for(connection)
            credentials, endpoints = credentials_for(connection)
        except SigenergyClientError as exc:
            return self._finish(run_id, connection_id, 0, 0, 0, 0, exc.error)

        # The window is decided before the client is built, so a tick with
        # nothing due costs no provider call at all -- not even the login. This
        # runs every few hours against an account that rate-limits, and after
        # the cursor reaches the last closed day most of those ticks have
        # nothing to fetch.
        #
        # `last_closed_day` is the last day the source has finished. Everything
        # after it is a counter still moving, and a counter still moving is not
        # a total.
        last_closed_day = utc_now().astimezone(contract.source_timezone).date() - timedelta(days=1)
        if start_date is None:
            start_date = self._resume_from(connection_id, default=last_closed_day)
        window_end = end_date if end_date is not None else start_date + timedelta(days=(max_days or 1) - 1)
        window_end = min(window_end, last_closed_day)
        if max_days is not None:
            window_end = min(window_end, start_date + timedelta(days=max_days - 1))

        if window_end < start_date:
            # Nothing has closed since the cursor. That is a complete run with
            # no obligations, not a failure -- and, crucially, not a reason to
            # move the cursor anywhere.
            return self._finish(
                run_id, connection_id, 0, 0, 0, 0, None,
                timezone_name=contract.source_timezone_name, nothing_due=True,
            )

        client = self._client_factory(credentials, endpoints, self._transport)
        _value, error = self._calls.call(
            connection_id=connection_id, sync_run_id=run_id, endpoint_family="authentication",
            purpose="sigenergy_production_authentication", operation=client.authenticate,
        )
        if error:
            return self._finish(run_id, connection_id, 0, 0, 0, 1, error)

        days = [start_date + timedelta(days=offset) for offset in range((window_end - start_date).days + 1)]
        expected, accepted, rejected, written, calls = 0, 0, 0, 0, 1
        # (mapping, source_day, parsed) for every day that produced evidence.
        # Bounded by `max_days` x mappings-per-connection: seven days across a
        # handful of Sigenergy systems, so holding it is cheaper than holding
        # a transaction open across the fetch.
        pending: list[tuple[AssetProviderMapping, date, ParsedDay]] = []
        # Mapping-days that came back with *some* real readings. They are not
        # accepted -- the day is not collected -- but they are the difference
        # between "the provider answered incompletely", which is worth another
        # attempt, and "the provider had nothing", which is not the same thing.
        partly_collected = 0
        last_error: ProviderError | None = None
        unresolved: list[str] = []
        for source_day in days:
            selected, findings = self._selected_mappings(connection_id, source_day)
            unresolved.extend(findings)
            if not selected:
                # A day nobody is configured to read is not a day that was
                # read. Counting it as neither expected nor rejected is how a
                # gap in the source policy became invisible.
                rejected += 1
                expected += 1
                continue
            for mapping in selected:
                expected += 1
                payload, error = self._calls.call(
                    connection_id=connection_id, sync_run_id=run_id, endpoint_family="production_history_daily",
                    purpose="sigenergy_daily_history",
                    operation=lambda mapping=mapping, source_day=source_day: client.get_system_history(
                        mapping.external_id, target_date=source_day
                    ),
                )
                calls += 1
                if error:
                    last_error = error
                    rejected += 1
                    continue
                try:
                    parsed = parse_daily_history(payload, confirmed_unit=contract.canonical_unit)
                except (ValueError, SigenergyHistoryUnitError) as exc:
                    last_error = ProviderError(ProviderErrorCode.INVALID_RESPONSE, str(exc))
                    rejected += 1
                    continue
                # The evidence is written either way -- a `missing` fact is a
                # durable record that this day was asked for and came back
                # empty, which is worth more than silence. What it is not is a
                # collected day, so only a complete one counts as accepted.
                # Buffered, not written. The provider is still on the other
                # end of this loop and an authoritative transaction must not
                # be open while we wait on the network.
                pending.append((mapping, source_day, parsed))
                if parsed.completeness == "complete":
                    accepted += 1
                    continue
                rejected += 1
                if parsed.completeness == "missing":
                    last_error = last_error or ProviderError(
                        ProviderErrorCode.INVALID_RESPONSE,
                        "Sigenergy returned no readings for a day that should have closed.",
                    )
                else:
                    partly_collected += 1
                    last_error = last_error or ProviderError(
                        ProviderErrorCode.INVALID_RESPONSE,
                        "Sigenergy returned only part of a day's metrics.",
                    )
        if unresolved:
            last_error = last_error or ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "Sigenergy production has mappings with no resolvable source policy.",
            )
        status_error = None if accepted else (
            last_error or ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy history returned nothing usable.")
        )
        # Complete means every obligation met. Anything less holds the cursor:
        # replaying a day already stored costs one call and is idempotent,
        # while stepping over one loses it with nothing left to notice by.
        complete = accepted == expected and expected > 0 and not unresolved
        partial = (accepted > 0 or partly_collected > 0) and not complete
        # Everything the provider had to say has been said. Only now does an
        # authoritative transaction open: facts, cursor and collection run all
        # land together or none of them do.
        written, cursor_advanced, ownership_error = self._commit_collection(
            connection_id=connection_id,
            run_id=run_id,
            pending=pending,
            contract=contract,
            window_start=start_date,
            window_end=window_end,
            advance=complete,
            fence=fence,
            expected=expected,
            accepted=accepted,
            last_closed_day=last_closed_day,
            error_code=None if complete else (status_error.code.value if status_error else None),
        )
        if ownership_error is not None:
            # The transaction was discarded. Nothing was written, the cursor
            # did not move, and the run cannot be fulfilled -- so the job must
            # not report success either.
            return self._finish(
                run_id, connection_id, expected, 0, 0, calls,
                ProviderError(ProviderErrorCode.CONFIGURATION, f"Ownership lost: {ownership_error}"),
                timezone_name=contract.source_timezone_name, rejected=rejected,
            )
        return self._finish(
            run_id, connection_id, expected, accepted, written, calls,
            None if complete else status_error,
            partial=partial, timezone_name=contract.source_timezone_name, rejected=rejected,
            cursor_advanced=cursor_advanced,
        )

    def _resume_from(self, connection_id: int, *, default: date) -> date:
        """The first day this connection still owes, from its own cursor."""
        with self._sessions() as session:
            cursor = session.scalar(
                select(SyncCursor).where(
                    SyncCursor.provider_connection_id == connection_id,
                    SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                    SyncCursor.cursor_key == _CURSOR_KEY,
                )
            )
            last_day = (cursor.checkpoint_json or {}).get("last_completed_day") if cursor else None
        if not isinstance(last_day, str):
            return default
        try:
            return date.fromisoformat(last_day) + timedelta(days=1)
        except ValueError:
            # A checkpoint nobody can read is not a checkpoint to trust; fall
            # back to the bounded default rather than skipping history on it.
            return default

    def _commit_collection(
        self,
        *,
        connection_id: int,
        run_id: int,
        pending: list[tuple[AssetProviderMapping, date, ParsedDay]],
        contract: Any,
        window_start: date,
        window_end: date,
        advance: bool,
        fence: OwnershipFence | None,
        expected: int,
        accepted: int,
        last_closed_day: date,
        error_code: str | None,
    ) -> tuple[int, bool, str | None]:
        """The whole authoritative unit, in one transaction.

        lock -> ownership -> facts -> cursor -> ownership -> run -> COMMIT.

        The second ownership assertion is not defensive duplication. The first
        one proves this worker owned the job when the writes began; a 30-second
        lease can expire while those writes are legitimately in progress, and
        an expired lease must not be able to close a run or leave a cursor
        behind. Only a check taken after the last write can say anything about
        the moment of commit.

        Returns `(facts_written, cursor_advanced, ownership_error)`. On lost
        ownership the transaction is rolled back and the loss is recorded
        separately -- never inside the transaction being discarded, which
        would throw the record away with it.
        """
        period_start = datetime.combine(window_start, time.min, tzinfo=contract.source_timezone)
        period_end = datetime.combine(window_end + timedelta(days=1), time.min, tzinfo=contract.source_timezone)
        collection_run_id: int | None = None
        if fence is not None:
            # The attempt is recorded before the authoritative transaction and
            # committed on its own. If it were created inside that transaction
            # it would be rolled back along with everything else, and a run
            # that lost its lease would leave no trace at all -- there would be
            # no row left to mark `lost_ownership`. A `running` row left behind
            # by a crashed worker is not a defect either; it is the honest
            # record that an attempt began and never reported back.
            with self._sessions() as session:
                collection_run_id = start_collection_run(
                    session,
                    provider_connection_id=connection_id,
                    capability=ProviderCapability.PRODUCTION_HISTORY.value,
                    scope_kind=SCOPE_KIND_CONNECTION,
                    scope_key=str(connection_id),
                    period_start=period_start,
                    period_end=period_end,
                    job_id=fence.job_id,
                    lease_generation=fence.lease_generation,
                ).id
                session.commit()
        try:
            with self._sessions() as session:
                acquire_collection_scope_lock(
                    session,
                    connection_id=connection_id,
                    capability=ProviderCapability.PRODUCTION_HISTORY.value,
                    scope_kind=SCOPE_KIND_CONNECTION,
                    scope_key=str(connection_id),
                    period_start=period_start,
                    period_end=period_end,
                )
                collection_run = None
                if fence is not None:
                    assert_ownership(session, fence)
                    collection_run = session.get(CollectionRun, collection_run_id)
                    assert collection_run is not None

                written = 0
                for mapping, source_day, parsed in pending:
                    written += self._persist(session, mapping, source_day, parsed, contract, run_id)

                cursor_advanced = False
                if advance:
                    run = session.get(SyncRun, run_id)
                    assert run is not None
                    # `advance_cursor` refuses to move coverage for a run
                    # that is not successful, and the `SyncRun` is still
                    # `running` at this point because `_finish` runs after this
                    # transaction. The flip is not a lie -- `advance` is only
                    # true when the window is complete, which is exactly the
                    # condition under which `_finish` will write `success` a
                    # moment later -- but it is restored before commit so that
                    # a crash in between leaves the run `running` for the
                    # abandoned-run sweep rather than `success` with no
                    # counters. Removing the guard instead would weaken a check
                    # that protects every other caller.
                    previous_status = run.status
                    run.status = "success"
                    advance_cursor(
                        session,
                        run=run,
                        cursor_key=_CURSOR_KEY,
                        checkpoint={
                            "last_completed_day": window_end.isoformat(),
                            "source_timezone": contract.source_timezone_name,
                        },
                        covered_through=datetime.combine(
                            window_end + timedelta(days=1), time.min, tzinfo=contract.source_timezone
                        ),
                        fence=fence,
                    )
                    run.status = previous_status
                    cursor_advanced = True

                if fence is not None and collection_run is not None:
                    finalize_collection_run(
                        session,
                        collection_run,
                        fence=fence,
                        evidence=CollectionEvidence(
                            facts_written=written,
                            scopes_required=expected,
                            scopes_written=accepted,
                            cursor_advanced=cursor_advanced,
                            # `window_end` is clamped to `last_closed_day`
                            # upstream, so this holds by construction -- stated
                            # rather than assumed, because the clamp is one
                            # edit away from being lost.
                            period_closed=window_end <= last_closed_day,
                            error_code=error_code,
                        ),
                        requires_cursor=True,
                    )
                session.commit()
                return written, cursor_advanced, None
        except OwnershipLost as exc:
            if collection_run_id is not None:
                self._record_lost_ownership(collection_run_id, exc.reason)
            return 0, False, exc.reason

    def _record_lost_ownership(self, collection_run_id: int, reason: str) -> None:
        """Best effort, in its own transaction, after the rollback.

        Failing to record the loss must never be able to resurrect the commit
        that was discarded, so this swallows its own errors.
        """
        try:
            with self._sessions() as session:
                mark_collection_run_lost_ownership(session, collection_run_id, reason=reason)
                session.commit()
        except Exception:  # pragma: no cover - diagnostics must not mask the loss
            pass

    def _persist(
        self,
        session: Session,
        mapping: AssetProviderMapping,
        source_day: date,
        parsed: ParsedDay,
        contract: Any,
        run_id: int,
    ) -> int:
        """One fact per metric, keyed so a re-read of the same day is idempotent.

        Borrows the caller's session and never commits. It used to open its
        own and commit per mapping-day, which meant a run's facts landed in as
        many transactions as it had mapping-days and the cursor landed in one
        more. There was no point at which the two were consistent, and a crash
        between them left facts with no cursor or -- worse, since the cursor
        moved last -- a cursor for facts that had failed.
        """
        period_start = datetime.combine(source_day, time.min, tzinfo=contract.source_timezone)
        written = 0
        for metric, value in parsed.values.items():
            record_production_fact(
                session,
                asset_id=mapping.asset_id,
                provider_mapping_id=mapping.id,
                sync_run_id=run_id,
                source_fact_key=f"sigenergy:{metric}:{source_day.isoformat()}",
                metric_kind=metric,
                period_start=period_start,
                period_end=period_start + timedelta(days=1),
                granularity="day",
                value=Decimal(str(value)) if value is not None else None,
                unit="kWh",
                quality=parsed.quality if value is not None else "missing",
                completeness=parsed.completeness,
                metadata={
                    "source_timezone": contract.source_timezone_name,
                    "source_unit": parsed.source_unit,
                    # Battery counters have no canonical metric; kept as
                    # evidence rather than dropped or forced into one.
                    **{name: value for name, value in parsed.battery.items() if value is not None},
                },
            )
            written += 1
        return written

    def _selected_mappings(
        self, connection_id: int, source_day: date
    ) -> tuple[list[AssetProviderMapping], list[str]]:
        """The mappings this day is read through, and the ones nobody could resolve.

        The unresolvable ones used to be swallowed: a `ValueError` from
        `resolve_source_policy` -- no primary policy valid for the period, or
        two primaries competing at the same priority -- simply removed the
        mapping from the run. The day then looked fully collected because
        nothing was left in it to fail.
        """
        with self._sessions() as session:
            selected: list[AssetProviderMapping] = []
            unresolved: list[str] = []
            for mapping in ProviderRepository(session).mappings_for_connection_on_date(connection_id, source_day):
                if mapping.mapping_status != "active" or mapping.resource_kind != "plant":
                    continue
                try:
                    policy = resolve_source_policy(session, asset_id=mapping.asset_id, source_use="production", on_date=source_day)
                except ValueError as exc:
                    unresolved.append(f"{source_day.isoformat()}:{mapping.external_id}:{exc}")
                    continue
                if policy.provider_mapping_id == mapping.id:
                    selected.append(mapping)
            for mapping in selected:
                session.expunge(mapping)
            return selected, unresolved

    def _start_run(self, connection_id: int) -> int:
        with self._sessions() as session:
            run = start_sync_run(session, provider_connection_id=connection_id, capability=ProviderCapability.PRODUCTION_HISTORY.value)
            session.commit()
            return run.id

    def _finish(
        self, run_id: int, connection_id: int, requested: int, accepted: int, written: int,
        calls: int, error: ProviderError | None, *, deferred: bool = False, partial: bool = False,
        timezone_name: str | None = None, rejected: int = 0, nothing_due: bool = False,
        cursor_advanced: bool = False,
    ) -> SigenergyProductionResult:
        """Turn what happened into one status, with nothing rounded upwards.

        `nothing_due` is the one case where zero accepted is still success:
        the cursor is already at the last closed day, so the run had no
        obligations to meet. Every other zero-accepted outcome is a failure,
        including the one that used to reach here as success -- a payload that
        parsed cleanly and contained no readings.
        """
        if deferred:
            status = "deferred"
        elif nothing_due:
            status = "success"
        elif partial:
            # Checked before the error, deliberately: a run that collected
            # something real and something not is partial whatever error it
            # also carries, and calling it failed would hide the part that
            # landed from anything counting coverage.
            status = "partial"
        elif error:
            status = "rate_limited" if error.code == ProviderErrorCode.RATE_LIMITED else "failed"
        else:
            status = "success"
        completeness = "complete" if status == "success" else ("partial" if status == "partial" else "none")
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {
                "actual_provider_calls": calls,
                # All three count the same unit -- mapping-days -- so
                # `accepted + rejected <= expected` is a statement that means
                # something. They used to mix days with mapping-days.
                "expected_items": requested,
                "items_received": accepted + rejected,
                "items_accepted": accepted,
                "items_rejected": rejected,
                "source_period_timezone": timezone_name,
                "production_mode": "daily_history",
                "cursor_advanced": cursor_advanced,
            }
            record_health(
                session, provider_connection_id=connection_id, partial=status == "partial",
                error=error, **health_values_for_error(error, operation="sync"),
            )
            finish_sync_run(
                session, run=run, status=status, completeness=completeness,
                error=error if status not in ("success", "partial") else None,
            )
            session.commit()
        return SigenergyProductionResult(
            status, requested, accepted, written, calls,
            error.code.value if error else None, rejected, run_id,
        )
