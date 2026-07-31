"""Alpaca as a `BrokerAdapter`: US equities, and the proof that nothing above is crypto-shaped.

This is the reference non-crypto integration (PLAN Phase 5). Four things differ from a spot
crypto venue, and each of them lives here rather than leaking upward:

* **The market closes.** `AlpacaCalendar` answers when it is open and which *session* a moment
  belongs to, which is also the day boundary the daily-loss baseline resets on for equities
  (DESIGN §6.6). Crypto's UTC midnight would be the wrong answer for both.
* **Positions are positions, not balances.** Alpaca reports holdings and cash separately, so
  unlike a spot venue there is nothing to de-duplicate.
* **Protective exits are order *classes*.** A linked pair is `order_class=oco` over an existing
  position; there is no way to place two independent legs and call them linked.
* **Corporate actions happen.** A split changes the share count with no fill behind it, and
  without the announcement feed the reconciler correctly calls that a mismatch and halts a basket
  for a routine event (R14). `AlpacaAnnouncements` is what prevents that.

Deliberately **not** ported: anything touching PDT fields (`pattern_day_trader`,
`daytrade_count`, `daytrading_buying_power`). FINRA retired the pattern-day-trader rule and Alpaca
replaced it with the Intraday Margin Framework in June 2026; those fields are gone from the API,
and code reading them would be reading `None` and drawing conclusions from it (PLAN §5).

Failure semantics match every other adapter, because one contract suite holds all of them: a
rejection is a result (`OrderState.REJECTED`), a vanished order is `found=False`, an ambiguous
placement raises `SubmitUnknownError` from the transport and may only be resolved by query
(PLAN §2.3). An unreachable announcement feed returns nothing, which fails closed into a halt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import DataStaleError, OrderNotFoundError, OrderRejectedError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO, to_decimal
from tradebot.core.orders import Fill, OrderIntent
from tradebot.core.portfolio import AccountState, Balance, CorporateAction, Position
from tradebot.core.schema import Money
from tradebot.interfaces.broker import (
    BrokerCapabilities,
    CancelAck,
    OrderAck,
    OrderRef,
    OrderStatus,
)
from tradebot.interfaces.exchange import TradingTransport

logger = get_logger(__name__)

VENUE_ID: Final = "alpaca"

#: The exchange session's own timezone. A "trading day" is a New York date, not a UTC one: an
#: extended-hours print at 01:00 UTC belongs to the previous session, and rolling the daily-loss
#: baseline at UTC midnight would reset it in the middle of one.
EXCHANGE_TZ: Final = ZoneInfo("America/New_York")

#: Every Alpaca call costs one unit of a request-count budget (see `venues/alpaca_transport.py`).
#: Uniform because Alpaca publishes a request limit, not a weight table.
WEIGHT: Final = 1

#: Alpaca order status → our lifecycle. The statuses Alpaca calls "pending" are all still working
#: as far as we are concerned: the venue may yet fill them, so the monitor must keep polling.
ORDER_STATES: Final[Mapping[str, OrderState]] = {
    "new": OrderState.OPEN,
    "accepted": OrderState.OPEN,
    "pending_new": OrderState.OPEN,
    "accepted_for_bidding": OrderState.OPEN,
    "held": OrderState.OPEN,
    "calculated": OrderState.OPEN,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "pending_cancel": OrderState.CANCELLED,
    "expired": OrderState.EXPIRED,
    "replaced": OrderState.CANCELLED,
    "pending_replace": OrderState.OPEN,
    "rejected": OrderState.REJECTED,
    "suspended": OrderState.REJECTED,
    "stopped": OrderState.REJECTED,
    "done_for_day": OrderState.EXPIRED,
}

#: Our order types → Alpaca's `type`. A stop-limit carries both a trigger and a limit; Alpaca
#: spells the trigger `stop_price` for both directions, so the take-profit leg is a plain limit.
ORDER_TYPES: Final[Mapping[OrderType, str]] = {
    OrderType.LIMIT: "limit",
    OrderType.MARKET: "market",
    OrderType.STOP_LOSS_LIMIT: "stop_limit",
    OrderType.TAKE_PROFIT_LIMIT: "limit",
}

#: Where each leg's prices go in an `oco` order. Alpaca models the pair as a take-profit limit and
#: a stop-loss trigger, not as two free-standing orders — which is exactly the linkage we need.
_OCO_LEG_FIELDS: Final[Mapping[OrderRole, str]] = {
    OrderRole.TAKE_PROFIT: "take_profit",
    OrderRole.STOP_LOSS: "stop_loss",
}

#: Alpaca's own cap on `client_order_id`. Our scheme produces 20 characters, well inside it, and
#: the contract suite asserts that rather than assuming it (PLAN §5).
MAX_CLIENT_ORDER_ID: Final = 48

#: Announcement `ca_type` values that change a share count. A cash dividend does not, so it is
#: read for the record but never used to explain a quantity difference.
_SHARE_CHANGING_TYPES: Final = frozenset({"split", "reverse_split", "stock_dividend", "merger"})

#: Days of calendar fetched when looking for the next open. Long enough to clear a long weekend
#: plus a holiday, short enough to stay one small response.
CALENDAR_SPAN: Final = 8


#: Alpaca's wire type → ours, for reading an order back. Written out rather than inverted from
#: `ORDER_TYPES`, because that mapping is deliberately many-to-one: a take-profit leg *is* a plain
#: resting limit at Alpaca, and reading it back as one is what lets the self-trade check see it.
_ORDER_TYPES_BY_WIRE: Final[Mapping[str, OrderType]] = {
    "limit": OrderType.LIMIT,
    "market": OrderType.MARKET,
    "stop_limit": OrderType.STOP_LOSS_LIMIT,
    "stop": OrderType.STOP_LOSS_LIMIT,
}


def _decimal(payload: Mapping[str, Any], key: str, *, default: Decimal | None = None) -> Decimal:
    value = payload.get(key)
    if value is None or value == "":
        if default is not None:
            return default
        raise DataStaleError(f"alpaca response is missing {key}")
    return to_decimal(value)


def _optional_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    """A price the venue may legitimately not carry — a market order has none."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    parsed = to_decimal(value)
    return parsed if parsed > ZERO else None


