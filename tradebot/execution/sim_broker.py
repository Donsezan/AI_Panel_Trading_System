"""Deterministic simulated venue. Used for simulation, backtest, and the primary paper soak.

`SimBroker` implements the exact `BrokerAdapter` interface every real venue does, which is what
makes paper results predictive of live behaviour instead of a different code path that happens
to look similar (DESIGN §5). It is also the primary paper-trading venue: live market data plus
deterministic fills beats a public testnet whose book is thin and whose state resets monthly
without notice (REVIEW A7).

Fill model, stated so nobody mistakes it for realism:

* A marketable **limit** order fills at its limit price — never better. Modelling price
  improvement would flatter the strategy.
* A **market** order fills at the reference price moved *against* us by `slippage_pct`.
* Fees are charged on notional at `fee_pct`.
* `fill_ratio` below 1 leaves the remainder open, so partial fills — the normal case at a real
  venue — are exercised rather than assumed away.

Phase 2 tightens this to require trade-through against replayed candles (REVIEW C11).

Failure semantics: `fail_next_submit` makes the adapter raise `SubmitUnknownError` exactly as a
timing-out venue does, so `SUBMIT_UNKNOWN` recovery is tested against the real code path. The
order still exists here afterwards — that is the whole point of the scenario.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.ids import new_uuid
from tradebot.core.money import ZERO, divide, multiply, percent_of
from tradebot.core.orders import Fill, OrderIntent
from tradebot.core.portfolio import AccountState, Balance
from tradebot.interfaces.broker import (
    BrokerCapabilities,
    CancelAck,
    OrderAck,
    OrderRef,
    OrderStatus,
)

#: Direction the market moves against us on a market order, per side.
_SLIPPAGE_SIGN: dict[Side, Decimal] = {Side.BUY: Decimal(1), Side.SELL: Decimal(-1)}


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
        reference_prices: dict[str, Decimal] | None = None,
    ) -> None:
        self.venue_id = venue_id
        self._clock = clock
        self._fee_pct = fee_pct
        self._slippage_pct = slippage_pct
        self._fill_ratio = fill_ratio
        self._balances = dict(balances or {"USDT": Decimal(10_000)})
        self._reference_prices = dict(reference_prices or {})
        self._orders: dict[str, OrderStatus] = {}
        #: Set by a test or chaos scenario to simulate an ambiguous submit.
        self.fail_next_submit = False

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            venue_id=self.venue_id,
            order_types=(OrderType.LIMIT, OrderType.MARKET),
            protective_orders=False,  # Phase 2a adds simulated OCO legs
            query_by_client_order_id=True,
            venue_side_ttl=False,
        )

    def set_reference_price(self, instrument_key: str, price: Decimal) -> None:
        self._reference_prices[instrument_key] = price

    async def submit(self, intent: OrderIntent) -> OrderAck:
        """Accept and immediately execute, recording the resulting fills.

        The order is registered *before* the ambiguity check, so a `SUBMIT_UNKNOWN` scenario
        leaves exactly what a real venue leaves: an order that exists but whose ack was lost.
        """
        price = self._fill_price(intent)
        filled_qty = multiply(intent.qty, self._fill_ratio)
        fills = self._execute(intent, price, filled_qty)
        state = OrderState.FILLED if filled_qty >= intent.qty else OrderState.PARTIALLY_FILLED

        self._orders[intent.client_order_id] = OrderStatus(
            client_order_id=intent.client_order_id,
            venue_order_id=f"{self.venue_id}-{new_uuid()[:8]}",
            instrument_key=intent.instrument_key,
            state=state,
            requested_qty=intent.qty,
            filled_qty=filled_qty,
            fills=fills,
            observed_at=self._clock.now(),
        )

        if self.fail_next_submit:
            self.fail_next_submit = False
            raise SubmitUnknownError(
                "connection lost after submit", client_order_id=intent.client_order_id
            )

        status = self._orders[intent.client_order_id]
        return OrderAck(
            client_order_id=intent.client_order_id,
            venue_order_id=status.venue_order_id,
            state=status.state,
            accepted_at=status.observed_at,
        )

    async def cancel(self, order_ref: OrderRef) -> CancelAck:
        status = self._orders.get(order_ref.client_order_id)
        if status is None or status.state.is_terminal:
            return CancelAck(
                client_order_id=order_ref.client_order_id,
                cancelled=False,
                detail="unknown or already terminal",
            )
        self._orders[order_ref.client_order_id] = status.model_copy(
            update={"state": OrderState.CANCELLED, "observed_at": self._clock.now()}
        )
        return CancelAck(client_order_id=order_ref.client_order_id, cancelled=True)

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        """Look up by our own id — the only legal resolution of `SUBMIT_UNKNOWN`."""
        status = self._orders.get(order_ref.client_order_id)
        if status is None:
            return OrderStatus(
                client_order_id=order_ref.client_order_id,
                venue_order_id=None,
                instrument_key=order_ref.instrument_key,
                state=OrderState.REJECTED,
                requested_qty=ZERO,
                filled_qty=ZERO,
                observed_at=self._clock.now(),
                reject_reason="not found at venue",
            )
        return status

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        return tuple(status for status in self._orders.values() if status.state.is_open)

    async def fetch_positions_and_balances(self) -> AccountState:
        return AccountState(
            venue=self.venue_id,
            balances=tuple(
                Balance(currency=currency, free=amount)
                for currency, amount in sorted(self._balances.items())
            ),
            observed_at=self._clock.now(),
        )

    # ------------------------------------------------------------------ internals

    def _fill_price(self, intent: OrderIntent) -> Decimal:
        if intent.order_type is OrderType.LIMIT and intent.limit_price is not None:
            return intent.limit_price
        reference = self._reference_prices.get(intent.instrument_key)
        if reference is None:
            raise SubmitUnknownError(
                f"no reference price for {intent.instrument_key}",
                client_order_id=intent.client_order_id,
            )
        drift = multiply(percent_of(reference, self._slippage_pct), _SLIPPAGE_SIGN[intent.side])
        return reference + drift

    def _execute(self, intent: OrderIntent, price: Decimal, qty: Decimal) -> tuple[Fill, ...]:
        if qty <= ZERO:
            return ()
        notional = multiply(qty, price)
        fee = percent_of(notional, self._fee_pct)
        self._settle(intent, notional, fee)
        return (
            Fill(
                fill_id=new_uuid(),
                client_order_id=intent.client_order_id,
                instrument_key=intent.instrument_key,
                side=intent.side,
                qty=qty,
                price=price,
                fee=fee,
                fee_currency=_quote_currency(intent.instrument_key),
                filled_at=self._clock.now(),
            ),
        )

    def _settle(self, intent: OrderIntent, notional: Decimal, fee: Decimal) -> None:
        """Move the venue's own balances, so `fetch_positions_and_balances` stays truthful."""
        quote = _quote_currency(intent.instrument_key)
        delta = -notional if intent.side is Side.BUY else notional
        self._balances[quote] = self._balances.get(quote, ZERO) + delta - fee


def _quote_currency(instrument_key: str) -> str:
    """`venue:BASE/QUOTE` → `QUOTE`."""
    _, _, pair = instrument_key.partition(":")
    _, _, quote = pair.partition("/")
    return quote or pair


def average_price(fills: tuple[Fill, ...]) -> Decimal:
    total = sum((fill.qty for fill in fills), start=ZERO)
    if total <= ZERO:
        return ZERO
    return divide(sum((fill.notional for fill in fills), start=ZERO), total)
