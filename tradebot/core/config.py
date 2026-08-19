"""User-editable configuration as data.

Nothing risk-related is a constant in code: every limit here is stored as a versioned row by
`control/config_store.py` and edited from the dashboard (PLAN scope, DESIGN §6.6, §6.10). These
models are both the storage format and what the engine consumes, so a form validated against them
is validated against exactly what will run.

**Only limits that are actually enforced appear here.** A configurable field that no rule reads
is worse than a missing one — an operator would believe a limit is in force when it is not.
Each field names the rule that enforces it; the remaining DESIGN §6.6 limits arrive with their
rules in Phase 2d.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from tradebot.core.clock import ensure_utc
from tradebot.core.enums import AssetClass, BasketStatus, ConfigKind, DecisionMode, ProviderKind
from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel, Money

TOKENS_PER_PRICING_UNIT = Decimal(1_000_000)

#: Ticks are counted from here rather than from process start, so a restart cannot shift a
#: basket's cadence and two processes reading one config agree on the same instants.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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

    #: Instrument keys an operator has excluded from *automated* trading. Enforced by
    #: `QuarantineRule`. A quarantined instrument keeps its market data, its indicators and its
    #: place in the panel's deliberation — only the resulting order is refused, and the position
    #: it already holds stays closable by hand
    #: ([ADR 0022](../../docs/adr/0022-quarantine-is-a-tier-1-veto-rule.md)).
    quarantined_instruments: tuple[str, ...] = ()

    #: Excludes every instrument in the basket at once, and additionally skips the panel: there
    #: is nothing to spend a model call on when every outcome is already vetoed downstream.
    quarantined: bool = False

    def excludes(self, instrument_key: str) -> bool:
        """Whether an operator has quarantined this instrument, or the basket holding it."""
        return self.quarantined or instrument_key in self.quarantined_instruments

    def with_quarantine(self, instrument_key: str = "", *, excluded: bool) -> RiskPolicy:
        """This policy with one instrument — or, for an empty key, the whole basket — set.

        Sorted and deduplicated, so publishing the same state twice yields the same document and
        a version diff shows only what an operator actually changed.
        """
        if not instrument_key:
            return self.model_copy(update={"quarantined": excluded})
        keys = set(self.quarantined_instruments)
        updated = keys | {instrument_key} if excluded else keys - {instrument_key}
        return self.model_copy(update={"quarantined_instruments": tuple(sorted(updated))})

    @property
    def quarantine(self) -> str:
        """What is excluded, as one phrase for a CLI row or a log line. Empty means nothing is."""
        if self.quarantined:
            return "whole basket"
        return ", ".join(self.quarantined_instruments)

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

    #: Largest notional a single order may carry, in the quote currency. `None` means uncapped,
    #: which is the sim and paper default. Live mode **requires** it: the value comes from the
    #: operator's arming row and is enforced by `OrderNotionalRule` (PLAN §2.4).
    max_order_notional: Money | None = None

    #: Loss against day-start equity that halts all new orders for the day. Watchdog-enforced.
    max_daily_loss_pct: Money = Decimal(3)

    #: Loss from the high-water mark that trips the kill switch. Watchdog-enforced.
    max_drawdown_pct: Money = Decimal(10)

    #: How far a USD stablecoin may drift from par before aggregation freezes and new orders
    #: halt. Valuing a depegged stablecoin at 1.00 overstates equity and loosens every limit.
    stablecoin_peg_tolerance_pct: Money = Decimal(2)

    #: How old a price may be and still value a position. Beyond it the mark is *absent*, the
    #: aggregate freezes, and new orders stop — because a stale mark is not a more conservative
    #: mark, it is a wrong one, in whichever direction the market moved (PHASE_12 §1.4). Policy
    #: rather than a constant for the reason every other limit is: a limit a restart can clear is
    #: not a limit (ADR 0005).
    #:
    #: That it must also exceed the supervisor's resync cadence — a tolerance below it freezes
    #: permanently — is deliberately *not* checked here: `core/` depends on nothing, and the
    #: cadence belongs to `control/supervisor.py`. `PortfolioWatch` asserts it where both numbers
    #: are known.
    mark_staleness_seconds: int = Field(default=300, gt=0)

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
        if self.max_order_notional is not None and self.max_order_notional <= Decimal(0):
            raise ValueError("max_order_notional must be positive when set, or left unset")
        return self

    @property
    def mark_tolerance(self) -> timedelta:
        """`mark_staleness_seconds` as the `timedelta` every valuation call passes."""
        return timedelta(seconds=self.mark_staleness_seconds)

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


class ModelPricing(DomainModel):
    """USD per million tokens, quoted the way every provider quotes it.

    Free slots price at zero, which is the v1 default. A model absent from the table costing zero
    is the right failure direction for a budget: a zero-cost model can never be truncated by a
    budget it cannot consume.
    """

    prompt_per_million: Money = Decimal(0)
    completion_per_million: Money = Decimal(0)

    @property
    def is_free(self) -> bool:
        return not self.prompt_per_million and not self.completion_per_million

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        if self.is_free:
            return Decimal(0)
        billed = self.prompt_per_million * Decimal(prompt_tokens) + (
            self.completion_per_million * Decimal(completion_tokens)
        )
        return billed / TOKENS_PER_PRICING_UNIT


#: What an unpriced model costs. Free slots are the v1 default panel, so this is the common case.
FREE = ModelPricing()


class PriceList(DomainModel):
    """One provider's prices, keyed by model id. GUI-editable, like every other limit."""

    models: Mapping[str, ModelPricing] = Field(default_factory=dict)

    def for_model(self, model: str) -> ModelPricing:
        return self.models.get(model, FREE)


