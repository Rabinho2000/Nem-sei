"""Resolve what a customer is charged, for a given asset on a given date.

The ported rules in `rules/` already know how to *apply* a tariff and a billing
arrangement. This module is where those inputs come from: persisted rows with
temporal validity, rather than arguments a caller happened to pass in. That is
the difference between a report that is reproducible and one that depends on
whoever generated it.

Nothing here invents a value. An asset with no billing configuration resolves to
`None`, and the assembler reports the fields as absent rather than defaulting to
a zero price — a zero price silently turns into "the customer saved nothing".
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.contracts.models import AssetServiceContract
from nemsei.reporting.commercial_models import AssetBillingConfig, AssetTariff, TariffPeriodRule
from nemsei.reporting.models import FinancialModel
from nemsei.reporting.rules.billing import detect_report_type_value
from nemsei.reporting.rules.types import BillingConfig, BillingEnergyBase, BillingMode, ReportType
from nemsei.shared.clock import utc_now


def _covering(column_from, column_to, moment: date):
    """A half-open validity window: `[valid_from, valid_to)`, open-ended if null."""
    return (column_from <= moment) & or_(column_to.is_(None), column_to > moment)


def resolve_billing_config(session: Session, *, asset_id: int, on: date) -> AssetBillingConfig | None:
    """The billing arrangement in force on a date, or None if none is recorded."""
    return session.scalar(
        select(AssetBillingConfig).where(
            AssetBillingConfig.asset_id == asset_id,
            _covering(AssetBillingConfig.valid_from, AssetBillingConfig.valid_to, on),
        )
    )


def resolve_billing_config_for_all(
    session: Session, *, on: date, report_type: str | None = None
) -> dict[int, AssetBillingConfig]:
    """`resolve_billing_config`, every asset at once, for a fleet page that
    needs to know which installations currently bill as ESCO (or any other
    `report_type`) without one query per asset. `report_type=None` returns
    every asset's config, whatever arrangement it is under.
    """
    statement = select(AssetBillingConfig).where(
        _covering(AssetBillingConfig.valid_from, AssetBillingConfig.valid_to, on)
    )
    if report_type is not None:
        statement = statement.where(AssetBillingConfig.report_type == report_type)
    return {config.asset_id: config for config in session.scalars(statement)}


def resolve_tariff(session: Session, *, asset_id: int, on: date) -> AssetTariff | None:
    """The tariff in force on a date, or None if none is recorded.

    At most one row can match: a database exclusion constraint refuses two
    tariffs covering the same day for one asset.
    """
    return session.scalar(
        select(AssetTariff).where(
            AssetTariff.asset_id == asset_id,
            _covering(AssetTariff.valid_from, AssetTariff.valid_to, on),
        )
    )


def confirmed_financial_model(session: Session, *, asset_id: int) -> FinancialModel | None:
    """The confirmed model an asset's expected production comes from, if any.

    The same query was written three times before this -- `reporting/
    service.py` and `web/commercial_routes.py` both look it up to know which
    model a new confirmation supersedes, and `reporting/datasets.py` looks it
    up to read expected values from. One version, reused by all four,
    instead of a fifth copy the day something else needs it.

    `status == "confirmed"` is unique in practice (confirming a new model
    supersedes the old one), but not enforced by a constraint, so the
    highest `version` wins rather than an arbitrary row if it ever is not.
    """
    return session.scalar(
        select(FinancialModel)
        .where(FinancialModel.asset_id == asset_id, FinancialModel.status == "confirmed")
        .order_by(FinancialModel.version.desc())
    )


def report_type_for(asset: Asset) -> ReportType:
    """EPC or ESCO, from the contract attributes rather than from a default."""
    return detect_report_type_value(
        {
            "contract_type": asset.contract_type,
            "asset_type": asset.asset_type,
            "coverage_type": asset.coverage_type,
            "sell_to": asset.sell_to,
            "project_name": asset.canonical_name,
        }
    )


def report_type_is_resolved(asset: Asset) -> bool:
    """Whether the type was actually decided, or merely fell back to EPC.

    `detect_report_type_value` answers EPC both when it reads "EPC" and when it
    reads nothing at all. A report must be able to tell those apart, because one
    is a fact about the contract and the other is a guess.
    """
    import re

    for value in (asset.contract_type, asset.asset_type, asset.coverage_type, asset.sell_to, asset.canonical_name):
        text = (value or "").strip().casefold()
        if re.search(r"(^|\W)(esco|epc)($|\W)", text):
            return True
    return False


def billing_config_from(config: AssetBillingConfig) -> BillingConfig:
    """Bridge a persisted row into the dataclass the ported rules consume."""
    return BillingConfig(
        report_type=ReportType(config.report_type),
        billing_mode=BillingMode(config.billing_mode),
        billing_energy_base=BillingEnergyBase(config.billing_energy_base),
        solcor_price_per_kwh=config.solcor_price_per_kwh,
        fixed_monthly_fee_eur=config.fixed_monthly_fee_eur,
        electricity_price_eur_kwh=config.default_electricity_price,
        export_price_eur_kwh=config.default_export_price,
        export_revenue_enabled=config.export_revenue_enabled,
    )


def tariff_price_summary(tariff: AssetTariff) -> dict[str, Any]:
    """The tariff's prices by period name, keeping an unpriced period absent."""
    prices = {
        "simple": tariff.simple_price_eur_kwh,
        "ponta": tariff.ponta_price_eur_kwh,
        "cheia": tariff.cheia_price_eur_kwh,
        "vazio": tariff.vazio_price_eur_kwh,
        "super_vazio": tariff.super_vazio_price_eur_kwh,
    }
    return {name: value for name, value in prices.items() if value is not None}


