"""Wire-level fake venues: stateful, and speaking each venue's own JSON.

The contract suite has to prove that three adapters behave identically. That is only meaningful if
each adapter is exercised through *its own* wire format — a mock returning `OrderStatus` objects
would prove the mock works. So these fakes hold a small order book and answer in the venue's
vocabulary: Binance's `executedQty` strings and kline-style arrays, Alpaca's `filled_qty` and
RFC 3339 timestamps.

They are also where a partial fill, a cancel race, a rejection and an ambiguous submit are *made*
to happen. Every one of those is a scripted seam (`fill`, `fail_next_submit`, `reject_next`), so the
suite drives the real adapter code down the paths that matter rather than hoping to observe them.

Deliberately not a matching engine: `SimBroker` is the venue with a fill model, and duplicating it
here would test the duplicate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from tradebot.core.errors import OrderNotFoundError, OrderRejectedError, VenueError
from tradebot.venues.alpaca_transport import AlpacaTransport

#: Trade ids are handed out in order, so a fill's identity is stable and comparable.
_FIRST_TRADE_ID = 5000


@dataclass
class _Order:
    """One order as a fake venue holds it, in venue-neutral terms."""

    client_order_id: str
    venue_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    status: str
    filled: Decimal = Decimal(0)
    trades: list[dict[str, Any]] = field(default_factory=list)
    #: OCO/bracket sibling ids, so filling one leg can cancel the other as the venue would.
    siblings: tuple[str, ...] = ()


class FakeVenueBook:
    """The shared state behind both fakes: orders, balances, and the scripted failures.

    One book, two wire formats. Keeping the *behaviour* in one place is what makes a divergence in
    the contract suite a divergence in the adapter rather than in the fake.
    """

    def __init__(
        self,
        *,
        cash: Decimal = Decimal(10_000),
        currency: str = "USDT",
        holdings: Mapping[str, Decimal] | None = None,
    ) -> None:
        self.cash = cash
        self.currency = currency
        self.holdings: dict[str, Decimal] = dict(holdings or {})
        self.orders: dict[str, _Order] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Scripted seams. Each fires once, so a test says "the *next* submit is ambiguous".
        self.fail_next_submit = False
        self.reject_next: str | None = None
        self.forget_next_order = False
        self._next_venue_id = 1
        self._next_trade_id = _FIRST_TRADE_ID

    # ------------------------------------------------------------------ scripting

    def fill(self, client_order_id: str, qty: Decimal, price: Decimal) -> dict[str, Any]:
        """Execute part or all of a resting order, as the venue would have."""
        order = self.orders[client_order_id]
        order.filled += qty
        order.status = "filled" if order.filled >= order.qty else "partially_filled"
        trade = {
            "id": self._next_trade_id,
            "qty": str(qty),
            "price": str(price),
            "commission": "0",
            "commissionAsset": self.currency,
            "isBuyer": order.side == "buy",
            "time": int(_NOW.timestamp() * 1000),
            "transaction_time": _NOW.isoformat(),
            "side": order.side,
            "order_id": order.venue_order_id,
        }
        self._next_trade_id += 1
        order.trades.append(trade)
        self._settle(order, qty, price)
        if order.status == "filled":
            for sibling in order.siblings:
                if sibling in self.orders and self.orders[sibling].status not in _TERMINAL:
                    self.orders[sibling].status = "canceled"
        return trade

    def _settle(self, order: _Order, qty: Decimal, price: Decimal) -> None:
        held = self.holdings.get(order.symbol, Decimal(0))
        if order.side == "buy":
            self.holdings[order.symbol] = held + qty
            self.cash -= qty * price
            return
        self.holdings[order.symbol] = held - qty
        self.cash += qty * price

    # ------------------------------------------------------------------ order book

    def place(
        self, params: Mapping[str, Any], *, key: str, siblings: tuple[str, ...] = ()
    ) -> _Order:
        """Register an order, honouring whatever failure is scripted next.

        The order is registered *before* the ambiguity check, exactly as a real venue would leave
        it: the order exists, and only our knowledge of it was lost (PLAN §2.3).
        """
        if self.reject_next is not None:
            reason, self.reject_next = self.reject_next, None
            raise OrderRejectedError(reason, reason=reason)

        client_order_id = str(params[key])
        if client_order_id in self.orders:
            raise OrderRejectedError("Duplicate order sent", reason="duplicate client order id")

        order = _Order(
            client_order_id=client_order_id,
            # A plain integer string: Binance reports `orderId` as a number and Alpaca `id` as a
            # string, and both adapters echo it back on the next call, so one form has to serve.
            venue_order_id=str(self._next_venue_id),
            symbol=str(params.get("symbol")),
            side=str(params.get("side", "buy")).lower(),
            order_type=str(params.get("type", "limit")).lower(),
            qty=Decimal(str(params.get("quantity") or params.get("qty") or "0")),
            limit_price=_maybe(params.get("price") or params.get("limit_price")),
            stop_price=_maybe(params.get("stopPrice") or params.get("stop_price")),
            status="new",
            siblings=siblings,
        )
        self._next_venue_id += 1
        self.orders[client_order_id] = order
        if self.forget_next_order:
            self.forget_next_order = False
            del self.orders[client_order_id]
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise VenueError("connection reset after the request left")
        return order

    def find(self, client_order_id: str) -> _Order:
        order = self.orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError(f"order {client_order_id} does not exist")
        return order

    def cancel(self, client_order_id: str) -> _Order:
        order = self.find(client_order_id)
        if order.status in _TERMINAL:
            raise OrderRejectedError("Unknown order sent", reason="already terminal")
        order.status = "canceled"
        return order

    def open_orders(self) -> list[_Order]:
        return [order for order in self.orders.values() if order.status not in _TERMINAL]


_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _maybe(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


class FakeBinanceTransport:
    """A `TradingTransport` speaking Binance spot's request and response shapes."""

    venue_id = "binance"

    def __init__(self, book: FakeVenueBook, *, server_time: datetime | None = None) -> None:
        self.book = book
        self.closed = False
        #: Replace one endpoint's response, for the cases where a venue answers *incompletely* —
        #: an OCO list that reports no legs, say. A public seam, so a test never has to reach into
        #: the fake's internals to describe a malformed answer.
        self.overrides: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
        self._server_time = server_time or _NOW

    async def call(
        self, endpoint: str, params: Mapping[str, Any], *, weight: int, is_order: bool = False
    ) -> Any:
        self.book.calls.append((endpoint, dict(params)))
        handler = self.overrides.get(endpoint) or getattr(self, f"_{endpoint}", None)
        if handler is None:
            raise AssertionError(f"fake binance has no handler for {endpoint!r}")
        try:
            return handler(params)
        except VenueError as error:
            # The signed transport is what escalates an ambiguous *placement*; the fake stands in
            # for the transport here, so it applies the same rule (PLAN §2.3).
            if is_order:
                from tradebot.core.errors import SubmitUnknownError

                raise SubmitUnknownError(
                    str(error),
                    client_order_id=str(
                        params.get("newClientOrderId") or params.get("listClientOrderId")
                    ),
                ) from error
            raise

    async def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ endpoints

    def _newOrder(self, params: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N802 — wire name
        order = self.book.place(params, key="newClientOrderId")
        return self._report(order, include_fills=True)

    def _newOco(self, params: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N802 — wire name
        ids = (str(params["aboveClientOrderId"]), str(params["belowClientOrderId"]))
        reports = []
        for prefix, client_order_id in zip(("above", "below"), ids, strict=True):
            order = self.book.place(
                {
                    "symbol": params["symbol"],
                    "side": params["side"],
                    "type": params[f"{prefix}Type"],
                    "quantity": params["quantity"],
                    "price": params[f"{prefix}Price"],
                    "stopPrice": params[f"{prefix}StopPrice"],
                    "newClientOrderId": client_order_id,
                },
                key="newClientOrderId",
                siblings=tuple(other for other in ids if other != client_order_id),
            )
            reports.append(self._report(order))
        return {
            "orderListId": 77,
            "listClientOrderId": params.get("listClientOrderId"),
            "listOrderStatus": "EXECUTING",
            "orderReports": reports,
        }

    def _queryOrder(self, params: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N802 — wire name
        return self._report(self.book.find(str(params["origClientOrderId"])))

    def _cancelOrder(self, params: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N802 — wire name
        return self._report(self.book.cancel(str(params["origClientOrderId"])))

    def _openOrders(self, _params: Mapping[str, Any]) -> list[dict[str, Any]]:  # noqa: N802
        return [self._report(order) for order in self.book.open_orders()]

    def _myTrades(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:  # noqa: N802
        wanted = str(params.get("orderId"))
        return [
            trade
            for order in self.book.orders.values()
            if order.venue_order_id == wanted
            for trade in order.trades
        ]

    def _account(self, _params: Mapping[str, Any]) -> dict[str, Any]:
        balances = [{"asset": self.book.currency, "free": str(self.book.cash), "locked": "0"}]
        balances += [
            {"asset": symbol[: -len(self.book.currency)] or symbol, "free": str(qty), "locked": "0"}
            for symbol, qty in self.book.holdings.items()
        ]
        return {"balances": balances}

    def _time(self, _params: Mapping[str, Any]) -> dict[str, Any]:
        return {"serverTime": int(self._server_time.timestamp() * 1000)}

    def _apiRestrictions(self, _params: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N802
        return {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}

    def _report(self, order: _Order, *, include_fills: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": order.symbol,
            "orderId": int(order.venue_order_id),
            "clientOrderId": order.client_order_id,
            "price": str(order.limit_price or "0.00000000"),
            "stopPrice": str(order.stop_price or "0.00000000"),
            "origQty": str(order.qty),
            "executedQty": str(order.filled),
            "status": _BINANCE_STATUS[order.status],
            "type": order.order_type.upper(),
            "side": order.side.upper(),
            "transactTime": int(_NOW.timestamp() * 1000),
        }
        if include_fills and order.trades:
            payload["fills"] = order.trades
        return payload


_BINANCE_STATUS = {
    "new": "NEW",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "canceled": "CANCELED",
    "expired": "EXPIRED",
    "rejected": "REJECTED",
}


class FakeAlpacaApi:
    """An `httpx.MockTransport` handler speaking Alpaca's trading API.

    Routed through the *real* `AlpacaTransport`, so the suite exercises our URL construction,
    headers, verb selection and status classification rather than stubbing them out.
    """

    def __init__(self, book: FakeVenueBook, *, clock_time: datetime | None = None) -> None:
        self.book = book
        self.requests: list[httpx.Request] = []
        #: Replace one path's response, keyed by URL path. A public seam for the same reason the
        #: Binance fake has one: reassigning `handler` after the transport is built does nothing,
        #: because `MockTransport` already captured the bound method.
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self._clock_time = clock_time or _NOW

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        override = self.overrides.get(path)
        if override is not None:
            return override(request)
        if path == "/v2/orders" and request.method == "POST":
            return self._place(request)
        if path == "/v2/orders" and request.method == "GET":
            return httpx.Response(200, json=[self._json(o) for o in self.book.open_orders()])
        if path == "/v2/orders:by_client_order_id":
            return self._lookup(request.url.params.get("client_order_id", ""))
        if path.startswith("/v2/orders/") and request.method == "DELETE":
            return self._cancel(path.rsplit("/", 1)[-1])
        if path == "/v2/account":
            return httpx.Response(200, json={"cash": str(self.book.cash), "currency": "USD"})
        if path == "/v2/positions":
            return httpx.Response(200, json=self._positions())
        if path == "/v2/account/activities/FILL":
            return httpx.Response(200, json=self._fills(request.url.params.get("order_id")))
        if path == "/v2/clock":
            return httpx.Response(200, json={"timestamp": self._clock_time.isoformat()})
        if path == "/v2/calendar":
            return httpx.Response(200, json=CALENDAR)
        if path == "/v2/corporate_actions/announcements":
            return httpx.Response(200, json=ANNOUNCEMENTS)
        return httpx.Response(404, json={"message": f"no route for {request.method} {path}"})

    # ------------------------------------------------------------------ routes

    def _place(self, request: httpx.Request) -> httpx.Response:
        import json

        params = json.loads(request.content)
        legs = params.get("take_profit"), params.get("stop_loss")
        try:
            if params.get("order_class") == "oco":
                return httpx.Response(200, json=self._oco(params, legs))
            order = self.book.place(params, key="client_order_id")
        except OrderRejectedError as exc:
            return httpx.Response(422, json={"message": exc.reason})
        except VenueError as exc:
            return httpx.Response(504, json={"message": str(exc)})
        return httpx.Response(200, json=self._json(order))

    def _oco(self, params: Mapping[str, Any], legs: tuple[Any, Any]) -> dict[str, Any]:
        take_profit, stop_loss = legs
        ids = (str(take_profit["client_order_id"]), str(stop_loss["client_order_id"]))
        children = []
        for leg, kind in ((take_profit, "limit"), (stop_loss, "stop_limit")):
            order = self.book.place(
                {
                    "symbol": params["symbol"],
                    "side": params["side"],
                    "type": kind,
                    "qty": params["qty"],
                    "limit_price": leg.get("limit_price"),
                    "stop_price": leg.get("stop_price"),
                    "client_order_id": leg["client_order_id"],
                },
                key="client_order_id",
                siblings=tuple(other for other in ids if other != str(leg["client_order_id"])),
            )
            children.append(self._json(order))
        return {
            "id": "oco-parent",
            "client_order_id": params.get("client_order_id"),
            "status": "new",
            "qty": params["qty"],
            "filled_qty": "0",
            "symbol": params["symbol"],
            "side": params["side"],
            "type": "limit",
            "legs": children,
        }

    def _lookup(self, client_order_id: str) -> httpx.Response:
        try:
            order = self.book.find(client_order_id)
        except OrderNotFoundError as exc:
            return httpx.Response(404, json={"message": str(exc)})
        return httpx.Response(200, json=self._json(order))

    def _cancel(self, venue_order_id: str) -> httpx.Response:
        found = next(
            (o for o in self.book.orders.values() if o.venue_order_id == venue_order_id), None
        )
        if found is None:
            return httpx.Response(404, json={"message": "order not found"})
        try:
            self.book.cancel(found.client_order_id)
        except OrderRejectedError as exc:
            return httpx.Response(422, json={"message": exc.reason})
        return httpx.Response(204)

    def _positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": symbol, "qty": str(qty), "avg_entry_price": "100"}
            for symbol, qty in self.book.holdings.items()
            if qty > 0
        ]

    def _fills(self, order_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "id": f"act-{trade['id']}",
                "order_id": order.venue_order_id,
                "side": order.side,
                "qty": trade["qty"],
                "price": trade["price"],
                "transaction_time": trade["transaction_time"],
            }
            for order in self.book.orders.values()
            for trade in order.trades
            if order_id is None or order.venue_order_id == order_id
        ]

    def _json(self, order: _Order) -> dict[str, Any]:
        return {
            "id": order.venue_order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type,
            "qty": str(order.qty),
            "filled_qty": str(order.filled),
            "limit_price": None if order.limit_price is None else str(order.limit_price),
            "stop_price": None if order.stop_price is None else str(order.stop_price),
            "status": order.status,
            "submitted_at": _NOW.isoformat(),
        }


#: Two consecutive sessions, so `next_open` has something to find. Alpaca publishes local times.
CALENDAR = [
    {"date": "2026-07-30", "open": "09:30", "close": "16:00"},
    {"date": "2026-07-31", "open": "09:30", "close": "16:00"},
]

#: A 3-for-1 split, plus a cash dividend that must *not* be read as a share-count change.
ANNOUNCEMENTS = [
    {
        "ca_type": "split",
        "target_symbol": "AAPL",
        "old_rate": "1",
        "new_rate": "3",
        "effective_date": "2026-07-29",
    },
    {
        "ca_type": "cash_dividend",
        "target_symbol": "AAPL",
        "cash": "0.24",
        "effective_date": "2026-07-29",
    },
]


def alpaca_transport(api: FakeAlpacaApi, clock: Any, **kwargs: Any) -> AlpacaTransport:
    """The real transport over a mocked network, which is the point of the exercise."""
    from tradebot.core.enums import Mode

    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return AlpacaTransport(
        client,
        clock,
        mode=kwargs.pop("mode", Mode.PAPER),
        key_id=kwargs.pop("key_id", "test-key"),
        secret_key=kwargs.pop("secret_key", "test-secret"),
        **kwargs,
    )
