"""Persistence-only canonical fact lookups."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, and_, func, literal, or_, select
from sqlalchemy.orm import Session

from nemsei.monitoring.models import MonitoringObservation, ProductionFact
from nemsei.sources.models import AssetSourcePolicy


def current_revision_facts(
    *,
    metric_kind: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    asset_ids: list[int] | None = None,
):
    """Every source fact of a period at its newest revision only.

    `production_facts` is append-only: a corrected reading is a new row that
    supersedes the previous one rather than an update, so any caller that
    totals a period must reduce to the current revision first. Summing the raw
    rows adds a value to the value that was meant to replace it -- that defect
    produced 129.28 kWh for a day that made 59.56.

    This is only half the reduction. It leaves one row per *mapping*, and an
    asset read by two mappings still has two rows for the same day; see
    `canonical_facts`.
    """
    conditions = [ProductionFact.metric_kind == metric_kind]
    if period_start is not None:
        conditions.append(ProductionFact.period_start >= period_start)
    if period_end is not None:
        conditions.append(ProductionFact.period_start < period_end)
    if asset_ids is not None:
        conditions.append(ProductionFact.asset_id.in_(asset_ids))
    return (
        select(
            ProductionFact.id.label("fact_id"),
            ProductionFact.asset_id.label("asset_id"),
            ProductionFact.provider_mapping_id.label("provider_mapping_id"),
            ProductionFact.period_start.label("period_start"),
            ProductionFact.period_end.label("period_end"),
            ProductionFact.value.label("value"),
            ProductionFact.metadata_json.label("metadata_json"),
        )
        .where(*conditions)
        .distinct(ProductionFact.provider_mapping_id, ProductionFact.source_fact_key)
        .order_by(
            ProductionFact.provider_mapping_id,
            ProductionFact.source_fact_key,
            ProductionFact.source_revision.desc(),
        )
        .subquery()
    )


def canonical_facts(
    *,
    metric_kind: str = "production_energy",
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    asset_ids: list[int] | None = None,
) -> Select:
    """One fact per asset and period: the source the policy actually selects.

    132 of the 267 assets on this deployment have two active plant mappings,
    and both can hold a reading for the same day. Every reader here used to
    reduce to the newest revision *per mapping* and then add whatever was
    left, so a day covered by a primary and a fallback was counted twice --
    asset 180 on 2026-07-24 holds 59.55 kWh from one mapping and 59.56 from
    another, for a day that made about 59.56.

    The choice is `sources.service.resolve_source_policy`'s, expressed as a
    ranking so it can be made for a whole fleet in one query rather than one
    call per asset per day: the policy's primary wins, then its fallback, then
    -- last -- a mapping with no policy for that day at all.

    That last rank is the deliberately conservative part. Excluding
    unpolicied mappings would be the stricter rule and would silently delete
    history: 125 of these assets have production facts and no production
    policy. Ranking them last means their days still report, while an asset
    that does have a policy never has an unpolicied mapping override it.
    """
    current = current_revision_facts(
        metric_kind=metric_kind, period_start=period_start, period_end=period_end, asset_ids=asset_ids
    )
    # `metadata_json` is a generic JSON column, so the timezone comes out
    # through `as_string()` rather than JSONB's `->>`.
    source_day = func.date(
        func.timezone(
            func.coalesce(current.c.metadata_json["source_timezone"].as_string(), literal("UTC")),
            current.c.period_start,
        )
    )
    policy = AssetSourcePolicy
    # One policy per fact: a mapping can have several policy rows whose
    # validity overlaps, and joining them all would put the same fact in the
    # result more than once -- reintroducing the double count by a new route.
    scored = (
        select(
            current.c.fact_id,
            current.c.asset_id,
            current.c.provider_mapping_id,
            current.c.period_start,
            current.c.period_end,
            current.c.value,
            policy.is_fallback.label("is_fallback"),
            policy.priority.label("priority"),
        )
        .select_from(current)
        .outerjoin(
            policy,
            and_(
                policy.asset_id == current.c.asset_id,
                policy.source_use == "production",
                policy.provider_mapping_id == current.c.provider_mapping_id,
                policy.valid_from <= source_day,
                or_(policy.valid_to.is_(None), policy.valid_to >= source_day),
            ),
        )
        .distinct(current.c.fact_id)
        .order_by(
            current.c.fact_id,
            policy.is_fallback.asc().nullslast(),
            policy.priority.asc().nullslast(),
            policy.id.asc().nullslast(),
        )
        .subquery()
    )
    return (
        select(scored)
        .distinct(scored.c.asset_id, scored.c.period_start, scored.c.period_end)
        .order_by(
            scored.c.asset_id,
            scored.c.period_start,
            scored.c.period_end,
            # A policy at all beats none; primary beats fallback; then the
            # policy's own priority, then the mapping id so the answer is
            # stable rather than whichever row the planner happened to emit.
            scored.c.is_fallback.asc().nullslast(),
            scored.c.priority.asc().nullslast(),
            scored.c.provider_mapping_id.asc(),
        )
    )


class CanonicalFactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_observation(self, *, provider_mapping_id: int, source_key: str) -> MonitoringObservation | None:
        return self.session.scalar(select(MonitoringObservation).where(MonitoringObservation.provider_mapping_id == provider_mapping_id, MonitoringObservation.source_observation_key == source_key).order_by(MonitoringObservation.source_revision.desc()))

    def latest_production_fact(self, *, provider_mapping_id: int, source_key: str) -> ProductionFact | None:
        return self.session.scalar(select(ProductionFact).where(ProductionFact.provider_mapping_id == provider_mapping_id, ProductionFact.source_fact_key == source_key).order_by(ProductionFact.source_revision.desc()))

    def latest_complete_production_fact(self, *, provider_mapping_id: int, source_key: str) -> ProductionFact | None:
        """Latest usable numeric fact, distinct from the latest provider evidence."""
        return self.session.scalar(select(ProductionFact).where(
            ProductionFact.provider_mapping_id == provider_mapping_id,
            ProductionFact.source_fact_key == source_key,
            ProductionFact.value.is_not(None),
            ProductionFact.quality == "complete",
            ProductionFact.completeness == "complete",
        ).order_by(ProductionFact.source_revision.desc()))

    def current_production_facts_for_asset(
        self,
        *,
        asset_id: int,
        period_start: datetime,
        period_end: datetime,
        metric_kind: str = "production_energy",
    ) -> list[ProductionFact]:
        """One fact per period for this asset: newest revision, chosen source.

        Two reductions, and both are needed. `production_facts` is append-only,
        so a corrected reading is a new row superseding the old one and summing
        the raw rows adds a value to the value meant to replace it. And an
        asset can be read by more than one mapping, so after that reduction a
        day covered by a primary and a fallback still has two rows -- adding
        those was the same defect by a second route.

        `canonical_facts` makes the source choice, the same one
        `sources.service.resolve_source_policy` makes, so this reader, the
        fleet totals and the portfolio chart all answer with one number.
        """
        chosen = canonical_facts(
            metric_kind=metric_kind,
            period_start=period_start,
            period_end=period_end,
            asset_ids=[asset_id],
        ).subquery()
        statement = (
            select(ProductionFact)
            .join(chosen, chosen.c.fact_id == ProductionFact.id)
            .order_by(ProductionFact.period_start, ProductionFact.id)
        )
        return list(self.session.scalars(statement))
