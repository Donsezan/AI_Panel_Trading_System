"""The ledger: positions and balances, driven by fills and nothing else.

Three rules make this module trustworthy:

* **Fills only.** A position never moves because an order reached a terminal state. Partial
  fills are the normal case, and terminal states lie about them (PLAN §2.5). The one exception
  is an `EXTERNAL_CHANGE` from the reconciler, which is venue truth arriving by another route.
* **Read-only outward.** Everything else — including the panel — receives a snapshot. The LLM
  never reads or modifies the ledger, because a hallucinated balance becomes a real order size
  (DESIGN [L4]).
* **Reconstructable.** `replay` rebuilds the whole ledger from the event log, which is what
  makes the log the source of truth rather than a diary the ledger keeps alongside its real
  state (DESIGN §8.2 step 1).

Balances are held as **totals**. What is free versus locked behind a resting order is venue
truth, adopted by the reconciler rather than mirrored here: two independent sets of books on
the same funds drift, and the drift shows up as phantom headroom in a risk check.

**The ledger knows what is held, never what it is worth.** Valuation lives in one place —
`risk.aggregate` — because six call sites each building their own price map is how the drawdown
gate came to measure cost basis and never see an unrealized loss (PHASE_12 Finding 1, ADR 0027).
The one pricing method left here, `exposure`, takes a strict map and raises rather than falling
back to what a position cost.

Failure semantics: a sell larger than the holding raises rather than going negative — v1 is
long-only, so a negative position is a corrupted ledger, not a short.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import Side
from tradebot.core.errors import ReconciliationMismatchError
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO, divide, multiply, to_decimal
from tradebot.core.orders import Fill
from tradebot.core.portfolio import AccountState, Balance, Position, RoundTrip
from tradebot.core.schema import UtcDatetime


@dataclass(slots=True)
class _OpenTrip:
    """Cost and proceeds accumulated since a position last left flat."""

    qty: Decimal = ZERO
    cost: Decimal = ZERO
    proceeds: Decimal = ZERO
    realized: Decimal = ZERO
    opened_at: UtcDatetime | None = None

    def closed(self, instrument_key: str, at: UtcDatetime) -> RoundTrip:
        return RoundTrip(
            instrument_key=instrument_key,
            qty=self.qty,
            entry_price=divide(self.cost, self.qty) if self.qty > ZERO else ZERO,
            exit_price=divide(self.proceeds, self.qty) if self.qty > ZERO else ZERO,
            realized_pnl=self.realized,
            opened_at=self.opened_at,
            closed_at=at,
        )


@dataclass(frozen=True, slots=True)
class Booking:
    """What a fill did: the new position, and the round trip it closed, if any."""

    position: Position
    round_trip: RoundTrip | None = None


@dataclass(frozen=True, slots=True)
class ExternalFlow:
    """A balance change that is not trading PnL — a deposit, withdrawal or manual transfer.

    Flow-adjusting the drawdown baselines with these is what stops a withdrawal from reading as
    a drawdown and a deposit from masking a real loss (DESIGN §6.6).
    """

    currency: str
    amount: Decimal
    reason: str = ""

    @property
    def is_withdrawal(self) -> bool:
        return self.amount < ZERO


class Ledger:
    """Reconciled holdings and balances for one venue portfolio."""

    def __init__(self, clock: Clock, *, venue: str, balances: Mapping[str, Decimal]) -> None:
        self._clock = clock
        self._venue = venue
        self._opening = dict(balances)
        self._balances: dict[str, Decimal] = dict(balances)
        self._locked: dict[str, Decimal] = {}
        self._positions: dict[str, Position] = {}
        self._trips: dict[str, _OpenTrip] = {}

    @property
    def venue(self) -> str:
        """Which venue portfolio this is. The key it appears under in the aggregate."""
        return self._venue

    def position(self, instrument_key: str) -> Position:
        """The holding, or a flat position. Never `None` — absence is a position of zero."""
        return self._positions.get(instrument_key) or Position(instrument_key=instrument_key)

    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def balance(self, currency: str) -> Decimal:
        """Total funds in a currency: free plus whatever a resting order has locked."""
        return self._balances.get(currency, ZERO)

    # ------------------------------------------------------------------ mutation

    def apply_fill(self, fill: Fill, *, base_currency: str, quote_currency: str) -> Booking:
        """Book a fill. The only trading mutation this class exposes."""
        before = self.position(fill.instrument_key)
        updated = _APPLIERS[fill.side](self, fill)
        self._positions[fill.instrument_key] = updated
        self._move_cash(fill, base_currency, quote_currency)
        realized = updated.realized_pnl - before.realized_pnl
        return Booking(position=updated, round_trip=self._settle_trip(fill, updated, realized))

    def _apply_buy(self, fill: Fill) -> Position:
        current = self.position(fill.instrument_key)
        new_qty = current.qty + fill.qty
        cost = multiply(current.qty, current.avg_entry) + fill.notional
        return current.model_copy(
            update={
                "qty": new_qty,
                "avg_entry": divide(cost, new_qty),
                "opened_at": current.opened_at or fill.filled_at,
            }
        )

    def _apply_sell(self, fill: Fill) -> Position:
        current = self.position(fill.instrument_key)
        if fill.qty > current.qty:
            raise ReconciliationMismatchError(
                f"sell of {fill.qty} exceeds holding {current.qty} on {fill.instrument_key}; "
                "v1 is long-only, so this is ledger corruption rather than a short"
            )
        realized = multiply(fill.qty, fill.price - current.avg_entry)
        remaining = current.qty - fill.qty
        return current.model_copy(
            update={
                "qty": remaining,
                "avg_entry": current.avg_entry if remaining > ZERO else ZERO,
                "realized_pnl": current.realized_pnl + realized,
                "opened_at": current.opened_at if remaining > ZERO else None,
            }
        )

    def _move_cash(self, fill: Fill, base_currency: str, quote_currency: str) -> None:
        direction = Decimal(1) if fill.side is Side.BUY else Decimal(-1)
        self._balances[base_currency] = self.balance(base_currency) + multiply(fill.qty, direction)
        self._balances[quote_currency] = (
            self.balance(quote_currency) - multiply(fill.notional, direction) - fill.fee
        )

    def _settle_trip(self, fill: Fill, position: Position, realized: Decimal) -> RoundTrip | None:
        """Accumulate the open round trip, and close it when the position goes flat.

        Fees count against the trip on both legs: a scratch exit that paid two commissions is a
        losing trade, and the consecutive-loss rule should see it as one.
        """
        trip = self._trips.setdefault(fill.instrument_key, _OpenTrip())
        trip.realized += realized - fill.fee
        if fill.side is Side.BUY:
            trip.qty += fill.qty
            trip.cost += fill.notional
            trip.opened_at = trip.opened_at or fill.filled_at
        else:
            trip.proceeds += fill.notional
        if not position.is_flat:
            return None
        del self._trips[fill.instrument_key]
        return trip.closed(fill.instrument_key, fill.filled_at)

    def apply_external_change(self, flow: ExternalFlow) -> Decimal:
        """Absorb a deposit, withdrawal or manual transfer. Returns the new total."""
        self._balances[flow.currency] = self.balance(flow.currency) + flow.amount
        return self._balances[flow.currency]

    def adopt_position(self, position: Position) -> Position:
        """Replace a holding with the venue's own view of it (reconciler only)."""
        self._positions[position.instrument_key] = position
        if position.is_flat:
            self._trips.pop(position.instrument_key, None)
        return position

    def set_locked(self, currency: str, amount: Decimal) -> None:
        """Adopt the venue's view of what resting orders have tied up."""
        self._locked[currency] = amount

    # ------------------------------------------------------------------ valuation

    def exposure(self, instrument_keys: tuple[str, ...], prices: Mapping[str, Decimal]) -> Decimal:
        """Value currently deployed across a set of instruments — a basket's exposure.

        `prices` is **strict**: a held key it does not carry raises rather than falling back to
        `avg_entry`. The caller has already decided what an unmarked position means, and the answer
        is a frozen aggregate — never a position quietly valued at what it cost, which reports zero
        drawdown on a portfolio that has halved (PHASE_12 Finding 1, ADR 0027).
        """
        return sum(
            (
                position.market_value(prices[key])
                for key in instrument_keys
                if not (position := self.position(key)).is_flat
            ),
            start=ZERO,
        )

    def realized_pnl(self) -> Decimal:
        return sum((position.realized_pnl for position in self._positions.values()), start=ZERO)

    def snapshot(self) -> AccountState:
        """Read-only view for the reconciler, the dashboard and the context builder."""
        return AccountState(
            venue=self._venue,
            positions=self.positions(),
            balances=tuple(
                Balance(
                    currency=currency,
                    free=amount - self._locked.get(currency, ZERO),
                    locked=self._locked.get(currency, ZERO),
                )
                for currency, amount in sorted(self._balances.items())
            ),
            observed_at=self._clock.now(),
        )

    # ------------------------------------------------------------------ recovery

    def replay(self, events: tuple[Event, ...], instruments: Mapping[str, tuple[str, str]]) -> int:
        """Rebuild from the event log. `instruments` maps key → (base, quote) currency.

        Startup does this before anything trades, so the in-memory ledger provably derives from
        the audit trail rather than from whatever the last process happened to leave behind.
        """
        self._balances = dict(self._opening)
        self._locked, self._positions, self._trips = {}, {}, {}
        applied = 0
        for event in events:
            handler = _REPLAY.get(event.type)
            if handler is not None:
                handler(self, event, instruments)
                applied += 1
        return applied

    def _replay_fill(self, event: Event, instruments: Mapping[str, tuple[str, str]]) -> None:
        fill = Fill.model_validate(event.payload["fill"])
        base, quote = instruments.get(fill.instrument_key, ("", ""))
        if not base:
            raise ReconciliationMismatchError(
                f"cannot replay a fill on unknown instrument {fill.instrument_key}"
            )
        self.apply_fill(fill, base_currency=base, quote_currency=quote)

    def _replay_external(self, event: Event, _instruments: Mapping[str, tuple[str, str]]) -> None:
        self.apply_external_change(
            ExternalFlow(
                currency=event.payload["currency"],
                amount=to_decimal(event.payload["amount"]),
                reason=event.payload.get("reason", ""),
            )
        )


_APPLIERS = {Side.BUY: Ledger._apply_buy, Side.SELL: Ledger._apply_sell}

_REPLAY = {
    EventType.FILL_RECEIVED: Ledger._replay_fill,
    EventType.EXTERNAL_CHANGE: Ledger._replay_external,
}