class ProviderSettings(DomainModel):
    """One reachable LLM endpoint.

    DESIGN §6.1 lists provider settings among the things the GUI edits and the ConfigStore
    versions, which is why this lives in `core` beside the panel that uses it rather than inside
    an adapter: the dashboard edits endpoints and seat bindings as one tree.
    """

    provider_id: str
    kind: ProviderKind
    #: Empty only for `STUB`, which has no endpoint at all.
    base_url: str = ""
    #: Environment variable *name* holding the key — never the value. The indirection is the
    #: control: a key can then be absent from the database, the logs and every prompt (PLAN §3.2).
    secret_ref: str | None = None
    prices: PriceList = PriceList()
    #: Several local servers reject `response_format`; asking for it there would take a fallback
    #: out of service exactly when the hosted slot it backs up has already failed.
    supports_json_mode: bool = True

    @model_validator(mode="after")
    def _check_endpoint(self) -> ProviderSettings:
        if self.kind.needs_endpoint and not self.base_url:
            raise ValueError(f"provider {self.provider_id!r} ({self.kind}) needs a base_url")
        return self


class ProviderBinding(DomainModel):
    """One (provider, model) pair a seat can run on.

    A fallback is a *binding* rather than a provider id because a model id is only meaningful to
    the provider that serves it: a seat whose free OpenRouter slot disappears has to move to a
    different model as well as a different endpoint (DESIGN §6.5, R11).
    """

    provider_id: str
    model: str

    @property
    def fingerprint(self) -> str:
        """What heterogeneity is measured over — two seats sharing one is a collapsed panel."""
        return f"{self.provider_id}:{self.model}"


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

    #: The desk's standing instruction for this seat, rendered into its system prompt above the
    #: rules it may not relax (`decision/prompts.py`). Operator-authored and versioned like every
    #: other field here, so a cycle's pinned basket version records the exact wording the panel
    #: deliberated under (ADR 0013) — which is what makes a change to it measurable rather than
    #: merely remembered. Capped because it is billed per seat, per round, per cycle: an
    #: accidental paste of a whole document would be a standing cost with no other symptom.
    instruction: str = Field(default="", max_length=4000)

    #: Tried in order when the primary binding fails. Deliberately allowed to cross provider
    #: families — a chain that stays inside one provider does not survive that provider's outage,
    #: which is the failure R11 predicts for free model slots.
    fallbacks: tuple[ProviderBinding, ...] = ()

    #: This seat argues against the emerging majority in every debate round. At least one such
    #: seat is a structural control against sycophantic convergence (DESIGN §6.5, [L5]).
    devils_advocate: bool = False

    @property
    def primary(self) -> ProviderBinding:
        return ProviderBinding(provider_id=self.provider_id, model=self.model)

    @property
    def bindings(self) -> tuple[ProviderBinding, ...]:
        """The primary binding, then the fallback chain, in the order they are attempted."""
        return (self.primary, *self.fallbacks)

    @model_validator(mode="after")
    def _check_chain(self) -> SeatConfig:
        """A chain must actually be a chain.

        Repeating a binding is not a fallback, it is a retry of something that just failed — and
        a silent one, since the seat would report the same binding it started on. Rejected at
        configuration time so a mis-filled GUI form cannot produce a seat with no real backup.
        """
        seen = [binding.fingerprint for binding in self.bindings]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"seat {self.seat_id!r} repeats {', '.join(duplicates)} in its fallback chain; "
                "a fallback must be a different provider or a different model"
            )
        return self


