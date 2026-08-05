"""Pydantic base types that enforce the money and time rules at the model boundary.

Two holes exist in a naive pydantic model of a trading system, and both are closed here:

* pydantic coerces `float` to `Decimal` silently, which would let a binary rounding error into
  a price through the front door. `Money` refuses `float` outright.
* JSON has no decimal type, so a round-trip through the event log would go via `float` unless
  `Decimal` is serialized as a *string*. `Money` serializes to a string and parses back exactly.

All domain models are frozen. Immutability is what makes "the snapshot the panel saw" a
truthful claim rather than an aspiration (PLAN §4).

Failure semantics: a `float` in a money field, or a naive datetime, raises immediately rather
than being collected as a validation error. Both are programming defects that must be loud.
Unreadable *text* in a money field is the opposite — an operator typed it — and is collected as
an ordinary validation error located on its field (`parse_money`).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from tradebot.core.clock import ensure_utc
from tradebot.core.errors import MoneyError
from tradebot.core.money import refuse_float, to_decimal


def parse_money(value: Any) -> Decimal:
    """`to_decimal`, with its two failures separated by who caused them.

    pydantic converts only `ValueError` into a located `ValidationError`; anything else escapes
    the model. `MoneyError` is not a `ValueError`, so text that is not a number — an operator
    typing `0,5` into a limit — used to leave the dashboard's form handler as an unhandled
    exception and reach the browser as a 500, losing the draft and naming no field. It is
    ordinary bad input and belongs on the field it was typed in.

    A `float` still raises `MoneyError` unconverted: nothing but our own code can put one here,
    and a binary rounding error listed among a form's validation messages is a defect made to
    look like a typo.
    """
    refuse_float(value)
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc


#: A money-semantic quantity: `Decimal` in Python, a string in JSON, never a `float`.
Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    PlainSerializer(str, return_type=str, when_used="json"),
]

#: A timezone-aware UTC instant. Naive input is rejected, not assumed to be UTC.
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]


class DomainModel(BaseModel):
    """Base for every persisted or transported domain object."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing and for the event log.

    Keys are sorted so that two structurally identical snapshots hash identically regardless of
    construction order — the property that makes a snapshot hash usable as replay evidence.
    """
    if isinstance(value, BaseModel):
        value = json.loads(value.model_dump_json())
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
