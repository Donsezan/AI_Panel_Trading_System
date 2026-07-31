"""Deterministic simulated venue. Used for simulation, backtest, and the primary paper soak.

`SimBroker` implements the exact `BrokerAdapter` interface every real venue does, which is what
makes paper results predictive of live behaviour instead of a different code path that happens
to look similar (DESIGN §5). It is also the primary paper-trading venue: live market data plus
deterministic fills beats a public testnet whose book is thin and whose state resets monthly
without notice (REVIEW A7).

Fill model, stated so nobody mistakes it for realism:

* An order fills only when the market **trades through** its price. A limit order that is not
  marketable rests until `observe` reports a bar that reaches it. Filling everything at submit,
  as Phase 1 did, made TTL expiry, partial fills and protective legs untestable.
* A limit order fills **at its limit price, never better**. Modelling price improvement would
  flatter the strategy.
* A **market** order fills at the last price moved *against* us by `slippage_pct`.
* `fill_ratio` below 1 fills that share of the remainder and leaves the rest resting, so partial
  fills — the normal case at a real venue — are exercised rather than assumed away.
* Fees are charged on notional at `fee_pct`, and the funds an open order commits are **locked**,
  so the account state the reconciler diffs against is truthful rather than optimistic.

Protective legs are venue-native here: a `STOP_LOSS_LIMIT`/`TAKE_PROFIT_LIMIT` arms when a bar
crosses its `stop_price`, and filling one leg cancels its OCO siblings *inside the venue* —
which is what Binance spot OCO provides and why `oco_groups` is declared true. A bar that closed
before an order was placed can never fill it, so a daily candle cannot retroactively trigger a
stop that did not exist when the bar traded.

Failure semantics: `fail_next_submit` makes the adapter raise `SubmitUnknownError` exactly as a
timing-out venue does, so `SUBMIT_UNKNOWN` recovery is tested against the real code path. The
order still exists here afterwards — that is the whole point of the scenario.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries, Quote
from tradebot.core.money import ZERO, divide, multiply, percent_of
from tradebot.core.orders import Fill, Order, OrderIntent
from tradebot.core.portfolio import AccountState, Balance, Position
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.interfaces.broker import (
    BrokerCapabilities,
    CancelAck,
    OrderAck,
    OrderRef,
    OrderStatus,
)
from tradebot.interfaces.market_data import DataCapabilities, MarketDataProvider

Comparator = Callable[[Decimal, Decimal], bool]

#: Direction the market moves against us on a market order, per side.
_SLIPPAGE_SIGN: dict[Side, Decimal] = {Side.BUY: Decimal(1), Side.SELL: Decimal(-1)}

#: Which extreme of a bar arms a protective leg, and in which direction. Keyed by (role, side)
#: rather than branched, so adding a leg type cannot silently fall through to "never triggers".
_TRIGGERS: dict[tuple[OrderRole, Side], tuple[str, Comparator]] = {
    (OrderRole.STOP_LOSS, Side.SELL): ("low", operator.le),
    (OrderRole.TAKE_PROFIT, Side.SELL): ("high", operator.ge),
    (OrderRole.STOP_LOSS, Side.BUY): ("high", operator.ge),
    (OrderRole.TAKE_PROFIT, Side.BUY): ("low", operator.le),
}

#: Which extreme of a bar reaches a resting limit, and in which direction.
_TRADE_THROUGH: dict[Side, tuple[str, Comparator]] = {
    Side.BUY: ("low", operator.le),
    Side.SELL: ("high", operator.ge),
}

#: Free-balance deltas `(base, quote)` once a fill's reservation has been released. A buy pays
#: the true cost out of the refunded reservation; a sell already surrendered its asset at
#: reservation time and is credited the proceeds.
_SETTLEMENT: dict[Side, Callable[[Fill, Decimal], tuple[Decimal, Decimal]]] = {
    Side.BUY: lambda fill, committed: (fill.qty, committed - fill.notional - fill.fee),
    Side.SELL: lambda fill, _committed: (ZERO, fill.notional - fill.fee),
}


class Tick(DomainModel):
    """What the venue sees: top of book plus the extremes a resting order can be reached through.

    `covers_since` is the start of the period the extremes describe. An order placed after it
    cannot be filled by it — otherwise yesterday's daily bar would trigger today's stop.
    """

    instrument_key: str
    bid: Money
    ask: Money
    last: Money
    high: Money
    low: Money
    covers_since: UtcDatetime
    observed_at: UtcDatetime

    @classmethod
    def from_candle(cls, instrument_key: str, candle: Candle, observed_at: UtcDatetime) -> Tick:
        return cls(
            instrument_key=instrument_key,
            bid=candle.close,
            ask=candle.close,
            last=candle.close,
            high=candle.high,
            low=candle.low,
            covers_since=candle.open_time,
            observed_at=observed_at,
        )

    @classmethod
    def from_quote(cls, quote: Quote) -> Tick:
        """A quote has no bar behind it: only the top of book can be traded through."""
        return cls(
            instrument_key=quote.instrument_key,
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            high=quote.ask,
            low=quote.bid,
            covers_since=quote.observed_at,
            observed_at=quote.observed_at,
        )

    def reach(self, extreme: str) -> Decimal:
        return self.high if extreme == "high" else self.low


@dataclass(slots=True)
class _Resting:
    """One order as the venue holds it."""

    intent: OrderIntent
    venue_order_id: str
    state: OrderState
    fills: list[Fill] = field(default_factory=list)
    armed: bool = False

    @property
    def filled_qty(self) -> Decimal:
        return sum((fill.qty for fill in self.fills), start=ZERO)

    @property
    def remaining(self) -> Decimal:
        return self.intent.qty - self.filled_qty

    def status(self, observed_at: datetime) -> OrderStatus:
        return OrderStatus(
            client_order_id=self.intent.client_order_id,
            venue_order_id=self.venue_order_id,
            instrument_key=self.intent.instrument_key,
            state=self.state,
            requested_qty=self.intent.qty,
            filled_qty=self.filled_qty,
            fills=tuple(self.fills),
            observed_at=observed_at,
            side=self.intent.side,
            order_type=self.intent.order_type,
            limit_price=self.intent.limit_price,
            stop_price=self.intent.stop_price,
        )


class SimBroker:
    """An in-process venue with an internal account the reconciler can be pointed at."""

    def __init__(
        self,
        clock: Clock,
        *,
        venue_id: str = "sim",
        balances: dict[str, Decimal] | None = None,
        fee_pct: Decimal = Decimal("0.1"),
        slippage_pct: Decimal = Decimal("0.05"),
        fill_ratio: Decimal = Decimal(1),
        default_quote_currency: str = "USDT",
    ) -> None:
        self.venue_id = venue_id
        self._clock = clock
        self._fee_pct = fee_pct
        self._slippage_pct = slippage_pct
        #: Share of a matched order's remainder that actually fills. Public because it is a test
        #: seam like `fail_next_submit`: a contract suite has to be able to *cause* a partial fill,
        #: and the alternative — building a second simulated venue to do it — would test the copy.
        self.fill_ratio = fill_ratio
        self._default_quote = default_quote_currency
        self._free: dict[str, Decimal] = dict(balances or {"USDT": Decimal(10_000)})
        self._locked: dict[str, Decimal] = {}
        self._qty: dict[str, Decimal] = {}
        self._cost: dict[str, Decimal] = {}
        self._orders: dict[str, _Resting] = {}
        self._ticks: dict[str, Tick] = {}
        #: Set by a test or chaos scenario to simulate an ambiguous submit.
        self.fail_next_submit = False

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            venue_id=self.venue_id,
            order_types=tuple(OrderType),
            protective_orders=True,
            oco_groups=True,
            query_by_client_order_id=True,
            venue_side_ttl=False,
        )

    # ------------------------------------------------------------------ market

    def observe(self, tick: Tick) -> tuple[Fill, ...]:
        """Advance the market and match every working order against it.

        Returns the fills the move produced, so tests can drive the venue without reaching into
        private state.
        """
        self._ticks[tick.instrument_key] = tick
        working = [
            resting
            for resting in self._orders.values()
            if resting.intent.instrument_key == tick.instrument_key and resting.state.is_open
        ]
        return tuple(fill for resting in working for fill in self._match(resting, tick))

    def _match(self, resting: _Resting, tick: Tick) -> tuple[Fill, ...]:
        intent = resting.intent
        if tick.covers_since < intent.created_at:
            return ()
        if intent.role.is_protective and not resting.armed:
            extreme, crossed = _TRIGGERS[intent.role, intent.side]
            if intent.stop_price is None or not crossed(tick.reach(extreme), intent.stop_price):
                return ()
            resting.armed = True

        extreme, reached = _TRADE_THROUGH[intent.side]
        if intent.limit_price is None or not reached(tick.reach(extreme), intent.limit_price):
            return ()
        return self._fill(resting, intent.limit_price)

    # ------------------------------------------------------------------ trading

    async def submit(self, intent: OrderIntent) -> OrderAck:
        """Accept the order, reserve its funds, and match it against the current book.

        The order is registered *before* the ambiguity check, so a `SUBMIT_UNKNOWN` scenario
        leaves exactly what a real venue leaves: an order that exists but whose ack was lost.
        """
        if intent.client_order_id in self._orders:
            return self._reject(intent, "duplicate client_order_id")

        resting = _Resting(
            intent=intent,
            venue_order_id=f"{self.venue_id}-{new_uuid()[:8]}",
            state=OrderState.OPEN,
        )
        self._orders[intent.client_order_id] = resting
        self._reserve(intent)
        self._match_on_submit(resting)

        if self.fail_next_submit:
            self.fail_next_submit = False
            raise SubmitUnknownError(
                "connection lost after submit", client_order_id=intent.client_order_id
            )
        return OrderAck(
            client_order_id=intent.client_order_id,
            venue_order_id=resting.venue_order_id,
            state=resting.state,
            accepted_at=self._clock.now(),
        )

    def _match_on_submit(self, resting: _Resting) -> None:
        """A marketable order crosses the spread now; anything else waits for `observe`."""
        intent = resting.intent
        if intent.order_type is OrderType.MARKET:
            self._fill(resting, self._market_price(intent))
            return
        tick = self._ticks.get(intent.instrument_key)
        if tick is None or intent.role.is_protective or intent.limit_price is None:
            return
        touch = tick.ask if intent.side is Side.BUY else tick.bid
        _, reached = _TRADE_THROUGH[intent.side]
        if reached(touch, intent.limit_price):
            self._fill(resting, intent.limit_price)

    def _market_price(self, intent: OrderIntent) -> Decimal:
        tick = self._ticks.get(intent.instrument_key)
        if tick is None:
            raise SubmitUnknownError(
                f"no reference price for {intent.instrument_key}",
                client_order_id=intent.client_order_id,
            )
        drift = multiply(percent_of(tick.last, self._slippage_pct), _SLIPPAGE_SIGN[intent.side])
        return tick.last + drift

    def _fill(self, resting: _Resting, price: Decimal) -> tuple[Fill, ...]:
        qty = multiply(resting.remaining, self.fill_ratio)
        if qty <= ZERO:
            return ()
        intent = resting.intent
        _, quote = self._currencies(intent.instrument_key)
        fee = percent_of(multiply(qty, price), self._fee_pct)

        fill = Fill(
            fill_id=new_uuid(),
            client_order_id=intent.client_order_id,
            instrument_key=intent.instrument_key,
            side=intent.side,
            qty=qty,
            price=price,
            fee=fee,
            fee_currency=quote,
            filled_at=self._clock.now(),
        )
        resting.fills.append(fill)
        resting.state = (
            OrderState.FILLED if resting.remaining <= ZERO else OrderState.PARTIALLY_FILLED
        )
        self._settle(fill, intent)
        if resting.state is OrderState.FILLED:
            self._cancel_siblings(resting)
        return (fill,)

    def _cancel_siblings(self, filled: _Resting) -> None:
        """Venue-native OCO: a filled leg takes its group's other protective legs with it."""
        for other in self._orders.values():
            if (
                other is not filled
                and other.intent.group_id == filled.intent.group_id
                and other.intent.role.is_protective
                and other.state.is_open
            ):
                self._release(other)
                other.state = OrderState.CANCELLED

    async def submit_group(self, intents: Sequence[OrderIntent]) -> tuple[OrderAck, ...]:
        """Place linked exit legs. The linkage already exists here: `_cancel_siblings`.

        A real venue needs one atomic call for this (Binance's OCO list, Alpaca's `oco` class); a
        simulated venue holds both legs in the same book, so placing them in turn *is* the linked
        outcome. What matters for the contract is that one leg filling cancels the other inside the
        venue, which is what `oco_groups=True` claims and what `_fill` does (ADR 0011).
        """
        return tuple([await self.submit(intent) for intent in intents])

    async def cancel(self, order_ref: OrderRef) -> CancelAck:
        resting = self._orders.get(order_ref.client_order_id)
        if resting is None or not resting.state.is_open:
            return CancelAck(
                client_order_id=order_ref.client_order_id,
                cancelled=False,
                detail="unknown or already terminal",
            )
        self._release(resting)
        resting.state = OrderState.CANCELLED
        return CancelAck(client_order_id=order_ref.client_order_id, cancelled=True)

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        """Look up by our own id — the only legal resolution of `SUBMIT_UNKNOWN`."""
        resting = self._orders.get(order_ref.client_order_id)
        if resting is None:
            return OrderStatus(
                client_order_id=order_ref.client_order_id,
                venue_order_id=None,
                instrument_key=order_ref.instrument_key,
                state=OrderState.REJECTED,
                requested_qty=ZERO,
                filled_qty=ZERO,
                observed_at=self._clock.now(),
                reject_reason="not found at venue",
                found=False,
            )
        return resting.status(self._clock.now())

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        now = self._clock.now()
        return tuple(
            resting.status(now) for resting in self._orders.values() if resting.state.is_open
        )

    async def server_time(self) -> datetime:
        """The simulated venue's clock *is* ours, so the startup skew check is trivially satisfied.

        Answering rather than refusing keeps `BrokerAdapter` uniform: the preflight that asserts
        skew against a real venue runs against this one too, and finds nothing.
        """
        return self._clock.now()

    async def close(self) -> None:
        """Nothing to release: there is no socket behind a simulated venue."""

    async def fetch_positions_and_balances(self) -> AccountState:
        currencies = sorted(set(self._free) | set(self._locked))
        return AccountState(
            venue=self.venue_id,
            positions=tuple(
                Position(
                    instrument_key=key,
                    qty=qty,
                    avg_entry=divide(self._cost[key], qty) if qty > ZERO else ZERO,
                )
                for key, qty in sorted(self._qty.items())
                if qty > ZERO
            ),
            balances=tuple(
                Balance(
                    currency=currency,
                    free=self._free.get(currency, ZERO),
                    locked=self._locked.get(currency, ZERO),
                )
                for currency in currencies
            ),
            observed_at=self._clock.now(),
        )

    # ------------------------------------------------------------------ account

    def _currencies(self, instrument_key: str) -> tuple[str, str]:
        """`venue:BASE/QUOTE` → `(BASE, QUOTE)`; a bare symbol quotes in the account currency."""
        _, _, symbol = instrument_key.partition(":")
        base, _, quote = symbol.partition("/")
        return base, quote or self._default_quote

    def _commitment(self, intent: OrderIntent, qty: Decimal) -> tuple[str, Decimal]:
        """What an open order ties up: quote currency for a buy, the asset itself for a sell."""
        base, quote = self._currencies(intent.instrument_key)
        if intent.side is Side.SELL:
            return base, qty
        return quote, multiply(qty, intent.limit_price or ZERO)

    def _reserve(self, intent: OrderIntent) -> None:
        self._move(*self._commitment(intent, intent.qty), locking=True)

    def _release(self, resting: _Resting) -> None:
        self._move(*self._commitment(resting.intent, resting.remaining), locking=False)

    def _move(self, currency: str, amount: Decimal, *, locking: bool) -> None:
        sign = Decimal(1) if locking else Decimal(-1)
        self._locked[currency] = self._locked.get(currency, ZERO) + multiply(amount, sign)
        self._free[currency] = self._free.get(currency, ZERO) - multiply(amount, sign)

    def _settle(self, fill: Fill, intent: OrderIntent) -> None:
        """Turn the locked commitment into a realised balance and position change.

        The reservation is released in full and the true cost taken from free funds, so a buy
        that fills inside its limit refunds the difference exactly as a venue does.
        """
        base, quote = self._currencies(fill.instrument_key)
        currency, committed = self._commitment(intent, fill.qty)
        self._locked[currency] = self._locked.get(currency, ZERO) - committed

        base_delta, quote_delta = _SETTLEMENT[fill.side](fill, committed)
        self._free[base] = self._free.get(base, ZERO) + base_delta
        self._free[quote] = self._free.get(quote, ZERO) + quote_delta
        self._apply_position(fill)

    def _apply_position(self, fill: Fill) -> None:
        """Cost basis moves proportionally on a sell, so average entry survives a partial exit."""
        key = fill.instrument_key
        held = self._qty.get(key, ZERO)
        cost = self._cost.get(key, ZERO)
        if fill.side is Side.BUY:
            self._qty[key], self._cost[key] = held + fill.qty, cost + fill.notional
            return
        share = divide(fill.qty, held) if held > ZERO else ZERO
        self._qty[key] = held - fill.qty
        self._cost[key] = cost - multiply(cost, share)

    def _reject(self, intent: OrderIntent, reason: str) -> OrderAck:
        return OrderAck(
            client_order_id=intent.client_order_id,
            venue_order_id=None,
            state=OrderState.REJECTED,
            accepted_at=self._clock.now(),
            reject_reason=reason,
        )

    def restore(self, state: AccountState, orders: tuple[Order, ...]) -> None:
        """Adopt the state recovered from our own event log (`RestorableVenue`).

        A simulated venue's books die with the process. Rebuilding them from the log is what
        stops an ordinary restart from looking exactly like a testnet wipe — while leaving the
        classification that catches a *genuine* wipe untouched.
        """
        self._free = {balance.currency: balance.free for balance in state.balances}
        self._locked = {balance.currency: balance.locked for balance in state.balances}
        self._qty = {p.instrument_key: p.qty for p in state.positions}
        self._cost = {p.instrument_key: multiply(p.qty, p.avg_entry) for p in state.positions}
        self._orders = {
            order.client_order_id: _Resting(
                intent=_intent_of(order),
                venue_order_id=order.venue_order_id or f"{self.venue_id}-restored",
                state=order.state,
                fills=list(order.fills),
                armed=order.role.is_protective and bool(order.fills),
            )
            for order in orders
            if order.state.is_open
        }

    # ------------------------------------------------------------------ test seams

    def credit(self, currency: str, amount: Decimal) -> None:
        """Simulate an external deposit or withdrawal, for reconciler scenarios."""
        self._free[currency] = self._free.get(currency, ZERO) + amount

    def wipe(self, balances: dict[str, Decimal]) -> None:
        """Simulate the reset a public testnet performs roughly monthly (R15)."""
        self._free = dict(balances)
        self._locked = {}
        self._qty = {}
        self._cost = {}
        self._orders = {}


