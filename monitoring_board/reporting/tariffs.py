from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Iterable

from monitoring_board.reporting.models import (
    HourlyEnergyRecord,
    TariffConfig,
    TariffPeriodBreakdown,
    TariffPeriodRule,
    TariffType,
    TariffValuationResult,
)


PERIOD_SIMPLE = "simple"
PERIOD_PONTA = "ponta"
PERIOD_CHEIA = "cheia"
PERIOD_VAZIO = "vazio"
PERIOD_SUPER_VAZIO = "super_vazio"
CANONICAL_PERIODS = (PERIOD_SIMPLE, PERIOD_PONTA, PERIOD_CHEIA, PERIOD_VAZIO, PERIOD_SUPER_VAZIO)
MULTI_PERIODS = (PERIOD_PONTA, PERIOD_CHEIA, PERIOD_VAZIO, PERIOD_SUPER_VAZIO)
DAY_TYPES = {"all", "weekday", "weekend"}
ZERO = Decimal("0")
HUNDRED = Decimal("100")


REQUIRED_PERIODS = {
    TariffType.SIMPLE: (PERIOD_SIMPLE,),
    TariffType.BI_HOURLY: (PERIOD_CHEIA, PERIOD_VAZIO),
    TariffType.TRI_HOURLY: (PERIOD_PONTA, PERIOD_CHEIA, PERIOD_VAZIO),
    TariffType.TETRA_HOURLY: (PERIOD_PONTA, PERIOD_CHEIA, PERIOD_VAZIO, PERIOD_SUPER_VAZIO),
}


class TariffValidationError(ValueError):
    pass


def decimal_from_tariff_value(value: Any, *, field_name: str = "price", required: bool = False) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise TariffValidationError(f"Preco obrigatorio em {field_name}.")
        return None
    try:
        parsed = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise TariffValidationError(f"Preco invalido em {field_name}.") from exc
    if not parsed.is_finite() or parsed < ZERO:
        raise TariffValidationError(f"Preco invalido em {field_name}.")
    return parsed


def parse_tariff_type(value: Any) -> TariffType:
    try:
        return TariffType(str(value or "").strip())
    except ValueError as exc:
        raise TariffValidationError("Tipo tarifario invalido.") from exc


