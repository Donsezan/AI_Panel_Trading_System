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
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from tradebot.core.clock import ensure_utc
from tradebot.core.money import to_decimal

#: A money-semantic quantity: `Decimal` in Python, a string in JSON, never a `float`.
Money = Annotated[
    Decimal,
    BeforeValidator(to_decimal),
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