def _intent_of(order: Order) -> OrderIntent:
    """The instruction a recovered order represents, as the venue would have received it."""
    return OrderIntent(
        client_order_id=order.client_order_id,
        basket_id=order.basket_id,
        cycle_id=order.cycle_id,
        instrument_key=order.instrument_key,
        side=order.side,
        qty=order.qty,
        order_type=order.order_type,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        role=order.role,
        group_id=order.group_id,
        created_at=order.created_at,
    )


class SimulatedMarket:
    """Forwards market data to the panel and the same prices to the simulated venue.

    The venue needs to see the market to match resting orders, but no core module may know that
    its broker is simulated. Bridging the two here keeps that knowledge inside the sim wiring.
    """

    provider_id = "simulated"

    def __init__(self, source: MarketDataProvider, broker: SimBroker) -> None:
        self._source = source
        self._broker = broker

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        series = await self._source.get_candles(instrument, timeframe, limit, end)
        self._broker.observe(Tick.from_candle(instrument.key, series.latest, series.observed_at))
        return series

    async def get_quote(self, instrument: Instrument) -> Quote:
        quote = await self._source.get_quote(instrument)
        self._broker.observe(Tick.from_quote(quote))
        return quote

    def capabilities(self) -> DataCapabilities:
        return self._source.capabilities()