def parse_hhmm(value: Any) -> time:
    try:
        return datetime.strptime(str(value or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise TariffValidationError("Hora tarifaria invalida.") from exc


def parse_date_optional(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise TariffValidationError("Data de validade invalida.") from exc


def time_in_rule(sample_time: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= sample_time < end
    return sample_time >= start or sample_time < end


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _rule_ranges(rule: TariffPeriodRule) -> list[tuple[int, int]]:
    start = _minutes(rule.start_time)
    end = _minutes(rule.end_time)
    if start == end:
        return [(0, 24 * 60)]
    if start < end:
        return [(start, end)]
    return [(start, 24 * 60), (0, end)]


def _rules_overlap(left: TariffPeriodRule, right: TariffPeriodRule) -> bool:
    for left_start, left_end in _rule_ranges(left):
        for right_start, right_end in _rule_ranges(right):
            if left_start < right_end and right_start < left_end:
                return True
    return False


def validate_rules(rules: Iterable[TariffPeriodRule], tariff_type: TariffType) -> tuple[str, ...]:
    warnings: list[str] = []
    normalized = tuple(rules)
    valid_periods = set(REQUIRED_PERIODS[tariff_type])
    if tariff_type == TariffType.BI_HOURLY:
        valid_periods = {PERIOD_CHEIA, PERIOD_VAZIO}
    for rule in normalized:
        if rule.weekday_type not in DAY_TYPES:
            raise TariffValidationError("Tipo de dia tarifario invalido.")
        if rule.period_name not in valid_periods:
            raise TariffValidationError("Periodo tarifario desconhecido.")
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            related_day = left.weekday_type == right.weekday_type
            if related_day and _rules_overlap(left, right):
                raise TariffValidationError("Regras tarifarias sobrepostas.")
    if normalized and not _rules_cover_full_day(normalized):
        warnings.append("incomplete_tariff_coverage")
    return tuple(warnings)


def _rules_cover_full_day(rules: Iterable[TariffPeriodRule]) -> bool:
    by_type: dict[str, set[int]] = {"weekday": set(), "weekend": set()}
    for rule in rules:
        targets = ("weekday", "weekend") if rule.weekday_type == "all" else (rule.weekday_type,)
        for start, end in _rule_ranges(rule):
            for target in targets:
                by_type[target].update(range(start, end))
    return all(len(minutes) == 24 * 60 for minutes in by_type.values())


def validate_tariff_config(config: TariffConfig) -> tuple[str, ...]:
    if config.asset_id is None:
        raise TariffValidationError("Tarifa sem instalacao.")
    if config.valid_from and config.valid_to and config.valid_to < config.valid_from:
        raise TariffValidationError("Validade tarifaria invalida.")
    prices = config.prices or {}
    required = REQUIRED_PERIODS[config.tariff_type]
    for period in required:
        if prices.get(period) is None:
            raise TariffValidationError(f"Preco obrigatorio em {period}.")
    incompatible = {
        TariffType.SIMPLE: set(MULTI_PERIODS),
        TariffType.BI_HOURLY: {PERIOD_SIMPLE, PERIOD_PONTA, PERIOD_SUPER_VAZIO},
        TariffType.TRI_HOURLY: {PERIOD_SIMPLE, PERIOD_SUPER_VAZIO},
        TariffType.TETRA_HOURLY: {PERIOD_SIMPLE},
    }[config.tariff_type]
    if any(prices.get(period) is not None for period in incompatible):
        raise TariffValidationError("Precos incompativeis com o tipo de tarifa.")
    if config.tariff_type != TariffType.SIMPLE and not config.rules:
        return ("missing_tariff_rules",)
    return validate_rules(config.rules, config.tariff_type)


def classify_tariff_period(moment: datetime, rules: Iterable[TariffPeriodRule | dict[str, Any]]) -> str | None:
    normalized = tuple(_coerce_rule(rule) for rule in rules)
    weekday_type = "weekend" if moment.weekday() >= 5 else "weekday"
    candidates = [
        rule
        for rule in normalized
        if rule.weekday_type in {"all", weekday_type} and time_in_rule(moment.time(), rule.start_time, rule.end_time)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda rule: 1 if rule.weekday_type == weekday_type else 0, reverse=True)
    return candidates[0].period_name


def _coerce_rule(row: TariffPeriodRule | dict[str, Any]) -> TariffPeriodRule:
    if isinstance(row, TariffPeriodRule):
        return row
    return TariffPeriodRule(
        weekday_type=str(row["weekday_type"] or "all"),
        start_time=parse_hhmm(row["start_time"]),
        end_time=parse_hhmm(row["end_time"]),
        period_name=str(row["period_name"]),
    )


def infer_hourly_self_use(record: HourlyEnergyRecord) -> tuple[Decimal | None, str | None]:
    if record.self_use_kwh is not None:
        return max(record.self_use_kwh, ZERO), None
    if record.production_kwh is not None and record.export_kwh is not None:
        return max(record.production_kwh - record.export_kwh, ZERO), "inferred_hourly_self_use"
    return None, "missing_hourly_self_use"


def _expected_slot_count(records: list[HourlyEnergyRecord], start: date | None = None, end: date | None = None) -> int:
    if not records:
        return 0
    durations = [
        int((record.period_end - record.period_start).total_seconds())
        for record in records
        if record.period_end > record.period_start
    ]
    if not durations:
        return len(records)
    seconds = Counter(durations).most_common(1)[0][0]
    if not start or not end:
        first = min(record.period_start for record in records)
        last = max(record.period_end for record in records)
    else:
        first = datetime.combine(start, time.min)
        last = datetime.combine(end, time.max)
    total_seconds = max((last - first).total_seconds(), 0)
    return max(int(round(total_seconds / seconds)), len(records))


def value_tariff_energy(
    config: TariffConfig | None,
    *,
    hourly_records: Iterable[HourlyEnergyRecord] = (),
    aggregate_self_use_kwh: Decimal | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    source: str | None = None,
) -> TariffValuationResult:
    if config is None:
        return _empty_result(None, source or "missing_tariff", ("missing_tariff",))
    warnings = list(validate_tariff_config(config))
    prices = config.prices or {}
    if config.tariff_type == TariffType.SIMPLE:
        energy = aggregate_self_use_kwh or ZERO
        price = prices.get(PERIOD_SIMPLE)
        value = (energy * price) if price is not None else None
        return TariffValuationResult(
            tariff_type=config.tariff_type,
            total_energy_kwh=energy,
            breakdown=(TariffPeriodBreakdown(PERIOD_SIMPLE, energy, ZERO, price, value or ZERO),),
            total_value_eur=value,
            hours_classified=0,
            hours_unclassified=0,
            expected_slots=0,
            slots_with_data=0,
            coverage_pct=HUNDRED,
            source=source or config.source,
            warnings=tuple(sorted(set(warnings))),
        )

    records = list(hourly_records)
    if not records:
        warnings.append("missing_hourly_self_use")
    energy_by_period = {period: ZERO for period in CANONICAL_PERIODS}
    production_by_period = {period: ZERO for period in CANONICAL_PERIODS}
    classified = 0
    unclassified = 0
    missing_self_use = False
    for record in records:
        period = classify_tariff_period(record.period_start, config.rules)
        if record.production_kwh is not None and period:
            production_by_period[period] += record.production_kwh
        if not period:
            unclassified += 1
            warnings.append("unclassified_hourly_energy")
            continue
        self_use, warning = infer_hourly_self_use(record)
        if warning:
            warnings.append(warning)
        if self_use is None:
            missing_self_use = True
            continue
        energy_by_period[period] += self_use
        classified += 1

    breakdown: list[TariffPeriodBreakdown] = []
    total = ZERO
    missing_price = False
    for period in CANONICAL_PERIODS:
        price = prices.get(period)
        value = (energy_by_period[period] * price) if price is not None else ZERO
        if energy_by_period[period] and price is None:
            warnings.append(f"missing_{period}_price")
            missing_price = True
        total += value
        breakdown.append(TariffPeriodBreakdown(period, energy_by_period[period], production_by_period[period], price, value))
    expected = _expected_slot_count(records, period_start, period_end)
    coverage = (Decimal(classified) / Decimal(expected) * HUNDRED) if expected else ZERO
    total_value = None if missing_self_use or missing_price or not records else total
    return TariffValuationResult(
        tariff_type=config.tariff_type,
        total_energy_kwh=sum(energy_by_period.values(), ZERO),
        breakdown=tuple(breakdown),
        total_value_eur=total_value,
        hours_classified=classified,
        hours_unclassified=unclassified,
        expected_slots=expected,
        slots_with_data=len(records),
        coverage_pct=coverage,
        source=source or config.source,
        warnings=tuple(sorted(set(warnings))),
    )


def _empty_result(
    tariff_type: TariffType | None,
    source: str,
    warnings: tuple[str, ...],
) -> TariffValuationResult:
    return TariffValuationResult(
        tariff_type=tariff_type,
        total_energy_kwh=ZERO,
        breakdown=tuple(TariffPeriodBreakdown(period, ZERO, ZERO, None, ZERO) for period in CANONICAL_PERIODS),
        total_value_eur=None,
        hours_classified=0,
        hours_unclassified=0,
        expected_slots=0,
        slots_with_data=0,
        coverage_pct=ZERO,
        source=source,
        warnings=warnings,
    )


def with_billing_fallback(
    result: TariffValuationResult,
    *,
    self_use_kwh: Decimal,
    default_price: Decimal,
) -> TariffValuationResult:
    if result.total_value_eur is not None:
        return result
    value = self_use_kwh * default_price
    warnings = tuple(sorted({*result.warnings, "missing_tariff"}))
    return replace(
        result,
        tariff_type=TariffType.SIMPLE,
        total_energy_kwh=self_use_kwh,
        breakdown=(TariffPeriodBreakdown(PERIOD_SIMPLE, self_use_kwh, ZERO, default_price, value),),
        total_value_eur=value,
        source="billing_default",
        warnings=warnings,
    )


def float_or_none_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and not isfinite(value):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return max(parsed, ZERO) if parsed.is_finite() else None


def result_to_legacy_dict(result: TariffValuationResult) -> dict[str, Any]:
    period_kwh = {breakdown.period_name: float(breakdown.energy_kwh) for breakdown in result.breakdown}
    production_period_kwh = {breakdown.period_name: float(breakdown.production_kwh) for breakdown in result.breakdown}
    return {
        "estimated_value_eur": round(float(result.total_value_eur), 2) if result.total_value_eur is not None else None,
        "period_kwh": {period: production_period_kwh.get(period, 0.0) for period in MULTI_PERIODS},
        "self_use_period_kwh": {period: period_kwh.get(period, 0.0) for period in MULTI_PERIODS},
        "production_period_kwh": {period: production_period_kwh.get(period, 0.0) for period in MULTI_PERIODS},
        "warnings": list(result.warnings),
        "tariff_source": result.source,
        "coverage_pct": float(result.coverage_pct),
        "breakdown": result.breakdown,
    }
