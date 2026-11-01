from datetime import UTC, datetime

import pytest

from decision_lab.period import covered_periods, parse_period, require_covered
from tradebot.core.errors import ConfigError


def utc(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def test_a_month_is_one_month() -> None:
    p = parse_period("2025-03")
    assert (p.start, p.end) == (utc(2025, 3, 1), utc(2025, 4, 1))
    assert [m.label for m in p.months] == ["2025-03"]


def test_a_quarter_holds_three_months() -> None:
    assert [m.label for m in parse_period("2025-Q2").months] == ["2025-04", "2025-05", "2025-06"]


def test_a_year_holds_twelve_and_december_ends_at_new_year() -> None:
    p = parse_period("2025")
    assert len(p.months) == 12 and p.end == utc(2026, 1, 1)


@pytest.mark.parametrize("bad", ["2025-13", "2025-Q5", "25", "2025-3", "", "last year"])
def test_an_unreadable_period_refuses(bad: str) -> None:
    with pytest.raises(ConfigError, match=bad or "period"):
        parse_period(bad)


def test_only_fully_covered_periods_are_offered() -> None:
    offered = covered_periods(utc(2024, 1, 1), utc(2024, 6, 30, 23))
    assert offered["year"] == ()
    assert offered["quarter"] == ("2024-Q1", "2024-Q2")
    assert offered["month"][-1] == "2024-06"


def test_require_covered_names_what_exists() -> None:
    with pytest.raises(ConfigError, match="2024-01-01"):
        require_covered(parse_period("2024"), utc(2024, 1, 1), utc(2024, 6, 30, 23))
