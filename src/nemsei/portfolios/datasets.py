"""Aggregate a portfolio from the per-asset datasets M5 already builds.

This module deliberately computes no metric of its own. For each asset in a
frozen snapshot it builds (or reuses) the individual `ReportingDataset`, then
sums what is summable and counts what is not. If a number here disagrees with
the same asset's own report, that is a bug in this file, not a difference of
method — which is the entire point of aggregating rather than recalculating.

Missing does not become zero, and it does not quietly shrink a total either. A
portfolio whose members are 18 measured and 2 missing reports a total marked
`partial` alongside a coverage of 18/20, because a smaller total that looks
complete is the most expensive kind of wrong number in a portfolio report.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.portfolios.models import (
    PortfolioDataset,
    PortfolioDatasetMember,
    PortfolioSnapshot,
)
from nemsei.reporting.datasets import DATASET_METRICS, build_dataset
from nemsei.reporting.models import ReportingDataset
from nemsei.shared.clock import utc_now


# What a portfolio totals. Production and the four energy signals come straight
# from each asset's dataset rows; expected production comes from its confirmed
# financial model, through the same rows.
SUMMABLE_METRICS = ("production", "expected", *DATASET_METRICS)


@dataclass(frozen=True)
class MetricTotal:
    value: Decimal | None
    state: str
    measured_assets: int
    total_assets: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else format(self.value, "f"),
            "state": self.state,
            "measured_assets": self.measured_assets,
            "total_assets": self.total_assets,
        }


def _row_metrics(dataset: ReportingDataset) -> tuple[dict[str, Decimal | None], dict[str, str]]:
    """One asset's period totals, from its own dataset rows."""
    values: dict[str, Decimal | None] = {}
    states: dict[str, str] = {}
    rows = list(dataset.rows)

    def total(value_attr: str, state_attr: str) -> tuple[Decimal | None, str]:
        present = [
            getattr(row, value_attr)
            for row in rows
            if getattr(row, state_attr) != "missing" and getattr(row, value_attr) is not None
        ]
        if not present:
            return None, "missing"
        state = "measured" if len(present) == len(rows) else "partial"
        if any(getattr(row, state_attr) == "partial" for row in rows):
            state = "partial"
        return sum(present, Decimal("0")), state

    values["production"], states["production"] = total("actual_production_kwh", "actual_state")
    values["expected"], states["expected"] = total("expected_production_kwh", "expected_state")
    for name in DATASET_METRICS:
        values[name], states[name] = total(f"{name}_kwh", f"{name}_state")
    return values, states


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_portfolio_dataset(
    session: Session,
    *,
    snapshot: PortfolioSnapshot,
    built_by: str,
) -> PortfolioDataset:
    """Aggregate one frozen snapshot into the single source for its reports."""
    actor = built_by.strip()
    if not actor:
        raise ValueError("A portfolio dataset must record who built it.")

    asset_ids = list(snapshot.asset_ids_json or [])
    member_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for asset_id in asset_ids:
        asset = session.get(Asset, asset_id)
        if asset is None:  # pragma: no cover - the snapshot's FK prevents this
            warnings.append(f"asset_missing:{asset_id}")
            continue
        # Reuse an identical dataset if one was already built for this asset and
        # period; otherwise build it through the same code path an individual
        # report uses.
        dataset = build_dataset(
            session,
            asset_id=asset_id,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            built_by=actor,
        )
        values, states = _row_metrics(dataset)
        member_rows.append(
            {
                "asset_id": asset_id,
                "reporting_dataset_id": dataset.id,
                "installed_dc_power_kw": asset.installed_dc_power_kw,
                "values": values,
                "states": states,
                "dataset_digest": dataset.input_digest,
            }
        )
        warnings.extend(f"{warning}:asset={asset_id}" for warning in (dataset.warnings_json or []))

    totals: dict[str, Any] = {}
    for metric in SUMMABLE_METRICS:
        present = [row["values"][metric] for row in member_rows if row["values"][metric] is not None]
        measured = len(present)
        if not present:
            state = "missing"
            value = None
        else:
            value = sum(present, Decimal("0"))
            partial = any(row["states"][metric] == "partial" for row in member_rows)
            state = "measured" if measured == len(member_rows) and not partial else "partial"
        totals[metric] = MetricTotal(value, state, measured, len(member_rows)).as_dict()

    # Installed capacity is a property of the estate, not of a period, so an
    # asset with no rated power makes the total partial rather than smaller.
    capacities = [row["installed_dc_power_kw"] for row in member_rows if row["installed_dc_power_kw"] is not None]
    totals["installed_dc_power_kw"] = MetricTotal(
        sum(capacities, Decimal("0")) if capacities else None,
        "measured" if len(capacities) == len(member_rows) and capacities else ("partial" if capacities else "missing"),
        len(capacities),
        len(member_rows),
    ).as_dict()

    # Performance is a ratio of two totals, and it is only stated when both are
    # complete. A ratio of a partial actual to a complete expected understates
    # the portfolio and reads as underperformance.
    performance = None
    production, expected = totals["production"], totals["expected"]
    if (
        production["state"] == "measured"
        and expected["state"] == "measured"
        and expected["value"] is not None
        and Decimal(expected["value"]) > 0
    ):
        performance = float(Decimal(production["value"]) / Decimal(expected["value"]) * 100)
    totals["performance_pct"] = performance

    members_total = len(member_rows)
    complete = sum(
        1 for row in member_rows if row["states"]["production"] == "measured"
    )
    coverage = {
        "assets_in_snapshot": len(asset_ids),
        "assets_with_dataset": members_total,
        "assets_complete": complete,
        "assets_partial": sum(1 for row in member_rows if row["states"]["production"] == "partial"),
        "assets_missing": sum(1 for row in member_rows if row["states"]["production"] == "missing"),
        # The "18/20 complete" the operator actually asks for.
        "label": f"{complete}/{members_total}" if members_total else "0/0",
        "unresolved_members": sum(
            1 for member in (snapshot.members_json or []) if member.get("resolution_state") != "resolved"
        ),
    }

    dataset_row = PortfolioDataset(
        portfolio_id=snapshot.portfolio_id,
        snapshot_id=snapshot.id,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        status="ready",
        input_digest=_digest(
            {
                "snapshot": snapshot.membership_digest,
                "members": sorted(
                    (row["asset_id"], row["dataset_digest"]) for row in member_rows
                ),
            }
        ),
        totals_json=totals,
        coverage_json=coverage,
        warnings_json=sorted(set(warnings)),
        built_at=utc_now(),
        built_by=actor,
    )
    session.add(dataset_row)
    session.flush()
    for row in member_rows:
        session.add(
            PortfolioDatasetMember(
                portfolio_dataset_id=dataset_row.id,
                asset_id=row["asset_id"],
                reporting_dataset_id=row["reporting_dataset_id"],
                metrics_json={
                    name: (None if value is None else format(value, "f"))
                    for name, value in row["values"].items()
                },
                states_json=row["states"],
            )
        )
    session.flush()
    return dataset_row


