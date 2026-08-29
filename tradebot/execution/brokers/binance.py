"""Binance spot as a `BrokerAdapter`: the wire format, and nothing else.

Same split as the market-data gateway, for the same reason — the code that turns a venue's JSON
into the decimals a position is booked from is the code most worth testing exhaustively, and here
it is testable with plain dictionaries. All I/O, rate budget and error classification live in
`venues/ccxt_transport.py`; this module holds Binance's vocabulary.

**Quantities and prices are read from Binance's string fields.** Binance publishes
`"0.01634790"` precisely so it survives; a unified client parses it to a float before we see it,
which is why every value here comes off the raw response (PLAN §2.1).

Three Binance realities shape the design:

* **Fills are trades, not order states.** `executedQty` on an order tells you how much filled, not
  in how many pieces or at what fees. Positions may only move on fills (PLAN §2.5), so trades are
  fetched from `myTrades` — but only when executed quantity has actually changed, because polling
  a trade list on every sweep is the kind of weight spend that gets an IP banned (PLAN §3.1).
* **OCO is one call, not two orders.** Two independent exit orders on one holding can both fill,
  and the second sells a position that is gone. `submit_group` therefore posts an OCO list, and
  `oco_groups` is declared true only because that call exists (DESIGN §6.7, ADR 0011).
* **There is no venue-side good-till-time.** Spot offers GTC/IOC/FOK only, so TTL is bot-enforced
  by the `ExecutionMonitor` and `venue_side_ttl` is declared false.

Failure semantics: a rejection is a *result* (`OrderState.REJECTED`), a vanished order is
`found=False`, and an ambiguous submit raises `SubmitUnknownError` from the transport — whose only
legal resolution is querying this same adapter by `client_order_id` (PLAN §2.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import DataStaleError, OrderNotFoundError, OrderRejectedError
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO, to_decimal
from tradebot.core.orders import Fill, OrderIntent
from tradebot.core.portfolio import AccountState, Balance, Position
from tradebot.core.schema import Money
from tradebot.interfaces.broker import (
    BrokerCapabilities,
    CancelAck,
    OrderAck,
    OrderRef,
    OrderStatus,
)
from tradebot.interfaces.exchange import TradingTransport
from tradebot.marketdata.binance import VENUE_ID, to_symbol_id

#: Endpoint weights (spot API v3), at or above the published figures. Overpaying weight costs
#: nothing; underpaying it is how an IP gets banned (PLAN §3.1).
WEIGHTS: Final[Mapping[str, int]] = {
    "newOrder": 1,
    "newOco": 1,
    "queryOrder": 4,
    "cancelOrder": 1,
    "cancelOrderList": 1,
    "openOrders": 6,
    "myTrades": 20,
    "account": 20,
    "time": 1,
    "apiRestrictions": 1,
}

#: Binance order status → our lifecycle. `EXPIRED_IN_MATCH` is a self-trade-prevention outcome:
#: the order never rested, which is a rejection rather than a TTL expiry.
ORDER_STATES: Final[Mapping[str, OrderState]] = {
    "NEW": OrderState.OPEN,
    "PENDING_NEW": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "PENDING_CANCEL": OrderState.CANCELLED,
    "EXPIRED": OrderState.EXPIRED,
    "EXPIRED_IN_MATCH": OrderState.REJECTED,
    "REJECTED": OrderState.REJECTED,
}

#: Our order types → Binance's. A type absent here cannot be expressed on spot and must never be
#: silently downgraded to one that can — a market order standing in for a stop is not the order
#: risk approved.
ORDER_TYPES: Final[Mapping[OrderType, str]] = {
    OrderType.LIMIT: "LIMIT",
    OrderType.MARKET: "MARKET",
    OrderType.STOP_LOSS_LIMIT: "STOP_LOSS_LIMIT",
    OrderType.TAKE_PROFIT_LIMIT: "TAKE_PROFIT_LIMIT",
}

#: Time in force per order type. `GTC` for anything that may rest; a market order takes none.
_TIME_IN_FORCE: Final = "GTC"

#: The OCO leg naming Binance requires: one leg is "above" the reference price and one "below".
#: Which of a stop and a target sits above depends on the exit side, so it is a table rather than
#: an `if` — a swapped pair would place a stop above the market, where it triggers instantly.
_OCO_LEG_KEYS: Final[Mapping[Side, Mapping[OrderRole, str]]] = {
    Side.SELL: {OrderRole.STOP_LOSS: "below", OrderRole.TAKE_PROFIT: "above"},
    Side.BUY: {OrderRole.STOP_LOSS: "above", OrderRole.TAKE_PROFIT: "below"},
}


#: Binance's wire type → ours, for reading an order back. The inverse of `ORDER_TYPES`, derived
#: rather than written twice so the two can never drift apart.
_ORDER_TYPES_BY_WIRE: Final[Mapping[str, OrderType]] = {
    wire: order_type for order_type, wire in ORDER_TYPES.items()
}


def _decimal(payload: Mapping[str, Any], key: str, *, default: Decimal | None = None) -> Decimal:
    """A money field from the venue's string, refusing to invent one that is absent."""
    value = payload.get(key)
    if value is None or value == "":
        if default is not None:
            return default
        raise DataStaleError(f"binance response is missing {key}")
    return to_decimal(value)


