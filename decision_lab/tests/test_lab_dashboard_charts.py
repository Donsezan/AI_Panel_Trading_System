"""The period page's SVG: integer and `Decimal` coordinates only, and nothing that collides."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from decision_lab.dashboard import charts
from decision_lab.tests.test_discipline import SOURCES

NUMBER = re.compile(r' (?:x|y|x1|x2|y1|y2|cx|cy|width|height)="([^"]*)"')


def coordinates(svg: str) -> list[str]:
    return NUMBER.findall(svg)


def assert_fixed_point(svg: str) -> None:
    found = coordinates(svg)
    assert found, "the drawing has no coordinates; the check is vacuous"
    for value in found:
        assert re.fullmatch(r"-?\d+(\.\d)?", value), value


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2025, 2, day, hour, tzinfo=UTC)


def test_chart_output_has_no_float_module() -> None:
    """`charts.py` is walked by `test_discipline` like every module, and names no `float`."""
    walked = {path.name: tree for path, tree in SOURCES}
    assert "charts.py" in walked, "test_discipline must see charts.py"
    tree = walked["charts.py"]
    named = [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "float"
    ]
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "total_seconds"
    ]
    assert not named and not calls
    assert Path(charts.__file__).name == "charts.py"


def test_a_year_of_month_ends_draws_fixed_point_coordinates_and_twelve_labels() -> None:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    points = [
        charts.Point(label=m, value=Decimal(1000 + 7 * i) / 3, title=m, flag="⚠" if i == 1 else "")
        for i, m in enumerate(months)
    ]
    svg = charts.line_svg(points, width=960, height=210)
    assert_fixed_point(svg)
    assert all(f">{m}<" in svg for m in months)
    xs = [Decimal(v) for v in re.findall(r'<text x="([^"]+)" y="202', svg)]
    assert len(xs) == 12 and min(b - a for a, b in pairwise(xs)) >= 60


def test_a_missing_figure_breaks_the_line_and_is_not_drawn_as_zero() -> None:
    points = [
        charts.Point(label="Jan", value=Decimal(1000), title="a"),
        charts.Point(label="Feb", value=None, title="UNVALUABLE"),
        charts.Point(label="Mar", value=Decimal(1100), title="c"),
    ]
    svg = charts.line_svg(points)
    assert "<path" not in svg, "two lone points around a gap are not a line"
    assert svg.count("<circle") == 4 and "—" in svg


def test_lanes_grow_wide_enough_that_marks_never_overlap() -> None:
    marks = [
        charts.Mark(at=at(1) + timedelta(hours=4 * i), letter="B", grade="correct", title="t")
        for i in range(180)
    ]
    svg = charts.lanes_svg(
        [(at(1) + timedelta(hours=i), Decimal(100) + Decimal(i) / 7) for i in range(720)],
        {"seat-a": marks, "Panel": marks},
        [charts.Shade(start=at(10), end=at(12), direction="SHOCK_DOWN")],
    )
    assert_fixed_point(svg)
    rects = [Decimal(v) for v in re.findall(r'<rect x="([^"]+)" y="\d+" width="14"', svg)]
    first_lane = rects[:180]
    assert min(b - a for a, b in pairwise(first_lane)) >= 14
    days = re.findall(r">(Feb \d\d)<", svg)
    assert days and len(days) == len(set(days))
    assert "↓ spike down" in svg


def test_the_after_chart_draws_the_band_and_the_decision() -> None:
    prices = [(at(3, h), Decimal("96.1") - Decimal(h) / 10) for h in range(8)]
    svg = charts.after_svg(prices, band=(Decimal("94"), Decimal("98")), decision_at=at(3))
    assert_fixed_point(svg)
    assert 'class="band"' in svg and "decision" in svg


def test_empty_inputs_draw_nothing() -> None:
    assert charts.line_svg([]) == ""
    assert charts.lanes_svg([], {}, []) == ""
    assert charts.after_svg([], band=None, decision_at=at(1)) == ""


def test_text_is_escaped() -> None:
    svg = charts.line_svg([charts.Point(label="<b>", value=Decimal(1), title="<script>")])
    assert "<script>" not in svg and "&lt;b&gt;" in svg


def test_neighbouring_spike_labels_do_not_overprint() -> None:
    marks = [
        charts.Mark(at=at(1) + timedelta(days=i), letter="W", grade="none", title="t")
        for i in range(28)
    ]
    shades = [
        charts.Shade(start=at(day), end=at(day, 6), direction=("SHOCK_UP", "SHOCK_DOWN")[day % 2])
        for day in (3, 4, 5, 20)
    ]
    svg = charts.lanes_svg([], {"Panel": marks}, shades)
    xs = [Decimal(v) for v in re.findall(r'<text x="([^"]+)" y="14.0" class="strong"', svg)]
    assert len(xs) >= 2 and min(b - a for a, b in pairwise(xs)) >= 72
    assert svg.count("<rect") - 28 == 4, "every event is still shaded"
