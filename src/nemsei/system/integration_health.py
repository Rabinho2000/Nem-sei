"""Whether the platform is actually working, answered from its own records.

Until now this question had no screen at all: the only way to learn that
FusionSolar had been rate-limiting every sync since morning was to open psql.
An operator who cannot see that a provider is degraded will read an empty chart
as an empty plant, which is the worst mistake this product can cause.

Everything here is read-only and derived from rows the sync engine already
writes -- `integration_health`, `sync_runs`, `operator_audit_events`. No
provider is contacted to render this page, which matters especially when the
provider is the thing that is unhappy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.providers.models import AssetProviderMapping, OperatorAuditEvent, ProviderConnection
from nemsei.shared.clock import utc_now
from nemsei.sync.models import IntegrationHealth, SyncRun

# A run that did not deliver what was asked, for whatever reason. Kept separate
# from `failed` because a rate limit is not a defect in this platform -- it is
# a shared account saying no, and the fix is an account, not a code change.
UNHAPPY = ("failed", "rate_limited", "partial", "deferred")

STATE_TONES = {
    "healthy": "success",
    "degraded": "warning",
    "unavailable": "danger",
    "not_configured": "muted",
    "unknown": "muted",
}


@dataclass(frozen=True)
class ConnectionHealth:
    connection: ProviderConnection
    health: IntegrationHealth | None
    mappings: int
    active_mappings: int
    recent: list[SyncRun]
    counts: dict[str, int]

    @property
    def last_success_at(self) -> datetime | None:
        return self.health.last_success_at if self.health else None

    @property
    def worst_state(self) -> str:
        """The unhappiest of the four tracked states, which is what to show."""
        if self.health is None:
            return "unknown"
        order = ("unavailable", "degraded", "not_configured", "unknown", "healthy")
        states = {self.health.auth_state, self.health.access_state, self.health.provider_state, self.health.quota_state, self.health.sync_state}
        for candidate in order:
            if candidate in states:
                return candidate
        return "unknown"

    @property
    def unhappy_recent(self) -> int:
        return sum(count for status, count in self.counts.items() if status in UNHAPPY)


def connection_health(session: Session, *, window_hours: int = 48, recent_runs: int = 8) -> list[ConnectionHealth]:
    since = utc_now() - timedelta(hours=window_hours)
    healths = {row.provider_connection_id: row for row in session.scalars(select(IntegrationHealth))}
    mapping_totals = dict(
        session.execute(
            select(AssetProviderMapping.provider_connection_id, func.count(AssetProviderMapping.id))
            .group_by(AssetProviderMapping.provider_connection_id)
        ).all()
    )
    active_totals = dict(
        session.execute(
            select(AssetProviderMapping.provider_connection_id, func.count(AssetProviderMapping.id))
            .where(AssetProviderMapping.mapping_status == "active")
            .group_by(AssetProviderMapping.provider_connection_id)
        ).all()
    )
    out: list[ConnectionHealth] = []
    for connection in session.scalars(select(ProviderConnection).order_by(ProviderConnection.id)):
        counts = dict(
            session.execute(
                select(SyncRun.status, func.count(SyncRun.id))
                .where(SyncRun.provider_connection_id == connection.id, SyncRun.started_at >= since)
                .group_by(SyncRun.status)
            ).all()
        )
        recent = list(
            session.scalars(
                select(SyncRun)
                .where(SyncRun.provider_connection_id == connection.id)
                .order_by(SyncRun.id.desc())
                .limit(recent_runs)
            )
        )
        out.append(
            ConnectionHealth(
                connection=connection,
                health=healths.get(connection.id),
                mappings=int(mapping_totals.get(connection.id, 0)),
                active_mappings=int(active_totals.get(connection.id, 0)),
                recent=recent,
                counts={str(status): int(count) for status, count in counts.items()},
            )
        )
    return out


def system_health(session: Session, *, window_hours: int = 48) -> dict[str, Any]:
    since = utc_now() - timedelta(hours=window_hours)
    totals = dict(
        session.execute(
            select(SyncRun.status, func.count(SyncRun.id)).where(SyncRun.started_at >= since).group_by(SyncRun.status)
        ).all()
    )
    counts = {str(status): int(count) for status, count in totals.items()}
    attempted = sum(counts.values())
    return {
        "connections": connection_health(session, window_hours=window_hours),
        "window_hours": window_hours,
        "run_counts": counts,
        "runs_attempted": attempted,
        "runs_unhappy": sum(count for status, count in counts.items() if status in UNHAPPY),
        "audit": list(session.scalars(select(OperatorAuditEvent).order_by(OperatorAuditEvent.id.desc()).limit(25))),
        "audit_total": int(session.scalar(select(func.count(OperatorAuditEvent.id))) or 0),
    }
