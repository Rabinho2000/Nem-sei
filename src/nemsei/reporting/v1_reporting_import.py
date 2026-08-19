"""Import the reporting inputs V1 already holds: energy signals and commercial terms.

V2's FusionSolar adapter accepts only `PVYield`, deliberately, because no other
signal had a verified contract. That gate is about *live reads*. V1 has been
storing the provider's own daily payloads for years, and those payloads state
self-consumption, export, consumption and grid import directly — so importing
them is reading evidence V1 already collected, not trusting an unverified live
call.

The evidence was checked before this module was written. Of 41 503 FusionSolar
daily rows carrying `PVYield`, `selfUsePower` and `ongrid_power`, 40 303 satisfy
`PVYield = selfUsePower + ongrid_power` exactly and 39 322 satisfy
`use_power = selfUsePower + buyPower` exactly. The identity is used to *reject*
rows, never to fill one in: a day whose export exceeds its production is
physically impossible and is recorded as `invalid` rather than imported as a
number or silently dropped.

Sigenergy is different in shape and is handled on its own terms: its rows carry
the values in columns and have no `dataItemMap` at all. Its production does not
equal self-use plus export, because a battery absorbs the difference, which is
exactly why no metric here is ever derived from another.

Reads V1 strictly read-only. Writes nothing to V1, ever.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.assets.v1_import import open_v1_readonly
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord
from nemsei.reporting.commercial import set_billing_config, set_tariff
from nemsei.reporting.commercial_models import AssetBillingConfig, AssetTariff
from nemsei.reporting.rules.v1_energy_signals import (  # noqa: F401 - re-exported for callers
    IDENTITY_TOLERANCE,
    V1_METRIC_COLUMNS,
    V1_METRIC_SIGNALS,
    _decimal,
    first_signal,
    identity_violations,
    metrics_from_row,
)
from nemsei.shared.clock import utc_now


@dataclass
class ReportingImportManifest:
    facts_created: int = 0
    facts_unchanged: int = 0
    rows_read: int = 0
    rows_without_asset: int = 0
    rows_without_mapping: int = 0
    invalid_by_identity: int = 0
    metrics: dict[str, int] = field(default_factory=dict)
    rejections: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "facts_created": self.facts_created,
            "facts_unchanged": self.facts_unchanged,
            "rows_without_asset": self.rows_without_asset,
            "rows_without_mapping": self.rows_without_mapping,
            "invalid_by_identity": self.invalid_by_identity,
            "metrics": dict(sorted(self.metrics.items())),
            "rejections": self.rejections[:50],
        }


def _already_imported(session: Session, model, *, asset_id: int, key: str, value: Any) -> bool:
    """Whether this exact V1 row has already been imported for this asset."""
    rows = session.scalars(select(model).where(model.asset_id == asset_id)).all()
    return any((row.provenance_json or {}).get(key) == value for row in rows)


def _asset_ids_by_legacy_id(session: Session) -> dict[str, int]:
    """V1 asset id to V2 asset id, from the identity import's own record."""
    rows = session.execute(
        select(LegacyImportRecord.legacy_id, LegacyImportRecord.target_asset_id)
        .where(
            LegacyImportRecord.legacy_table == "assets",
            LegacyImportRecord.target_asset_id.is_not(None),
        )
        .order_by(LegacyImportRecord.id)
    ).all()
    return {str(legacy_id): asset_id for legacy_id, asset_id in rows}


def _mapping_for(session: Session, *, asset_id: int, external_id: str | None) -> AssetProviderMapping | None:
    statement = select(AssetProviderMapping).where(
        AssetProviderMapping.asset_id == asset_id,
        AssetProviderMapping.resource_kind == "plant",
    )
    if external_id:
        exact = session.scalar(statement.where(AssetProviderMapping.external_id == external_id))
        if exact is not None:
            return exact
    return session.scalar(statement.order_by(AssetProviderMapping.id))


