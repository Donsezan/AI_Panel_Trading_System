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


class MarketSession(StrEnum):
    """Which trading session a bar belongs to (DESIGN §6.2).

    Crypto is `CONTINUOUS`: there is no session structure to respect. Equities have `REGULAR`
    and `EXTENDED` bars, and mixing them into one indicator average silently blends two
    different liquidity regimes — extended-hours prints are thin and wide, so an ATR computed
    across them misstates the stop distance that sizing divides by.
    """

    CONTINUOUS = "continuous"
    REGULAR = "regular"
    EXTENDED = "extended"

    @property
    def is_indicator_input(self) -> bool:
        """Whether an indicator may include this bar in its window."""
        return self is not MarketSession.EXTENDED


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

    @property
    def rank(self) -> int:
        """Ordering, so a panel can be reduced to its *most conservative* size hint."""
        return _SIZE_HINT_RANK[self]


_SIZE_HINT_FRACTION: dict[SizeHint, str] = {
    SizeHint.NONE: "0",
    SizeHint.QUARTER: "0.25",
    SizeHint.HALF: "0.5",
    SizeHint.FULL: "1",
}
_SIZE_HINT_RANK: dict[SizeHint, int] = {
    SizeHint.NONE: 0,
    SizeHint.QUARTER: 1,
    SizeHint.HALF: 2,
    SizeHint.FULL: 3,
}


class DecisionMode(StrEnum):
    """How a basket's panel is run (DESIGN §4)."""

    PER_ASSET = "per_asset"
    BASKET = "basket"


class ProviderKind(StrEnum):
    """Wire protocol an LLM endpoint speaks.

    The *kind* is what varies between vendors; the endpoint, the key and the models are data. One
    `OPENAI_COMPAT` adapter therefore covers OpenRouter, OpenAI, vLLM, LM Studio and llama.cpp,
    which is what lets a local runtime be a seat's fallback rather than a separate integration.
    """

    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    #: The scripted offline provider. Not a vendor — it is how the demo and the whole test suite
    #: run without a key or a network, and it needs no endpoint.
    STUB = "stub"

    @property
    def needs_endpoint(self) -> bool:
        return self is not ProviderKind.STUB


class BasketStatus(StrEnum):
    """`HALTED` is deliberately not self-clearing — it requires a human in the GUI."""

    ACTIVE = "active"
    PAUSED = "paused"
    HALTED = "halted"

    @property
    def may_trade(self) -> bool:
        return self is BasketStatus.ACTIVE


class ConfigKind(StrEnum):
    """What a versioned ConfigStore document holds (DESIGN §6.1).

    A basket carries its panel and its Tier-1 policy *inside* it, because that is the tree the
    dashboard edits and the runner consumes; pinning one version therefore pins the whole
    decision-making configuration of a cycle. The Tier-2 policy is separate because it belongs to
    no basket — it outranks all of them.
    """

    BASKET = "basket"
    GLOBAL_RISK = "global_risk"

    @property
    def is_singleton(self) -> bool:
        """Whether exactly one document of this kind exists, under `SINGLETON_ID`."""
        return self is ConfigKind.GLOBAL_RISK


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS_LIMIT = "stop_loss_limit"
    TAKE_PROFIT_LIMIT = "take_profit_limit"

    @property
    def needs_stop_price(self) -> bool:
        return self in _TRIGGERED_ORDER_TYPES


_TRIGGERED_ORDER_TYPES = frozenset({OrderType.STOP_LOSS_LIMIT, OrderType.TAKE_PROFIT_LIMIT})


class OrderRole(StrEnum):
    """An order's job within its protective group (DESIGN §6.7).

    A cycle-based system cannot babysit stops itself: between cycles the *venue* holds them.
    Entry and its protective legs are one group — one leg fills, the sibling is cancelled.
    """

    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"

    @property
    def is_protective(self) -> bool:
        return self is not OrderRole.ENTRY

    @property
    def order_type(self) -> OrderType:
        return _ROLE_ORDER_TYPE[self]


_ROLE_ORDER_TYPE: dict[OrderRole, OrderType] = {
    OrderRole.ENTRY: OrderType.LIMIT,
    OrderRole.STOP_LOSS: OrderType.STOP_LOSS_LIMIT,
    OrderRole.TAKE_PROFIT: OrderType.TAKE_PROFIT_LIMIT,
}


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


class RiskTier(StrEnum):
    """Which layer produced a risk event. Tier 2 outranks Tier 1 and can halt everything."""

    TIER1 = "tier1"
    TIER2 = "tier2"
    EXECUTION = "execution"
    RECONCILIATION = "reconciliation"


class ReconcileClass(StrEnum):
    """How the reconciler explains a difference between the ledger and the venue (DESIGN §6.8).

    The classification *is* the response: only `MISMATCH` is unexplained, and only unexplained
    differences halt. Misclassifying a corporate action or a testnet wipe as drift would halt
    for a routine event; misclassifying a real discrepancy as drift would trade on a fiction.
    """

    MATCH = "match"
    DRIFT = "drift"
    EXTERNAL_CHANGE = "external_change"
    CORPORATE_ACTION = "corporate_action"
    VENUE_RESET = "venue_reset"
    MISMATCH = "mismatch"

    @property
    def is_clean(self) -> bool:
        """True when the ledger may keep trading after auto-correction."""
        return self in _CLEAN_RECONCILE_CLASSES


_CLEAN_RECONCILE_CLASSES = frozenset(
    {ReconcileClass.MATCH, ReconcileClass.DRIFT, ReconcileClass.EXTERNAL_CHANGE}
)


class KillSwitchState(StrEnum):
    """The one big red button. Re-arming is a human act with a typed phrase (DESIGN §6.6)."""

    ARMED = "armed"
    TRIPPED = "tripped"

    @property
    def may_trade(self) -> bool:
        return self is KillSwitchState.ARMED


class CycleOutcome(StrEnum):
    """Terminal outcome of one decision cycle, recorded for research and ops."""

    ORDERS_PLACED = "orders_placed"
    NO_ACTION = "no_action"
    RISK_VETOED = "risk_vetoed"
    DATA_STALE = "data_stale"
    PANEL_DEGRADED = "panel_degraded"
    #: Trading was blocked before the panel ran — kill switch tripped or basket halted. The
    #: cycle is recorded rather than skipped, so a halt is visible in the log as a decision.
    BLOCKED = "blocked"
    #: The whole basket is quarantined by its operator. Distinct from `BLOCKED` because the
    #: snapshot was still built: market data and indicators kept flowing, and only the panel and
    #: everything downstream of it were skipped (ADR 0022).
    QUARANTINED = "quarantined"
    FAILED = "failed"
