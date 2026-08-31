"""Reading the contract off the data, and refusing to when the data is thin.

The value of this layer is entirely in what it declines to conclude. A verdict
that appears from twelve samples, or from evidence pointing both ways, is worse
than no verdict: it would be typed into an environment variable and never
questioned again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal


from nemsei.integrations.huawei_scada.contract_evidence import (
    MIN_BALANCE_SAMPLES,
    MIN_SIGN_SAMPLES,
    evidence_for,
    grid_sign_evidence,
    power_scale_evidence,
    production_signal_evidence,
)


class Reading:
    """Only the five columns the evidence functions read."""

    def __init__(self, *, pv=None, load=None, grid=None, battery=None, total=None, offset=0):
        self.observed_at = datetime(2026, 8, 24, tzinfo=timezone.utc) + timedelta(seconds=offset)
        self.pv_input_power_kw = None if pv is None else Decimal(str(pv))
        self.load_power_kw = None if load is None else Decimal(str(load))
        self.grid_power_kw = None if grid is None else Decimal(str(grid))
        self.battery_power_kw = None if battery is None else Decimal(str(battery))
        self.total_active_power_kw = None if total is None else Decimal(str(total))


def night(count: int, *, grid: float, load: float = 2.0, battery: float = 0.0):
    """Moments where the site can only be importing."""
    return [Reading(pv=0, load=load, grid=grid, battery=battery, total=0, offset=i * 30) for i in range(count)]


def daylight(count: int, *, pv: float, ratio: float = 0.97):
    return [
        Reading(pv=pv, load=3, grid=-(pv - 3), battery=0, total=pv * ratio, offset=i * 30)
        for i in range(count)
    ]


# --- the grid sign ------------------------------------------------------------


def test_a_full_night_of_importing_settles_the_convention() -> None:
    evidence = grid_sign_evidence(night(120, grid=2.0))
    assert evidence.verdict == "positive_import"
    assert evidence.quiet_samples == 120
    assert evidence.positive_while_importing == 120


def test_the_opposite_sign_settles_it_the_other_way() -> None:
    assert grid_sign_evidence(night(120, grid=-2.0)).verdict == "positive_export"


def test_too_few_moments_settle_nothing_by_either_route() -> None:
    evidence = grid_sign_evidence(night(MIN_BALANCE_SAMPLES - 1, grid=2.0))
    assert evidence.verdict is None
    assert "are needed" in evidence.reason


def test_contradictory_evidence_settles_nothing_rather_than_taking_a_majority() -> None:
    """A 60/40 split is not a 60% answer; it means something else was happening."""
    evidence = grid_sign_evidence(night(60, grid=2.0) + night(40, grid=-2.0))
    assert evidence.verdict is None
    assert "Contradictory" in evidence.reason


def test_moments_with_the_battery_moving_do_not_qualify() -> None:
    """A discharging battery can export at night, so those moments prove nothing."""
    evidence = grid_sign_evidence(night(120, grid=2.0, battery=-3.0))
    assert evidence.quiet_samples == 0
    assert evidence.verdict is None


def test_moments_with_any_pv_do_not_qualify() -> None:
    evidence = grid_sign_evidence([Reading(pv=5, load=2, grid=-3, battery=0, total=5) for _ in range(120)])
    assert evidence.quiet_samples == 0


def test_moments_with_no_load_do_not_qualify() -> None:
    evidence = grid_sign_evidence(night(120, grid=2.0, load=0.0))
    assert evidence.quiet_samples == 0


def test_daylight_alone_settles_the_sign_through_the_energy_balance() -> None:
    """What the real hardware taught, and what the night-only rule missed.

    `daylight()` builds a site obeying load = pv + grid with grid negative for
    export... except it does not: it builds grid = -(pv - load), which is the
    import convention seen from the other side. Either way only one hypothesis
    closes, and that is the whole point -- no night required.
    """
    evidence = grid_sign_evidence(daylight(60, pv=20))
    assert evidence.verdict is not None
    assert evidence.method == "energy_balance"
    assert evidence.quiet_samples == 0, "settled without a single night-time moment"


def test_the_balance_route_reports_which_hypothesis_closed() -> None:
    evidence = grid_sign_evidence(night(60, grid=2.0))
    assert evidence.method == "energy_balance"
    assert evidence.balance_says_import == 60
    assert evidence.balance_says_export == 0
    assert "load = pv + grid" in evidence.reason


def test_a_site_whose_balance_closes_for_neither_falls_back_to_the_night_count() -> None:
    """Some other flow exists, so the identity fails -- but night still works."""
    unbalanced = night(60, grid=2.0, load=5.0)
    evidence = grid_sign_evidence(unbalanced)
    assert evidence.method == "night_import"
    assert evidence.verdict == "positive_import"
    assert evidence.balance_says_import == 0 and evidence.balance_says_export == 0


def test_a_moving_battery_is_excluded_from_the_balance_too() -> None:
    """The identity only holds while nothing else is absorbing the difference."""
    evidence = grid_sign_evidence(night(60, grid=2.0, battery=-3.0))
    assert evidence.balance_samples == 0
    assert evidence.verdict is None


# --- the power scale ----------------------------------------------------------


def test_a_peak_near_installed_capacity_reads_as_plausible_kilowatts() -> None:
    evidence = power_scale_evidence(daylight(50, pv=28), installed_dc_power_kw=Decimal("30"))
    assert evidence.verdict == "plausible"


def test_a_peak_a_thousand_times_capacity_is_called_out(  ) -> None:
    """The factor-of-1000 error this check exists for: 30 kWp reading 30 MW."""
    evidence = power_scale_evidence(daylight(50, pv=28000), installed_dc_power_kw=Decimal("30"))
    assert evidence.verdict == "implausible"
    assert "gain" in evidence.reason


def test_a_plant_without_stated_capacity_gets_no_verdict() -> None:
    evidence = power_scale_evidence(daylight(50, pv=28), installed_dc_power_kw=None)
    assert evidence.verdict is None
    assert "installed capacity" in evidence.reason


def test_no_readings_at_all_gets_no_verdict() -> None:
    assert power_scale_evidence([], installed_dc_power_kw=Decimal("30")).verdict is None


# --- which register is which --------------------------------------------------


def test_two_registers_reading_the_same_value_are_reported_as_identical() -> None:
    """Observed on the pilot: 37498 and 37516 return the same number, always.

    Calling that "downstream" would invent a conversion step this installation
    does not show.
    """
    evidence = production_signal_evidence(daylight(100, pv=20, ratio=1.0))
    assert evidence.verdict == "registers_are_identical"
    assert "the choice is free" in evidence.reason


def test_conversion_losses_identify_the_ac_side() -> None:
    evidence = production_signal_evidence(daylight(100, pv=20, ratio=0.96))
    assert evidence.verdict == "total_active_is_downstream"
    assert evidence.median_total_over_pv < Decimal("1")
    # It reports which register is which, and says the commercial choice is
    # not its to make.
    assert "commercial decision" in evidence.reason


def test_a_ratio_above_unity_is_reported_as_unexpected_not_resolved() -> None:
    evidence = production_signal_evidence(daylight(100, pv=20, ratio=1.4))
    assert evidence.verdict == "unexpected"


def test_night_samples_do_not_feed_the_ratio() -> None:
    assert production_signal_evidence(night(100, grid=2.0)).verdict is None


# --- together -----------------------------------------------------------------

def test_one_day_and_one_night_answers_every_question_at_once() -> None:
    samples = daylight(200, pv=28) + night(200, grid=2.0)
    evidence = evidence_for(samples, installed_dc_power_kw=Decimal("30"))
    assert evidence.sample_count == 400
    assert evidence.settles_grid_sign
    assert evidence.grid_sign.verdict == "positive_import"
    assert evidence.power_scale.verdict == "plausible"
    assert evidence.production_signal.verdict == "total_active_is_downstream"


def test_an_empty_window_settles_nothing_and_says_so() -> None:
    evidence = evidence_for([], installed_dc_power_kw=Decimal("30"))
    assert not evidence.settles_grid_sign
    assert evidence.power_scale.verdict is None
    assert evidence.production_signal.verdict is None
