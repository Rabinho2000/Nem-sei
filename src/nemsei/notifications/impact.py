"""Energy and financial impact of an operational episode -- real data only.

Req 6: "Sempre que existirem dados suficientes, usar potência, duração,
produção esperada, produção perdida estimada, ESCO, receita potencial
perdida. Não inventar valores. Se não houver dados suficientes: impacto: não
calculável -- e não 0€."

Two honest exceptions to "no data means unknown", both real zeros rather than
guesses: an outage that never overlaps the installation's productive window
(`monitoring.production_window`) genuinely cost ~0 kWh, because a solar plant
was not expected to produce then regardless of the fault -- that is a
calculated zero, not a missing one. Everywhere else, with no recent power
reading for this exact asset to anchor the estimate on, the answer is `None`
("não calculável"), never `0`.

The estimate itself is deliberately simple, and says so: the asset's own most
recent known power just before the episode began, held flat for however much
of the episode overlapped the productive window. Not a capacity-based guess
(`installed_dc_power_kw x factor`) -- that would be inventing a number this
specific installation never actually produced. Not a same-hour historical
average either (a nicer estimate, but this codebase's fact tables are too
sparse for most of the portfolio today -- `docs/v2/DIAGNOSTICS.md` -- to make
that comparison honest for more than a handful of installations); revisit
once live polling covers enough of the fleet for that comparison to mean
something for most episodes, not just the canary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import DeviceStatusFact
from nemsei.monitoring.production_window import window_for

# How far back before the episode started to look for a "what was this
# installation doing right before it broke" reading. Two hours: recent
# enough that a reading from before the outage still describes the same
# operating conditions (same time of day, same weather), not so tight that
# a poll cadence slower than a few minutes finds nothing.
LOOKBACK = timedelta(hours=2)


@dataclass(frozen=True)
class EnergyImpact:
    lost_kwh: Decimal | None
    #: Why `lost_kwh` is what it is -- always populated, including for a
    #: calculated zero or an honest "não calculável", so a caller never has
    #: to guess which case it is from the number alone.
    basis: str


@dataclass(frozen=True)
class FinancialImpact:
    lost_eur: Decimal | None
    basis: str


def estimate_energy_impact(
    session: Session,
    *,
    asset_id: int,
    device_id: int | None,
    start: datetime,
    end: datetime,
    latitude: Decimal | float | None,
    longitude: Decimal | float | None,
) -> EnergyImpact:
    if end <= start:
        return EnergyImpact(None, "janela de duração inválida")

    productive_hours = _productive_overlap_hours(start=start, end=end, latitude=latitude, longitude=longitude)
    if productive_hours is None:
        return EnergyImpact(None, "período produtivo desconhecido (instalação sem coordenadas)")
    if productive_hours == Decimal("0"):
        return EnergyImpact(Decimal("0"), "fora do período produtivo (noite) durante toda a janela")

    pre_outage_power_kw = _last_known_power_before(session, asset_id=asset_id, device_id=device_id, before=start)
    if pre_outage_power_kw is None:
        return EnergyImpact(None, "sem leitura de potência recente antes do início do episódio")

    lost_kwh = (pre_outage_power_kw * productive_hours).quantize(Decimal("0.1"))
    return EnergyImpact(
        lost_kwh,
        f"{pre_outage_power_kw}kW (última leitura antes do episódio) x {productive_hours}h produtivas na janela",
    )


def estimate_financial_impact(*, energy: EnergyImpact, price_eur_per_kwh: Decimal | None) -> FinancialImpact:
    if energy.lost_kwh is None:
        return FinancialImpact(None, "impacto energético não calculável")
    if price_eur_per_kwh is None:
        return FinancialImpact(None, "sem tarifa configurada para esta instalação")
    lost_eur = (energy.lost_kwh * price_eur_per_kwh).quantize(Decimal("0.01"))
    return FinancialImpact(lost_eur, f"{energy.lost_kwh}kWh x {price_eur_per_kwh}€/kWh")


def _productive_overlap_hours(
    *, start: datetime, end: datetime, latitude: Decimal | float | None, longitude: Decimal | float | None
) -> Decimal | None:
    """How many hours of `[start, end)` fall inside the installation's
    productive window -- summed per calendar day, since sunrise/sunset are a
    per-day concept. `None` when the installation has no coordinates at all
    (a real, common gap -- `installations/models.py`), which is a different
    answer from "computed and it is zero hours".
    """
    if latitude is None or longitude is None:
        return None

    total = timedelta()
    day_start = start
    while day_start < end:
        day_end = min(end, datetime.combine(day_start.date(), datetime.min.time(), tzinfo=day_start.tzinfo) + timedelta(days=1))
        window = window_for(latitude=latitude, longitude=longitude, at=day_start)
        if window.is_known and window.starts_at is not None and window.ends_at is not None:
            overlap_start = max(day_start, window.starts_at)
            overlap_end = min(day_end, window.ends_at)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        day_start = day_end

    return Decimal(total.total_seconds() / 3600).quantize(Decimal("0.01"))


def _last_known_power_before(
    session: Session, *, asset_id: int, device_id: int | None, before: datetime
) -> Decimal | None:
    """The asset's (or one specific device's) most recent known active power
    in the `LOOKBACK` window right before an episode began -- real evidence
    of what this installation was actually doing, not a capacity guess.

    Asset-level (`device_id is None`, e.g. `plant_offline`) sums every
    device's own most recent reading in the window rather than picking one:
    a multi-inverter site's plant-level power is the sum of its inverters',
    and a partial reading (one inverter reported, another did not) still
    honestly under-states rather than fabricating the missing one.
    """
    query = select(DeviceStatusFact.device_id, DeviceStatusFact.active_power_kw, DeviceStatusFact.observed_at).where(
        DeviceStatusFact.asset_id == asset_id,
        DeviceStatusFact.observed_at >= before - LOOKBACK,
        DeviceStatusFact.observed_at < before,
        DeviceStatusFact.active_power_kw.is_not(None),
    )
    if device_id is not None:
        query = query.where(DeviceStatusFact.device_id == device_id)
    rows = session.execute(query.order_by(DeviceStatusFact.observed_at.desc())).all()
    if not rows:
        return None

    latest_by_device: dict[int, Decimal] = {}
    for row_device_id, power_kw, _observed_at in rows:
        if row_device_id not in latest_by_device:
            latest_by_device[row_device_id] = power_kw
    return sum(latest_by_device.values(), Decimal("0"))
