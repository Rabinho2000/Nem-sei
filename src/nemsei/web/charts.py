"""Chart geometry, computed in Python so the templates only draw.

Every chart in this application is an inline SVG rendered by the server. There
is no charting library and no JavaScript: the same markup that reaches a
browser also survives into a PDF, and a page that draws itself cannot fall out
of sync with the numbers beside it.

Keeping the arithmetic here rather than in Jinja is what makes it testable --
a bar's height is a function with a unit test, not an expression buried in a
template.

The one rule every chart in this file obeys: **a value that does not exist is
not a zero**. A day with no reading is drawn as a gap, never as a bar of height
zero, because the difference between "produced nothing" and "we have no idea"
is the single most important distinction this platform has to make.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

Number = Decimal | float | int | None


@dataclass(frozen=True)
class Point:
    """One column of a series: a value, or the honest absence of one."""

    label: str
    value: float | None
    # How much of what this column covers actually reported, 0.0-1.0. For a
    # month it is days with data over days in the month; for a portfolio, plants
    # reporting over plants expected. `None` where coverage is not meaningful.
    coverage: float | None = None
    hint: str = ""

    @property
    def missing(self) -> bool:
        return self.value is None


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    point: Point
    delay: float
    coverage_y: float = 0.0
    coverage_height: float = 0.0


@dataclass(frozen=True)
class BarChart:
    width: int
    height: int
    bars: list[Bar]
    gridlines: list[tuple[float, str]]
    label_y: float
    coverage_top: float
    coverage_height: float
    has_coverage: bool
    empty: bool
    unit: str = ""


def _nice_ceiling(value: float) -> float:
    """A round number at or above `value`, so the top gridline reads cleanly."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        candidate = magnitude * step
        if candidate >= value:
            return float(candidate)
    return float(magnitude * 10)


def _format(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}"


def bar_chart(
    points: list[Point],
    *,
    width: int = 720,
    plot_height: int = 150,
    coverage_height: int = 18,
    unit: str = "",
) -> BarChart:
    """A column per point, with the coverage strip that qualifies it.

    The coverage strip sits below the bars rather than inside them: a total and
    the confidence in that total are two facts, and drawing one on top of the
    other would make a partial month look like a small month.
    """
    pad_left, pad_right, pad_top = 46, 10, 12
    gap = 14
    plot_bottom = pad_top + plot_height
    has_coverage = any(point.coverage is not None for point in points)
    coverage_top = plot_bottom + gap
    label_y = (coverage_top + coverage_height if has_coverage else plot_bottom) + 15
    height = int(label_y + 8)

    if not points:
        return BarChart(width, height, [], [], label_y, coverage_top, coverage_height, has_coverage, True, unit)

    values = [point.value for point in points if point.value is not None]
    top = _nice_ceiling(max(values)) if values else 1.0
    inner = width - pad_left - pad_right
    slot = inner / len(points)
    bar_width = max(2.0, slot * 0.58)

    gridlines = [
        (plot_bottom - fraction * plot_height, _format(top * fraction))
        for fraction in (0.0, 0.5, 1.0)
    ]

    bars: list[Bar] = []
    for index, point in enumerate(points):
        centre = pad_left + slot * index + slot / 2
        x = centre - bar_width / 2
        delay = round(min(index * 0.035, 1.2), 3)
        if point.missing:
            bar = Bar(x, pad_top, bar_width, plot_height, point, delay)
        else:
            drawn = (point.value / top) * plot_height if top else 0.0
            # A real but tiny value still gets a visible mark: rounding it away
            # would read as missing, which is the one thing it is not.
            drawn = max(drawn, 1.5) if point.value > 0 else 1.0
            bar = Bar(x, plot_bottom - drawn, bar_width, drawn, point, delay)
        if point.coverage is not None:
            filled = coverage_height * max(0.0, min(1.0, point.coverage))
            bar = Bar(
                bar.x, bar.y, bar.width, bar.height, point, delay,
                coverage_y=coverage_top + coverage_height - filled,
                coverage_height=filled,
            )
        bars.append(bar)

    return BarChart(width, height, bars, gridlines, label_y, coverage_top, coverage_height, has_coverage, False, unit)


