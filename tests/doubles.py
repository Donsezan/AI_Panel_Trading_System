"""Test doubles for the venue layers.

Deliberately hand-written rather than mocks: the point of the transport/gateway split is that
Binance's wire format can be tested with plain dictionaries, and a mock that returns whatever it
was told would prove nothing about the parsing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.core.errors import ProviderError
from tradebot.core.market import Candle
from tradebot.interfaces.exchange import TopOfBook, VenueMarket
from tradebot.interfaces.llm import CompletionRequest, CompletionResult
from tradebot.interfaces.market_data import DataCapabilities

#: Sentinel: a scripted entry equal to this raises instead of returning, exactly as a provider
#: would on a timeout, so the fallback-then-abstain path runs through the same code.
FAIL = "<<PROVIDER_FAILURE>>"


class ScriptedLLM:
    """Returns canned text per *model*, so a multi-seat panel is scripted seat by seat.

    `StubLLMProvider` cycles one list across every caller, which makes a three-seat debate depend
    on the order `asyncio.gather` happens to schedule in. Keying by model removes that coupling:
    a test says what each seat answers in each round and gets exactly that.
    """

    def __init__(
        self,
        by_model: Mapping[str, Sequence[str]],
        *,
        provider_id: str = "scripted",
        cost_usd: Decimal = Decimal(0),
    ) -> None:
        self.provider_id = provider_id
        self._by_model = {model: list(texts) for model, texts in by_model.items()}
        self._cost_usd = cost_usd
        self.calls: list[CompletionRequest] = []

    def calls_for(self, model: str) -> list[CompletionRequest]:
        return [call for call in self.calls if call.model == model]

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        scripted = self._by_model.get(request.model)
        if not scripted:
            raise ProviderError(f"{self.provider_id} has no script for {request.model}")
        # The last entry repeats, so a test scripts only the rounds it cares about.
        text = scripted[min(len(self.calls_for(request.model)), len(scripted) - 1)]
        self.calls.append(request)
        if text == FAIL:
            raise ProviderError(f"scripted failure from {self.provider_id}")
        return CompletionResult(
            text=text,
            model_fingerprint=f"{self.provider_id}:{request.model}",
            prompt_tokens=len(request.user) // 4,
            completion_tokens=len(text) // 4,
            cost_usd=self._cost_usd,
        )


class FakeTransport:
    """Replays canned payloads and records what was asked for, at what weight."""

    venue_id = "binance"

    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.errors: deque[Exception] = deque()
        self.closed = False

    def fail_next(self, error: Exception) -> None:
        self.errors.append(error)

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        self.calls.append((endpoint, dict(params), weight))
        if self.errors:
            raise self.errors.popleft()
        if endpoint not in self.responses:
            raise AssertionError(f"no canned response for {endpoint!r}")
        return self.responses[endpoint]

    async def close(self) -> None:
        self.closed = True

    @property
    def total_weight(self) -> int:
        return sum(weight for _, _, weight in self.calls)


class FakeGateway:
    """A `VenueGateway` over in-memory candles, so the provider can be tested on its own."""

    def __init__(
        self,
        bars: Sequence[Candle],
        *,
        venue_id: str = "binance",
        book: TopOfBook | None = None,
        markets: Sequence[VenueMarket] = (),
        timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d"),
        max_history: int = 1000,
        delay: timedelta = timedelta(0),
        server_time: datetime | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.bars = tuple(bars)
        self.book = book
        self.markets = tuple(markets)
        self.requests: list[tuple[str, str, int, datetime | None]] = []
        self.closed = False
        self._timeframes = timeframes
        self._max_history = max_history
        self._delay = delay
        self._server_time = server_time

    async def fetch_bars(
        self, symbol: str, timeframe: str, limit: int, *, end: datetime | None = None
    ) -> tuple[Candle, ...]:
        self.requests.append((symbol, timeframe, limit, end))
        visible = [bar for bar in self.bars if end is None or bar.close_time <= end]
        return tuple(visible[-limit:])

    async def fetch_top_of_book(self, symbol: str) -> TopOfBook:
        if self.book is None:
            raise AssertionError(f"no canned book for {symbol}")
        return self.book

    async def fetch_markets(self) -> tuple[VenueMarket, ...]:
        return self.markets

    async def server_time(self) -> datetime:
        if self._server_time is None:
            raise AssertionError("no canned server time")
        return self._server_time

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=self._timeframes,
            max_history=self._max_history,
            delay=self._delay,
            supports_point_in_time=True,
        )

    async def close(self) -> None:
        self.closed = True


def kline(open_time: datetime, interval: timedelta, close: str, **overrides: str) -> list[Any]:
    """One Binance kline array. Prices are strings, exactly as Binance publishes them."""
    values = {
        "open": close,
        "high": str(Decimal(close) + Decimal(1)),
        "low": str(Decimal(close) - Decimal(1)),
        "close": close,
        "volume": "10",
        **overrides,
    }
    return [
        int(open_time.timestamp() * 1000),
        values["open"],
        values["high"],
        values["low"],
        values["close"],
        values["volume"],
        # Binance's closeTime is the last inclusive millisecond, not the exclusive boundary.
        int((open_time + interval).timestamp() * 1000) - 1,
        "0",
        7,
        "0",
        "0",
        "0",
    ]


def klines(start: datetime, interval: timedelta, closes: Sequence[str]) -> list[list[Any]]:
    return [kline(start + interval * index, interval, close) for index, close in enumerate(closes)]


def symbol_entry(
    symbol_id: str = "BTCUSDT",
    base: str = "BTC",
    quote: str = "USDT",
    *,
    status: str = "TRADING",
    notional_filter: str = "NOTIONAL",
) -> dict[str, Any]:
    """One `exchangeInfo` symbol entry, with the filter shapes Binance actually returns."""
    return {
        "symbol": symbol_id,
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
            {"filterType": notional_filter, "minNotional": "5.00000000"},
        ],
    }
