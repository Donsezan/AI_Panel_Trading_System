"""Inline SVG for the period page: equity by month, one month's lanes, and one decision's after.

The bot's `dashboard/chart.py` admits floats as coordinates; this package admits none at all
(`test_discipline.py`, period spec §8), so every coordinate here is a `Decimal` or an `int`,
rendered as a fixed-point string. Time becomes an x position through `timedelta // timedelta`,
which is an `int`, never through `total_seconds()`, which is a float.

Every label and tooltip passes through `html.escape`: a seat id or an argument is operator or model
text, and an SVG is markup.

Failure semantics: pure functions over values in memory. No point, no price or a flat series
yields a smaller drawing that says so (or an empty string the template replaces with a sentence),
never an exception: a chart is an aid to a table that is always on the page beside it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Final

#: Per-grade fill and ink, as CSS variables the stylesheet defines in both colour schemes.
GRADE_COLOUR: Final[dict[str, str]] = {
    "correct": "var(--good)",
    "wrong": "var(--bad)",
    "unscored": "var(--muted)",
    "none": "var(--border)",
}
#: A spike's shading follows its labelled direction, never the sign of its move: a merged event
#: can retrace, so `move_pct` may disagree with the label it carries (spikes.py).
SPIKE_COLOUR: Final[dict[str, str]] = {"SHOCK_UP": "var(--good)", "SHOCK_DOWN": "var(--bad)"}
SPIKE_LABEL: Final[dict[str, str]] = {"SHOCK_UP": "↑ spike up", "SHOCK_DOWN": "↓ spike down"}

_TENTH: Final = Decimal("0.1")
_SECOND: Final = timedelta(seconds=1)
#: Narrowest slot a lane mark is drawn with its letter. Below it the letter would overprint its
#: neighbour, so the chart grows wider and scrolls instead (spec §4 item 6).
_SLOT: Final = 16
_MIN_LABEL_GAP: Final = 72


@dataclass(frozen=True, slots=True)
class Point:
    """One month end. `value` of `None` has no figure (unvaluable, or not run) and is a gap."""

    label: str
    value: Decimal | None
    title: str
    flag: str = ""


@dataclass(frozen=True, slots=True)
class Mark:
    """One vote on a lane: what was said (`letter`) and how it was graded."""

    at: datetime
    letter: str
    grade: str
    title: str


@dataclass(frozen=True, slots=True)
class Shade:
    """A spike event's span, shaded on the lanes."""

    start: datetime
    end: datetime
    direction: str


def fmt(value: Decimal | int) -> str:
    """A coordinate as fixed-point text: one decimal, never an exponent."""
    return format(Decimal(value).quantize(_TENTH), "f")


def _scale(value: Decimal, lo: Decimal, hi: Decimal, top: int, bottom: int) -> Decimal:
    """`value` in `[lo, hi]` onto `[bottom, top]` (SVG y grows down). A flat series sits mid-way."""
    if hi == lo:
        return Decimal(top + bottom) / 2
    return Decimal(bottom) - (value - lo) / (hi - lo) * Decimal(bottom - top)


def _text(x: Decimal | int, y: Decimal | int, body: str, extra: str = "") -> str:
    return f'<text x="{fmt(x)}" y="{fmt(y)}"{" " + extra if extra else ""}>{escape(body)}</text>'


def _path(points: Sequence[tuple[Decimal, Decimal]]) -> str:
    return " ".join(
        f"{'L' if index else 'M'}{fmt(x)} {fmt(y)}" for index, (x, y) in enumerate(points)
    )


def _short(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,}"


