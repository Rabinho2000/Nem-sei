"""Priority score (Telegram O&M redesign, req 5/17) -- pure, no database."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from nemsei.notifications.priority import PriorityInputs, score_episode


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def inputs(**overrides) -> PriorityInputs:
    defaults = dict(
        problem_family="fault", severity_peak="critical", opened_at=utc(9), now=utc(9, 30),
        commercial_family="unknown", om_status="active", installed_dc_power_kw=None, impact_eur=None,
        recurrence_count_24h=0, has_work_order=False, work_planned_or_in_progress_today=False, recovered=False,
    )
    defaults.update(overrides)
    return PriorityInputs(**defaults)


def test_score_always_carries_a_non_empty_reason_for_every_point_contributed() -> None:
    result = score_episode(inputs())
    assert result.score > 0
    assert result.reasons  # never hide the cause


def test_a_bare_communication_or_fault_signal_lands_at_medium() -> None:
    fault = score_episode(inputs(problem_family="fault", severity_peak="critical"))
    comms = score_episode(inputs(problem_family="communication"))
    assert fault.bucket == "MEDIUM"
    assert comms.bucket == "MEDIUM"


def test_esco_with_active_om_adds_points_and_a_reason() -> None:
    without = score_episode(inputs(commercial_family="unknown"))
    with_esco = score_episode(inputs(commercial_family="esco", om_status="active"))
    assert with_esco.score == without.score + 20
    assert any("ESCO" in reason for reason in with_esco.reasons)


def test_esco_without_active_om_adds_nothing() -> None:
    result = score_episode(inputs(commercial_family="esco", om_status="expired"))
    assert not any("ESCO" in reason for reason in result.reasons)


def test_financial_impact_only_scores_when_a_real_value_was_calculated() -> None:
    unknown = score_episode(inputs(impact_eur=None))
    small = score_episode(inputs(impact_eur=Decimal("50")))
    material = score_episode(inputs(impact_eur=Decimal("620")))
    assert not any("Impacto financeiro" in r for r in unknown.reasons)
    assert not any("Impacto financeiro" in r for r in small.reasons)  # below the 100€ threshold
    assert any("Impacto financeiro" in r for r in material.reasons)
    assert material.score == unknown.score + 20


def test_a_small_installed_power_never_scores() -> None:
    result = score_episode(inputs(installed_dc_power_kw=Decimal("50")))
    assert not any("kWp" in reason for reason in result.reasons)


def test_a_large_installation_scores_the_power_component() -> None:
    small = score_episode(inputs(installed_dc_power_kw=Decimal("50")))
    large = score_episode(inputs(installed_dc_power_kw=Decimal("800")))
    assert large.score == small.score + 15


def test_recurrence_below_three_never_scores() -> None:
    result = score_episode(inputs(recurrence_count_24h=2))
    assert not any("Recorrente" in reason for reason in result.reasons)


def test_recurrence_of_three_or_more_scores() -> None:
    base = score_episode(inputs(recurrence_count_24h=0))
    recurrent = score_episode(inputs(recurrence_count_24h=4))
    assert recurrent.score == base.score + 10
    assert any("4 episódios" in reason for reason in recurrent.reasons)


def test_a_work_order_already_in_place_removes_the_no_work_order_points() -> None:
    without = score_episode(inputs(has_work_order=False))
    withwo = score_episode(inputs(has_work_order=True))
    assert withwo.score == without.score - 15


def test_a_planned_visit_today_lowers_the_score_even_for_a_fault() -> None:
    open_ = score_episode(inputs(work_planned_or_in_progress_today=False))
    planned = score_episode(inputs(work_planned_or_in_progress_today=True))
    assert planned.score == open_.score - 30
    assert any("já planeado" in reason for reason in planned.reasons)


def test_a_recovered_episode_scores_lower_than_the_same_one_still_open() -> None:
    open_ = score_episode(inputs(recovered=False))
    recovered = score_episode(inputs(recovered=True))
    assert recovered.score == open_.score - 20


def test_a_small_epc_installation_already_planned_and_recovered_is_low() -> None:
    result = score_episode(
        inputs(
            problem_family="fault", severity_peak="warning", commercial_family="epc", om_status="active",
            installed_dc_power_kw=Decimal("20"), impact_eur=None, recurrence_count_24h=0, has_work_order=True,
            work_planned_or_in_progress_today=True, recovered=True,
        )
    )
    assert result.bucket == "LOW"


def test_a_large_esco_fault_with_material_impact_and_no_work_order_is_high() -> None:
    result = score_episode(
        inputs(
            problem_family="fault", severity_peak="critical", opened_at=utc(9), now=utc(14),
            commercial_family="esco", om_status="active", installed_dc_power_kw=Decimal("800"),
            impact_eur=Decimal("620"), recurrence_count_24h=0, has_work_order=False,
            work_planned_or_in_progress_today=False, recovered=False,
        )
    )
    assert result.bucket == "HIGH"
    # A small malfunction already planned must never outrank this, even
    # though both could independently be "critical" -- req 11.
    small_but_planned = score_episode(
        inputs(
            problem_family="fault", severity_peak="critical", opened_at=utc(9), now=utc(9, 30),
            installed_dc_power_kw=Decimal("10"), has_work_order=True, work_planned_or_in_progress_today=True,
        )
    )
    assert result.score > small_but_planned.score