class PanelConfig(DomainModel):
    """The agent panel definition. A panel is data, not code.

    Self-describing on purpose: the panel carries the endpoints it may reach *and* the seats that
    reach them, so one GUI form edits both and validation can prove every binding resolves. A
    panel that named providers someone else had to remember to wire would fail at runtime as a
    quietly degraded seat — the failure mode hardest to notice and most expensive to diagnose.
    """

    panel_id: str
    seats: tuple[SeatConfig, ...]
    #: Endpoints this panel may reach. Nothing outside this tuple is ever constructed or
    #: contacted, and every seat binding must resolve to one of them.
    providers: tuple[ProviderSettings, ...] = ()
    protocol: str = "single_round"
    #: Total rounds *including* the blind round 0, so `3` is one blind round plus two debate
    #: rounds — the DESIGN §6.5 default. A protocol may stop earlier; it may never run more.
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
        if self.max_cost_usd_per_cycle < Decimal(0):
            raise ValueError("max_cost_usd_per_cycle cannot be negative")
        if all(seat.devils_advocate for seat in self.seats):
            raise ValueError(
                "a panel of nothing but devil's advocates has no majority to argue against; "
                "at least one seat must reason from the evidence directly"
            )
        self._check_bindings_resolve()
        return self

    def _check_bindings_resolve(self) -> None:
        """Every binding, primary and fallback, must name a provider this panel declares.

        A panel with no declared providers is exempt: that is a panel whose providers are supplied
        by the composition root, which is how the test suite and the scenario harness build one.
        """
        declared = {provider.provider_id for provider in self.providers}
        if len(declared) != len(self.providers):
            raise ValueError("provider ids must be unique within a panel")
        if not declared:
            return
        unresolved = sorted(
            {
                f"{seat.seat_id} → {binding.provider_id}"
                for seat in self.seats
                for binding in seat.bindings
                if binding.provider_id not in declared
            }
        )
        if unresolved:
            raise ValueError(
                f"these bindings name providers the panel does not declare: "
                f"{'; '.join(unresolved)}. Declared: {', '.join(sorted(declared))}"
            )

    @property
    def seat_count(self) -> int:
        return len(self.seats)

    @property
    def is_heterogeneous(self) -> bool:
        """Whether the configured seats span more than one provider+model.

        Checked at configuration time; the panel can still *collapse* at runtime when fallbacks
        land two seats on the same binding, which the consensus rule flags separately.
        """
        return len({seat.primary.fingerprint for seat in self.seats}) == self.seat_count

    def provider(self, provider_id: str) -> ProviderSettings:
        found = next((p for p in self.providers if p.provider_id == provider_id), None)
        if found is None:
            raise KeyError(f"{provider_id} is not declared by panel {self.panel_id}")
        return found

    def fallback_plan(self) -> dict[str, tuple[str, ...]]:
        """Each seat's chain as readable fingerprints — what the GUI renders and an operator reads.

        The whole point of per-seat chains is that they differ; a plan showing three identical
        rows is a panel that will lose every seat to the same outage (R11).
        """
        return {seat.seat_id: tuple(b.fingerprint for b in seat.bindings) for seat in self.seats}


class Schedule(DomainModel):
    """When a basket cycles (DESIGN §6.1).

    `market_open+15m` is deliberately *not* a second schedule kind. It is a daily interval whose
    first tick of a session is deferred to that session's open by the trading calendar, plus
    `open_delay_seconds` — so an equities schedule and a crypto schedule are one code path and one
    set of tests, rather than two that agree only by inspection (`control/scheduler.py`).
    """

    every_seconds: int = Field(default=600, gt=0)
    #: Phase within the interval: "every 1h at :05" is `every_seconds=3600, offset_seconds=300`.
    offset_seconds: int = Field(default=0, ge=0)
    #: How long after a session opens the first cycle of that session runs. Zero fires at the open;
    #: a delay lets the opening auction's prints clear before indicators are computed over them.
    open_delay_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_offset(self) -> Schedule:
        if self.offset_seconds >= self.every_seconds:
            raise ValueError(
                f"offset_seconds must fall inside one interval, got {self.offset_seconds} "
                f"in {self.every_seconds}s"
            )
        return self

    def next_tick(self, after: datetime) -> datetime:
        """The first scheduled instant strictly after `after`.

        Strictly, because a tick is consumed by the cycle it starts: computing the next fire from
        the instant a cycle *ended* must never hand back the tick that cycle just ran.
        """
        elapsed = (ensure_utc(after) - EPOCH) // timedelta(seconds=1)
        periods = (elapsed - self.offset_seconds) // self.every_seconds + 1
        return EPOCH + timedelta(seconds=self.offset_seconds + periods * self.every_seconds)