@dataclass(frozen=True)
class DualBarChart:
    width: int
    height: int
    production_bars: list[Bar]
    consumption_bars: list[Bar]
    gridlines: list[tuple[float, str]]
    label_y: float
    empty: bool
    unit: str = ""


def dual_bar_chart(
    production: list[Point],
    consumption: list[Point],
    *,
    width: int = 720,
    plot_height: int = 150,
    unit: str = "",
) -> DualBarChart:
    """Produção e consumo, mesmos períodos, uma escala partilhada.

    One shared y-axis, on purpose: comparing the two at independently scaled
    axes would misstate which one is bigger, the same reason `stacked_bars`
    compares shares rather than two independently-scaled absolutes.
    `production` and `consumption` must describe the same periods in the same
    order -- this function does not align them, the caller's query already
    built both from the same period boundaries (`web/series.py` calls
    `daily_series`/`monthly_series` twice, once per `metric_kind`, over the
    same date range).

    Missing stays a gap here exactly as in `bar_chart`: a period with no
    production reading and a period with no consumption reading are drawn
    independently, so a plant with production data but no consumption meter
    still shows real production bars beside honest gaps, never zeros.
    """
    if len(production) != len(consumption):
        raise ValueError("production and consumption must describe the same periods")
    pad_left, pad_right, pad_top = 46, 10, 12
    plot_bottom = pad_top + plot_height
    label_y = plot_bottom + 15
    height = int(label_y + 8)

    if not production:
        return DualBarChart(width, height, [], [], [], label_y, True, unit)

    values = [point.value for series in (production, consumption) for point in series if point.value is not None]
    top = _nice_ceiling(max(values)) if values else 1.0
    inner = width - pad_left - pad_right
    slot = inner / len(production)
    pair_width = min(66.0, slot * 0.7)
    bar_width = max(2.0, pair_width / 2 - 1.0)

    gridlines = [
        (plot_bottom - fraction * plot_height, _format(top * fraction))
        for fraction in (0.0, 0.5, 1.0)
    ]

    def make_bar(point: Point, x: float, delay: float) -> Bar:
        if point.missing:
            return Bar(x, pad_top, bar_width, plot_height, point, delay)
        drawn = (point.value / top) * plot_height if top else 0.0
        drawn = max(drawn, 1.5) if point.value > 0 else 1.0
        return Bar(x, plot_bottom - drawn, bar_width, drawn, point, delay)

    production_bars: list[Bar] = []
    consumption_bars: list[Bar] = []
    for index, (production_point, consumption_point) in enumerate(zip(production, consumption)):
        centre = pad_left + slot * index + slot / 2
        delay = round(min(index * 0.035, 1.2), 3)
        production_bars.append(make_bar(production_point, centre - pair_width / 2, delay))
        consumption_bars.append(make_bar(consumption_point, centre - pair_width / 2 + bar_width + 2.0, delay))

    return DualBarChart(width, height, production_bars, consumption_bars, gridlines, label_y, False, unit)


@dataclass(frozen=True)
class Sparkline:
    width: int
    height: int
    line: str
    area: str
    last_x: float
    last_y: float
    empty: bool


def sparkline(values: list[float | None], *, width: int = 104, height: int = 28) -> Sparkline:
    """A shape, not a chart: no axes, no labels, just the direction of travel.

    Gaps break the line rather than interpolating across them. A straight
    segment over a week with no data would invent a trend nobody measured.
    """
    known = [value for value in values if value is not None]
    if len(known) < 2:
        return Sparkline(width, height, "", "", 0.0, 0.0, True)
    low, high = min(known), max(known)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    top, bottom = 3.0, height - 3.0

    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        if value is None:
            if len(current) > 1:
                runs.append(current)
            current = []
            continue
        x = index * step
        y = bottom - ((value - low) / span) * (bottom - top)
        current.append((x, y))
    if len(current) > 1:
        runs.append(current)
    if not runs:
        return Sparkline(width, height, "", "", 0.0, 0.0, True)

    def path(points: list[tuple[float, float]]) -> str:
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)

    line = " ".join(path(run) for run in runs)
    # The fill follows the last unbroken run only. Closing it across a gap
    # would shade an area under data that was never recorded.
    tail = runs[-1]
    area = f"{path(tail)} L{tail[-1][0]:.1f},{height} L{tail[0][0]:.1f},{height} Z"
    last_x, last_y = tail[-1]
    return Sparkline(width, height, line, area, last_x, last_y, False)


