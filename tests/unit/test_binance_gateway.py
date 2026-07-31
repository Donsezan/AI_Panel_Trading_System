"""Binance wire format → exact decimals.

This is the boundary where a venue's numbers become the numbers an order is sized from, so the
tests are about exactness and about the two Binance quirks that would otherwise propagate: the
inclusive `closeTime`, and an empty book published as `"0.00000000"`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.doubles import FakeTransport, kline, klines, symbol_entry

from tradebot.core.clock import ManualClock
from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError, DataStaleError, MoneyError
from tradebot.marketdata.binance import (
    MAX_BARS,
    TIMEFRAMES,
    BinanceSpotGateway,
    klines_weight,
    parse_kline,
    parse_market,
    parse_ticker,
    to_symbol_id,
)

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def gateway(transport: FakeTransport, clock: ManualClock) -> BinanceSpotGateway:
    return BinanceSpotGateway(transport, clock)


class TestSymbolMapping:
    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [("BTC/USDT", "BTCUSDT"), ("eth/usdt", "ETHUSDT"), ("BTC-USDT", "BTCUSDT")],
    )
    def test_separators_are_stripped(self, symbol: str, expected: str) -> None:
        assert to_symbol_id(symbol) == expected


class TestKlineParsing:
    def test_prices_keep_full_decimal_precision(self) -> None:
        """Binance publishes strings so they survive exactly; a float here would lose that."""
        bar = parse_kline(kline(START, HOUR, "0.01634790"), HOUR)
        assert bar.close == Decimal("0.01634790")
        # Trailing zeros and scale survive, so the value re-serializes to what the venue sent.
        assert str(bar.close) == "0.01634790"

    def test_close_time_is_the_exclusive_boundary(self) -> None:
        """Binance's closeTime is `open + interval − 1ms`; propagating it fabricates a gap."""
        bar = parse_kline(kline(START, HOUR, "100"), HOUR)
        assert bar.open_time == START
        assert bar.close_time == START + HOUR

    def test_consecutive_bars_report_no_gap(self) -> None:
        gateway_bars = [parse_kline(row, HOUR) for row in klines(START, HOUR, ["100", "101"])]
        assert gateway_bars[0].close_time == gateway_bars[1].open_time

    def test_spot_bars_are_continuous_session(self) -> None:
        assert parse_kline(kline(START, HOUR, "100"), HOUR).session is MarketSession.CONTINUOUS

    def test_a_truncated_row_fails_closed(self) -> None:
        with pytest.raises(DataStaleError, match="expected at least 6"):
            parse_kline([1, "1", "2"], HOUR)

    def test_a_null_price_fails_closed(self) -> None:
        row = kline(START, HOUR, "100")
        row[4] = None
        with pytest.raises(DataStaleError, match="kline close is null"):
            parse_kline(row, HOUR)

    def test_a_float_price_is_refused(self) -> None:
        """A float here means something upstream already parsed it, losing exactness."""
        row = kline(START, HOUR, "100")
        row[4] = 100.5
        with pytest.raises(MoneyError, match="float is not accepted"):
            parse_kline(row, HOUR)

    @pytest.mark.parametrize("bad", [1.5, None, True, [1]])
    def test_a_non_integer_timestamp_fails_closed(self, bad: object) -> None:
        row = kline(START, HOUR, "100")
        row[0] = bad
        with pytest.raises(DataStaleError, match="openTime"):
            parse_kline(row, HOUR)

    def test_an_out_of_range_timestamp_fails_closed(self) -> None:
        row = kline(START, HOUR, "100")
        row[0] = 10**20
        with pytest.raises(DataStaleError, match="unusable epoch"):
            parse_kline(row, HOUR)


class TestTickerParsing:
    def test_top_of_book_is_read_from_the_string_fields(self) -> None:
        book = parse_ticker(
            {
                "symbol": "BTCUSDT",
                "bidPrice": "49999.99",
                "askPrice": "50000.01",
                "lastPrice": "50000.00",
            },
            START,
        )
        assert book.bid == Decimal("49999.99")
        assert book.ask == Decimal("50000.01")
        assert book.last == Decimal("50000.00")
        assert book.observed_at == START

    @pytest.mark.parametrize(
        "payload",
        [
            {"symbol": "X", "bidPrice": "0.00000000", "askPrice": "1", "lastPrice": "1"},
            {"symbol": "X", "bidPrice": "1", "askPrice": "0.00000000", "lastPrice": "1"},
        ],
    )
    def test_an_empty_book_fails_closed(self, payload: dict[str, str]) -> None:
        """`"0.00000000"` is a valid-looking price and not a price."""
        with pytest.raises(DataStaleError, match="empty book"):
            parse_ticker(payload, START)

    def test_a_missing_field_fails_closed(self) -> None:
        with pytest.raises(DataStaleError, match="missing askPrice"):
            parse_ticker({"symbol": "X", "bidPrice": "1", "lastPrice": "1"}, START)


class TestMarketParsing:
    def test_trading_rules_come_from_the_venue_filters(self) -> None:
        market = parse_market(symbol_entry())
        assert market.symbol == "BTC/USDT"
        assert market.lot_size == Decimal("0.00001")
        assert market.tick_size == Decimal("0.01")
        assert market.min_qty == Decimal("0.00001")
        assert market.min_notional == Decimal("5.00000000")
        assert market.tradable

    def test_the_legacy_notional_filter_name_is_accepted(self) -> None:
        """Both `NOTIONAL` and `MIN_NOTIONAL` appear in the wild, depending on the symbol."""
        market = parse_market(symbol_entry(notional_filter="MIN_NOTIONAL"))
        assert market.min_notional == Decimal("5.00000000")

    def test_a_delisted_symbol_is_marked_untradable(self) -> None:
        assert not parse_market(symbol_entry(status="BREAK")).tradable

    def test_a_missing_filter_fails_closed(self) -> None:
        """A missing minimum would let risk size against a zero floor."""
        entry = symbol_entry()
        entry["filters"] = [f for f in entry["filters"] if f["filterType"] != "LOT_SIZE"]
        with pytest.raises(DataStaleError, match=r"LOT_SIZE\.stepSize"):
            parse_market(entry)

    def test_an_incomplete_entry_fails_closed(self) -> None:
        with pytest.raises(DataStaleError, match="incomplete"):
            parse_market({"symbol": "BTCUSDT", "filters": []})