def line_svg(points: Sequence[Point], *, width: int = 960, height: int = 210) -> str:
    """Equity through the period, one point per month end; a missing figure breaks the line.

    Labels sit under every point, so twelve months need `width` to hold twelve short names: the
    caller passes three-letter months for a year, and `_MIN_LABEL_GAP` is the spacing they fit.
    """
    if not points:
        return ""
    left, right, top, bottom = 72, 24, 22, height - 32
    count = len(points)
    step = Decimal(width - left - right) / max(count - 1, 1)

    def x(index: int) -> Decimal:
        return Decimal(left) + step * index if count > 1 else Decimal(left + width - right) / 2

    known = [p.value for p in points if p.value is not None]
    lo, hi = (min(known), max(known)) if known else (Decimal(0), Decimal(0))
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" class="chart" '
        f'aria-label="Equity at each month end">'
    ]
    for value in dict.fromkeys((lo, hi)):
        y = _scale(value, lo, hi, top, bottom)
        parts.append(
            f'<line x1="{left}" x2="{width - right}" y1="{fmt(y)}" y2="{fmt(y)}" class="grid"/>'
        )
        parts.append(_text(4, y + 4, _short(value)))
    # A run of known values is one segment; an unvaluable or unrun month ends it rather than
    # drawing a line through a figure that does not exist.
    segment: list[tuple[Decimal, Decimal]] = []
    segments: list[list[tuple[Decimal, Decimal]]] = [segment]
    for index, point in enumerate(points):
        if point.value is None:
            segment = []
            segments.append(segment)
            continue
        segment.append((x(index), _scale(point.value, lo, hi, top, bottom)))
    for run in segments:
        if len(run) > 1:
            parts.append(f'<path d="{_path(run)}" class="line"/>')
    for index, point in enumerate(points):
        parts.append(_text(x(index), height - 8, point.label, 'text-anchor="middle"'))
        if point.value is None:
            parts.append(_text(x(index), (top + bottom) // 2, "—", 'text-anchor="middle"'))
            continue
        y = _scale(point.value, lo, hi, top, bottom)
        parts.append(
            f"<g><title>{escape(point.title)}</title>"
            f'<circle cx="{fmt(x(index))}" cy="{fmt(y)}" r="4" class="dot"/>'
            f'<circle cx="{fmt(x(index))}" cy="{fmt(y)}" r="12" fill="transparent"/></g>'
        )
        if point.flag:
            parts.append(_text(x(index), y - 9, point.flag, 'text-anchor="middle" class="strong"'))
    last = next(
        ((i, p.value) for i, p in reversed(list(enumerate(points))) if p.value is not None), None
    )
    if last is not None:
        where, final = last
        y = _scale(final, lo, hi, top, bottom)
        parts.append(_text(x(where) + 8, y + 16, _short(final), 'class="strong"'))
    parts.append("</svg>")
    return "".join(parts)


def _time_axis(start: datetime, end: datetime, left: int, right_edge: int) -> tuple[int, int]:
    """Span in seconds and drawable width, so `x(t)` is integer arithmetic over `Decimal`."""
    return max((end - start) // _SECOND, 1), right_edge - left


def lanes_svg(
    prices: Sequence[tuple[datetime, Decimal]],
    seats: Mapping[str, Sequence[Mark]],
    spikes: Sequence[Shade],
) -> str:
    """Price above one row per seat (and the panel), on one shared time axis (spec §4 item 6).

    The width grows with the busiest lane, so a month of 4-hour cycles scrolls rather than
    printing 180 letters on top of each other; day labels are thinned to `_MIN_LABEL_GAP`.
    """
    moments = [at for at, _ in prices] + [m.at for lane in seats.values() for m in lane]
    if not moments:
        return ""
    start, end = min(moments), max(moments)
    busiest = max((len(lane) for lane in seats.values()), default=0)
    left, right, top, price_bottom, lane_height = 96, 24, 18, 140, 30
    width = max(960, left + right + busiest * _SLOT)
    height = price_bottom + 18 + lane_height * len(seats) + 26
    span, drawable = _time_axis(start, end, left, width - right)

    def x(at: datetime) -> Decimal:
        return Decimal(left) + Decimal((at - start) // _SECOND) * drawable / span

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" class="chart lanes" '
        f'aria-label="Price above one row of votes per seat, on one time axis">'
    ]
    for shade in spikes:
        if shade.end < start or shade.start > end:
            continue
        x0, x1 = x(max(shade.start, start)), x(min(shade.end, end))
        colour = SPIKE_COLOUR.get(shade.direction, "var(--muted)")
        parts.append(
            f'<rect x="{fmt(x0 - 6)}" y="{top}" width="{fmt(x1 - x0 + 12)}" '
            f'height="{height - 22 - top}" fill="{colour}" opacity="0.12"/>'
        )
        parts.append(
            _text(
                x0 - 4, top - 4, SPIKE_LABEL.get(shade.direction, shade.direction), 'class="strong"'
            )
        )
    parts.append(_text(4, top + 10, "Price"))
    if prices:
        values = [value for _, value in prices]
        lo, hi = min(values), max(values)
        line = [(x(at), _scale(value, lo, hi, top, price_bottom)) for at, value in prices]
        parts.append(f'<path d="{_path(line)}" class="line"/>')
        parts.append(_text(4, top + 26, _short(hi)))
        parts.append(_text(4, price_bottom, _short(lo)))
    for row, (name, lane) in enumerate(seats.items()):
        middle = price_bottom + 18 + lane_height * row + lane_height // 2
        strong = ' class="strong"' if name == "Panel" else ""
        parts.append(f'<text x="4" y="{middle + 4}"{strong}>{escape(name)}</text>')
        for mark in lane:
            colour = GRADE_COLOUR.get(mark.grade, "var(--muted)")
            filled = mark.grade == "correct"
            cx = x(mark.at)
            parts.append(
                f"<g><title>{escape(mark.title)}</title>"
                f'<rect x="{fmt(cx - 7)}" y="{middle - 9}" width="14" height="18" rx="3" '
                f'fill="{colour if filled else "var(--bg)"}" stroke="{colour}"/>'
                f'<text x="{fmt(cx)}" y="{middle + 4}" text-anchor="middle" '
                f'style="fill:{"var(--bg)" if filled else colour};font-weight:700">'
                f"{escape(mark.letter)}</text></g>"
            )
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_label: Decimal | None = None
    while day <= end:
        if day >= start:
            here = x(day)
            if last_label is None or here - last_label >= _MIN_LABEL_GAP:
                parts.append(
                    f'<line x1="{fmt(here)}" x2="{fmt(here)}" y1="{top}" y2="{height - 22}" '
                    f'class="grid"/>'
                )
                parts.append(_text(here + 2, height - 6, day.strftime("%b %d")))
                last_label = here
        day += timedelta(days=1)
    parts.append("</svg>")
    return "".join(parts)


def after_svg(
    prices: Sequence[tuple[datetime, Decimal]],
    *,
    band: tuple[Decimal, Decimal] | None,
    decision_at: datetime,
) -> str:
    """Price from the decision through its horizon, with the grading band (spec §4 item 7)."""
    if not prices:
        return ""
    width, height, left, right, top, bottom = 560, 136, 72, 84, 12, 110
    values = [value for _, value in prices] + (list(band) if band else [])
    lo, hi = min(values), max(values)
    start, end = min(prices[0][0], decision_at), max(prices[-1][0], decision_at)
    span, drawable = _time_axis(start, end, left, width - right)

    def x(at: datetime) -> Decimal:
        return Decimal(left) + Decimal((at - start) // _SECOND) * drawable / span

    def y(value: Decimal) -> Decimal:
        return _scale(value, lo, hi, top, bottom)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" class="chart" '
        f'aria-label="Price after the decision, against the grading band">'
    ]
    decided = x(decision_at)
    if band is not None:
        low, high = band
        parts.append(
            f'<rect x="{fmt(decided)}" y="{fmt(y(high))}" width="{fmt(x(end) - decided)}" '
            f'height="{fmt(y(low) - y(high))}" class="band"/>'
        )
        parts.append(_text(x(end) + 6, y(high) + 4, _short(high)))
        parts.append(_text(x(end) + 6, y(low) + 4, _short(low)))
    parts.append(
        f'<line x1="{fmt(decided)}" x2="{fmt(decided)}" y1="{top}" y2="{bottom}" class="decision"/>'
    )
    parts.append(_text(decided - 24, height - 6, "decision"))
    parts.append(f'<path d="{_path([(x(at), y(value)) for at, value in prices])}" class="line"/>')
    parts.append(_text(4, y(prices[0][1]) + 4, _short(prices[0][1])))
    parts.append("</svg>")
    return "".join(parts)
