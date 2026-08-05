"""What the operator has selected in the blotter, as it travels in the URL.

Selection drives the chart, the operation log and the scoped controls, so it must survive a
reload, a bookmark and a socket-triggered refresh. It therefore lives in the query string
(`/?scope=…`) and never in JavaScript — a pane refreshed by htmx re-reads the same URL the
operator can see, so there is one answer to "what is selected" rather than two that can drift.

The encoding is `<kind>:<rest>`, and every scope carries its kind:

    basket:demo
    instrument:demo:binance:BTC/USDT

An instrument scope names its basket as well as its instrument because two baskets may hold the
same instrument, and a decision belongs to the basket that made it. The instrument key keeps its
own colons (`binance:BTC/USDT`, `Instrument.key`) and is not re-encoded: it is split off with a
bounded partition instead, so the key that reaches a query is byte-identical to the one in the
projections. Mangling it would mean two spellings of one instrument.

Failure semantics: an unparseable, unknown or absent scope is **no selection** (`None`), never a
guess. No selection is a legitimate state — the workspace opens on it — so nothing here raises,
and a hand-edited URL degrades to the unfiltered view rather than to an error page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["BASKET", "INSTRUMENT", "Scope", "parse"]

BASKET = "basket"
INSTRUMENT = "instrument"


@dataclass(frozen=True, slots=True)
class Scope:
    """One selected basket, or one instrument within one basket."""

    basket_id: str
    instrument_key: str | None = None

    def __str__(self) -> str:
        """The URL form. `parse(str(scope)) == scope` for every scope this module builds."""
        if self.instrument_key is None:
            return f"{BASKET}:{self.basket_id}"
        return f"{INSTRUMENT}:{self.basket_id}:{self.instrument_key}"


def parse(raw: str | None) -> Scope | None:
    """`"basket:demo"` → `Scope("demo")`. Anything unrecognised is no selection."""
    kind, _, rest = (raw or "").partition(":")
    parser = _PARSERS.get(kind)
    return parser(rest) if parser is not None else None


def _basket(rest: str) -> Scope | None:
    return Scope(rest) if rest else None


def _instrument(rest: str) -> Scope | None:
    """`"demo:binance:BTC/USDT"` → basket `demo`, instrument `binance:BTC/USDT`.

    Bounded at one split: everything after the basket is the key, colons and all.
    """
    basket_id, separator, instrument_key = rest.partition(":")
    return Scope(basket_id, instrument_key) if basket_id and separator and instrument_key else None


#: Dispatch rather than a branch, so an unknown kind is a lookup miss instead of a fall-through.
_PARSERS: dict[str, Callable[[str], Scope | None]] = {BASKET: _basket, INSTRUMENT: _instrument}
