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


class InvoiceStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    REVIEW_REQUIRED = "review_required"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXTRACTION_FAILED = "extraction_failed"
    ARCHIVED = "archived"


class InvoiceExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    REVIEW_REQUIRED = "review_required"
    EXTRACTION_FAILED = "extraction_failed"


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
    billing_energy_base: BillingEnergyBase = BillingEnergyBase.SELF_CONSUMPTION
    solcor_price_per_kwh: Decimal = Decimal("0")
    fixed_monthly_fee_eur: Decimal = Decimal("0")
    electricity_price_eur_kwh: Decimal = Decimal("0")
    export_price_eur_kwh: Decimal = Decimal("0")


@dataclass(frozen=True)
class BillingResult:
    production_kwh: Decimal
    self_use_kwh: Decimal
    export_kwh: Decimal
    consumption_kwh: Decimal
    billable_energy_kwh: Decimal
    grid_import_kwh: Decimal
    exported_energy_kwh: Decimal
    savings_eur: Decimal
    export_revenue_eur: Decimal
    gross_benefit_eur: Decimal
    solcor_payment_eur: Decimal
    net_benefit_eur: Decimal
    autoconsumption_pct: Decimal
    export_pct: Decimal
    self_sufficiency_pct: Decimal
    billing_mode: BillingMode
    billing_energy_base: BillingEnergyBase
    solcor_price_per_kwh: Decimal
    fixed_monthly_fee_eur: Decimal
    months_count: int
    warnings: tuple[str, ...] = ()

    @property
    def total_benefit_eur(self) -> Decimal:
        return self.gross_benefit_eur


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
    data_quality: str | None = None
    source_fields: dict[str, str] | None = None


@dataclass(frozen=True)
class TariffConfig:
    tariff_id: int | None
    asset_id: int | None
    tariff_type: TariffType
    cycle_type: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    prices: dict[str, Decimal] | None = None
    rules: tuple[TariffPeriodRule, ...] = ()
    source: str = "stored_tariff"
    invoice_file_id: int | None = None
    notes: str = ""


@dataclass(frozen=True)
class TariffPeriodBreakdown:
    period_name: str
    energy_kwh: Decimal
    production_kwh: Decimal
    price_eur_kwh: Decimal | None
    value_eur: Decimal


@dataclass(frozen=True)
class TariffValuationResult:
    tariff_type: TariffType | None
    total_energy_kwh: Decimal
    breakdown: tuple[TariffPeriodBreakdown, ...]
    total_value_eur: Decimal | None
    hours_classified: int
    hours_unclassified: int
    expected_slots: int
    slots_with_data: int
    coverage_pct: Decimal
    source: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvoiceCandidate:
    field_name: str
    value: str
    confidence: Decimal
    evidence: str = ""
    source: str = ""


@dataclass(frozen=True)
class InvoiceTariffCandidate:
    tariff_type: TariffType | None
    simple_price_eur_kwh: Decimal | None = None
    ponta_price_eur_kwh: Decimal | None = None
    cheia_price_eur_kwh: Decimal | None = None
    vazio_price_eur_kwh: Decimal | None = None
    super_vazio_price_eur_kwh: Decimal | None = None


@dataclass(frozen=True)
class InvoiceValidationResult:
    valid: bool
    status: InvoiceStatus
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvoiceExtractionResult:
    method: str
    parser_name: str
    parser_version: str
    status: InvoiceExtractionStatus
    candidates: tuple[InvoiceCandidate, ...]
    tariff_candidate: InvoiceTariffCandidate
    confidence: Decimal
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    requires_review: bool = True


@dataclass(frozen=True)
class AvailabilityResult:
    valid_slots: int
    available_slots: int
    unavailable_slots: int
    availability_pct: float | None