class TestWeights:
    @pytest.mark.parametrize(
        ("limit", "weight"), [(1, 2), (100, 2), (101, 4), (500, 4), (1000, 10)]
    )
    def test_weight_rises_with_the_bar_count(self, limit: int, weight: int) -> None:
        assert klines_weight(limit) == weight

    def test_above_the_venue_cap_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigError, match="exceeds Binance"):
            klines_weight(MAX_BARS + 1)


class TestGatewayCalls:
    async def test_bars_are_requested_with_the_venue_symbol_and_charged_weight(
        self, clock: ManualClock
    ) -> None:
        transport = FakeTransport({"klines": klines(START - HOUR * 3, HOUR, ["100", "101", "102"])})
        bars = await gateway(transport, clock).fetch_bars("BTC/USDT", "1h", 500)
        endpoint, params, weight = transport.calls[0]
        assert endpoint == "klines"
        assert params["symbol"] == "BTCUSDT"
        assert params["interval"] == "1h"
        assert weight == klines_weight(500)
        assert len(bars) == 3

    async def test_a_forming_bar_is_dropped(self, clock: ManualClock) -> None:
        """Binance filters on *open* time, so the newest bar it returns may still be forming."""
        transport = FakeTransport({"klines": klines(START - HOUR, HOUR, ["100", "101"])})
        bars = await gateway(transport, clock).fetch_bars("BTC/USDT", "1h", 10)
        assert [bar.close_time for bar in bars] == [START]

    async def test_the_end_cutoff_is_passed_to_the_venue(self, clock: ManualClock) -> None:
        transport = FakeTransport({"klines": klines(START - HOUR * 2, HOUR, ["100", "101"])})
        cutoff = START - HOUR
        await gateway(transport, clock).fetch_bars("BTC/USDT", "1h", 10, end=cutoff)
        assert transport.calls[0][1]["endTime"] == int(cutoff.timestamp() * 1000)

    async def test_the_bar_limit_is_clamped_to_the_venue_cap(self, clock: ManualClock) -> None:
        transport = FakeTransport({"klines": klines(START - HOUR, HOUR, ["100"])})
        await gateway(transport, clock).fetch_bars("BTC/USDT", "1h", MAX_BARS * 5)
        assert transport.calls[0][1]["limit"] == MAX_BARS

    async def test_an_unsupported_timeframe_is_refused(self, clock: ManualClock) -> None:
        with pytest.raises(ConfigError, match="does not serve"):
            await gateway(FakeTransport(), clock).fetch_bars("BTC/USDT", "3m", 10)

    async def test_a_non_list_klines_payload_fails_closed(self, clock: ManualClock) -> None:
        with pytest.raises(DataStaleError, match="expected a list"):
            await gateway(FakeTransport({"klines": {}}), clock).fetch_bars("BTC/USDT", "1h", 10)

    async def test_markets_are_parsed_from_exchange_info(self, clock: ManualClock) -> None:
        transport = FakeTransport({"exchangeInfo": {"symbols": [symbol_entry()]}})
        markets = await gateway(transport, clock).fetch_markets()
        assert [market.symbol for market in markets] == ["BTC/USDT"]

    @pytest.mark.parametrize("payload", [{"symbols": []}, {}, []])
    async def test_an_empty_exchange_info_fails_closed(
        self, clock: ManualClock, payload: object
    ) -> None:
        transport = FakeTransport({"exchangeInfo": payload})
        with pytest.raises(DataStaleError, match="no symbols"):
            await gateway(transport, clock).fetch_markets()

    async def test_server_time_is_parsed_for_the_skew_check(self, clock: ManualClock) -> None:
        transport = FakeTransport({"time": {"serverTime": int(START.timestamp() * 1000)}})
        assert await gateway(transport, clock).server_time() == START

    async def test_a_non_object_time_payload_fails_closed(self, clock: ManualClock) -> None:
        with pytest.raises(DataStaleError, match="non-object"):
            await gateway(FakeTransport({"time": []}), clock).server_time()

    async def test_the_book_is_stamped_with_our_clock_not_the_venues(
        self, clock: ManualClock
    ) -> None:
        """`observed_at` means when *we* saw it, which is what staleness is measured from."""
        transport = FakeTransport(
            {
                "ticker24h": {
                    "symbol": "BTCUSDT",
                    "bidPrice": "1",
                    "askPrice": "2",
                    "lastPrice": "1.5",
                }
            }
        )
        book = await gateway(transport, clock).fetch_top_of_book("BTC/USDT")
        assert book.observed_at == clock.now()

    def test_capabilities_report_the_venue_limits(self, clock: ManualClock) -> None:
        capabilities = gateway(FakeTransport(), clock).capabilities()
        assert capabilities.timeframes == TIMEFRAMES
        assert capabilities.max_history == MAX_BARS
        assert capabilities.supports_point_in_time
        assert capabilities.delay == timedelta(0)

    async def test_closing_releases_the_transport(self, clock: ManualClock) -> None:
        transport = FakeTransport()
        await gateway(transport, clock).close()
        assert transport.closed
