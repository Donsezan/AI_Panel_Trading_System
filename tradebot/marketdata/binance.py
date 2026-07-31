"""Binance spot: the one venue whose wire format this system currently understands.

Everything here is Binance-specific and nothing here does I/O. That separation is the point —
the code that turns a venue's JSON into the decimals an order is sized from is the code most
worth testing exhaustively, and it is testable here with plain dictionaries.

**Prices are read from Binance's string fields.** Binance publishes `"0.01634790"`, exactly so
it survives; a unified crypto client parses that to a float before we ever see it, which is why
this gateway reads the *raw* response rather than a library's normalized view (PLAN §2.1).

Two Binance quirks are corrected rather than propagated:

* `closeTime` is the last *inclusive* millisecond of the bar (`openTime + interval − 1 ms`).
  Our `Candle.close_time` is the exclusive boundary, so it is computed as `openTime + interval`.
  Propagating Binance's value would make every consecutive pair of bars look like a 1 ms gap.
* An empty book is published as `"0.00000000"`, which is a valid-looking price and not a price.
  A zero bid or ask fails closed as `DataStaleError`.

Failure semantics: parse failures and missing fields raise `DataStaleError` (the response is not
usable, so no decision is taken from it); an unlisted symbol or timeframe raises `ConfigError`.
Transport-level failures are already classified by the `VenueTransport`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from tradebot.core.clock import Clock
from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO, to_decimal
from tradebot.interfaces.exchange import TopOfBook, VenueMarket, VenueTransport
from tradebot.interfaces.market_data import DataCapabilities

VENUE_ID: Final = "binance"

#: Timeframes we serve, which is our vocabulary intersected with Binance's. Binance offers more
#: (3m, 1w, 1M); adding one means adding it to `TIMEFRAME_INTERVALS` first, so a timeframe means
#: the same duration everywhere in the system.
TIMEFRAMES: Final = ("1m", "5m", "15m", "1h", "4h", "1d")

#: Binance's hard cap on bars per `klines` call.
MAX_BARS: Final = 1000

#: Endpoint weights (spot API v3), deliberately at or above the published figures. Overpaying
#: weight costs nothing; underpaying it is how an IP gets banned (PLAN §3.1).
_KLINES_WEIGHT_LADDER: Final = ((100, 2), (500, 4), (MAX_BARS, 10))
_WEIGHTS: Final[Mapping[str, int]] = {"ticker24h": 4, "time": 1, "exchangeInfo": 20}

#: Kline array offsets. Binance returns a positional array, so the layout is the contract.
_OPEN_TIME, _OPEN, _HIGH, _LOW, _CLOSE, _VOLUME = 0, 1, 2, 3, 4, 5
_KLINE_FIELDS: Final = 6

#: `NOTIONAL` is the current filter name; `MIN_NOTIONAL` is the legacy one. Both appear in the
#: wild depending on the symbol, and a missing minimum would let risk size against a zero floor.
_NOTIONAL_FILTERS: Final = ("NOTIONAL", "MIN_NOTIONAL")


def klines_weight(limit: int) -> int:
    """Weight of one `klines` call at this bar count."""
    for threshold, weight in _KLINES_WEIGHT_LADDER:
        if limit <= threshold:
            return weight
    raise ConfigError(f"{limit} bars exceeds Binance's {MAX_BARS}-bar cap")


def to_symbol_id(symbol: str) -> str:
    """`BTC/USDT` → `BTCUSDT`. Binance's wire symbol has no separator."""
    return symbol.replace("/", "").replace("-", "").upper()


