"""Power samples in, daily energy out -- as an explicit, declared estimate.

This is the only place in the Huawei SCADA integration where a number changes
meaning: kilowatts measured at instants become kilowatt-hours attributed to a
day. Every rule that makes that step defensible is written down here and
stamped onto each fact, because a reader of a customer report cannot see the
samples behind it.

What it does:

* **Trapezoidal integration between consecutive samples**, in the asset's own
  local day, so a 23-hour or 25-hour DST day integrates over its real length.
* **Gaps are excluded, never bridged.** Two samples further apart than the
  connection's verified `MAX_SAMPLE_GAP_SECONDS` are treated as two segments
  with nothing in between. Bridging a four-hour outage by assuming the power
  in the middle was the average of its ends is exactly the kind of invention
  this codebase refuses elsewhere.
* **Every fact is `quality='partial'`, `estimated=true`,
  `measurement_method='power_integral'`.** An integrated estimate is not a
  metered total and never claims to be, no matter how dense the sampling gets.

What it deliberately does not do:

* **No export or grid import without a verified sign convention.** Register
  37502 is signed and nothing observed says which direction positive means.
  Without `GRID_SIGN_CONVENTION` on the connection, those two metrics are
  simply not written.
* **No self-consumption unless it is asked for and defensible.** Even with the
  derivation enabled, a day in which the battery moved any energy at all is
  skipped: `consumption - grid_import` only equals self-use when nothing else
  is absorbing the difference, which is precisely the reason
  `monitoring/models.py` refuses to derive between metrics in general.
* **No provider call, ever.** This reads rows V2 already holds. It is a job,
  not a sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.assets.models import Asset
from nemsei.config import Settings
from nemsei.integrations.huawei_scada.models import HuaweiScadaPowerSample
from nemsei.integrations.huawei_scada.service import (
    HuaweiScadaContract,
    contract_for,
    require_huawei_scada_connection,
)
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sources.service import resolve_source_policy

SECONDS_PER_HOUR = Decimal(3600)
# What every fact this module writes declares about itself.
MEASUREMENT_METHOD = "power_integral"
INTEGRATION_RULE = "trapezoidal"
# The signal each metric integrates. Production is contract-selected because
# 37498 (PV input) and 37516 (AC active) are different measurements.
LOAD_SIGNAL = "load_power_kw"
GRID_SIGNAL = "grid_power_kw"
BATTERY_SIGNAL = "battery_power_kw"
PRODUCTION_SIGNAL_COLUMNS = {
    "pv_input_power": "pv_input_power_kw",
    "total_active_power": "total_active_power_kw",
}


@dataclass(frozen=True)
class IntegrationResult:
    """One integrated series over one period, with the shape of its coverage."""

    energy_kwh: Decimal | None
    sample_count: int
    covered_seconds: int
    gap_count: int
    largest_gap_seconds: int
    session_boundaries: int
    clock_anomalies: int
    first_sample_at: datetime | None
    last_sample_at: datetime | None

    @property
    def has_energy(self) -> bool:
        return self.energy_kwh is not None


@dataclass
class RollupResult:
    days_requested: int = 0
    days_with_samples: int = 0
    facts_written: int = 0
    mappings_selected: int = 0
    mappings_skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


def current_samples(
    session: Session, *, provider_mapping_id: int, period_start: datetime, period_end: datetime
) -> list[HuaweiScadaPowerSample]:
    """Every sample of a period at its newest revision only.

    `huawei_scada_power_samples` is append-only with supersession, exactly like
    `production_facts`. Integrating the raw rows would count a corrected
    reading *and* the reading it corrected -- the same defect that once
    reported 129 kWh for a day that produced 59.
    """
    statement = (
        select(HuaweiScadaPowerSample)
        .where(
            HuaweiScadaPowerSample.provider_mapping_id == provider_mapping_id,
            HuaweiScadaPowerSample.observed_at >= as_utc(period_start),
            HuaweiScadaPowerSample.observed_at < as_utc(period_end),
            HuaweiScadaPowerSample.quality != "missing",
        )
        .distinct(HuaweiScadaPowerSample.source_sample_key)
        .order_by(
            HuaweiScadaPowerSample.source_sample_key,
            HuaweiScadaPowerSample.source_revision.desc(),
        )
    )
    samples = list(session.scalars(statement))
    samples.sort(key=lambda sample: (as_utc(sample.observed_at), sample.id))
    return samples


def integrate(
    samples: Iterable[HuaweiScadaPowerSample],
    *,
    column: str,
    max_gap_seconds: int,
    clip: str | None = None,
) -> IntegrationResult:
    """Trapezoidal energy under a power series, in kWh.

    `clip` splits a signed signal into one direction: `"positive"` integrates
    max(p, 0) and `"negative"` integrates max(-p, 0). Clipping per sample
    before integrating puts a small error at each sign crossing -- the true
    crossing lies between two samples, not at one. That error is real and it
    is why these facts are marked estimated; it is far smaller than the error
    of not distinguishing import from export at all.
    """
    ordered = [sample for sample in samples]
    # Accumulated in kilowatt-seconds and divided once at the end. Dividing
    # each interval by 3600 as it is added rounds 288 times a day at Decimal's
    # 28-digit precision, and those roundings accumulate: a constant 10 kW for
    # an hour came out as 9.999999999999999999999999997 kWh before this.
    energy_kilowatt_seconds = Decimal(0)
    covered = 0
    gaps = 0
    largest_gap = 0
    boundaries = 0
    anomalies = 0
    usable = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    previous: tuple[datetime, Decimal, int | None] | None = None

    for sample in ordered:
        value = getattr(sample, column)
        moment = as_utc(sample.observed_at)
        if first_at is None or moment < first_at:
            first_at = moment
        if last_at is None or moment > last_at:
            last_at = moment
        if value is None:
            # An absent register is not zero power. The interval on either
            # side of it is simply not covered.
            previous = None
            continue
        usable += 1
        current = _clip(Decimal(value), clip)
        if previous is not None:
            previous_at, previous_value, previous_session = previous
            delta = int((moment - previous_at).total_seconds())
            if delta < 0:
                # Time went backwards: reordered or a clock stepped. Neither
                # is integrable, and pretending otherwise would add energy.
                anomalies += 1
                previous = (moment, current, sample.session_id)
                continue
            if sample.session_id != previous_session:
                boundaries += 1
            if delta == 0:
                pass
            elif delta > max_gap_seconds:
                gaps += 1
                largest_gap = max(largest_gap, delta)
            else:
                energy_kilowatt_seconds += (previous_value + current) / 2 * Decimal(delta)
                covered += delta
        previous = (moment, current, sample.session_id)

    return IntegrationResult(
        energy_kwh=(energy_kilowatt_seconds / SECONDS_PER_HOUR) if usable else None,
        sample_count=usable,
        covered_seconds=covered,
        gap_count=gaps,
        largest_gap_seconds=largest_gap,
        session_boundaries=boundaries,
        clock_anomalies=anomalies,
        first_sample_at=first_at,
        last_sample_at=last_at,
    )


def _clip(value: Decimal, clip: str | None) -> Decimal:
    if clip is None:
        return value
    if clip == "positive":
        return value if value > 0 else Decimal(0)
    if clip == "negative":
        return -value if value < 0 else Decimal(0)
    raise ValueError(f"Unknown clip direction {clip!r}.")


def battery_moved_energy(samples: Iterable[HuaweiScadaPowerSample]) -> bool:
    """Did the battery do anything at all in this window?

    Any flow at all disqualifies the self-use derivation, not a threshold: the
    point is whether `consumption - grid_import` can be trusted to equal
    self-consumption, and a battery moving 200 W for an hour breaks that
    identity just as surely as one moving 5 kW.
    """
    return any(
        getattr(sample, BATTERY_SIGNAL) is not None and Decimal(getattr(sample, BATTERY_SIGNAL)) != 0
        for sample in samples
    )


class HuaweiScadaRollupService:
    """Daily energy from stored samples. Zero provider calls, by construction."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self._sessions = session_factory
        self._settings = settings

    def roll_up(self, connection_id: int, *, lookback_days: int = 2, today: date | None = None) -> RollupResult:
        """Re-integrate the last `lookback_days` local days for every selected mapping.

        Re-running is the reconciliation mechanism: a day integrated while it
        was still in progress is integrated again once complete, and the
        append-only revision rule turns that into a correction rather than a
        duplicate. There is nothing to reconcile *against* a provider, because
        there is no provider endpoint here to ask.
        """
        result = RollupResult()
        if lookback_days <= 0:
            raise ValueError("Huawei SCADA rollup lookback must be positive.")
        with self._sessions() as session:
            connection = require_huawei_scada_connection(ProviderRepository(session).connection(connection_id))
            contract = contract_for(connection)
            mappings = [
                mapping
                for mapping in ProviderRepository(session).current_mappings_for_connection(connection_id)
                if mapping.mapping_status == "active"
            ]
            for mapping in mappings:
                session.expunge(mapping)

        for mapping in mappings:
            self._roll_up_mapping(mapping, contract=contract, lookback_days=lookback_days, today=today, result=result)
        return result

    def _roll_up_mapping(
        self,
        mapping: AssetProviderMapping,
        *,
        contract: HuaweiScadaContract,
        lookback_days: int,
        today: date | None,
        result: RollupResult,
    ) -> None:
        with self._sessions() as session:
            asset = session.get(Asset, mapping.asset_id)
            if asset is None or not asset.timezone:
                result.mappings_skipped += 1
                result.skip("asset_timezone_missing")
                return
            try:
                zone = ZoneInfo(asset.timezone)
            except ZoneInfoNotFoundError:
                result.mappings_skipped += 1
                result.skip("asset_timezone_invalid")
                return
            timezone_name = asset.timezone

        now = utc_now()
        last_day = today or now.astimezone(zone).date()
        days = [last_day - timedelta(days=offset) for offset in range(lookback_days)]
        selected_any = False
        for day in sorted(days):
            result.days_requested += 1
            with self._sessions() as session:
                try:
                    policy = resolve_source_policy(
                        session, asset_id=mapping.asset_id, source_use="production", on_date=day
                    )
                except ValueError:
                    result.skip("no_production_source_policy")
                    continue
                if policy.provider_mapping_id != mapping.id:
                    # Another provider is this asset's production source for
                    # that day. Writing facts anyway would double-count in
                    # `build_dataset`, which totals every mapping of an asset.
                    result.skip("not_the_selected_production_source")
                    continue
                selected_any = True
                period_start = datetime.combine(day, time.min, tzinfo=zone)
                period_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
                samples = current_samples(
                    session, provider_mapping_id=mapping.id, period_start=period_start, period_end=period_end
                )
                if not samples:
                    result.skip("no_samples")
                    continue
                result.days_with_samples += 1
                written = self._write_day(
                    session,
                    mapping=mapping,
                    contract=contract,
                    samples=samples,
                    period_start=period_start,
                    period_end=period_end,
                    timezone_name=timezone_name,
                    now=now,
                    result=result,
                )
                result.facts_written += written
                session.commit()
        if selected_any:
            result.mappings_selected += 1

    def _write_day(
        self,
        session: Session,
        *,
        mapping: AssetProviderMapping,
        contract: HuaweiScadaContract,
        samples: list[HuaweiScadaPowerSample],
        period_start: datetime,
        period_end: datetime,
        timezone_name: str,
        now: datetime,
        result: RollupResult,
    ) -> int:
        day = period_start.date()
        day_seconds = int((period_end - period_start).total_seconds())
        in_progress = as_utc(period_end) > as_utc(now)
        gap = contract.max_sample_gap_seconds

        production = integrate(
            samples, column=PRODUCTION_SIGNAL_COLUMNS[contract.production_signal], max_gap_seconds=gap
        )
        consumption = integrate(samples, column=LOAD_SIGNAL, max_gap_seconds=gap)
        metrics: list[tuple[str, IntegrationResult, dict[str, Any]]] = [
            ("production_energy", production, {"source_signal": contract.production_signal}),
            ("consumption_energy", consumption, {"source_signal": "load_power"}),
        ]

        grid_import: IntegrationResult | None = None
        if contract.derives_grid_flows:
            export_clip = "positive" if contract.grid_export_is_positive else "negative"
            import_clip = "negative" if contract.grid_export_is_positive else "positive"
            export = integrate(samples, column=GRID_SIGNAL, max_gap_seconds=gap, clip=export_clip)
            grid_import = integrate(samples, column=GRID_SIGNAL, max_gap_seconds=gap, clip=import_clip)
            convention = {"source_signal": "grid_power", "grid_sign_convention": contract.grid_sign_convention}
            metrics.append(("export_energy", export, convention))
            metrics.append(("grid_import_energy", grid_import, convention))
        else:
            result.skip("grid_sign_convention_unverified")

        if contract.derives_self_use and grid_import is not None:
            if battery_moved_energy(samples):
                # A battery absorbs the difference, so the identity that makes
                # this derivation valid does not hold for this day.
                result.skip("self_use_not_derivable_with_battery_flow")
                result.warnings.append(f"self_use_skipped_battery_flow:{day.isoformat()}")
            elif consumption.has_energy and grid_import.has_energy:
                derived = consumption.energy_kwh - grid_import.energy_kwh
                if derived < 0:
                    result.skip("self_use_derivation_negative")
                    result.warnings.append(f"self_use_skipped_negative:{day.isoformat()}")
                else:
                    self_use = IntegrationResult(
                        energy_kwh=derived,
                        sample_count=min(consumption.sample_count, grid_import.sample_count),
                        covered_seconds=min(consumption.covered_seconds, grid_import.covered_seconds),
                        gap_count=max(consumption.gap_count, grid_import.gap_count),
                        largest_gap_seconds=max(consumption.largest_gap_seconds, grid_import.largest_gap_seconds),
                        session_boundaries=consumption.session_boundaries,
                        clock_anomalies=consumption.clock_anomalies,
                        first_sample_at=consumption.first_sample_at,
                        last_sample_at=consumption.last_sample_at,
                    )
                    metrics.append(
                        (
                            "self_use_energy",
                            self_use,
                            {"derivation": contract.self_use_derivation, "source_signal": "load_power,grid_power"},
                        )
                    )

        written = 0
        for metric_kind, integration, extra in metrics:
            if not integration.has_energy:
                continue
            completeness = self._completeness(
                integration, period_start=period_start, period_end=period_end, day_seconds=day_seconds, gap=gap
            )
            metadata = {
                "measurement_method": MEASUREMENT_METHOD,
                "estimated": True,
                "integration_rule": INTEGRATION_RULE,
                "power_unit": contract.power_unit,
                "asset_timezone": timezone_name,
                "sample_count": integration.sample_count,
                "covered_seconds": integration.covered_seconds,
                "day_seconds": day_seconds,
                "coverage_ratio": float(round(Decimal(integration.covered_seconds) / Decimal(day_seconds), 4)),
                "gap_count": integration.gap_count,
                "largest_gap_seconds": integration.largest_gap_seconds,
                "max_sample_gap_seconds": gap,
                "session_boundaries": integration.session_boundaries,
                "clock_anomalies": integration.clock_anomalies,
                "first_sample_at": integration.first_sample_at.isoformat() if integration.first_sample_at else None,
                "last_sample_at": integration.last_sample_at.isoformat() if integration.last_sample_at else None,
                "day_in_progress": in_progress,
                **extra,
            }
            record_production_fact(
                session,
                asset_id=mapping.asset_id,
                provider_mapping_id=mapping.id,
                # No `sync_run_id`: nothing was synchronised. This is a local
                # derivation over rows the listener already persisted, and
                # attaching it to a provider sync run would make the health
                # surface report a provider that was never contacted.
                sync_run_id=None,
                source_fact_key=f"huawei-scada:{metric_kind}:{day.isoformat()}",
                metric_kind=metric_kind,
                period_start=period_start,
                period_end=period_end,
                granularity="day",
                # Six decimals matches `production_facts.value`; rounding here
                # rather than at the column keeps the stored number identical
                # to the one this method computed.
                value=integration.energy_kwh.quantize(Decimal("0.000001")),
                unit="kWh",
                # Always partial: an integrated estimate is not a metered
                # total, however dense the sampling.
                quality="partial",
                completeness=completeness,
                metadata=metadata,
            )
            written += 1
        return written

    def _completeness(
        self,
        integration: IntegrationResult,
        *,
        period_start: datetime,
        period_end: datetime,
        day_seconds: int,
        gap: int,
    ) -> str:
        """`complete` only when the day is covered edge to edge.

        Coverage alone is not enough: a day sampled densely from 09:00 to
        17:00 has high coverage of the hours it saw and knows nothing about
        the ones it did not. The edges have to be within one sampling gap of
        local midnight for the total to describe the whole day.
        """
        if integration.first_sample_at is None or integration.last_sample_at is None:
            return "partial"
        starts_early = (as_utc(integration.first_sample_at) - as_utc(period_start)).total_seconds() <= gap
        ends_late = (as_utc(period_end) - as_utc(integration.last_sample_at)).total_seconds() <= gap
        covered_enough = integration.covered_seconds >= day_seconds - gap
        if starts_early and ends_late and covered_enough and not integration.clock_anomalies:
            return "complete"
        return "partial"