def _side(value: Any) -> Side | None:
    return {"buy": Side.BUY, "sell": Side.SELL}.get(str(value).lower())


def _timestamp(payload: Mapping[str, Any], key: str, fallback: datetime) -> datetime:
    """An RFC 3339 timestamp, or the observation time when the venue omits one."""
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        return fallback
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return fallback


def parse_state(payload: Mapping[str, Any]) -> OrderState:
    status = str(payload.get("status") or "").lower()
    state = ORDER_STATES.get(status)
    if state is None:
        raise DataStaleError(f"alpaca reported an unknown order status {status!r}")
    return state


def parse_account(
    account: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    instruments: Sequence[Instrument],
    observed_at: datetime,
) -> AccountState:
    """Alpaca's account and positions → the venue's own view.

    `avg_entry` is taken from Alpaca here, unlike on a spot crypto venue, because Alpaca actually
    tracks it per position — but the ledger still owns PnL: this is the figure reconciliation
    compares against, not the figure it books from (DESIGN §6.8).
    """
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    return AccountState(
        venue=VENUE_ID,
        positions=tuple(
            Position(
                instrument_key=instrument.key,
                qty=_decimal(entry, "qty", default=ZERO),
                avg_entry=_decimal(entry, "avg_entry_price", default=ZERO),
            )
            for entry in positions
            if (instrument := by_symbol.get(str(entry.get("symbol")))) is not None
        ),
        # Alpaca publishes no locked-cash figure: `cash` does not fall when a buy order rests. The
        # ledger holds balances as totals and treats the free/locked split as venue presentation
        # only, so reporting the whole balance as free is accurate rather than convenient.
        balances=(
            Balance(
                currency=str(account.get("currency") or "USD"),
                free=_decimal(account, "cash", default=ZERO),
            ),
        ),
        observed_at=observed_at,
    )