def _ms(value: Any, field: str) -> datetime:
    """Millisecond epoch → UTC datetime, refusing anything that is not an integer.

    A float timestamp would mean the response was parsed by something that also parsed the
    prices, which is exactly the path this gateway exists to avoid.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise DataStaleError(f"binance {field}: expected an integer epoch, got {value!r}")
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise DataStaleError(f"binance {field}: unusable epoch {value!r}") from exc


def _price(row: Mapping[str, Any] | Sequence[Any], key: Any, field: str) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError) as exc:
        raise DataStaleError(f"binance response is missing {field}") from exc
    if value is None:
        raise DataStaleError(f"binance {field} is null")
    return to_decimal(value)


def parse_kline(row: Sequence[Any], interval: timedelta) -> Candle:
    """One Binance kline array → one `Candle`, with the exclusive close boundary."""
    if len(row) < _KLINE_FIELDS:
        raise DataStaleError(f"binance kline has {len(row)} fields, expected at least 6")
    open_time = _ms(row[_OPEN_TIME], "kline openTime")
    return Candle(
        open_time=open_time,
        close_time=open_time + interval,
        open=_price(row, _OPEN, "kline open"),
        high=_price(row, _HIGH, "kline high"),
        low=_price(row, _LOW, "kline low"),
        close=_price(row, _CLOSE, "kline close"),
        volume=_price(row, _VOLUME, "kline volume"),
        # Spot crypto trades continuously; there is no session structure to respect.
        session=MarketSession.CONTINUOUS,
    )


def parse_ticker(payload: Mapping[str, Any], observed_at: datetime) -> TopOfBook:
    """A `ticker/24hr` payload → top of book, rejecting an empty book."""
    book = TopOfBook(
        bid=_price(payload, "bidPrice", "bidPrice"),
        ask=_price(payload, "askPrice", "askPrice"),
        last=_price(payload, "lastPrice", "lastPrice"),
        observed_at=observed_at,
    )
    if book.bid <= ZERO or book.ask <= ZERO:
        raise DataStaleError(
            f"binance published an empty book for {payload.get('symbol')} "
            f"(bid={book.bid} ask={book.ask}); an absent price is not a price"
        )
    return book


def _filter_value(filters: Sequence[Mapping[str, Any]], types: Sequence[str], key: str) -> Any:
    for filter_type in types:
        for entry in filters:
            if entry.get("filterType") == filter_type and entry.get(key) is not None:
                return to_decimal(entry[key])
    raise DataStaleError(f"binance symbol filters are missing {'/'.join(types)}.{key}")


def parse_market(payload: Mapping[str, Any]) -> VenueMarket:
    """An `exchangeInfo` symbol entry → its trading rules."""
    filters = payload.get("filters") or []
    symbol_id = payload.get("symbol")
    base, quote = payload.get("baseAsset"), payload.get("quoteAsset")
    if not (symbol_id and base and quote):
        raise DataStaleError(f"binance symbol entry is incomplete: {payload!r}")
    return VenueMarket(
        symbol=f"{base}/{quote}",
        base_currency=str(base),
        quote_currency=str(quote),
        lot_size=_filter_value(filters, ("LOT_SIZE",), "stepSize"),
        tick_size=_filter_value(filters, ("PRICE_FILTER",), "tickSize"),
        min_qty=_filter_value(filters, ("LOT_SIZE",), "minQty"),
        min_notional=_filter_value(filters, _NOTIONAL_FILTERS, "minNotional"),
        tradable=payload.get("status") == "TRADING",
    )


class BinanceSpotGateway:
    """`VenueGateway` for Binance spot. Read-only: this class cannot place an order."""

    venue_id = VENUE_ID

    def __init__(self, transport: VenueTransport, clock: Clock) -> None:
        self._transport = transport
        self._clock = clock

    async def fetch_bars(
        self, symbol: str, timeframe: str, limit: int, *, end: datetime | None = None
    ) -> tuple[Candle, ...]:
        interval = self._assert_timeframe(timeframe)
        params: dict[str, Any] = {
            "symbol": to_symbol_id(symbol),
            "interval": timeframe,
            "limit": min(limit, MAX_BARS),
        }
        if end is not None:
            # Binance filters on *open* time, so the newest bar returned may still be forming.
            # It is dropped below rather than trusted.
            params["endTime"] = int(end.timestamp() * 1000)
        rows = await self._transport.get("klines", params, weight=klines_weight(params["limit"]))
        if not isinstance(rows, list):
            raise DataStaleError(f"binance klines returned {type(rows).__name__}, expected a list")
        cutoff = end or self._clock.now()
        bars = tuple(parse_kline(row, interval) for row in rows)
        return tuple(bar for bar in bars if bar.close_time <= cutoff)

    async def fetch_top_of_book(self, symbol: str) -> TopOfBook:
        payload = await self._transport.get(
            "ticker24h", {"symbol": to_symbol_id(symbol)}, weight=_WEIGHTS["ticker24h"]
        )
        return parse_ticker(payload, self._clock.now())

    async def fetch_markets(self) -> tuple[VenueMarket, ...]:
        payload = await self._transport.get("exchangeInfo", {}, weight=_WEIGHTS["exchangeInfo"])
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not symbols:
            raise DataStaleError("binance exchangeInfo returned no symbols")
        return tuple(parse_market(entry) for entry in symbols)

    async def server_time(self) -> datetime:
        payload = await self._transport.get("time", {}, weight=_WEIGHTS["time"])
        if not isinstance(payload, dict):
            raise DataStaleError("binance time returned a non-object payload")
        return _ms(payload.get("serverTime"), "serverTime")

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=TIMEFRAMES,
            max_history=MAX_BARS,
            # Spot REST is real-time; there is no publication delay to budget for.
            delay=timedelta(0),
            supports_point_in_time=True,
        )

    async def close(self) -> None:
        await self._transport.close()

    @staticmethod
    def _assert_timeframe(timeframe: str) -> timedelta:
        if timeframe not in TIMEFRAMES:
            raise ConfigError(
                f"binance spot does not serve {timeframe!r} here; available: {list(TIMEFRAMES)}"
            )
        return timeframe_interval(timeframe)
