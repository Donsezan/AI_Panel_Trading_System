"""Current prices for everything the portfolio holds, in the notional currency.

This is a **cache, not a ledger**. Nothing here may adjust a position, a balance or a baseline,
and it has no write path to the database. It is shared mutable state read on the money path, and
the staleness rule below is the only thing keeping it honest.

**A stale mark is not a mark.** `price_of` returns `None` for a key that is absent or older than
the caller's tolerance, and there is no third outcome. Valuing a position at a four-hour-old price
is not more conservative than valuing it at cost — it is differently wrong, in whichever direction
the market moved, and the fallback-to-cost it replaces is the entire mechanism of the drawdown
defect this phase exists to fix (PHASE_12 Finding 1, §1.4).

Instrument marks and currency marks share one map. Instrument keys are `venue:symbol` and always
carry a colon; currency codes never do, so the two cannot collide. A future key format without a
colon would break that, which is why it is stated here rather than left to be noticed.

Failure semantics: this module has no dependencies and cannot fail from the outside. An unobserved
key reads as unknown, which its callers resolve to a frozen aggregate — never to a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from tradebot.core.market import Quote
from tradebot.core.money import refuse_float
from tradebot.core.schema import UtcDatetime


@dataclass(frozen=True, slots=True)
class Mark:
    """One observed price, and when it was observed."""

    price: Decimal
    observed_at: UtcDatetime


class Marks:
    """Instrument and currency prices, in the notional currency, with their ages."""

    def __init__(self) -> None:
        self._marks: dict[str, Mark] = {}

    def observe(self, key: str, price: Decimal, at: UtcDatetime) -> None:
        """Record a price. A later observation replaces an earlier one."""
        refuse_float(price)
        self._marks[key] = Mark(price=price, observed_at=at)

    def observe_quote(self, quote: Quote) -> None:
        """Record a quote's last trade under its own instrument key."""
        self.observe(quote.instrument_key, quote.last, quote.observed_at)

    def price_of(self, key: str, *, now: UtcDatetime, tolerance: timedelta) -> Decimal | None:
        """The current mark, or `None` if it is absent or stale. Never a fallback."""
        mark = self._marks.get(key)
        if mark is None or now - mark.observed_at > tolerance:
            return None
        return mark.price

    def age_of(self, key: str, *, now: UtcDatetime) -> timedelta | None:
        """How old this mark is, for an operator to read. `None` when there is none."""
        mark = self._marks.get(key)
        return None if mark is None else now - mark.observed_at

    def keys(self) -> frozenset[str]:
        return frozenset(self._marks)
