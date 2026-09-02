"""The adapter seam: a minimal stand-in, never a second priority engine.

These tests pin the current, deliberately temporary behaviour so a future
swap to the real shared `operational_priority` engine has something concrete
to diff against, not just a vague sense the ranking "changed".
"""
from __future__ import annotations

from nemsei.web.operational_priority import installation_priority


def test_a_real_fault_at_a_prioritized_installation_ranks_first() -> None:
    result = installation_priority(real_fault_count=1, communication_count=0, commercial_priority="high")
    assert result.rank == 0


def test_a_real_fault_at_a_normal_installation_ranks_below_a_prioritized_one() -> None:
    prioritized = installation_priority(real_fault_count=1, communication_count=0, commercial_priority="high")
    normal = installation_priority(real_fault_count=1, communication_count=0, commercial_priority="normal")
    assert normal.rank > prioritized.rank


def test_a_real_fault_always_outranks_a_communication_problem() -> None:
    fault = installation_priority(real_fault_count=1, communication_count=5, commercial_priority="normal")
    communication_only = installation_priority(real_fault_count=0, communication_count=1, commercial_priority="high")
    assert fault.rank < communication_only.rank


def test_no_confirmed_problem_ranks_last() -> None:
    result = installation_priority(real_fault_count=0, communication_count=0, commercial_priority="high")
    assert result.rank == 4


def test_coverage_gaps_play_no_part_in_the_ranking_at_all() -> None:
    """The signature does not even accept a coverage count -- there is
    nothing to pass, on purpose. A pile of monitoring gaps must never move an
    installation up the attention list."""
    import inspect

    parameters = set(inspect.signature(installation_priority).parameters)
    assert "coverage_count" not in parameters
    assert not any("coverage" in name for name in parameters)


def test_every_result_carries_a_human_reason() -> None:
    for real_faults in (0, 1):
        for communication in (0, 1):
            for priority in ("high", "normal", "low"):
                result = installation_priority(
                    real_fault_count=real_faults, communication_count=communication, commercial_priority=priority
                )
                assert result.reason
