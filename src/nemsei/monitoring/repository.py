"""Persistence-only canonical fact lookups."""
from __future__ import annotations

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
