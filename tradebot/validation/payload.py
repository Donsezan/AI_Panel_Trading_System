"""Reading event payloads defensively, in the one way every report reads them.

A report is built from JSON that a past version of this system wrote. Field names drift, payloads
gain keys, and a database that has been soaking for weeks contains events from more than one
build. So every accessor here answers with an empty value rather than raising: a report missing
one number is more useful than a report that cannot be produced, and every gate treats a missing
number as a failure to prove the gate rather than as a pass (`promotion.py`).

Shared by `evidence.py` and `comparison.py` so the two folds cannot disagree about what a payload
key means — the kind of divergence that shows up as two reports quoting different numbers for the
same soak.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradebot.core.events import Event
from tradebot.core.money import ZERO, to_decimal


def text(event: Event, key: str) -> str:
    value = event.payload.get(key)
    return "" if value is None else str(value)


def nested(event: Event, container: str, key: str) -> Any:
    payload = event.payload.get(container)
    return payload.get(key) if isinstance(payload, dict) else None


def money(event: Event, key: str) -> Decimal:
    value = event.payload.get(key)
    return to_decimal(value) if value is not None else ZERO


def rows(event: Event, key: str) -> tuple[dict[str, Any], ...]:
    """A list-of-objects payload field. Anything else in the list is skipped, not guessed at."""
    value = event.payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))