def parse_announcement(
    payload: Mapping[str, Any], instruments: Sequence[Instrument]
) -> CorporateAction | None:
    """One Alpaca announcement → a `CorporateAction`, or nothing if it changes no share count.

    The ratio is `new_rate / old_rate`: a 3-for-1 split publishes `old_rate=1, new_rate=3`, so a
    100-share holding becomes 300. Getting this inverted would turn a split into a mismatch and a
    mismatch into a split, which is why it is parsed in one place with its own test.
    """
    ca_type = str(payload.get("ca_type") or "").lower()
    symbol = str(payload.get("target_symbol") or payload.get("initiating_symbol") or "")
    instrument = next((i for i in instruments if i.symbol == symbol), None)
    if instrument is None or ca_type not in _SHARE_CHANGING_TYPES:
        return None
    old_rate = _decimal(payload, "old_rate", default=Decimal(1))
    new_rate = _decimal(payload, "new_rate", default=Decimal(1))
    if old_rate <= ZERO:
        raise DataStaleError(f"alpaca announcement for {symbol} has a non-positive old_rate")
    return CorporateAction(
        instrument_key=instrument.key,
        ratio=new_rate / old_rate,
        cash_per_share=_decimal(payload, "cash", default=ZERO),
        detail=f"{ca_type} {old_rate}:{new_rate}",
        effective_on=str(payload.get("effective_date") or payload.get("ex_date") or ""),
    )


