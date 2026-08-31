"""Turning collected samples into the evidence the contract questions need.

Three values have no default anywhere in this integration -- the power unit,
which register counts as production, and which direction positive means on the
grid register -- because guessing any of them produces a plausible, wrong
number. That refusal is correct, and on its own it leaves an operator staring
at three environment variables with nothing to fill them from.

This module closes that. It reads samples the listener already persisted and
reports what they imply, without deciding anything:

* **The grid sign.** With the battery still, a site obeys `load = pv + grid` if
  positive means import and `load = pv - grid` if it means export. Only one of
  those closes. On the pilot hardware the right one closes to 0.000 kW on every
  single sample, so this settles in daylight and in minutes. A night-time count
  (no PV, no battery, some load, so the site can only be importing) is kept as
  a fallback for sites where the balance does not close.
* **The power scale.** Peak PV against the plant's own installed capacity. A
  gain that is wrong by a factor of a thousand does not look subtly wrong, it
  looks like a 30 kW roof producing 30 megawatts.
* **Which register is which.** The ratio between 37516 and 37498 while the
  plant is producing. Conversion losses put AC below DC, so the ratio says
  which of the two is the inverter's output -- it does not say which one the
  customer's report should call production, which is a commercial decision.

Every verdict refuses itself when the evidence is thin or contradictory.
`None` here means "still unknown", never "probably fine".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any, Iterable, Sequence

# Below this, a register is treated as reading zero rather than as a small
# measurement. The registers carry watts, so a milliwatt of noise is 0.001 kW.
QUIET_KW = Decimal("0.05")
# A site drawing less than this is not clearly consuming anything, so its grid
# direction proves nothing.
MIN_LOAD_KW = Decimal("0.10")
# Enough night-time moments that a handful of odd readings cannot decide it.
MIN_SIGN_SAMPLES = 20
# One direction has to dominate this completely; anything less is contradictory
# evidence, and the honest answer to contradictory evidence is "unknown".
SIGN_MAJORITY = Decimal("0.95")
MIN_RATIO_SAMPLES = 20
# The energy balance is an identity, not a statistic, so it needs far fewer
# moments than the night-time heuristic -- but not so few that one glitched
# register decides it.
MIN_BALANCE_SAMPLES = 5
# Residual tolerance. The registers are integers of watts and the observed
# balance closes to 0.000, so anything above a few tens of watts means the
# hypothesis under test is the wrong one.
BALANCE_TOLERANCE_KW = Decimal("0.05")


@dataclass(frozen=True)
class GridSignEvidence:
    """Which direction the grid register calls positive, and how we know.

    Two independent methods, reported together. The energy balance is the
    stronger one and works at any hour; the night-time count is the fallback
    for a site whose load or PV register is unavailable.
    """

    quiet_samples: int
    positive_while_importing: int
    negative_while_importing: int
    balance_samples: int
    balance_says_import: int
    balance_says_export: int
    method: str | None
    verdict: str | None
    reason: str


@dataclass(frozen=True)
class PowerScaleEvidence:
    peak_pv_kw: Decimal | None
    peak_total_kw: Decimal | None
    installed_dc_power_kw: Decimal | None
    implied_factor: Decimal | None
    verdict: str | None
    reason: str


@dataclass(frozen=True)
class ProductionSignalEvidence:
    compared_samples: int
    median_total_over_pv: Decimal | None
    verdict: str | None
    reason: str


@dataclass(frozen=True)
class ContractEvidence:
    sample_count: int
    grid_sign: GridSignEvidence
    power_scale: PowerScaleEvidence
    production_signal: ProductionSignalEvidence

    @property
    def settles_grid_sign(self) -> bool:
        return self.grid_sign.verdict is not None


def _value(sample: Any, name: str) -> Decimal | None:
    raw = getattr(sample, name, None)
    return None if raw is None else Decimal(raw)


def grid_sign_evidence(samples: Iterable[Any]) -> GridSignEvidence:
    """Read the convention off the data, by two independent routes.

    **The energy balance**, tried first. With the battery still, a site obeys
    `load = pv + grid` if positive means import, and `load = pv - grid` if it
    means export. Only one of those closes, and on the real hardware the right
    one closes to 0.000 kW on every sample -- so this settles the question in
    daylight, in minutes, and does not need a special hour of the day.

    **The night-time count**, kept as a fallback. With no PV and no battery
    movement, a site with load can only be importing, so the sign it shows then
    is the convention. It needs a night, and it needs the load register, which
    is why it is no longer the primary route.
    """
    ordered = list(samples)
    balance = _balance_evidence(ordered)
    positive = negative = quiet = 0
    for sample in ordered:
        pv = _value(sample, "pv_input_power_kw")
        load = _value(sample, "load_power_kw")
        grid = _value(sample, "grid_power_kw")
        battery = _value(sample, "battery_power_kw")
        if pv is None or load is None or grid is None or battery is None:
            continue
        if abs(pv) > QUIET_KW or abs(battery) > QUIET_KW:
            continue
        if load < MIN_LOAD_KW or abs(grid) <= QUIET_KW:
            continue
        quiet += 1
        if grid > 0:
            positive += 1
        else:
            negative += 1

    total, says_import, says_export, balance_verdict, balance_reason = balance
    if balance_verdict is not None:
        return GridSignEvidence(
            quiet, positive, negative, total, says_import, says_export,
            "energy_balance", balance_verdict, balance_reason,
        )

    if quiet < MIN_SIGN_SAMPLES:
        return GridSignEvidence(
            quiet, positive, negative, total, says_import, says_export, None, None,
            f"{balance_reason} Night-time route: only {quiet} qualifying moment(s); "
            f"{MIN_SIGN_SAMPLES} are needed.",
        )
    dominant = max(positive, negative)
    if Decimal(dominant) / Decimal(quiet) < SIGN_MAJORITY:
        return GridSignEvidence(
            quiet, positive, negative, total, says_import, says_export, None, None,
            f"Contradictory: {positive} positive and {negative} negative while importing. "
            "Something else was moving energy; do not set a convention from this.",
        )
    verdict = "positive_import" if positive > negative else "positive_export"
    return GridSignEvidence(
        quiet, positive, negative, total, says_import, says_export, "night_import", verdict,
        f"{dominant} of {quiet} night-time moments with no PV and no battery flow read "
        f"{'positive' if positive > negative else 'negative'} while the site had to be importing.",
    )


def _balance_evidence(samples: Iterable[Any]) -> tuple[int, int, int, str | None, str]:
    """Test both sign hypotheses against `load = pv ± grid`. Only one closes."""
    says_import = says_export = total = 0
    for sample in samples:
        pv = _value(sample, "pv_input_power_kw")
        load = _value(sample, "load_power_kw")
        grid = _value(sample, "grid_power_kw")
        battery = _value(sample, "battery_power_kw")
        if pv is None or load is None or grid is None or battery is None:
            continue
        if abs(battery) > QUIET_KW or abs(grid) <= QUIET_KW:
            # A moving battery breaks the identity; a grid reading of zero
            # satisfies both hypotheses equally and so distinguishes nothing.
            continue
        total += 1
        if abs(load - pv - grid) <= BALANCE_TOLERANCE_KW:
            says_import += 1
        elif abs(load - pv + grid) <= BALANCE_TOLERANCE_KW:
            says_export += 1

    if total < MIN_BALANCE_SAMPLES:
        return total, says_import, says_export, None, (
            f"Energy balance: only {total} usable moment(s); {MIN_BALANCE_SAMPLES} are needed."
        )
    dominant = max(says_import, says_export)
    if Decimal(dominant) / Decimal(total) < SIGN_MAJORITY:
        return total, says_import, says_export, None, (
            f"Energy balance closes for neither hypothesis consistently "
            f"({says_import} import, {says_export} export, of {total})."
        )
    verdict = "positive_import" if says_import > says_export else "positive_export"
    direction = "load = pv + grid" if says_import > says_export else "load = pv - grid"
    return total, says_import, says_export, verdict, (
        f"{dominant} of {total} moments satisfy {direction} with the battery still, "
        f"and the opposite hypothesis does not."
    )


def power_scale_evidence(samples: Sequence[Any], *, installed_dc_power_kw: Decimal | None) -> PowerScaleEvidence:
    """Does the peak look like this plant, or like this plant times a thousand?"""
    pv_values = [value for value in (_value(sample, "pv_input_power_kw") for sample in samples) if value is not None]
    total_values = [value for value in (_value(sample, "total_active_power_kw") for sample in samples) if value is not None]
    peak_pv = max(pv_values) if pv_values else None
    peak_total = max(total_values) if total_values else None
    peak = max([value for value in (peak_pv, peak_total) if value is not None], default=None)

    if peak is None:
        return PowerScaleEvidence(peak_pv, peak_total, installed_dc_power_kw, None, None, "No power readings yet.")
    if installed_dc_power_kw is None or installed_dc_power_kw <= 0:
        return PowerScaleEvidence(
            peak_pv, peak_total, installed_dc_power_kw, None, None,
            "The asset states no installed capacity, so there is nothing to compare the peak against.",
        )
    factor = peak / installed_dc_power_kw
    if factor > Decimal("10"):
        return PowerScaleEvidence(
            peak_pv, peak_total, installed_dc_power_kw, factor, "implausible",
            f"Peak {peak} kW against {installed_dc_power_kw} kWp installed -- {factor:.0f}x. "
            "The register gain is almost certainly not 1000, or the unit is not kW.",
        )
    if factor > Decimal("1.3"):
        return PowerScaleEvidence(
            peak_pv, peak_total, installed_dc_power_kw, factor, "suspicious",
            f"Peak {peak} kW exceeds {installed_dc_power_kw} kWp installed by {factor:.2f}x. "
            "Possible, but worth confirming against the inverter's own display.",
        )
    if peak < installed_dc_power_kw / Decimal("100"):
        return PowerScaleEvidence(
            peak_pv, peak_total, installed_dc_power_kw, factor, None,
            f"Peak {peak} kW is tiny against {installed_dc_power_kw} kWp. Either the window "
            "holds no daylight yet, or the scale is wrong in the other direction.",
        )
    return PowerScaleEvidence(
        peak_pv, peak_total, installed_dc_power_kw, factor, "plausible",
        f"Peak {peak} kW against {installed_dc_power_kw} kWp installed ({factor:.2f}x): consistent with kW.",
    )


def production_signal_evidence(samples: Iterable[Any]) -> ProductionSignalEvidence:
    """Which of 37498 and 37516 sits downstream of the other."""
    ratios: list[Decimal] = []
    for sample in samples:
        pv = _value(sample, "pv_input_power_kw")
        total = _value(sample, "total_active_power_kw")
        if pv is None or total is None or pv <= QUIET_KW:
            continue
        ratios.append(total / pv)

    if len(ratios) < MIN_RATIO_SAMPLES:
        return ProductionSignalEvidence(
            len(ratios), None, None,
            f"Only {len(ratios)} producing moment(s); {MIN_RATIO_SAMPLES} are needed.",
        )
    middle = Decimal(str(median(ratios)))
    if abs(middle - Decimal("1")) <= Decimal("0.001"):
        # Observed on the pilot: 37498 and 37516 return the identical value on
        # every sample. Calling that "downstream" would invent a conversion
        # step that this installation does not show.
        return ProductionSignalEvidence(
            len(ratios), middle, "registers_are_identical",
            f"37516 and 37498 read the same value ({middle:.3f}) on every producing moment. "
            "Either register gives the same energy here, so the choice is free -- but a site "
            "with a battery or a separate meter may not behave this way.",
        )
    if middle <= Decimal("1.02"):
        return ProductionSignalEvidence(
            len(ratios), middle, "total_active_is_downstream",
            f"37516 runs at {middle:.3f} of 37498 while producing: 37516 is the AC side, "
            "37498 the DC input. Reports normally state AC yield, so total_active_power "
            "is the usual choice -- but that is a commercial decision, not this one.",
        )
    return ProductionSignalEvidence(
        len(ratios), middle, "unexpected",
        f"37516 reads {middle:.3f} of 37498 while producing, which is above unity. These are "
        "not the two sides of one conversion; do not choose between them from this evidence.",
    )


def evidence_for(samples: Sequence[Any], *, installed_dc_power_kw: Decimal | None) -> ContractEvidence:
    return ContractEvidence(
        sample_count=len(samples),
        grid_sign=grid_sign_evidence(samples),
        power_scale=power_scale_evidence(samples, installed_dc_power_kw=installed_dc_power_kw),
        production_signal=production_signal_evidence(samples),
    )
