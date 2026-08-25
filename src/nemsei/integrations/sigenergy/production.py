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
from nemsei.sources.service import resolve_source_policy
from nemsei.sync.models import SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run

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
    status: str
    days_requested: int
    days_accepted: int
    facts_written: int
    provider_calls: int
    error_code: str | None = None


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

    def sync_daily_production(
        self, connection_id: int, *, start_date: date, end_date: date
    ) -> SigenergyProductionResult:
        if end_date < start_date:
            raise ValueError("Sigenergy production window is invalid.")
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

        client = self._client_factory(credentials, endpoints, self._transport)
        _value, error = self._calls.call(
            connection_id=connection_id, sync_run_id=run_id, endpoint_family="authentication",
            purpose="sigenergy_production_authentication", operation=client.authenticate,
        )
        if error:
            return self._finish(run_id, connection_id, 0, 0, 0, 1, error)

        days = [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]
        accepted, written, calls, last_error = 0, 0, 1, None
        for source_day in days:
            selected = self._selected_mappings(connection_id, source_day)
            for mapping in selected:
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
                    continue
                try:
                    parsed = parse_daily_history(payload, confirmed_unit=contract.canonical_unit)
                except (ValueError, SigenergyHistoryUnitError) as exc:
                    last_error = ProviderError(ProviderErrorCode.INVALID_RESPONSE, str(exc))
                    continue
                written += self._persist(mapping, source_day, parsed, contract, run_id)
                accepted += 1
        status_error = None if accepted else (last_error or ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy history returned nothing usable."))
        partial = bool(last_error) and accepted > 0
        return self._finish(run_id, connection_id, len(days), accepted, written, calls, status_error, partial=partial, timezone_name=contract.source_timezone_name)

    def _persist(self, mapping: AssetProviderMapping, source_day: date, parsed: ParsedDay, contract: Any, run_id: int) -> int:
        """One fact per metric, keyed so a re-read of the same day is idempotent."""
        period_start = datetime.combine(source_day, time.min, tzinfo=contract.source_timezone)
        written = 0
        with self._sessions() as session:
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
            session.commit()
        return written

    def _selected_mappings(self, connection_id: int, source_day: date) -> list[AssetProviderMapping]:
        with self._sessions() as session:
            selected: list[AssetProviderMapping] = []
            for mapping in ProviderRepository(session).mappings_for_connection_on_date(connection_id, source_day):
                if mapping.mapping_status != "active" or mapping.resource_kind != "plant":
                    continue
                try:
                    policy = resolve_source_policy(session, asset_id=mapping.asset_id, source_use="production", on_date=source_day)
                except ValueError:
                    continue
                if policy.provider_mapping_id == mapping.id:
                    selected.append(mapping)
            for mapping in selected:
                session.expunge(mapping)
            return selected

    def _start_run(self, connection_id: int) -> int:
        with self._sessions() as session:
            run = start_sync_run(session, provider_connection_id=connection_id, capability=ProviderCapability.PRODUCTION_HISTORY.value)
            session.commit()
            return run.id

    def _finish(
        self, run_id: int, connection_id: int, requested: int, accepted: int, written: int,
        calls: int, error: ProviderError | None, *, deferred: bool = False, partial: bool = False,
        timezone_name: str | None = None,
    ) -> SigenergyProductionResult:
        if deferred:
            status = "deferred"
        elif error and not accepted:
            status = "rate_limited" if error.code == ProviderErrorCode.RATE_LIMITED else "failed"
        elif partial:
            status = "partial"
        else:
            status = "success"
        completeness = "complete" if status == "success" else ("partial" if status == "partial" else "none")
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            run.metadata_json = {
                "actual_provider_calls": calls,
                "expected_items": requested,
                "items_received": accepted,
                "items_accepted": accepted,
                "items_rejected": 0,
                "source_period_timezone": timezone_name,
                "production_mode": "daily_history",
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
        return SigenergyProductionResult(status, requested, accepted, written, calls, error.code.value if error else None)
