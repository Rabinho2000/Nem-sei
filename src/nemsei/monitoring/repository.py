"""Persistence-only canonical fact lookups."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.monitoring.models import MonitoringObservation, ProductionFact


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
        """Every source fact of a period, at its newest revision only.

        `production_facts` is append-only: a corrected reading is a new row that
        supersedes the previous one rather than an update. Any caller that totals
        a period must reduce to the current revision first, because summing the
        raw rows adds a value to the value that was meant to replace it.
        """
        statement = (
            select(ProductionFact)
            .where(
                ProductionFact.asset_id == asset_id,
                ProductionFact.metric_kind == metric_kind,
                ProductionFact.period_start >= period_start,
                ProductionFact.period_start < period_end,
            )
            .distinct(ProductionFact.provider_mapping_id, ProductionFact.source_fact_key)
            .order_by(
                ProductionFact.provider_mapping_id,
                ProductionFact.source_fact_key,
                ProductionFact.source_revision.desc(),
            )
        )
        facts = list(self.session.scalars(statement))
        facts.sort(key=lambda fact: (fact.period_start, fact.id))
        return facts
