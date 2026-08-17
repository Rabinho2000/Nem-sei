"""Provider-neutral production coverage derived only from canonical facts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.monitoring.models import ProductionFact


@dataclass(frozen=True)
class ProductionDayCoverage:
    source_day: date
    status: str  # complete | partial | missing
    fact_id: int | None


def production_coverage(
    session: Session,
    *,
    provider_mapping_id: int,
    source_timezone: str,
    start_date: date,
    end_date: date,
    metric_kind: str = "production_energy",
) -> list[ProductionDayCoverage]:
    """Classify provider-local days without calling any provider adapter.

    Facts carry their source day/timezone explicitly.  This service refuses to
    infer calendar boundaries from UTC timestamps, preserving provider-neutral
    semantics and keeping an explicit numeric zero complete.
    """
    if end_date < start_date or not source_timezone:
        raise ValueError("Production coverage window and source timezone are required.")
    facts = list(session.scalars(select(ProductionFact).where(
        ProductionFact.provider_mapping_id == provider_mapping_id,
        ProductionFact.metric_kind == metric_kind,
        ProductionFact.granularity == "day",
    ).order_by(ProductionFact.source_fact_key, ProductionFact.source_revision.desc())))
    latest: dict[str, ProductionFact] = {}
    for fact in facts:
        latest.setdefault(fact.source_fact_key, fact)
    by_day: dict[date, list[ProductionFact]] = {}
    for fact in latest.values():
        metadata = fact.metadata_json or {}
        if metadata.get("source_period_timezone") != source_timezone:
            continue
        raw_day = metadata.get("source_period_date")
        if not isinstance(raw_day, str):
            continue
        try:
            source_day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        by_day.setdefault(source_day, []).append(fact)
    result: list[ProductionDayCoverage] = []
    current = start_date
    while current <= end_date:
        candidates = by_day.get(current, [])
        complete = next((fact for fact in candidates if fact.value is not None and fact.quality == "complete" and fact.completeness == "complete"), None)
        if complete is not None:
            result.append(ProductionDayCoverage(current, "complete", complete.id))
        elif candidates:
            result.append(ProductionDayCoverage(current, "partial", candidates[0].id))
        else:
            result.append(ProductionDayCoverage(current, "missing", None))
        current += timedelta(days=1)
    return result
