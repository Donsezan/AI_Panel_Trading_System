"""Venue access. The layer that deserves most of the engineering effort (DESIGN [L11]).

Failure semantics, identical for every implementation and asserted by one shared contract
suite:

* A submit whose outcome is unknown (timeout, 5xx, reset) raises `SubmitUnknownError` carrying
  the `client_order_id`. It must **never** raise a generic error that a caller could retry —
  blind resubmission is the duplicate-order failure this whole design exists to prevent.
* Transient venue failures raise `VenueError` / `RateLimitedError`; the caller retries within
  its budget.
* A rejected order is a *result*, not an exception: it comes back as `OrderState.REJECTED`
  with the venue's reason.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.enums import OrderState, OrderType
from tradebot.core.orders import Fill, Order, OrderIntent
from tradebot.core.portfolio import AccountState
from tradebot.core.schema import DomainModel, Money, UtcDatetime


class OrderRef(DomainModel):
    """How an order is named when talking to a venue.

    `client_order_id` is authoritative because it is ours and deterministic; `venue_order_id`
    is an optimization we may not have after a failed submit.
    """

    client_order_id: str
    instrument_key: str
    venue_order_id: str | None = None


class OrderAck(DomainModel):
    """The venue's acknowledgement of a submit."""

    client_order_id: str
    venue_order_id: str | None
    state: OrderState
    accepted_at: UtcDatetime
    reject_reason: str | None = None


class CancelAck(DomainModel):
    client_order_id: str
    cancelled: bool
    detail: str = ""


class OrderStatus(DomainModel):
    """The venue's current view of an order, including fills we may not have seen yet."""

    client_order_id: str
    venue_order_id: str | None
    instrument_key: str
    state: OrderState
    requested_qty: Money
    filled_qty: Money
    fills: tuple[Fill, ...] = ()
    observed_at: UtcDatetime
    reject_reason: str | None = None
    #: Whether the venue has a record of this order at all. A *rejected* order is a definite
    #: answer — it did not execute. An order the venue has never heard of is not: it may have
    #: been lost, or we may be querying the wrong account, and only one of those is survivable.
    #: Every adapter must distinguish the two; inferring it from a reject string cannot be done
    #: portably (PLAN §2.3).
    found: bool = True


class BrokerCapabilities(DomainModel):
    """What a venue supports, declared rather than assumed.

    `protective_orders` is load-bearing: where it is false, a position is unguarded between
    cycles, Tier-1 applies a sizing haircut, and the panel is told (DESIGN §6.7, R12).
    """

    venue_id: str
    order_types: tuple[OrderType, ...]
    protective_orders: bool = False
    #: Whether the venue *links* protective legs (Binance spot OCO, Alpaca bracket). Without it,
    #: only a single stop leg is placed: two unlinked exit orders on one holding can both fill
    #: and sell the position twice (DESIGN §6.7).
    oco_groups: bool = False
    fractional_quantities: bool = True
    #: Whether the venue can be queried by *our* id. Without it, `SUBMIT_UNKNOWN` recovery has
    #: no safe resolution and the adapter must not be used for live trading.
    query_by_client_order_id: bool = True
    max_client_order_id_length: int = 36
    #: Venue-side good-till-time. Binance spot has none, so TTL is bot-enforced (REVIEW B7).
    venue_side_ttl: bool = False


@runtime_checkable
class RestorableVenue(Protocol):
    """A venue whose books live inside this process and are lost when it exits.

    Only *simulated* venues implement this. A real venue keeps its own records and is the source
    of truth; handing it ours would be exactly backwards. Without it, restarting against a
    simulated venue looks identical to a venue reset — every position gone at once — and the
    reconciler correctly refuses to trade. The restore closes that gap without weakening the
    classification that catches a genuine reset (R15).
    """

    def restore(self, state: AccountState, orders: tuple[Order, ...]) -> None:
        """Adopt the account state and working orders recovered from our own event log."""
        ...


@runtime_checkable
class BrokerAdapter(Protocol):
    """One venue account. Implementations: `SimBroker`, `CcxtBroker`, `AlpacaBroker`."""

    venue_id: str

    async def submit(self, intent: OrderIntent) -> OrderAck:
        """Submit an order. Raises `SubmitUnknownError` if the outcome cannot be determined."""
        ...

    async def cancel(self, order_ref: OrderRef) -> CancelAck: ...

    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus:
        """Look up one order, by `client_order_id` where the venue allows it.

        This is the only legal resolution of `SUBMIT_UNKNOWN` (PLAN §2.3).
        """
        ...

    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]: ...

    async def fetch_positions_and_balances(self) -> AccountState:
        """The venue's own account state — the source of truth the ledger is reconciled to."""
        ...

    def capabilities(self) -> BrokerCapabilities: ...
