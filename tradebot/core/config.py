"""User-editable configuration as data.

Nothing risk-related is a constant in code: every limit here becomes a versioned row in the
ConfigStore and a form field in the dashboard (PLAN scope, DESIGN §6.6, §6.10). Phase 6 adds
versioning; the shapes are already the ones the engine consumes.

**Only limits that are actually enforced appear here.** A configurable field that no rule reads
is worse than a missing one — an operator would believe a limit is in force when it is not.
Each field names the rule that enforces it; the remaining DESIGN §6.6 limits arrive with their
rules in Phase 2d.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from tradebot.core.enums import AssetClass, BasketStatus, DecisionMode
from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel, Money


class RiskPolicy(DomainModel):
    """Tier-1 limits attached to a basket. All percentages are on a 0–100 scale."""

    #: Share of portfolio equity this basket may deploy. Defines the basket budget, which is
    #: the denominator of every other Tier-1 percentage. Enforced by `MaxBasketAllocationRule`.
    max_basket_allocation_pct: Money = Decimal(10)

    #: Share of the basket budget one instrument may hold. Enforced by `MaxPositionSizeRule`.
    max_position_pct_of_basket: Money = Decimal(25)

    #: Fraction of the basket budget risked between entry and stop on one trade. Consumed by
    #: volatility-normalized sizing (`qty = risk_amount / (stop_multiple × ATR)`).
    risk_per_trade_pct: Money = Decimal(1)

    #: Stop distance in ATR multiples. Sizing treats this as the true loss on an adverse move,
    #: which is only honest because a venue-native protective order holds it there.
    stop_loss_atr_multiple: Money = Decimal(2)

    #: Take-profit distance in ATR multiples. Only placed where the venue links legs into an
    #: OCO group — an unlinked second exit can sell a position that a filled stop already closed.
    take_profit_atr_multiple: Money = Decimal(3)

    #: How far through its trigger a protective leg's limit sits, so a triggered stop is
    #: marketable instead of resting above a falling market.
    protective_limit_offset_pct: Money = Decimal("0.5")

    #: How far an entry limit crosses the spread. A limit *at* the touch is not marketable once
    #: quantization has rounded it to the passive side, so without this an entry rests instead
    #: of trading and the cycle's decision silently expires (DESIGN §6.7).
    marketable_cross_pct: Money = Decimal("0.05")

    #: Panel conviction, 0–1, below which no order is placed. Enforced by `MinConvictionRule`.
    min_conviction: Money = Decimal("0.6")

    #: Cycles that must pass after trading an instrument before trading it again. Enforced by
    #: `CooldownRule` — the guard against a panel re-deciding the same thesis every cycle.
    cooldown_cycles: int = Field(default=2, ge=0)

    #: Orders this basket may place per UTC day. Enforced by `DailyTradeCapRule`.
    max_trades_per_day: int = Field(default=6, gt=0)

    #: Closed losing round trips in a row before the basket auto-pauses for human review.
    #: Enforced by `ConsecutiveLossRule` (DESIGN §6.6).
    max_consecutive_losses: int = Field(default=4, gt=0)

    #: Size reduction applied when the venue cannot hold a protective stop, so the position
    #: would be unguarded between cycles. Consumed by sizing (DESIGN §6.7, R12).
    unprotected_haircut_pct: Money = Decimal(50)

    #: v1 is long-only: SELL is reduce-only and SELL while flat is vetoed. Enforced by
    #: `LongOnlyRule`. Turning this off is not supported — shorting ripples through every
    #: other rule (DESIGN §12).
    long_only: bool = True

    @model_validator(mode="after")
    def _check_ranges(self) -> RiskPolicy:
        for name in (
            "max_basket_allocation_pct",
            "max_position_pct_of_basket",
            "risk_per_trade_pct",
        ):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(100):
                raise ValueError(f"{name} must be within (0, 100], got {value}")
        if not Decimal(0) <= self.min_conviction <= Decimal(1):
            raise ValueError(f"min_conviction is on the 0–1 scale, got {self.min_conviction}")
        if not Decimal(0) <= self.unprotected_haircut_pct < Decimal(100):
            raise ValueError("unprotected_haircut_pct must be within [0, 100)")
        for name in ("stop_loss_atr_multiple", "take_profit_atr_multiple"):
            if getattr(self, name) <= Decimal(0):
                raise ValueError(f"{name} must be positive")
        if self.take_profit_atr_multiple <= self.stop_loss_atr_multiple:
            raise ValueError(
                "take_profit_atr_multiple must exceed stop_loss_atr_multiple; a target inside "
                "the stop guarantees a losing expectancy"
            )
        if not self.long_only:
            raise ValueError("v1 is long-only; short exposure is not modelled by the risk rules")
        return self


class CorrelationCluster(DomainModel):
    """A set of instruments treated as one exposure bucket.

    Static in v1 by choice: estimating a live correlation matrix is future work, and static
    buckets already prevent the classic failure of two baskets independently maxing out on
    near-identical assets (DESIGN §6.6).
    """

    cluster_id: str
    instrument_keys: tuple[str, ...] = ()
    asset_classes: tuple[AssetClass, ...] = ()

    def contains(self, instrument: Instrument) -> bool:
        return (
            instrument.key in self.instrument_keys or instrument.asset_class in self.asset_classes
        )


class GlobalRiskPolicy(DomainModel):
    """Tier-2 limits: one instance per venue portfolio, plus the cross-venue aggregate rules.

    Tier 2 outranks every basket. It may veto or **shrink** an intent to fit remaining headroom;
    a shrink that lands below an exchange minimum becomes a veto (DESIGN §6.6).
    """

    #: Share of equity that may be deployed at once. Enforced by `GrossExposureRule`.
    max_gross_exposure_pct: Money = Decimal(80)

    #: Share of equity one instrument may reach across *all* baskets. Enforced by
    #: `InstrumentExposureRule` — Tier-1's per-basket cap cannot see a sibling basket.
    max_instrument_exposure_pct: Money = Decimal(20)

    #: Share of equity one correlation bucket may reach. Enforced by `ClusterExposureRule`.
    max_cluster_exposure_pct: Money = Decimal(40)

    #: How far an order's price may sit from the last quote before it is rejected as a fat
    #: finger or a stale-book artifact. Enforced by `PriceCollarRule`.
    price_collar_pct: Money = Decimal(5)

    #: Orders per rolling hour across every basket. Enforced by `OrderRateRule`; the defence
    #: against a bug that decides to trade every cycle forever (PLAN §3.1).
    max_orders_per_hour: int = Field(default=20, gt=0)

    #: Loss against day-start equity that halts all new orders for the day. Watchdog-enforced.
    max_daily_loss_pct: Money = Decimal(3)

    #: Loss from the high-water mark that trips the kill switch. Watchdog-enforced.
    max_drawdown_pct: Money = Decimal(10)

    #: How far a USD stablecoin may drift from par before aggregation freezes and new orders
    #: halt. Valuing a depegged stablecoin at 1.00 overstates equity and loosens every limit.
    stablecoin_peg_tolerance_pct: Money = Decimal(2)

    #: The kill switch halts and cancels; it does not liquidate. Flattening into a broken market
    #: is often the worse outcome, and it is the operator's call, not the bot's (DESIGN §6.6).
    flatten_on_kill: bool = False

    clusters: tuple[CorrelationCluster, ...] = (
        CorrelationCluster(cluster_id="crypto", asset_classes=(AssetClass.CRYPTO,)),
        CorrelationCluster(
            cluster_id="equities", asset_classes=(AssetClass.EQUITY, AssetClass.INDEX_ETF)
        ),
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> GlobalRiskPolicy:
        for name in (
            "max_gross_exposure_pct",
            "max_instrument_exposure_pct",
            "max_cluster_exposure_pct",
            "price_collar_pct",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "stablecoin_peg_tolerance_pct",
        ):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(100):
                raise ValueError(f"{name} must be within (0, 100], got {value}")
        if len({cluster.cluster_id for cluster in self.clusters}) != len(self.clusters):
            raise ValueError("cluster ids must be unique")
        return self

    def cluster_for(self, instrument: Instrument) -> CorrelationCluster | None:
        return next((c for c in self.clusters if c.contains(instrument)), None)

    def cluster_members(
        self, instrument: Instrument, universe: tuple[Instrument, ...]
    ) -> tuple[str, ...]:
        """Keys sharing `instrument`'s correlation bucket. Falls back to the instrument alone."""
        cluster = self.cluster_for(instrument)
        if cluster is None:
            return (instrument.key,)
        return tuple(other.key for other in universe if cluster.contains(other))