def assets_needing_attention(session: Session, dataset: PortfolioDataset) -> list[dict[str, Any]]:
    """The installations an operator should look at first.

    Ordered by how badly they are missing, then by how far below expectation
    they are. This is the question the overview answers before any other.
    """
    ranked: list[dict[str, Any]] = []
    for member in dataset.members:
        asset = session.get(Asset, member.asset_id)
        states = member.states_json or {}
        metrics = member.metrics_json or {}
        production = metrics.get("production")
        expected = metrics.get("expected")
        ratio = None
        if production is not None and expected is not None and Decimal(expected) > 0:
            ratio = float(Decimal(production) / Decimal(expected) * 100)
        severity = {"missing": 0, "partial": 1, "measured": 2}.get(states.get("production", "missing"), 0)
        ranked.append(
            {
                "asset_id": member.asset_id,
                "name": asset.canonical_name if asset else None,
                "production_state": states.get("production", "missing"),
                "production_kwh": None if production is None else float(production),
                "expected_kwh": None if expected is None else float(expected),
                "performance_pct": ratio,
                "severity": severity,
            }
        )
    ranked.sort(key=lambda row: (row["severity"], row["performance_pct"] if row["performance_pct"] is not None else 0))
    return ranked


def latest_dataset(session: Session, *, portfolio_id: int, period_start: date) -> PortfolioDataset | None:
    return session.scalar(
        select(PortfolioDataset)
        .where(PortfolioDataset.portfolio_id == portfolio_id, PortfolioDataset.period_start == period_start)
        .order_by(PortfolioDataset.built_at.desc())
    )
