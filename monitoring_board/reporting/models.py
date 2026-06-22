from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum


class ReportPeriodType(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


class BillingMode(StrEnum):
    ENERGY = "energy"
    FIXED_MONTHLY_FEE = "fixed_monthly_fee"


class BillingEnergyBase(StrEnum):
    SELF_CONSUMPTION = "self_consumption"
    TOTAL_PRODUCTION = "total_production"


class ReportType(StrEnum):
    ESCO = "esco"
    EPC = "epc"


class TariffType(StrEnum):
    SIMPLE = "simple"
    BI_HOURLY = "bi-hourly"
    TRI_HOURLY = "tri-hourly"
    TETRA_HOURLY = "tetra-hourly"


@dataclass(frozen=True)
class ReportingPeriod:
    period_type: ReportPeriodType
    start: date
    end: date
    label: str
    month_count: int
    included_months: tuple[date, ...]


@dataclass(frozen=True)
class EnergyBreakdown:
    production_kwh: Decimal
    self_use_kwh: Decimal
    export_kwh: Decimal
    consumption_kwh: Decimal


@dataclass(frozen=True)
class BillingConfig:
    report_type: ReportType
    billing_mode: BillingMode = BillingMode.ENERGY
    billing_energy_base: BillingEnergyBase = BillingEnergyBase.TOTAL_PRODUCTION
    solcor_price_per_kwh: Decimal = Decimal("0")
    fixed_monthly_fee_eur: Decimal = Decimal("0")
    electricity_price_eur_kwh: Decimal = Decimal("0")
    export_price_eur_kwh: Decimal = Decimal("0")


@dataclass(frozen=True)
class BillingResult:
    savings_eur: Decimal
    export_revenue_eur: Decimal
    total_benefit_eur: Decimal
    solcor_payment_eur: Decimal
    net_benefit_eur: Decimal
    autoconsumption_pct: Decimal
    export_pct: Decimal
    self_sufficiency_pct: Decimal


@dataclass(frozen=True)
class TariffPeriodRule:
    weekday_type: str
    start_time: time
    end_time: time
    period_name: str


@dataclass(frozen=True)
class HourlyEnergyRecord:
    period_start: datetime
    period_end: datetime
    production_kwh: Decimal | None = None
    self_use_kwh: Decimal | None = None
    export_kwh: Decimal | None = None
    consumption_kwh: Decimal | None = None
    grid_import_kwh: Decimal | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    valid_slots: int
    available_slots: int
    unavailable_slots: int
    availability_pct: float | None