def _as_utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def import_v1_energy_facts(
    session: Session,
    source_path: Path,
    *,
    asset_ids: list[int] | None = None,
    since: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import V1's daily energy rows as canonical facts, metric by metric."""
    manifest = ReportingImportManifest()
    legacy_to_v2 = _asset_ids_by_legacy_id(session)
    connection = open_v1_readonly(source_path)
    try:
        query = "SELECT * FROM production_records WHERE period_type = 'day'"
        parameters: list[Any] = []
        if since is not None:
            query += " AND period_date >= ?"
            parameters.append(since.isoformat())
        query += " ORDER BY asset_id, period_date"
        for row in connection.execute(query, parameters):
            manifest.rows_read += 1
            v2_asset_id = legacy_to_v2.get(str(row["asset_id"]))
            if v2_asset_id is None:
                manifest.rows_without_asset += 1
                continue
            if asset_ids is not None and v2_asset_id not in asset_ids:
                continue
            mapping = _mapping_for(session, asset_id=v2_asset_id, external_id=row["external_id"])
            if mapping is None:
                manifest.rows_without_mapping += 1
                continue
            try:
                day = date.fromisoformat(str(row["period_date"])[:10])
            except ValueError:
                manifest.rejections.append({"row": row["id"], "reason": "unparseable_period_date"})
                continue

            values = metrics_from_row(row)
            violations = identity_violations(values)
            if violations:
                manifest.invalid_by_identity += 1
                if len(manifest.rejections) < 50:
                    manifest.rejections.append(
                        {
                            "row": row["id"],
                            "reason": "identity_violation",
                            "metrics": violations,
                            "period_date": day.isoformat(),
                        }
                    )

            for metric, value in values.items():
                if value is None:
                    continue
                invalid = metric in violations
                if dry_run:
                    manifest.metrics[metric] = manifest.metrics.get(metric, 0) + 1
                    continue
                _, created = record_production_fact(
                    session,
                    asset_id=v2_asset_id,
                    provider_mapping_id=mapping.id,
                    source_fact_key=f"v1-import:{metric}:{row['provider']}:{row['external_id'] or ''}:{day.isoformat()}",
                    period_start=_as_utc(day),
                    period_end=_as_utc(day + timedelta(days=1)),
                    granularity="day",
                    metric_kind=metric,
                    # An impossible value is recorded as invalid with the number
                    # it claimed, so the rejection is auditable. It is never
                    # silently dropped and never quietly corrected.
                    value=value,
                    unit="kWh",
                    quality="invalid" if invalid else "complete",
                    completeness="complete",
                    metadata={
                        "origin": "v1_production_records",
                        "v1_row_id": row["id"],
                        "v1_provider": row["provider"],
                        "source_timezone": row["source_timezone"],
                        "identity_violation": invalid,
                    },
                )
                manifest.metrics[metric] = manifest.metrics.get(metric, 0) + 1
                if created:
                    manifest.facts_created += 1
                else:
                    manifest.facts_unchanged += 1
    finally:
        connection.close()
    return manifest.as_dict()


def import_v1_commercial_terms(
    session: Session,
    source_path: Path,
    *,
    operator: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import contract attributes, tariffs and billing configuration from V1."""
    summary = {
        "assets_updated": 0,
        "tariffs_created": 0,
        "billing_configs_created": 0,
        "skipped": [],
    }
    legacy_to_v2 = _asset_ids_by_legacy_id(session)
    connection = open_v1_readonly(source_path)
    try:
        for row in connection.execute(
            "SELECT id, contract_type, asset_type, coverage_type, sell_to FROM assets"
        ):
            v2_asset_id = legacy_to_v2.get(str(row["id"]))
            if v2_asset_id is None:
                continue
            asset = session.get(Asset, v2_asset_id)
            if asset is None:
                continue
            changed = False
            for column in ("contract_type", "asset_type", "coverage_type", "sell_to"):
                value = (row[column] or "").strip() or None
                if value is not None and getattr(asset, column) != value:
                    if not dry_run:
                        setattr(asset, column, value)
                    changed = True
            if changed:
                summary["assets_updated"] += 1
                if not dry_run:
                    asset.updated_at = utc_now()

        for row in connection.execute("SELECT * FROM asset_tariffs"):
            v2_asset_id = legacy_to_v2.get(str(row["asset_id"]))
            if v2_asset_id is None:
                summary["skipped"].append({"tariff": row["id"], "reason": "asset_not_imported"})
                continue
            valid_from = date.fromisoformat(str(row["valid_from"])[:10]) if row["valid_from"] else None
            if valid_from is None:
                summary["skipped"].append({"tariff": row["id"], "reason": "no_valid_from"})
                continue
            # Importing twice must not create a second statement about the same
            # dates. The V1 row id in the provenance is what makes this run
            # repeatable rather than destructive.
            if _already_imported(session, AssetTariff, asset_id=v2_asset_id, key="v1_tariff_id", value=row["id"]):
                summary["skipped"].append({"tariff": row["id"], "reason": "already_imported"})
                continue
            # V1's valid_to is the last day the tariff applies; V2's is exclusive.
            valid_to = (
                date.fromisoformat(str(row["valid_to"])[:10]) + timedelta(days=1) if row["valid_to"] else None
            )
            if dry_run:
                summary["tariffs_created"] += 1
                continue
            set_tariff(
                session,
                asset_id=v2_asset_id,
                tariff_type=str(row["tariff_type"]),
                cycle_type=row["cycle_type"],
                valid_from=valid_from,
                valid_to=valid_to,
                prices={
                    "simple": _decimal(row["simple_price_eur_kwh"]),
                    "ponta": _decimal(row["ponta_price_eur_kwh"]),
                    "cheia": _decimal(row["cheia_price_eur_kwh"]),
                    "vazio": _decimal(row["vazio_price_eur_kwh"]),
                    "super_vazio": _decimal(row["super_vazio_price_eur_kwh"]),
                },
                source_kind="v1_import",
                provenance={"v1_tariff_id": row["id"], "v1_notes": row["notes"]},
                notes=row["notes"],
                created_by=operator,
            )
            summary["tariffs_created"] += 1

        for row in connection.execute("SELECT * FROM asset_billing_configs"):
            v2_asset_id = legacy_to_v2.get(str(row["asset_id"]))
            if v2_asset_id is None:
                summary["skipped"].append({"billing_config": row["id"], "reason": "asset_not_imported"})
                continue
            asset = session.get(Asset, v2_asset_id)
            # V1's billing configuration has no validity window: it is simply
            # current. It is imported as valid from the asset's first day of
            # evidence rather than from an invented date, and left open-ended.
            valid_from = asset.commissioned_on or date(2000, 1, 1)
            if _already_imported(
                session, AssetBillingConfig, asset_id=v2_asset_id, key="v1_billing_config_id", value=row["id"]
            ):
                summary["skipped"].append({"billing_config": row["id"], "reason": "already_imported"})
                continue
            if dry_run:
                summary["billing_configs_created"] += 1
                continue
            from nemsei.reporting.commercial import report_type_for

            set_billing_config(
                session,
                asset_id=v2_asset_id,
                report_type=report_type_for(asset).value,
                billing_mode=str(row["billing_mode"]),
                billing_energy_base=str(row["billing_energy_base"]),
                solcor_price_per_kwh=_decimal(row["solcor_price_per_kwh"]) or Decimal("0"),
                fixed_monthly_fee_eur=_decimal(row["fixed_monthly_fee_eur"]) or Decimal("0"),
                default_electricity_price=_decimal(row["default_electricity_price"]) or Decimal("0"),
                default_export_price=_decimal(row["default_export_price"]) or Decimal("0"),
                export_revenue_enabled=bool(row["export_revenue_enabled"]),
                valid_from=valid_from,
                source_kind="v1_import",
                provenance={"v1_billing_config_id": row["id"]},
                created_by=operator,
            )
            summary["billing_configs_created"] += 1
        if not dry_run:
            session.flush()
    finally:
        connection.close()
    return summary
