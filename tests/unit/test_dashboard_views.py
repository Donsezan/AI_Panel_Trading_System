"""The render filters. Display-only, and that is the point worth testing.

Every one of these formats a value for a human and feeds nothing. What they must never do is
lose an exact decimal on the way — the same numbers are read back out of forms on the Configure
page, so a filter that rounded through a float would put a rounded limit in the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.dashboard.views import ABSENT, fromjson, moment, money, prettyjson, quantity


@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        (Decimal("1234.5678"), 2, "1,234.57"),
        (Decimal("1234.5678"), 4, "1,234.5678"),
        (Decimal("0"), 2, "0.00"),
        (Decimal("-12.5"), 2, "-12.50"),
        ("10.5", 2, "10.50"),
        (10, 2, "10.00"),
    ],
)
def test_money_formats_at_a_fixed_width(value: object, places: int, expected: str) -> None:
    assert money(value, places) == expected  # type: ignore[arg-type]


def test_money_absent_is_not_zero() -> None:
    """A blank cell must never read as a zero balance."""
    assert money(None) == ABSENT


def test_money_refuses_to_lose_an_unquantizable_value() -> None:
    """Too large to round is shown in full: an unwieldy number beats a wrong one."""
    enormous = Decimal("1" + "0" * 40)
    assert money(enormous) == str(enormous)


def test_money_never_returns_a_float() -> None:
    assert isinstance(money(Decimal("1.005")), str)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(Decimal("0.00001000"), "0.00001"), (Decimal("2.500"), "2.5"), (None, ABSENT)],
)
def test_quantity_keeps_full_precision(value: Decimal | None, expected: str) -> None:
    """A lot size can be 0.00001; truncating a holding lies about what is held."""
    assert quantity(value) == expected


def test_moment_renders_utc() -> None:
    assert moment(datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)) == "2026-03-01 12:30:45"


def test_moment_absent() -> None:
    assert moment(None) == ABSENT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('["a", "b"]', ["a", "b"]), ("[]", []), (None, None), ("", None), ("not json", None)],
)
def test_fromjson_never_raises_into_a_page(raw: str | None, expected: object) -> None:
    """A malformed projection column must not take down the audit view around it."""
    assert fromjson(raw) == expected


def test_prettyjson_keeps_a_decimal_as_its_digits() -> None:
    """`default=str`, so an exact price survives the snapshot view as text, not as a float."""
    assert '"50000.01"' in prettyjson({"price": Decimal("50000.01")})
