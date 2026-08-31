"""Reclaiming raw samples, but only once they have already become energy.

A 30-second cadence writes about 2 880 rows per plant per day, which is 1.05
million rows per plant per year. Keeping those forever is a cost with no
reader: once a day has been integrated into a complete `ProductionFact`, the
per-sample detail answers no question the fact does not, and the fact is the
thing reports and customers actually see.

The rule is narrow on purpose. A day's samples are deleted only when **all** of
these hold:

* the day ended longer ago than the configured retention window;
* a current-revision `production_energy` fact exists for that day *and that
  mapping*;
* that fact is `completeness='complete'` -- a day integrated from partial
  coverage may still be improved by a late sample, and deleting its evidence
  would freeze an estimate that was never final.

So this can never delete evidence that has not been used yet, and it can never
delete the only record of a day the rollup could not finish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from nemsei.integrations.huawei_scada.models import HuaweiScadaPowerSample
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import as_utc, utc_now


@dataclass
class RetentionResult:
    days_deleted: int = 0
    samples_deleted: int = 0
    mappings_examined: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


def _settled_days(session: Session, *, provider_mapping_id: int, cutoff: datetime) -> list[ProductionFact]:
    """Days whose energy is final: newest revision, complete, and old enough."""
    statement = (
        select(ProductionFact)
        .where(
            ProductionFact.provider_mapping_id == provider_mapping_id,
            ProductionFact.metric_kind == "production_energy",
            ProductionFact.period_end <= cutoff,
        )
        .distinct(ProductionFact.source_fact_key)
        .order_by(ProductionFact.source_fact_key, ProductionFact.source_revision.desc())
    )
    return [fact for fact in session.scalars(statement) if fact.completeness == "complete" and fact.quality != "missing"]


def purge_samples(
    session_factory: sessionmaker[Session],
    *,
    connection_id: int,
    retention_days: int,
    now: datetime | None = None,
) -> RetentionResult:
    if retention_days <= 0:
        raise ValueError("Huawei SCADA retention window must be positive.")
    cutoff = as_utc(now or utc_now()) - timedelta(days=retention_days)
    result = RetentionResult()
    with session_factory() as session:
        mappings = [
            (mapping.id, mapping.asset_id)
            for mapping in ProviderRepository(session).current_mappings_for_connection(connection_id)
            if mapping.mapping_status == "active"
        ]

    for mapping_id, _asset_id in mappings:
        result.mappings_examined += 1
        with session_factory() as session:
            for fact in _settled_days(session, provider_mapping_id=mapping_id, cutoff=cutoff):
                window = (
                    HuaweiScadaPowerSample.provider_mapping_id == mapping_id,
                    HuaweiScadaPowerSample.observed_at >= as_utc(fact.period_start),
                    HuaweiScadaPowerSample.observed_at < as_utc(fact.period_end),
                )
                # The supersession chain is provenance between rows that are
                # all about to go. `supersedes_sample_id` is RESTRICT so a
                # stray single-row delete cannot silently break a chain --
                # which means dismantling one has to be deliberate, and this
                # is the one place that is allowed to be.
                session.execute(
                    update(HuaweiScadaPowerSample).where(*window).values(supersedes_sample_id=None)
                )
                deleted = session.execute(delete(HuaweiScadaPowerSample).where(*window)).rowcount or 0
                if deleted:
                    result.days_deleted += 1
                    result.samples_deleted += deleted
                else:
                    result.skip("already_empty")
            session.commit()
    return result