def _optional_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    """A price the venue may legitimately not have. Binance writes an absent price as `"0.00"`,
    which is not a price and must not be read as one (a zero limit crosses everything)."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    parsed = to_decimal(value)
    return parsed if parsed > ZERO else None


def _side(value: Any) -> Side | None:
    return {"BUY": Side.BUY, "SELL": Side.SELL}.get(str(value).upper())


def _epoch_ms(payload: Mapping[str, Any], key: str, fallback: datetime) -> datetime:
    """A venue timestamp, or the observation time when the venue omits one.

    Falling back rather than raising is deliberate for timestamps only: a missing `transactTime`
    is cosmetic, while a missing quantity is not — that one raises.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | str):
        return fallback
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return fallback


def parse_state(payload: Mapping[str, Any]) -> OrderState:
    status = str(payload.get("status") or "")
    state = ORDER_STATES.get(status)
    if state is None:
        raise DataStaleError(f"binance reported an unknown order status {status!r}")
    return state


def parse_trade(
    payload: Mapping[str, Any],
    client_order_id: str,
    instrument: Instrument,
    side: Side,
    *,
    observed_at: datetime,
) -> Fill:
    """One trade — from `myTrades` or from a submit's `fills` array — into one `Fill`.

    The fill id is the venue's trade id, which is what makes booking idempotent across polls: the
    monitor re-reads the same order every sweep, and a fill counted twice is a position that does
    not exist (PLAN §2.5).

    `side` comes from the order rather than the trade because the two payload shapes disagree:
    `myTrades` reports `isBuyer`, while a submit's embedded fills report no side at all. Deriving
    it from the order we sent is correct for both and cannot be misread.
    """
    return Fill(
        fill_id=f"{VENUE_ID}-{payload.get('id') or payload.get('tradeId')}",
        client_order_id=client_order_id,
        instrument_key=instrument.key,
        side=side,
        qty=_decimal(payload, "qty"),
        price=_decimal(payload, "price"),
        fee=_decimal(payload, "commission", default=ZERO),
        fee_currency=str(payload.get("commissionAsset") or instrument.quote_currency),
        filled_at=_epoch_ms(payload, "time", observed_at),
    )


