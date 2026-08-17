"""Provider-neutral production coverage derived only from canonical facts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.monitoring.models import ProductionFact


@dataclass(frozen=True)
class ProductionDayCoverage:
    source_day: date
    latest_evidence_status: str  # complete | partial | missing
    latest_evidence_fact_id: int | None
    effective_complete_fact_id: int | None
    effective_complete_value: Decimal | None

    @property
    def status(self) -> str:
        """Compatibility alias; coverage safety follows latest evidence."""
        return self.latest_evidence_status

    @property
    def fact_id(self) -> int | None:
        return self.latest_evidence_fact_id


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
    effective_complete: dict[str, ProductionFact] = {}
    for fact in facts:
        latest.setdefault(fact.source_fact_key, fact)
        if fact.value is not None and fact.quality == "complete" and fact.completeness == "complete":
            effective_complete.setdefault(fact.source_fact_key, fact)
    by_day: dict[date, list[tuple[ProductionFact, ProductionFact | None]]] = {}
    for source_key, fact in latest.items():
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
        by_day.setdefault(source_day, []).append((fact, effective_complete.get(source_key)))
    result: list[ProductionDayCoverage] = []
    current = start_date
    while current <= end_date:
        candidates = by_day.get(current, [])
        if candidates:
            latest_fact, effective_fact = candidates[0]
            if latest_fact.value is not None and latest_fact.quality == "complete" and latest_fact.completeness == "complete":
                status = "complete"
            elif latest_fact.value is None and latest_fact.quality == "missing":
                status = "missing"
            else:
                status = "partial"
            result.append(ProductionDayCoverage(
                current,
                status,
                latest_fact.id,
                effective_fact.id if effective_fact else None,
                effective_fact.value if effective_fact else None,
            ))
        else:
            result.append(ProductionDayCoverage(current, "missing", None, None, None))
        current += timedelta(days=1)
    return result
