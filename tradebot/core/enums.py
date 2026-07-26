"""Closed vocabularies shared across the system.

Every enum here is persisted (event payloads, projections) — values are part of the
on-disk contract and must not be renamed without a migration.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Execution mode. Selected by a required CLI argument; never defaulted (PLAN §2.4)."""

    SIM = "sim"
    PAPER = "paper"
    LIVE = "live"

    @property
    def id_prefix(self) -> str:
        """Per-environment `client_order_id` prefix, so ids can never be confused across modes."""
        return _MODE_ID_PREFIX[self]

    @property
    def is_live(self) -> bool:
        return self is Mode.LIVE


_MODE_ID_PREFIX: dict[Mode, str] = {Mode.SIM: "sim", Mode.PAPER: "pap", Mode.LIVE: "liv"}


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    INDEX_ETF = "index_etf"


class Side(StrEnum):
    """Order side.

    v1 is long-only: SELL is reduce-only and can never open a short (DESIGN §6.6, R13).
    """

    BUY = "buy"
    SELL = "sell"


class Action(StrEnum):
    """Panel output.

    HOLD is an affirmative "keep the current position" vote and counts *against* acting.
    WAIT is the no-signal outcome (non-consensus, degraded panel, explicit uncertainty).
    Both produce no order, but they are distinct research signals (DESIGN §6.5).
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    ABSTAIN = "ABSTAIN"

    @property
    def is_tradable(self) -> bool:
        return self in _TRADABLE_ACTIONS

    @property
    def side(self) -> Side:
        """The order side this action implies. Only valid when `is_tradable`."""
        return _ACTION_SIDE[self]


_ACTION_SIDE: dict[Action, Side] = {Action.BUY: Side.BUY, Action.SELL: Side.SELL}
_TRADABLE_ACTIONS = frozenset(_ACTION_SIDE)


class SizeHint(StrEnum):
    """Seat's requested size as a fraction of the *risk-allowed* maximum (DESIGN §6.5).

    The panel never sizes in absolute terms; this fraction is an input to deterministic sizing.
    """

    NONE = "none"
    QUARTER = "quarter"
    HALF = "half"
    FULL = "full"

    @property
    def fraction(self) -> str:
        """Decimal-safe string, converted by the money layer. Never a float."""
        return _SIZE_HINT_FRACTION[self]


_SIZE_HINT_FRACTION: dict[SizeHint, str] = {
    SizeHint.NONE: "0",
    SizeHint.QUARTER: "0.25",
    SizeHint.HALF: "0.5",
    SizeHint.FULL: "1",
}


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS_LIMIT = "stop_loss_limit"
    TAKE_PROFIT_LIMIT = "take_profit_limit"


class OrderState(StrEnum):
    """Order lifecycle (DESIGN §6.7).

    `SUBMIT_UNKNOWN` is the most important state in the system: the only legal exits are
    querying the venue by `client_order_id`, or — after a bounded window — failing the order
    and halting the basket for human review. There is no resubmission path (PLAN §2.3).
    """

    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    SUBMIT_UNKNOWN = "submit_unknown"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_ORDER_STATES

    @property
    def is_open(self) -> bool:
        """True while the venue may still fill this order (so the monitor must keep polling)."""
        return self in _OPEN_ORDER_STATES


_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.FAILED,
    }
)
_OPEN_ORDER_STATES = frozenset({OrderState.SUBMITTED, OrderState.OPEN, OrderState.PARTIALLY_FILLED})


class RiskDecision(StrEnum):
    """Outcome of a single risk rule. Recorded on every intent as risk provenance."""

    PASS = "pass"  # noqa: S105 — a risk verdict, not a credential
    ADJUSTED = "adjusted"
    VETO = "veto"


class CycleOutcome(StrEnum):
    """Terminal outcome of one decision cycle, recorded for research and ops."""

    ORDERS_PLACED = "orders_placed"
    NO_ACTION = "no_action"
    RISK_VETOED = "risk_vetoed"
    DATA_STALE = "data_stale"
    PANEL_DEGRADED = "panel_degraded"
    FAILED = "failed"