def parse_account(
    payload: Mapping[str, Any], instruments: Sequence[Instrument], observed_at: datetime
) -> AccountState:
    """A spot `account` payload → the venue's own view of the account.

    A spot balance *is* the position: holding 0.4 BTC and being long 0.4 BTC/USDT are the same
    fact. Positions are therefore projected from balances for the instruments we trade, and
    `avg_entry` is left at zero — the venue does not know our cost basis, and inventing one would
    put a number the ledger owns into the source it reconciles against (DESIGN §6.8).
    """
    balances = tuple(
        Balance(
            currency=str(entry.get("asset") or ""),
            free=_decimal(entry, "free", default=ZERO),
            locked=_decimal(entry, "locked", default=ZERO),
        )
        for entry in payload.get("balances") or []
        if entry.get("asset")
    )
    held = {balance.currency: balance.total for balance in balances}
    return AccountState(
        venue=VENUE_ID,
        positions=tuple(
            Position(instrument_key=instrument.key, qty=held[instrument.base_currency])
            for instrument in instruments
            if held.get(instrument.base_currency, ZERO) > ZERO
        ),
        balances=balances,
        observed_at=observed_at,
    )


class BinanceSpotBroker:
    """`BrokerAdapter` for Binance spot over a signed transport."""

    venue_id = VENUE_ID

    def __init__(
        self,
        transport: TradingTransport,
        clock: Clock,
        *,
        universe: Callable[[], Sequence[Instrument]],
        recv_window_ms: int = 5_000,
    ) -> None:
        self._transport = transport
        self._clock = clock
        #: What this adapter may translate between venue symbols and instruments, **read at
        #: each call** rather than held from wiring. A spot balance *is* a position, so this map
        #: is what turns the venue's assets into positions the reconciler can diff — and a basket
        #: published while the process runs adds an instrument a set captured at boot would leave
        #: out, making a real holding look like one that vanished. The same callable
        #: `PortfolioWatch` and `Reconciler` take, for the same reason (ADR 0021).
        self._universe = universe
        self._recv_window = recv_window_ms

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            venue_id=self.venue_id,
            order_types=tuple(ORDER_TYPES),
            protective_orders=True,
            # Declared true because `submit_group` posts a real OCO list. Without that call this
            # must be false, and then only a stop is ever placed (DESIGN §6.7).
            oco_groups=True,
            fractional_quantities=True,
            query_by_client_order_id=True,
            max_client_order_id_length=36,
            # Spot has GTC/IOC/FOK only; TTL is bot-enforced by the ExecutionMonitor.
            venue_side_ttl=False,
        )

    # ------------------------------------------------------------------ trading

    async def submit(self, intent: OrderIntent) -> OrderAck:
        """Place one order. A venue rejection comes back as a result, not an exception."""
        instrument = self._instrument(intent.instrument_key)
        try:
            payload = await self._transport.call(
                "newOrder",
                self._order_params(intent, instrument),
                weight=WEIGHTS["newOrder"],
                is_order=True,
            )
        except OrderRejectedError as exc:
            return self._rejected(intent.client_order_id, exc.reason)
        return self._ack(intent.client_order_id, payload)

    async def submit_group(self, intents: Sequence[OrderIntent]) -> tuple[OrderAck, ...]:
        """Place linked exit legs as one Binance OCO list.

        One call, one atomic outcome: either both legs rest or neither does. Submitting them
        separately would leave a window in which a filled stop and a live take-profit coexist
        over a position that no longer exists (ADR 0011).
        """
        legs = tuple(intents)
        if len(legs) == 1:
            return (await self.submit(legs[0]),)
        instrument = self._instrument(legs[0].instrument_key)
        try:
            payload = await self._transport.call(
                "newOco",
                self._oco_params(legs, instrument),
                weight=WEIGHTS["newOco"],
                is_order=True,
            )
        except OrderRejectedError as exc:
            return tuple(self._rejected(leg.client_order_id, exc.reason) for leg in legs)
        return self._group_acks(legs, payload)

    def _group_acks(
        self, legs: Sequence[OrderIntent], payload: Mapping[str, Any]
    ) -> tuple[OrderAck, ...]:
        """Read one ack per leg out of the OCO list's `orderReports`.

        A leg the venue does not report on is a fail-closed case, not a default: the legs are at
        the venue either way, so inventing a state for one would leave the monitor guarding a
        position with an order it has never seen. The cycle halts and startup recovery resolves
        each leg by querying its `client_order_id` (PLAN §2.3).
        """
        reports = {
            str(report.get("clientOrderId")): report for report in payload.get("orderReports") or ()
        }
        missing = [leg.client_order_id for leg in legs if leg.client_order_id not in reports]
        if missing:
            raise DataStaleError(
                f"binance accepted the OCO list but reported no state for {', '.join(missing)}; "
                "the legs exist at the venue and must be resolved by query, not assumed"
            )
        return tuple(self._ack(leg.client_order_id, reports[leg.client_order_id]) for leg in legs)

    async def cancel(self, order_ref: OrderRef) -> CancelAck:
        """Cancel by our own id. An order already gone is reported, never raised.

        A cancel losing a race with a fill is the normal case, not an error: the monitor's next
        sweep books the fill from the venue's own record.
        """
        instrument = self._instrument(order_ref.instrument_key)
        try:
            await self._transport.call(
                "cancelOrder",
                self._signed(
                    symbol=to_symbol_id(instrument.symbol),
                    origClientOrderId=order_ref.client_order_id,
                ),
                weight=WEIGHTS["cancelOrder"],
            )
        except (OrderNotFoundError, OrderRejectedError) as exc:
            return CancelAck(
                client_order_id=order_ref.client_order_id, cancelled=False, detail=str(exc)
            )
        return CancelAck(client_order_id=order_ref.client_order_id, cancelled=True)

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        """Look up by `client_order_id` — the only legal resolution of `SUBMIT_UNKNOWN`."""
        instrument = self._instrument(order_ref.instrument_key)
        try:
            payload = await self._transport.call(
                "queryOrder",
                self._signed(
                    symbol=to_symbol_id(instrument.symbol),
                    origClientOrderId=order_ref.client_order_id,
                ),
                weight=WEIGHTS["queryOrder"],
            )
        except OrderNotFoundError as exc:
            return self._vanished(order_ref, str(exc))
        return await self._status(payload, instrument)

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        """Every working order at the venue, ours and a human's alike.

        Not filtered by our id prefix: the reconciler needs to *see* foreign orders to leave them
        alone deliberately, and the self-trade check needs them to know what our own resting
        orders would cross (DESIGN §8.2, PLAN §3.3).
        """
        payload = await self._transport.call(
            "openOrders", self._signed(), weight=WEIGHTS["openOrders"]
        )
        by_symbol = {to_symbol_id(i.symbol): i for i in self._universe()}
        return tuple(
            self._status_without_fills(entry, instrument)
            for entry in (payload if isinstance(payload, list) else ())
            if (instrument := by_symbol.get(str(entry.get("symbol")))) is not None
        )

    async def fetch_positions_and_balances(self) -> AccountState:
        payload = await self._transport.call("account", self._signed(), weight=WEIGHTS["account"])
        if not isinstance(payload, dict):
            raise DataStaleError("binance account returned a non-object payload")
        return parse_account(payload, self._universe(), self._clock.now())

    async def server_time(self) -> datetime:
        payload = await self._transport.call("time", {}, weight=WEIGHTS["time"])
        if not isinstance(payload, dict) or payload.get("serverTime") is None:
            raise DataStaleError("binance time returned no serverTime")
        return _epoch_ms(payload, "serverTime", self._clock.now())

    async def withdrawals_enabled(self) -> bool | None:
        """Whether this key may withdraw, as the venue reports it (PLAN §3.2).

        `None` means the venue cannot answer — the spot testnet has no `sapi` at all. Trusting a
        checkbox set months ago is not a control; asserting it every boot is, and where the venue
        will not answer, the caller decides whether that is acceptable for the mode.
        """
        payload = await self._transport.call(
            "apiRestrictions", self._signed(), weight=WEIGHTS["apiRestrictions"]
        )
        if not isinstance(payload, dict) or "enableWithdrawals" not in payload:
            return None
        return bool(payload["enableWithdrawals"])

    async def close(self) -> None:
        await self._transport.close()

    # ------------------------------------------------------------------ internals

    def _instrument(self, instrument_key: str) -> Instrument:
        instrument = next((i for i in self._universe() if i.key == instrument_key), None)
        if instrument is None:
            raise DataStaleError(
                f"{instrument_key} is not configured on this binance adapter; refusing to trade "
                "an instrument whose precision and minimums are unknown"
            )
        return instrument

    def _signed(self, **params: Any) -> dict[str, Any]:
        """Every signed call carries a receive window. ccxt supplies the timestamp and signature.

        `recvWindow` bounds how long a signed request stays valid: a delayed request is rejected
        rather than executed late, which is the correct outcome for an order whose price was
        approved seconds ago.
        """
        return {"recvWindow": self._recv_window, **params}

    def _order_params(self, intent: OrderIntent, instrument: Instrument) -> dict[str, Any]:
        order_type = ORDER_TYPES.get(intent.order_type)
        if order_type is None:
            raise DataStaleError(f"binance spot cannot express {intent.order_type}")
        params = self._signed(
            symbol=to_symbol_id(instrument.symbol),
            side=intent.side.value.upper(),
            type=order_type,
            quantity=_wire(intent.qty),
            newClientOrderId=intent.client_order_id,
            # Ask for the full report so an immediate fill is visible in the ack rather than only
            # on the next poll.
            newOrderRespType="FULL",
        )
        if intent.order_type is not OrderType.MARKET:
            params["price"] = _wire(intent.limit_price)
            params["timeInForce"] = _TIME_IN_FORCE
        if intent.stop_price is not None:
            params["stopPrice"] = _wire(intent.stop_price)
        return params

    def _oco_params(self, legs: Sequence[OrderIntent], instrument: Instrument) -> dict[str, Any]:
        """Map our two legs onto Binance's above/below OCO vocabulary.

        An OCO list carries **one** quantity for both legs, so legs that disagree cannot be
        expressed. They never should: `plan_legs` sizes both legs to the same quantity. If they
        ever differ, sending the larger would place an exit for more than is held, so this refuses
        instead.
        """
        quantities = {leg.qty for leg in legs}
        if len(quantities) != 1:
            raise DataStaleError(
                f"binance OCO takes one quantity for both legs, got {sorted(quantities)}"
            )
        keys = _OCO_LEG_KEYS[legs[0].side]
        params = self._signed(
            symbol=to_symbol_id(instrument.symbol),
            side=legs[0].side.value.upper(),
            quantity=_wire(legs[0].qty),
            listClientOrderId=legs[0].group_id or legs[0].client_order_id,
        )
        for leg in legs:
            prefix = keys[leg.role]
            params[f"{prefix}Type"] = ORDER_TYPES[leg.order_type]
            params[f"{prefix}ClientOrderId"] = leg.client_order_id
            params[f"{prefix}StopPrice"] = _wire(leg.stop_price)
            params[f"{prefix}Price"] = _wire(leg.limit_price)
            params[f"{prefix}TimeInForce"] = _TIME_IN_FORCE
        return params

    def _ack(self, client_order_id: str, payload: Mapping[str, Any]) -> OrderAck:
        return OrderAck(
            client_order_id=client_order_id,
            venue_order_id=_venue_order_id(payload),
            state=parse_state(payload),
            accepted_at=_epoch_ms(payload, "transactTime", self._clock.now()),
        )

    def _rejected(self, client_order_id: str, reason: str) -> OrderAck:
        return OrderAck(
            client_order_id=client_order_id,
            venue_order_id=None,
            state=OrderState.REJECTED,
            accepted_at=self._clock.now(),
            reject_reason=reason,
        )

    def _vanished(self, order_ref: OrderRef, detail: str) -> OrderStatus:
        """The venue has never heard of it. Deliberately not a rejection (DESIGN §8.1)."""
        return OrderStatus(
            client_order_id=order_ref.client_order_id,
            venue_order_id=order_ref.venue_order_id,
            instrument_key=order_ref.instrument_key,
            state=OrderState.REJECTED,
            requested_qty=ZERO,
            filled_qty=ZERO,
            observed_at=self._clock.now(),
            reject_reason=detail,
            found=False,
        )

    def _status_without_fills(
        self, payload: Mapping[str, Any], instrument: Instrument, fills: tuple[Fill, ...] = ()
    ) -> OrderStatus:
        """The venue's view of one order. Used directly where fills are not wanted.

        `fetch_open_orders` is the case: it feeds reconciliation and the self-trade check, neither
        of which books anything, and fetching a trade list per open order would cost 20 weight
        each for data nobody reads (PLAN §3.1).
        """
        return OrderStatus(
            client_order_id=str(payload.get("clientOrderId") or ""),
            venue_order_id=_venue_order_id(payload),
            instrument_key=instrument.key,
            state=parse_state(payload),
            requested_qty=_decimal(payload, "origQty", default=ZERO),
            filled_qty=_decimal(payload, "executedQty", default=ZERO),
            fills=fills,
            observed_at=self._clock.now(),
            reject_reason=str(payload.get("rejectReason") or "") or None,
            side=_side(payload.get("side")),
            order_type=_ORDER_TYPES_BY_WIRE.get(str(payload.get("type") or "")),
            limit_price=_optional_decimal(payload, "price"),
            stop_price=_optional_decimal(payload, "stopPrice"),
        )

    async def _status(self, payload: Mapping[str, Any], instrument: Instrument) -> OrderStatus:
        fills = await self._fills(payload, instrument)
        return self._status_without_fills(payload, instrument, fills)

    async def _fills(self, payload: Mapping[str, Any], instrument: Instrument) -> tuple[Fill, ...]:
        """The order's trades, fetched only when the venue reports something executed.

        A submit response with `newOrderRespType=FULL` already carries its own fills, so the common
        case — an order that filled on submission — costs no extra call at all. Otherwise `myTrades`
        is consulted, because fees and individual trade ids are what the ledger books from and
        neither can be derived from a cumulative total (PLAN §2.5).

        Deliberately **stateless**: the adapter reports what the venue says every time, and does not
        remember what it has already handed over. Caching that would advance a "seen" marker before
        the caller has committed the fills, so a failure while booking would lose them permanently
        and leave the ledger quietly short of a position the venue holds. The cost of not caching is
        bounded — the monitor stops polling an order once it is terminal.
        """
        client_order_id = str(payload.get("clientOrderId") or "")
        side = _side(payload.get("side")) or Side.BUY
        observed_at = self._clock.now()
        embedded = payload.get("fills")
        if not embedded:
            if _decimal(payload, "executedQty", default=ZERO) <= ZERO:
                return ()
            trades = await self._transport.call(
                "myTrades",
                self._signed(
                    symbol=to_symbol_id(instrument.symbol), orderId=payload.get("orderId")
                ),
                weight=WEIGHTS["myTrades"],
            )
            embedded = trades if isinstance(trades, list) else ()
        return tuple(
            parse_trade(entry, client_order_id, instrument, side, observed_at=observed_at)
            for entry in embedded
        )


def _venue_order_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("orderId") or payload.get("orderListId")
    return str(value) if value not in (None, "", -1, "-1") else None


def _wire(value: Money | None) -> str | None:
    """A decimal as the venue must receive it: plain digits, never scientific notation.

    `str(Decimal("0.00001") / 3)` is fine, but `str(Decimal("1E-5"))` is `1E-5`, which Binance
    rejects. Normalising through a fixed-point format is the difference between an order and a
    filter error.
    """
    if value is None:
        return None
    return f"{value:f}"