class ConfigRef(DomainModel):
    """One exact configuration version — what a cycle pins and a replay resolves.

    A cycle records the refs it ran on, so a past decision can be re-read against the
    configuration that produced it rather than against whatever the limits are today (DESIGN §6.1).
    """

    kind: ConfigKind
    config_id: str
    version: int = Field(ge=1)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.config_id}"


class Basket(DomainModel):
    """A GUI-created group of instruments with its own schedule, panel and risk budget."""

    basket_id: str
    name: str
    instruments: tuple[Instrument, ...]
    panel: PanelConfig
    #: A challenger panel evaluated on the **same frozen snapshot** each cycle, for the record
    #: only — it never trades and never affects the cycle's outcome (DESIGN §12, PLAN Phase 7).
    #: Comparing two panels on identical evidence is the statistically honest comparison; two
    #: panels run in different weeks are compared on different markets, not on their reasoning.
    #: Unset — the default — turns shadow evaluation off entirely and costs nothing.
    shadow_panel: PanelConfig | None = None
    risk_policy: RiskPolicy = RiskPolicy()
    decision_mode: DecisionMode = DecisionMode.PER_ASSET

    #: Timeframes indicators are computed over. Empty means the indicator engine's default set.
    #: Validated against the registry where the registry lives — `core` must not import it.
    timeframes: tuple[str, ...] = ()
    #: Indicator reading names from the registry. Empty means the engine's default set.
    indicators: tuple[str, ...] = ()
    #: News source ids feeding this basket's snapshots. Empty means no news at all, which the
    #: snapshot states explicitly rather than leaving the panel to assume a quiet market.
    news_sources: tuple[str, ...] = ()

    schedule: Schedule = Schedule()
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
        self._check_quarantine()
        self._check_challenger()
        return self

    def _check_quarantine(self) -> None:
        """A quarantine may only name an instrument this basket holds.

        A key matching nothing excludes nothing, and the operator who typed it would believe an
        instrument is out of service while the panel keeps trading it — which is exactly the
        "a limit an operator believes is in force but is not" failure this module refuses to have.
        """
        held = {instrument.key for instrument in self.instruments}
        unknown = sorted(set(self.risk_policy.quarantined_instruments) - held)
        if unknown:
            raise ValueError(
                f"quarantined_instruments names {', '.join(unknown)}, which basket "
                f"{self.basket_id!r} does not hold, so nothing would be excluded. "
                f"Held: {', '.join(sorted(held))}"
            )

    def _check_challenger(self) -> None:
        """Two rules that make a shadow comparison mean something, checked before it can run.

        The panels must be **distinguishable**, because the comparison report names each side by
        its panel id — two sides called `p1` is a report nobody can read. And a provider id the
        two panels both declare must be declared *identically*: one wiring serves both, so two
        endpoints or two price lists under one id would price the challenger's tokens against the
        champion's table and make `$/decision` a fiction for whichever lost the tie.
        """
        if self.shadow_panel is None:
            return
        if self.shadow_panel.panel_id == self.panel.panel_id:
            raise ValueError(
                f"the shadow panel repeats the champion's id {self.panel.panel_id!r}; a "
                "comparison whose two sides have the same name cannot be read"
            )
        champion = {provider.provider_id: provider for provider in self.panel.providers}
        conflicting = sorted(
            provider.provider_id
            for provider in self.shadow_panel.providers
            if provider != champion.get(provider.provider_id, provider)
        )
        if conflicting:
            raise ValueError(
                f"the champion and shadow panels declare {', '.join(conflicting)} differently; "
                "one wiring serves both, so a shared provider id must carry identical settings"
            )

    @property
    def panels(self) -> tuple[PanelConfig, ...]:
        """Every panel this basket runs — the champion, and the challenger when it has one."""
        return (self.panel,) if self.shadow_panel is None else (self.panel, self.shadow_panel)

    @property
    def challenger(self) -> Basket | None:
        """This basket as the challenger sees it: same instruments and mode, its own panel.

        Shaped as a `Basket` so the decision engine runs the challenger through *exactly* the code
        the champion went through — same protocol dispatch, same consensus rule, same budget
        mechanics. A second code path would compare two panels and one implementation difference.
        """
        if self.shadow_panel is None:
            return None
        return self.model_copy(update={"panel": self.shadow_panel, "shadow_panel": None})

    @property
    def cycle_interval_seconds(self) -> int:
        return self.schedule.every_seconds

    @property
    def order_ttl_seconds(self) -> int:
        """Bot-enforced order lifetime: `cycle_interval − buffer` (DESIGN §6.7)."""
        return self.cycle_interval_seconds - self.ttl_buffer_seconds

    def instrument(self, key: str) -> Instrument:
        found = next((i for i in self.instruments if i.key == key), None)
        if found is None:
            raise KeyError(f"{key} is not in basket {self.basket_id}")
        return found