@dataclass(frozen=True)
class CalendarCell:
    day: int
    x: float
    y: float
    level: int
    value: float | None
    delay: float


@dataclass(frozen=True)
class CoverageCalendar:
    width: int
    height: int
    cells: list[CalendarCell]
    weekday_labels: list[tuple[float, str]]
    cell_size: int
    empty: bool


def coverage_calendar(
    values: dict[int, float | None],
    *,
    year: int,
    month: int,
    cell: int = 24,
    gap: int = 4,
) -> CoverageCalendar:
    """One square per day of a month, shaded by how much that day reported.

    Four levels, not a continuous ramp: an operator needs to tell "nothing",
    "barely", "partial" and "complete" apart at a glance, and a smooth gradient
    makes the first two look alike.
    """
    from calendar import monthrange

    first_weekday, days_in_month = monthrange(year, month)
    known = [value for value in values.values() if value is not None]
    top = max(known) if known else 0.0
    columns = 7
    width = 22 + columns * (cell + gap)
    rows = ((first_weekday + days_in_month - 1) // columns) + 1
    height = 18 + rows * (cell + gap)

    cells: list[CalendarCell] = []
    for day in range(1, days_in_month + 1):
        index = first_weekday + day - 1
        row, column = divmod(index, columns)
        value = values.get(day)
        if value is None:
            level = 0
        elif top <= 0:
            level = 1
        else:
            ratio = value / top
            level = 1 if ratio <= 0.05 else 2 if ratio <= 0.4 else 3 if ratio <= 0.8 else 4
        cells.append(
            CalendarCell(
                day=day,
                x=22 + column * (cell + gap),
                y=18 + row * (cell + gap),
                level=level,
                value=value,
                delay=round(day * 0.012, 3),
            )
        )
    labels = [(22 + index * (cell + gap) + cell / 2, name) for index, name in enumerate(("S", "T", "Q", "Q", "S", "S", "D"))]
    return CoverageCalendar(width, height, cells, labels, cell, not known)


@dataclass(frozen=True)
class StackSegment:
    x: float
    y: float
    width: float
    height: float
    label: str
    value: float
    share: float
    series: int
    delay: float
    show_label: bool


@dataclass(frozen=True)
class StackedBars:
    width: int
    height: int
    segments: list[StackSegment]
    labels: list[tuple[float, str]]
    baseline: float
    empty: bool


def stacked_bars(
    columns: list[tuple[str, list[tuple[str, float, int]]]],
    *,
    width: int = 300,
    plot_height: int = 132,
) -> StackedBars:
    """Two or three columns, each split into named parts of its own total.

    Shares, not absolutes: the point of an energy balance is what fraction of
    production was self-used, and columns of different totals compared by height
    answer a question nobody asked.
    """
    baseline = 18 + plot_height
    usable = [column for column in columns if sum(value for _, value, _ in column[1]) > 0]
    if not usable:
        return StackedBars(width, int(baseline + 26), [], [], baseline, True)

    slot = width / len(usable)
    bar_width = min(66.0, slot * 0.5)
    segments: list[StackSegment] = []
    labels: list[tuple[float, str]] = []
    for index, (name, parts) in enumerate(usable):
        centre = slot * index + slot / 2
        total = sum(value for _, value, _ in parts) or 1.0
        accumulated = 0.0
        for order, (part_label, value, series) in enumerate(parts):
            share = value / total
            height = share * plot_height
            y = baseline - ((accumulated + value) / total) * plot_height
            segments.append(
                StackSegment(
                    x=centre - bar_width / 2,
                    y=y,
                    width=bar_width,
                    # A 2px gap between stacked parts, so adjacent fills read as
                    # two segments rather than one shape that changed colour.
                    height=max(height - 2, 1.0),
                    label=part_label,
                    value=value,
                    share=share,
                    series=series,
                    delay=round((index * 2 + order) * 0.08, 3),
                    show_label=height > 24,
                )
            )
            accumulated += value
        labels.append((centre, name))
    return StackedBars(width, int(baseline + 26), segments, labels, baseline, False)
