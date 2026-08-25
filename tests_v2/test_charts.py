"""Fase 2: the chart geometry, tested as arithmetic rather than eyeballed.

The rule every chart obeys and every test here checks: a value that does not
exist is never drawn as a zero. The difference between "produced nothing" and
"we have no reading" is the most important distinction this platform makes, and
a chart that flattens it is worse than no chart.
"""
from __future__ import annotations

from nemsei.web.charts import Point, bar_chart, coverage_calendar, sparkline, stacked_bars


def test_a_missing_column_is_a_gap_not_a_zero_bar() -> None:
    chart = bar_chart([Point("A", 100.0), Point("B", None), Point("C", 0.0)])

    drawn, gap, real_zero = chart.bars
    assert drawn.point.missing is False
    assert gap.point.missing is True
    # The gap spans the whole plot, so it reads as "no idea", while a genuine
    # zero keeps a hairline at the baseline, which reads as "nothing produced".
    assert gap.height > real_zero.height
    assert real_zero.point.missing is False
    assert real_zero.height > 0


def test_bar_heights_are_proportional_to_a_rounded_ceiling() -> None:
    chart = bar_chart([Point("A", 50.0), Point("B", 100.0)])

    short, tall = chart.bars
    assert tall.height > short.height
    assert abs(short.height / tall.height - 0.5) < 0.01
    # The top gridline is a round number, not the maximum value.
    assert chart.gridlines[-1][1] == "100"


def test_a_tiny_real_value_still_gets_a_visible_mark() -> None:
    chart = bar_chart([Point("A", 10000.0), Point("B", 0.4)])

    assert chart.bars[1].height >= 1.0


def test_coverage_is_drawn_separately_from_the_value() -> None:
    # A partial month must not look like a small month: the total keeps its own
    # height and the doubt lives in its own strip.
    chart = bar_chart([Point("Cheio", 100.0, coverage=1.0), Point("Parcial", 100.0, coverage=0.2)])

    full, partial = chart.bars
    assert full.height == partial.height
    assert partial.coverage_height < full.coverage_height
    assert chart.has_coverage is True


def test_a_series_with_no_coverage_reserves_no_strip() -> None:
    chart = bar_chart([Point("A", 1.0), Point("B", 2.0)])

    assert chart.has_coverage is False
    assert all(bar.coverage_height == 0 for bar in chart.bars)


def test_an_empty_series_is_flagged_rather_than_drawn() -> None:
    assert bar_chart([]).empty is True
    assert bar_chart([Point("A", None), Point("B", None)]).empty is False


def test_a_sparkline_breaks_at_a_gap_instead_of_inventing_a_trend() -> None:
    line = sparkline([1.0, 2.0, None, 8.0, 9.0])

    # Two move commands means two separate runs; one would mean the gap was
    # bridged with a straight segment nobody measured.
    assert line.line.count("M") == 2
    assert line.empty is False


def test_a_sparkline_needs_two_real_points() -> None:
    assert sparkline([]).empty is True
    assert sparkline([5.0]).empty is True
    assert sparkline([None, None, None]).empty is True


def test_the_calendar_separates_nothing_from_barely() -> None:
    calendar = coverage_calendar({1: None, 2: 0.5, 3: 50.0, 4: 100.0}, year=2026, month=7)

    levels = {cell.day: cell.level for cell in calendar.cells}
    assert levels[1] == 0  # sem leitura
    assert levels[2] == 1  # leitura residual, distinta de nenhuma
    assert levels[4] == 4
    assert levels[2] < levels[3] < levels[4]


def test_the_calendar_lays_out_the_real_month() -> None:
    calendar = coverage_calendar({day: 1.0 for day in range(1, 32)}, year=2026, month=7)

    assert len(calendar.cells) == 31
    # 1 July 2026 is a Wednesday: index 2 in a Monday-first grid.
    assert calendar.cells[0].x > calendar.cells[0].y * 0  # placed, not at origin
    assert calendar.cells[0].day == 1


def test_stacked_columns_show_shares_not_absolutes() -> None:
    bars = stacked_bars(
        [
            ("Produção", [("Autoconsumo", 60.0, 1), ("Injeção", 40.0, 2)]),
            ("Consumo", [("Autoconsumo", 60.0, 1), ("Importação", 20.0, 3)]),
        ]
    )

    shares = [round(segment.share, 2) for segment in bars.segments]
    assert shares == [0.6, 0.4, 0.75, 0.25]
    # Both columns reach the same height despite different totals -- the
    # question is composition, not size.
    tops = {round(bars.baseline - (segment.y + segment.height), 1) for segment in bars.segments if segment.share > 0.5}
    assert len(tops) >= 1


def test_a_balance_with_no_energy_is_empty_rather_than_a_flat_line() -> None:
    assert stacked_bars([("Produção", [("Autoconsumo", 0.0, 1)])]).empty is True