def representative_price(tariff: AssetTariff) -> Decimal | None:
    """The single price a summary line shows for a tariff.

    A simple tariff has one price and that is the answer. A cycle tariff does
    not have "a" price, and picking one would misstate the customer's cost, so
    this returns None and the caller reports the breakdown instead.
    """
    if tariff.tariff_type == "simple":
        return tariff.simple_price_eur_kwh
    return None


def close_open_row(rows, *, on: date) -> None:
    """End an open validity window the day a new one starts.

    A row that already starts on or after that day cannot be shortened to make
    room: closing it would give it a zero-length or negative window, which the
    database refuses anyway. That is a genuine conflict — two statements about
    the same dates — and it is raised rather than resolved by guesswork, because
    guessing which one is right is guessing what a customer is charged.
    """
    for row in rows:
        if row.valid_from >= on:
            raise ValueError(
                f"A row already covers {row.valid_from.isoformat()} onwards; "
                "supersede or delete it explicitly rather than overlapping it."
            )
        if row.valid_to is None or row.valid_to > on:
            row.valid_to = on
            row.updated_at = utc_now()


def set_billing_config(
    session: Session,
    *,
    asset_id: int,
    report_type: str,
    valid_from: date,
    created_by: str,
    billing_mode: str = "energy",
    billing_energy_base: str = "self_consumption",
    solcor_price_per_kwh: Decimal = Decimal("0"),
    fixed_monthly_fee_eur: Decimal = Decimal("0"),
    default_electricity_price: Decimal = Decimal("0"),
    default_export_price: Decimal = Decimal("0"),
    export_revenue_enabled: bool = True,
    source_kind: str = "operator",
    provenance: dict[str, Any] | None = None,
    contract_id: int | None = None,
) -> AssetBillingConfig:
    """Record a billing arrangement, ending whichever one it replaces.

    `contract_id`, when given, records which `AssetServiceContract` this
    arrangement prices -- an ESCO contract's 2031 renewal at a new tariff is a
    new `AssetBillingConfig` row pointing at the new contract row, not an edit
    of the old one, matching how contracts themselves are never edited in
    place. It is provenance, not a lookup key: resolution still happens by
    `(asset_id, on)` in `resolve_billing_config`, unchanged.
    """
    actor = created_by.strip()
    if not actor:
        raise ValueError("A billing configuration must record who set it.")
    if session.get(Asset, asset_id) is None:
        raise ValueError("Unknown asset.")
    if contract_id is not None and session.get(AssetServiceContract, contract_id) is None:
        raise ValueError("Unknown contract.")
    existing = session.scalars(
        select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id)
    ).all()
    close_open_row(existing, on=valid_from)
    session.flush()

    now = utc_now()
    config = AssetBillingConfig(
        asset_id=asset_id,
        contract_id=contract_id,
        report_type=report_type,
        billing_mode=billing_mode,
        billing_energy_base=billing_energy_base,
        solcor_price_per_kwh=solcor_price_per_kwh,
        fixed_monthly_fee_eur=fixed_monthly_fee_eur,
        default_electricity_price=default_electricity_price,
        default_export_price=default_export_price,
        export_revenue_enabled=export_revenue_enabled,
        valid_from=valid_from,
        valid_to=None,
        source_kind=source_kind,
        provenance_json=provenance or {},
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(config)
    session.flush()
    return config


def set_tariff(
    session: Session,
    *,
    asset_id: int,
    tariff_type: str,
    valid_from: date,
    created_by: str,
    valid_to: date | None = None,
    cycle_type: str | None = None,
    prices: dict[str, Decimal | None] | None = None,
    source_kind: str = "operator",
    source_financial_model_id: int | None = None,
    source_file_id: int | None = None,
    provenance: dict[str, Any] | None = None,
    notes: str | None = None,
    period_rules: list[dict[str, Any]] | None = None,
) -> AssetTariff:
    """Record a tariff, ending whichever one it replaces."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("A tariff must record who set it.")
    if session.get(Asset, asset_id) is None:
        raise ValueError("Unknown asset.")
    prices = prices or {}
    existing = session.scalars(select(AssetTariff).where(AssetTariff.asset_id == asset_id)).all()
    close_open_row(existing, on=valid_from)
    session.flush()

    now = utc_now()
    tariff = AssetTariff(
        asset_id=asset_id,
        tariff_type=tariff_type,
        cycle_type=cycle_type,
        simple_price_eur_kwh=prices.get("simple"),
        ponta_price_eur_kwh=prices.get("ponta"),
        cheia_price_eur_kwh=prices.get("cheia"),
        vazio_price_eur_kwh=prices.get("vazio"),
        super_vazio_price_eur_kwh=prices.get("super_vazio"),
        valid_from=valid_from,
        valid_to=valid_to,
        source_kind=source_kind,
        source_financial_model_id=source_financial_model_id,
        source_file_id=source_file_id,
        provenance_json=provenance or {},
        notes=notes,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(tariff)
    session.flush()
    for rule in period_rules or []:
        session.add(
            TariffPeriodRule(
                tariff_id=tariff.id,
                weekday_type=rule["weekday_type"],
                start_time=rule["start_time"],
                end_time=rule["end_time"],
                period_name=rule["period_name"],
            )
        )
    session.flush()
    return tariff
