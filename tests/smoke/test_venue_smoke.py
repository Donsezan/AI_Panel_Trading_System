"""Opt-in smoke checks against the real test venues. Never part of CI.

Everything else in the suite is offline, deterministic and free, which is what lets it run on every
commit. These are the opposite: they need keys, they need the network, and they depend on a venue
being up. They exist because a recorded fixture proves we parse what we *recorded*, and only a real
call proves the venue still speaks that way — endpoints change, and a contract suite passing against
a stale cassette is exactly how that goes unnoticed (ADR 0009's stated cost, applied to venues).

Run them deliberately:

```powershell
$env:BINANCE_TESTNET_API_KEY="..."; $env:BINANCE_TESTNET_API_SECRET="..."
$env:ALPACA_PAPER_KEY_ID="..."; $env:ALPACA_PAPER_SECRET_KEY="..."
.venv\\Scripts\\python.exe -m pytest -m smoke
```

Without those variables every test here skips, so `pytest` stays green offline.

**These are read-only.** Nothing here places, cancels or modifies an order — even on a testnet,
because the value is in proving the *shapes* still parse, and an order-placing smoke test is a
thing that eventually runs against the wrong environment.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tradebot.core.clock import SystemClock
from tradebot.core.enums import AssetClass, Mode
from tradebot.core.instrument import Instrument
from tradebot.execution.brokers.alpaca import AlpacaBroker, AlpacaCalendar
from tradebot.execution.brokers.binance import BinanceSpotBroker
from tradebot.venues.alpaca_transport import ALPACA_HOSTS, AlpacaTransport
from tradebot.venues.ccxt_transport import binance_spot_trading_transport
from tradebot.venues.credentials import credentials, has_credentials, secret_refs

pytestmark = pytest.mark.smoke

BINANCE_KEYS = pytest.mark.skipif(
    not has_credentials("binance", Mode.PAPER),
    reason=f"needs {' and '.join(secret_refs('binance', Mode.PAPER))}",
)
ALPACA_KEYS = pytest.mark.skipif(
    not has_credentials("alpaca", Mode.PAPER),
    reason=f"needs {' and '.join(secret_refs('alpaca', Mode.PAPER))}",
)

BTC = Instrument(
    symbol="BTC/USDT",
    venue="binance",
    asset_class=AssetClass.CRYPTO,
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal(10),
)

AAPL = Instrument(
    symbol="AAPL",
    venue="alpaca",
    asset_class=AssetClass.EQUITY,
    base_currency="AAPL",
    quote_currency="USD",
    lot_size=Decimal("0.001"),
    tick_size=Decimal("0.01"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal(1),
)


@BINANCE_KEYS
class TestBinanceTestnet:
    async def _broker(self) -> BinanceSpotBroker:
        clock = SystemClock()
        transport = binance_spot_trading_transport(
            clock, credentials("binance", Mode.PAPER), mode=Mode.PAPER
        )
        return BinanceSpotBroker(transport, clock, instruments=(BTC,))

    async def test_the_account_reads_and_parses(self) -> None:
        """Proves the balance shape is still what `parse_account` expects."""
        broker = await self._broker()
        try:
            state = await broker.fetch_positions_and_balances()
            assert state.venue == "binance"
            assert state.balances
        finally:
            await broker.close()

    async def test_open_orders_read_and_parse(self) -> None:
        broker = await self._broker()
        try:
            await broker.fetch_open_orders()
        finally:
            await broker.close()

    async def test_the_venue_clock_is_within_tolerance(self) -> None:
        """The same check the startup preflight makes, against the real venue (PLAN §3.1)."""
        from tradebot.control.preflight import SKEW_HALT

        broker = await self._broker()
        try:
            skew = abs(await broker.server_time() - SystemClock().now())
            assert skew < SKEW_HALT, f"local clock is {skew} from Binance's"
        finally:
            await broker.close()


@ALPACA_KEYS
class TestAlpacaPaper:
    def _transport(self) -> tuple[AlpacaTransport, httpx.AsyncClient]:
        key_id, secret_key = credentials("alpaca", Mode.PAPER)
        client = httpx.AsyncClient()
        return (
            AlpacaTransport(
                client, SystemClock(), mode=Mode.PAPER, key_id=key_id, secret_key=secret_key
            ),
            client,
        )

    async def test_the_account_reads_and_parses(self) -> None:
        transport, client = self._transport()
        try:
            state = await AlpacaBroker(
                transport, SystemClock(), instruments=(AAPL,)
            ).fetch_positions_and_balances()
            assert state.venue == "alpaca"
        finally:
            await client.aclose()

    async def test_the_calendar_reads_and_parses(self) -> None:
        """The session bounds the scheduler and the daily-loss boundary both depend on."""
        transport, client = self._transport()
        try:
            calendar = AlpacaCalendar(transport, SystemClock())
            await calendar.is_open(SystemClock().now())
        finally:
            await client.aclose()

    async def test_the_transport_is_pinned_to_the_paper_host(self) -> None:
        """A smoke test that ever reached the live host would be the incident, not the check."""
        transport, client = self._transport()
        try:
            assert transport.mode is Mode.PAPER
            assert ALPACA_HOSTS[Mode.PAPER] != ALPACA_HOSTS[Mode.LIVE]
        finally:
            await client.aclose()
