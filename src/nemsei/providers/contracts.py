"""Small capability-specific adapter contracts; intentionally no transport layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True)
class DiscoveredPlant:
    external_id: str
    display_name: str | None


class DiscoveryCapability(Protocol):
    def discover_plants(self, *, connection_key: str) -> Sequence[DiscoveredPlant]: ...


@dataclass(frozen=True)
class MonitoringSample:
    source_key: str
    observed_at: datetime
    condition: str
    raw_status_code: str | None = None
    raw_status_text: str | None = None


class CurrentMonitoringCapability(Protocol):
    def current_monitoring(self, *, external_plant_id: str) -> Sequence[MonitoringSample]: ...


@dataclass(frozen=True)
class ProductionSample:
    source_key: str
    period_start: datetime
    period_end: datetime
    value: Decimal | None
    unit: str


class ProductionHistoryCapability(Protocol):
    def production_history(self, *, external_plant_id: str, from_at: datetime, until_at: datetime) -> Sequence[ProductionSample]: ...
