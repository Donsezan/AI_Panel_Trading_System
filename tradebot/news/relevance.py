"""Scoring a news item against the instruments a basket trades.

Scoring **ranks and filters only**. It does not decide whether news is bullish or bearish — that
is precisely what the LLM seats are for, and a keyword sentiment score here would be a second,
worse opinion competing with the panel's (DESIGN §6.4).

The scale is deliberately coarse and the table is deliberately explicit. A ranked list of
headlines is the output; pretending to three decimal places of relevance would imply a precision
that keyword matching does not have.

Matching is token-based for single words, so `"ETH"` cannot match `"together"`, and substring for
phrases, so `"federal reserve"` matches across a token boundary.

Failure semantics: pure and total. An item that matches nothing scores zero and is filtered out
by the hub's floor, which is the correct outcome — irrelevant news in a snapshot is tokens spent
to dilute the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.interfaces.news import NewsItem
from tradebot.news.normalize import tokens

#: Common names for an instrument's base asset. Config may extend this per deployment; the point
#: is that a headline says "Bitcoin", not "BTC/USDT".
DEFAULT_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "XRP": ("ripple",),
    "ADA": ("cardano",),
    "DOGE": ("dogecoin",),
    "BNB": ("binance coin",),
}

#: Terms that make an item relevant to a whole asset class rather than one instrument.
CLASS_TERMS: Final[dict[AssetClass, tuple[str, ...]]] = {
    AssetClass.CRYPTO: (
        "crypto",
        "cryptocurrency",
        "cryptocurrencies",
        "blockchain",
        "digital asset",
        "stablecoin",
        "altcoin",
        "defi",
    ),
    AssetClass.EQUITY: ("stocks", "equities", "earnings", "nasdaq", "nyse", "buyback"),
    AssetClass.INDEX_ETF: ("etf", "index fund", "nasdaq", "s&p"),
}

#: Macro catalysts move everything, so they earn a floor score even with no instrument match.
MACRO_TERMS: Final[tuple[str, ...]] = (
    "federal reserve",
    "fed",
    "fomc",
    "inflation",
    "cpi",
    "interest rate",
    "rate cut",
    "rate hike",
    "recession",
    "tariff",
    "sanctions",
    "regulation",
    "sec",
)

MACRO_SCORE: Final = Decimal("0.20")


@dataclass(frozen=True, slots=True)
class _Rule:
    """One way an item can be relevant, and how much that is worth."""

    score: Decimal
    terms: str  # "primary" | "alias" | "class"
    field: str  # "title" | "body"


#: Ordered high to low. Every rule is evaluated and the best match wins, so a title mention
#: cannot be outranked by an incidental body mention of a different instrument.
_RULES: Final[tuple[_Rule, ...]] = (
    _Rule(Decimal("1.00"), "primary", "title"),
    _Rule(Decimal("0.90"), "alias", "title"),
    _Rule(Decimal("0.60"), "primary", "body"),
    _Rule(Decimal("0.50"), "alias", "body"),
    _Rule(Decimal("0.40"), "class", "title"),
    _Rule(Decimal("0.25"), "class", "body"),
)


@dataclass(frozen=True, slots=True)
class _Field:
    """A pre-tokenized piece of text, so each rule is a set lookup rather than a rescan."""

    text: str
    words: frozenset[str]

    def contains(self, term: str) -> bool:
        return term in self.text if " " in term else term in self.words


class KeywordRelevanceFilter:
    """Ranks items by how directly they name a basket's instruments."""

    def __init__(
        self,
        *,
        aliases: dict[str, tuple[str, ...]] | None = None,
        macro_terms: tuple[str, ...] = MACRO_TERMS,
    ) -> None:
        self._aliases = {key.upper(): value for key, value in (aliases or DEFAULT_ALIASES).items()}
        self._macro = macro_terms

    def relevance(self, item: NewsItem, instruments: tuple[Instrument, ...]) -> Decimal:
        fields = {
            "title": _Field(item.title.lower(), tokens(item.title)),
            "body": _Field(item.excerpt.lower(), tokens(item.excerpt)),
        }
        best = self._macro_score(fields)
        for instrument in instruments:
            terms = self._terms_for(instrument)
            for rule in _RULES:
                if rule.score > best and self._matches(terms[rule.terms], fields[rule.field]):
                    best = rule.score
        return best

    def _macro_score(self, fields: dict[str, _Field]) -> Decimal:
        return (
            MACRO_SCORE
            if any(field.contains(term) for term in self._macro for field in fields.values())
            else Decimal(0)
        )

    def _terms_for(self, instrument: Instrument) -> dict[str, tuple[str, ...]]:
        base = instrument.base_currency.upper()
        return {
            "primary": (base.lower(), instrument.symbol.split("/")[0].lower()),
            "alias": self._aliases.get(base, ()),
            "class": CLASS_TERMS.get(instrument.asset_class, ()),
        }

    @staticmethod
    def _matches(terms: tuple[str, ...], field: _Field) -> bool:
        return any(field.contains(term) for term in terms)