class SeatConfig(DomainModel):
    """One seat in the panel: a role bound to a provider and model.

    Seats are given *different evidence slices* on purpose — it manufactures genuine
    disagreement, which is what makes debate work rather than converge (DESIGN §6.5, [L5]).
    """

    seat_id: str
    role: str
    provider_id: str
    model: str
    temperature: float = 0.3
    evidence: tuple[str, ...] = ("indicators", "news", "position")
    fallbacks: tuple[str, ...] = ()


class PanelConfig(DomainModel):
    """The agent panel definition. A panel is data, not code."""

    panel_id: str
    seats: tuple[SeatConfig, ...]
    protocol: str = "single_round"
    max_rounds: int = Field(default=1, ge=1)
    #: Fraction of the original seat count that must agree for a tradable action. Counted over
    #: the *original* seats, never the remaining ones, so an abstention can never make a
    #: minority decisive (DESIGN §6.5).
    qualified_majority: Money = Decimal("0.5")
    #: Abstention fraction above which the panel is degraded and the cycle resolves to WAIT.
    max_abstain_fraction: Money = Decimal("0.34")
    max_cost_usd_per_cycle: Money = Decimal("0.50")

    @model_validator(mode="after")
    def _check_panel(self) -> PanelConfig:
        if not self.seats:
            raise ValueError("a panel needs at least one seat")
        if len({seat.seat_id for seat in self.seats}) != len(self.seats):
            raise ValueError("seat ids must be unique within a panel")
        if not Decimal(0) < self.qualified_majority <= Decimal(1):
            raise ValueError("qualified_majority must be within (0, 1]")
        return self

    @property
    def seat_count(self) -> int:
        return len(self.seats)


