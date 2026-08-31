"""What one installation is doing *right now*, from the evidence V2 holds.

This exists because the interface had no answer to that question. The badge an
operator reads next to a plant's name was `lifecycle_status` -- an
administrative field (ativa/inativa/desativada) that nothing computes, left at
`unknown` by the V1 import for 264 of 267 centrais. So a plant generating
happily read "Desconhecida", which is precisely the wrong thing for an O&M
product to say about a plant that is working.

The state here is *derived*, never stored: recomputed from facts on every
call, exactly like `diagnostics.findings`, so there is no third copy of the
truth to fall out of date. It answers from two sources, in this order:

1. the plant-level `MonitoringObservation` -- the provider's own statement
   about the whole system, and the closest thing to an authoritative answer;
2. the installation's `DeviceStatusFact` rows, when the provider states
   nothing useful -- inverters are what a plant is made of, and a site whose
   inverters are all available is working whatever the plant endpoint says.

Two answers are deliberately *not* "unknown", because conflating them wastes
the operator's time: `no_evidence` (nothing was ever read for this plant) and
`stale` (something was read, but too long ago to describe "agora"). Only
"we have a fresh reading and it does not say" is `unknown`.

Production facts are not consulted. A daily energy total is evidence about
yesterday, not about now, and using it here would let a plant that died this
morning keep reading "operacional" all day.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import DeviceStatusFact
from nemsei.monitoring.models import MonitoringCurrentState, MonitoringObservation
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import utc_now


INSTALLATION_STATES = ("operational", "standby", "warning", "fault", "offline", "unknown", "stale", "no_evidence")

# Beyond this, a reading no longer describes "now". Same window as
# `diagnostics.findings._DEFAULT_STALE_AFTER`, deliberately: two screens
# disagreeing about what counts as a current reading would be worse than
# either threshold being slightly wrong.
FRESH_WINDOW = timedelta(hours=24)

STATE_LABELS = {
    "operational": "Operacional",
    "standby": "Em repouso",
    "warning": "Com aviso",
    "fault": "Avaria",
    "offline": "Sem comunicação",
    "unknown": "Estado desconhecido",
    "stale": "Sem leitura recente",
    "no_evidence": "Sem leitura",
}

STATE_TONES = {
    "operational": "success",
    "standby": "muted",
    "warning": "warning",
    "fault": "danger",
    "offline": "danger",
    "unknown": "muted",
    "stale": "warning",
    "no_evidence": "muted",
}

# A plant-level condition maps straight through, except `unknown`: the
# provider saying nothing is not an answer, so the devices get asked instead.
_PLANT_CONDITIONS = {"operational": "operational", "warning": "warning", "fault": "fault", "offline": "offline"}


@dataclass(frozen=True)
class InstallationState:
    asset_id: int
    state: str
    #: When the evidence behind this state was last *confirmed* -- the answer
    #: to "how current is this", and what the freshness window is measured on.
    observed_at: datetime | None = None
    #: When the plant entered this condition, when that is a different
    #: question. `monitoring_observations` only gets a new row when the
    #: evidence changes, so a plant operational since yesterday keeps
    #: yesterday's row while being re-read every 15 minutes.
    since: datetime | None = None
    source: str | None = None
    detail: str | None = None

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def tone(self) -> str:
        return STATE_TONES.get(self.state, "muted")

    @property
    def is_known(self) -> bool:
        return self.state in _PLANT_CONDITIONS or self.state == "standby"


def classify_installation_state(
    *,
    asset_id: int,
    plant_condition: str | None,
    plant_observed_at: datetime | None,
    plant_confirmed_at: datetime | None = None,
    device_readings: Sequence[tuple[str, datetime]],
    now: datetime,
) -> InstallationState:
    """Decide one installation's current state. Pure: no session, no clock.

    `device_readings` is one `(availability_status, observed_at)` per device --
    the device's most recent reading, not its history.

    `plant_confirmed_at` is `MonitoringCurrentState.last_confirmed_at`: the last
    time a read actually confirmed this plant's state. It is **not**
    interchangeable with `plant_observed_at`, because an unchanged plant keeps
    its old observation row -- measuring freshness on the observation would
    report every healthy, unchanging installation as unread within a day.
    """
    plant_seen_at = plant_confirmed_at or plant_observed_at
    freshest = _freshest(plant_seen_at, [observed_at for _, observed_at in device_readings])
    if freshest is None:
        return InstallationState(asset_id, "no_evidence")
    if now - freshest > FRESH_WINDOW:
        return InstallationState(
            asset_id,
            "stale",
            observed_at=freshest,
            since=plant_observed_at if freshest == plant_seen_at else None,
            source="plant_observation" if freshest == plant_seen_at else "device_facts",
            detail=f"a leitura mais recente é de {freshest.strftime('%Y-%m-%d %H:%M')}",
        )

    if plant_condition is not None and plant_seen_at is not None and now - plant_seen_at <= FRESH_WINDOW:
        mapped = _PLANT_CONDITIONS.get(plant_condition)
        if mapped is not None:
            return InstallationState(
                asset_id,
                mapped,
                observed_at=plant_seen_at,
                since=plant_observed_at,
                source="plant_observation",
                detail="estado indicado pelo provider para a instalação",
            )

    fresh_devices = [(status, observed_at) for status, observed_at in device_readings if now - observed_at <= FRESH_WINDOW]
    if fresh_devices:
        state, detail = _from_devices(fresh_devices)
        return InstallationState(
            asset_id,
            state,
            observed_at=max(observed_at for _, observed_at in fresh_devices),
            source="device_facts",
            detail=detail,
        )

    # A fresh plant reading that says nothing, with no fresh device reading
    # behind it to settle the question.
    return InstallationState(
        asset_id,
        "unknown",
        observed_at=plant_seen_at,
        since=plant_observed_at,
        source="plant_observation",
        detail="o provider respondeu mas não indicou estado",
    )


def _from_devices(readings: Sequence[tuple[str, datetime]]) -> tuple[str, str]:
    statuses = [status for status, _ in readings]
    total = len(statuses)
    available = statuses.count("available")
    unavailable = statuses.count("unavailable")
    standby = statuses.count("standby")
    if available:
        return "operational", f"{available} de {total} equipamentos disponíveis"
    if unavailable and unavailable == total:
        return "fault", f"os {total} equipamentos estão indisponíveis"
    if unavailable:
        return "fault", f"{unavailable} de {total} equipamentos indisponíveis"
    if standby:
        # Every inverter at rest and none faulty: the plant is fine and not
        # generating, which at night is the correct answer and not a problem.
        return "standby", f"{standby} de {total} equipamentos em repouso"
    return "unknown", f"nenhum dos {total} equipamentos indica estado"


def _freshest(plant_observed_at: datetime | None, device_times: Iterable[datetime]) -> datetime | None:
    candidates = [value for value in [plant_observed_at, *device_times] if value is not None]
    return max(candidates) if candidates else None


def current_installation_states(
    session: Session, *, asset_ids: Sequence[int], now: datetime | None = None
) -> dict[int, InstallationState]:
    """One state per asset id, for a page of installations at a time."""
    if not asset_ids:
        return {}
    moment = now or utc_now()
    ids = list(dict.fromkeys(asset_ids))

    observations = session.execute(
        select(MonitoringObservation.asset_id, MonitoringObservation.condition, MonitoringObservation.observed_at)
        .where(MonitoringObservation.asset_id.in_(ids))
        .distinct(MonitoringObservation.asset_id)
        .order_by(MonitoringObservation.asset_id, MonitoringObservation.observed_at.desc(), MonitoringObservation.id.desc())
    ).all()
    plant = {asset_id: (condition, observed_at) for asset_id, condition, observed_at in observations}
    # When each plant was last successfully re-read, which is a different clock
    # from the observation's own timestamp -- see classify_installation_state.
    confirmations = dict(
        session.execute(
            select(AssetProviderMapping.asset_id, func.max(MonitoringCurrentState.last_confirmed_at))
            .join(MonitoringCurrentState, MonitoringCurrentState.provider_mapping_id == AssetProviderMapping.id)
            .where(AssetProviderMapping.asset_id.in_(ids))
            .group_by(AssetProviderMapping.asset_id)
        ).all()
    )

    facts = session.execute(
        select(DeviceStatusFact.asset_id, DeviceStatusFact.availability_status, DeviceStatusFact.observed_at)
        .where(DeviceStatusFact.asset_id.in_(ids))
        .distinct(DeviceStatusFact.device_id)
        .order_by(DeviceStatusFact.device_id, DeviceStatusFact.observed_at.desc(), DeviceStatusFact.id.desc())
    ).all()
    devices: dict[int, list[tuple[str, datetime]]] = {}
    for asset_id, status, observed_at in facts:
        devices.setdefault(asset_id, []).append((status, observed_at))

    states: dict[int, InstallationState] = {}
    for asset_id in ids:
        condition, observed_at = plant.get(asset_id, (None, None))
        states[asset_id] = classify_installation_state(
            asset_id=asset_id,
            plant_condition=condition,
            plant_observed_at=observed_at,
            plant_confirmed_at=confirmations.get(asset_id),
            device_readings=devices.get(asset_id, []),
            now=moment,
        )
    return states


def current_installation_state(session: Session, *, asset_id: int, now: datetime | None = None) -> InstallationState:
    return current_installation_states(session, asset_ids=[asset_id], now=now)[asset_id]