class AlpacaBroker:
    """`BrokerAdapter` for Alpaca equities over a signed httpx transport."""

    venue_id = VENUE_ID

    def __init__(
        self,
        transport: TradingTransport,
        clock: Clock,
        *,
        instruments: Sequence[Instrument],
        extended_hours: bool = False,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._instruments = {instrument.key: instrument for instrument in instruments}
        self._by_symbol = {instrument.symbol: instrument for instrument in instruments}
        self._extended_hours = extended_hours

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            venue_id=self.venue_id,
            order_types=tuple(ORDER_TYPES),
            protective_orders=True,
            # Alpaca's `oco` order class is a genuinely linked pair, so `submit_group` can honour
            # the contract. Extended-hours orders cannot be bracketed, which is one reason
            # extended hours is off unless asked for.
            oco_groups=not self._extended_hours,
            fractional_quantities=True,
            query_by_client_order_id=True,
            max_client_order_id_length=MAX_CLIENT_ORDER_ID,
            # Alpaca has `day` and `gtc`, neither of which is a good-till-*time*. TTL stays
            # bot-enforced so every venue behaves identically (DESIGN §6.7).
            venue_side_ttl=False,
        )

    # ------------------------------------------------------------------ trading

    async def submit(self, intent: OrderIntent) -> OrderAck:
        instrument = self._instrument(intent.instrument_key)
        try:
            payload = await self._transport.call(
                "POST /v2/orders",
                self._order_params(intent, instrument),
                weight=WEIGHT,
                is_order=True,
            )
        except OrderRejectedError as exc:
            return self._rejected(intent.client_order_id, exc.reason)
        return self._ack(intent.client_order_id, payload)

    async def submit_group(self, intents: Sequence[OrderIntent]) -> tuple[OrderAck, ...]:
        """Place linked exit legs as one Alpaca `oco` order.

        Alpaca returns a *single* order carrying both legs as children, so each of our leg ids maps
        to one child. A leg the venue does not report on fails closed rather than being assumed
        placed: the order exists either way, and recovery resolves it by query (ADR 0011).
        """
        legs = tuple(intents)
        if len(legs) == 1:
            return (await self.submit(legs[0]),)
        instrument = self._instrument(legs[0].instrument_key)
        try:
            payload = await self._transport.call(
                "POST /v2/orders", self._oco_params(legs, instrument), weight=WEIGHT, is_order=True
            )
        except OrderRejectedError as exc:
            return tuple(self._rejected(leg.client_order_id, exc.reason) for leg in legs)

        children = {str(child.get("client_order_id")): child for child in payload.get("legs") or ()}
        missing = [leg.client_order_id for leg in legs if leg.client_order_id not in children]
        if missing:
            raise DataStaleError(
                f"alpaca accepted the OCO order but reported no leg for {', '.join(missing)}; "
                "the legs exist at the venue and must be resolved by query, not assumed"
            )
        return tuple(self._ack(leg.client_order_id, children[leg.client_order_id]) for leg in legs)

    async def cancel(self, order_ref: OrderRef) -> CancelAck:
        """Cancel by venue id where we have it, else resolve our id first.

        Alpaca cancels by *its* id only. An order we have no venue id for has to be looked up, and
        one the venue has already finished with reports `cancelled=False` rather than raising —
        losing a cancel race to a fill is the normal case, and the next poll books the fill.
        """
        venue_order_id = order_ref.venue_order_id or await self._resolve_venue_id(order_ref)
        if venue_order_id is None:
            return CancelAck(
                client_order_id=order_ref.client_order_id,
                cancelled=False,
                detail="alpaca has no record of this order",
            )
        try:
            await self._transport.call(f"DELETE /v2/orders/{venue_order_id}", {}, weight=WEIGHT)
        except (OrderNotFoundError, OrderRejectedError) as exc:
            return CancelAck(
                client_order_id=order_ref.client_order_id, cancelled=False, detail=str(exc)
            )
        return CancelAck(client_order_id=order_ref.client_order_id, cancelled=True)

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        """Look up by `client_order_id` — the only legal resolution of `SUBMIT_UNKNOWN`."""
        try:
            payload = await self._transport.call(
                "GET /v2/orders:by_client_order_id",
                {"client_order_id": order_ref.client_order_id},
                weight=WEIGHT,
            )
        except OrderNotFoundError as exc:
            return self._vanished(order_ref, str(exc))
        return await self._status(payload, self._instrument(order_ref.instrument_key))

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        payload = await self._transport.call(
            "GET /v2/orders", {"status": "open", "nested": "true"}, weight=WEIGHT
        )
        return tuple(
            self._status_without_fills(entry, instrument)
            for entry in (payload if isinstance(payload, list) else ())
            if (instrument := self._by_symbol.get(str(entry.get("symbol")))) is not None
        )

    async def fetch_positions_and_balances(self) -> AccountState:
        account = await self._transport.call("GET /v2/account", {}, weight=WEIGHT)
        positions = await self._transport.call("GET /v2/positions", {}, weight=WEIGHT)
        if not isinstance(account, dict):
            raise DataStaleError("alpaca account returned a non-object payload")
        return parse_account(
            account,
            positions if isinstance(positions, list) else (),
            tuple(self._instruments.values()),
            self._clock.now(),
        )

    async def server_time(self) -> datetime:
        payload = await self._transport.call("GET /v2/clock", {}, weight=WEIGHT)
        if not isinstance(payload, dict) or not payload.get("timestamp"):
            raise DataStaleError("alpaca clock returned no timestamp")
        return _timestamp(payload, "timestamp", self._clock.now())

    async def close(self) -> None:
        await self._transport.close()

    # ------------------------------------------------------------------ internals

    def _instrument(self, instrument_key: str) -> Instrument:
        instrument = self._instruments.get(instrument_key)
        if instrument is None:
            raise DataStaleError(
                f"{instrument_key} is not configured on this alpaca adapter; refusing to trade an "
                "instrument whose precision and minimums are unknown"
            )
        return instrument

    def _order_params(self, intent: OrderIntent, instrument: Instrument) -> dict[str, Any]:
        order_type = ORDER_TYPES.get(intent.order_type)
        if order_type is None:
            raise DataStaleError(f"alpaca cannot express {intent.order_type}")
        params: dict[str, Any] = {
            "symbol": instrument.symbol,
            "side": intent.side.value,
            "type": order_type,
            "qty": _wire(intent.qty),
            "time_in_force": "day",
            "client_order_id": intent.client_order_id,
            "extended_hours": self._extended_hours,
        }
        if intent.limit_price is not None:
            params["limit_price"] = _wire(intent.limit_price)
        if intent.stop_price is not None and intent.order_type is OrderType.STOP_LOSS_LIMIT:
            params["stop_price"] = _wire(intent.stop_price)
        return params

    def _oco_params(self, legs: Sequence[OrderIntent], instrument: Instrument) -> dict[str, Any]:
        quantities = {leg.qty for leg in legs}
        if len(quantities) != 1:
            raise DataStaleError(
                f"alpaca OCO takes one quantity for both legs, got {sorted(quantities)}"
            )
        params: dict[str, Any] = {
            "symbol": instrument.symbol,
            "side": legs[0].side.value,
            "type": "limit",
            "qty": _wire(legs[0].qty),
            "time_in_force": "gtc",
            "order_class": "oco",
            "client_order_id": legs[0].group_id or legs[0].client_order_id,
        }
        for leg in legs:
            field = _OCO_LEG_FIELDS.get(leg.role)
            if field is None:
                raise DataStaleError(f"{leg.role} is not an exit leg and cannot join an OCO order")
            params[field] = self._leg_prices(leg, field)
        return params

    @staticmethod
    def _leg_prices(leg: OrderIntent, field: str) -> dict[str, Any]:
        """A take-profit is a limit; a stop is a trigger with an optional limit through it."""
        if field == "take_profit":
            return {"limit_price": _wire(leg.limit_price), "client_order_id": leg.client_order_id}
        return {
            "stop_price": _wire(leg.stop_price),
            "limit_price": _wire(leg.limit_price),
            "client_order_id": leg.client_order_id,
        }

    async def _resolve_venue_id(self, order_ref: OrderRef) -> str | None:
        try:
            payload = await self._transport.call(
                "GET /v2/orders:by_client_order_id",
                {"client_order_id": order_ref.client_order_id},
                weight=WEIGHT,
            )
        except OrderNotFoundError:
            return None
        found = payload.get("id") if isinstance(payload, dict) else None
        return str(found) if found else None

    def _ack(self, client_order_id: str, payload: Mapping[str, Any]) -> OrderAck:
        return OrderAck(
            client_order_id=client_order_id,
            venue_order_id=str(payload.get("id")) if payload.get("id") else None,
            state=parse_state(payload),
            accepted_at=_timestamp(payload, "submitted_at", self._clock.now()),
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
        return OrderStatus(
            client_order_id=str(payload.get("client_order_id") or ""),
            venue_order_id=str(payload.get("id")) if payload.get("id") else None,
            instrument_key=instrument.key,
            state=parse_state(payload),
            requested_qty=_decimal(payload, "qty", default=ZERO),
            filled_qty=_decimal(payload, "filled_qty", default=ZERO),
            fills=fills,
            observed_at=self._clock.now(),
            reject_reason=None,
            side=_side(payload.get("side")),
            order_type=_ORDER_TYPES_BY_WIRE.get(str(payload.get("type") or "")),
            limit_price=_optional_decimal(payload, "limit_price"),
            stop_price=_optional_decimal(payload, "stop_price"),
        )

    async def _status(self, payload: Mapping[str, Any], instrument: Instrument) -> OrderStatus:
        return self._status_without_fills(
            payload, instrument, await self._fills(payload, instrument)
        )

    async def _fills(self, payload: Mapping[str, Any], instrument: Instrument) -> tuple[Fill, ...]:
        """Individual executions from the activities feed, fetched only when something filled.

        Alpaca's order object carries `filled_qty` and `filled_avg_price` — a cumulative average,
        not the executions. Positions may only move on fills, and a fill needs a stable id to be
        booked idempotently across polls (PLAN §2.5), so the fill activities are the source. They
        are asked for only when the order reports a fill at all, which keeps a resting order free.
        """
        if _decimal(payload, "filled_qty", default=ZERO) <= ZERO:
            return ()
        venue_order_id = payload.get("id")
        activities = await self._transport.call(
            "GET /v2/account/activities/FILL",
            {"order_id": venue_order_id} if venue_order_id else {},
            weight=WEIGHT,
        )
        client_order_id = str(payload.get("client_order_id") or "")
        observed_at = self._clock.now()
        return tuple(
            self._fill(entry, client_order_id, instrument, observed_at)
            for entry in (activities if isinstance(activities, list) else ())
            if not venue_order_id or str(entry.get("order_id")) == str(venue_order_id)
        )

    def _fill(
        self,
        entry: Mapping[str, Any],
        client_order_id: str,
        instrument: Instrument,
        observed_at: datetime,
    ) -> Fill:
        """One FILL activity → one `Fill`. Equities are commission-free here, so `fee` is zero.

        Regulatory pass-through fees are billed separately by Alpaca rather than per execution;
        booking a fee we cannot attribute would misstate the cost basis of this trade.
        """
        return Fill(
            fill_id=f"{VENUE_ID}-{entry.get('id')}",
            client_order_id=client_order_id,
            instrument_key=instrument.key,
            # A fill activity's side is `"buy"`/`"sell"`, sometimes suffixed (`"sell_short"`).
            side=Side.SELL if str(entry.get("side")).lower().startswith("sell") else Side.BUY,
            qty=_decimal(entry, "qty"),
            price=_decimal(entry, "price"),
            fee=ZERO,
            fee_currency=instrument.quote_currency,
            filled_at=_timestamp(entry, "transaction_time", observed_at),
        )


class AlpacaCalendar:
    """When the exchange is open, from Alpaca's own calendar (`TradingCalendar`).

    Fetched, never computed: US market holidays, half days and the odd unscheduled closure are a
    published list, and a hand-rolled weekday check would cycle a basket into a closed market on
    Thanksgiving. Results are cached per day because the calendar for a past day cannot change.
    """

    venue_id = VENUE_ID

    def __init__(self, transport: TradingTransport, clock: Clock) -> None:
        self._transport = transport
        self._clock = clock
        self._sessions: dict[str, tuple[datetime, datetime]] = {}

    async def is_open(self, at: datetime) -> bool:
        session = await self._session(at.astimezone(EXCHANGE_TZ).date())
        return session is not None and session[0] <= at < session[1]

    async def session_day(self, at: datetime) -> str:
        """The trading day `at` belongs to — a New York date, not a UTC one.

        This is what the daily-loss baseline rolls over on for equities. A UTC date would reset it
        at 19:00 the previous session, in the middle of trading (DESIGN §6.6).
        """
        return at.astimezone(EXCHANGE_TZ).date().isoformat()

    async def next_open(self, after: datetime) -> datetime | None:
        if await self.is_open(after):
            return None
        for entry in await self._calendar(after.astimezone(EXCHANGE_TZ).date(), days=CALENDAR_SPAN):
            opens = _session_bounds(entry)
            if opens is not None and opens[0] > after:
                return opens[0]
        return None

    async def _session(self, day: date) -> tuple[datetime, datetime] | None:
        key = day.isoformat()
        if key not in self._sessions:
            for entry in await self._calendar(day, days=1):
                bounds = _session_bounds(entry)
                if bounds is not None:
                    self._sessions[str(entry.get("date"))] = bounds
        return self._sessions.get(key)

    async def _calendar(self, start: date, *, days: int) -> Sequence[Mapping[str, Any]]:
        payload = await self._transport.call(
            "GET /v2/calendar",
            {"start": start.isoformat(), "end": (start + timedelta(days=days)).isoformat()},
            weight=WEIGHT,
        )
        return payload if isinstance(payload, list) else ()


class AlpacaAnnouncements:
    """Venue-announced splits and dividends (`CorporateActionSource`).

    Failure semantics: an unreachable feed logs and returns nothing. That leaves an unexplained
    share-count change classified as a mismatch, which halts the affected basket — the fail-closed
    direction. Silently inventing an explanation is the failure that would actually cost money.
    """

    def __init__(self, transport: TradingTransport) -> None:
        self._transport = transport

    async def fetch(
        self, instruments: Sequence[Instrument], *, since: date, until: date
    ) -> tuple[CorporateAction, ...]:
        symbols = sorted({instrument.symbol for instrument in instruments})
        if not symbols:
            return ()
        try:
            payload = await self._transport.call(
                "GET /v2/corporate_actions/announcements",
                {
                    "ca_types": "split,reverse_split,stock_dividend,merger",
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "symbol": ",".join(symbols),
                },
                weight=WEIGHT,
            )
        except (DataStaleError, OrderNotFoundError, OrderRejectedError) as exc:
            logger.warning("alpaca announcements unavailable", extra={"error": str(exc)})
            return ()
        actions = (
            parse_announcement(entry, instruments)
            for entry in (payload if isinstance(payload, list) else ())
        )
        return tuple(action for action in actions if action is not None)


def _session_bounds(entry: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """A calendar row → the session's UTC open and close.

    Alpaca publishes local exchange times (`"09:30"`), so they are localised to the exchange's own
    timezone before conversion. Treating them as UTC would place every session four or five hours
    early and shift with daylight saving.
    """
    day, opens, closes = entry.get("date"), entry.get("open"), entry.get("close")
    if not (isinstance(day, str) and isinstance(opens, str) and isinstance(closes, str)):
        return None
    try:
        session_date = date.fromisoformat(day)
        start = time.fromisoformat(opens)
        end = time.fromisoformat(closes)
    except ValueError:
        return None
    return (
        datetime.combine(session_date, start, EXCHANGE_TZ).astimezone(UTC),
        datetime.combine(session_date, end, EXCHANGE_TZ).astimezone(UTC),
    )


def _wire(value: Money | None) -> str | None:
    """Plain fixed-point digits. `str(Decimal("1E-5"))` is `1E-5`, which venues reject."""
    return None if value is None else f"{value:f}"