class Basket(DomainModel):
    """A GUI-created group of instruments with its own schedule, panel and risk budget."""

    basket_id: str
    name: str
    instruments: tuple[Instrument, ...]
    panel: PanelConfig
    risk_policy: RiskPolicy = RiskPolicy()
    decision_mode: DecisionMode = DecisionMode.PER_ASSET
    cycle_interval_seconds: int = Field(default=600, gt=0)
    #: Slack between an order's TTL and the next cycle, so the remainder is cancelled and booked
    #: before the next decision is taken against a position that is still moving.
    ttl_buffer_seconds: int = Field(default=60, ge=0)
    status: BasketStatus = BasketStatus.ACTIVE

    @model_validator(mode="after")
    def _check_instruments(self) -> Basket:
        if not self.instruments:
            raise ValueError("a basket needs at least one instrument")
        if len({i.key for i in self.instruments}) != len(self.instruments):
            raise ValueError("an instrument may appear in a basket only once")
        if self.ttl_buffer_seconds >= self.cycle_interval_seconds:
            raise ValueError("ttl_buffer_seconds must leave a positive order lifetime")
        return self

    @property
    def order_ttl_seconds(self) -> int:
        """Bot-enforced order lifetime: `cycle_interval − buffer` (DESIGN §6.7)."""
        return self.cycle_interval_seconds - self.ttl_buffer_seconds

    def instrument(self, key: str) -> Instrument:
        found = next((i for i in self.instruments if i.key == key), None)
        if found is None:
            raise KeyError(f"{key} is not in basket {self.basket_id}")
        return found
