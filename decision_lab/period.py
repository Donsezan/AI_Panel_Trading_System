"""A period is a calendar month, quarter or year, read as UTC (period-evaluation spec).

The unit an operator names ("2025-Q2") and the unit the tool replays are the same thing: a
half-open `[start, end)` span that decomposes into whole months, so a quarter's result is the
three months it holds rather than a separate measurement.

Failure semantics: nothing here touches the network or disk. Text that is not `YYYY`, `YYYY-MM`
or `YYYY-Qn` refuses with `ConfigError` naming what was typed; a period the recorded history does
not fully cover refuses naming the span that exists. Fail closed: a partly covered period is never
offered, because a result over a subset the tool chose would be reported as the whole.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel, UtcDatetime

_PATTERN: Final = re.compile(r"^(\d{4})(?:-(?:(0[1-9]|1[0-2])|Q([1-4])))?$")
_BAR: Final = timedelta(hours=1)


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _next_month(year: int, month: int) -> datetime:
    return datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)


class Period(DomainModel):
    """A labelled half-open span; `end` is exclusive."""

    label: str
    start: UtcDatetime
    end: UtcDatetime

    @property
    def months(self) -> tuple[Period, ...]:
        out: list[Period] = []
        year, month = self.start.year, self.start.month
        while (here := _month_start(year, month)) < self.end:
            out.append(
                Period(label=f"{year:04d}-{month:02d}", start=here, end=_next_month(year, month))
            )
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return tuple(out)


def _span(label: str, year: int, first_month: int, last_month: int) -> Period:
    return Period(
        label=label,
        start=_month_start(year, first_month),
        end=_next_month(year, last_month),
    )


def parse_period(text: str) -> Period:
    match = _PATTERN.match(text)
    if match is None:
        raise ConfigError(
            f"unreadable period {text!r}: expected YYYY, YYYY-MM or YYYY-Qn (for example 2025-Q2)"
        )
    year = int(match.group(1))
    if match.group(2):
        month = int(match.group(2))
        return _span(text, year, month, month)
    if match.group(3):
        first = (int(match.group(3)) - 1) * 3 + 1
        return _span(text, year, first, first + 2)
    return _span(text, year, 1, 12)


def _is_covered(period: Period, first: datetime, last: datetime) -> bool:
    # `last` is the final bar's open time: the period is whole when that bar is its last hour.
    return first <= period.start and last >= period.end - _BAR


def covered_periods(first: datetime, last: datetime) -> dict[str, tuple[str, ...]]:
    """Every month, quarter and year the history `[first, last]` covers in full."""
    offered: dict[str, list[str]] = {"month": [], "quarter": [], "year": []}
    for year in range(first.year, last.year + 1):
        if _is_covered(_span(str(year), year, 1, 12), first, last):
            offered["year"].append(str(year))
        for quarter in range(1, 5):
            label = f"{year}-Q{quarter}"
            if _is_covered(parse_period(label), first, last):
                offered["quarter"].append(label)
        for month in range(1, 13):
            label = f"{year}-{month:02d}"
            if _is_covered(parse_period(label), first, last):
                offered["month"].append(label)
    return {kind: tuple(labels) for kind, labels in offered.items()}


def require_covered(period: Period, first: datetime, last: datetime) -> None:
    if not _is_covered(period, first, last):
        raise ConfigError(
            f"period {period.label} is not fully covered: recorded history runs "
            f"{first.date().isoformat()} to {last.date().isoformat()}"
        )
