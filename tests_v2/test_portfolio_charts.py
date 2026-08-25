"""Portfolio charts, built from the report's own frozen rows.

A chart that disagreed with the table beside it would be worse than no chart,
so the ranking and the balance read the dataset the report already produced
rather than querying again. Only the twelve-month trend needs facts of its own,
because no single period's dataset contains them.
"""
from __future__ import annotations

from nemsei.web.series import portfolio_balance, ranked_installations


def test_the_ranking_orders_by_production_and_keeps_gaps_as_gaps() -> None:
    result = ranked_installations(
        [
            {"asset_id": 1, "name": "Pequena", "production_kwh": 10},
            {"asset_id": 2, "name": "Grande", "production_kwh": 900},
            {"asset_id": 3, "name": "Sem medição", "production_kwh": None},
            {"asset_id": None, "name": "Membro por resolver", "production_kwh": None},
        ]
    )

    bars = result["chart"].bars
    assert [bar.point.value for bar in bars[:2]] == [900.0, 10.0]
    # A plant that reported nothing is not a plant that produced nothing.
    assert bars[-1].point.missing is True
    # A membership with no installation is not an installation.
    assert result["total"] == 3


def test_a_long_installation_name_is_truncated_for_the_axis_but_not_the_hint() -> None:
    result = ranked_installations([{"asset_id": 1, "name": "Queijaria Lourenço & Filhos", "production_kwh": 5}])

    bar = result["chart"].bars[0]
    assert bar.point.label.endswith("…")
    assert "Queijaria Lourenço & Filhos" in bar.point.hint


def test_the_balance_reads_the_frozen_totals() -> None:
    result = portfolio_balance(
        {
            "production": {"value": 1000},
            "self_use": {"value": 600},
            "export": {"value": 400},
            "grid_import": {"value": 250},
        }
    )

    assert abs(result["self_use_share"] - 0.6) < 0.001
    assert result["stack"].empty is False
    assert [round(segment.share, 2) for segment in result["stack"].segments] == [0.6, 0.4, 0.71, 0.29]


def test_a_portfolio_with_no_measured_production_says_so() -> None:
    result = portfolio_balance({"production": {"value": None}, "self_use": {"value": None}})

    assert result["self_use_share"] is None
    assert result["stack"].empty is True


def test_an_empty_portfolio_produces_an_empty_chart_not_a_crash() -> None:
    assert ranked_installations([])["chart"].empty is True
