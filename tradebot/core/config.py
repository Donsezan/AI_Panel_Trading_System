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

from tradebot.core.enums import BasketStatus, DecisionMode
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
    #: which is only honest once a venue-native protective order holds it (Phase 2a).
    stop_loss_atr_multiple: Money = Decimal(2)

    #: Panel conviction, 0–1, below which no order is placed. Enforced by `MinConvictionRule`.
    min_conviction: Money = Decimal("0.6")

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
        if self.stop_loss_atr_multiple <= Decimal(0):
            raise ValueError("stop_loss_atr_multiple must be positive")
        if not self.long_only:
            raise ValueError("v1 is long-only; short exposure is not modelled by the risk rules")
        return self


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
    status: BasketStatus = BasketStatus.ACTIVE

    @model_validator(mode="after")
    def _check_instruments(self) -> Basket:
        if not self.instruments:
            raise ValueError("a basket needs at least one instrument")
        if len({i.key for i in self.instruments}) != len(self.instruments):
            raise ValueError("an instrument may appear in a basket only once")
        return self

    def instrument(self, key: str) -> Instrument:
        found = next((i for i in self.instruments if i.key == key), None)
        if found is None:
            raise KeyError(f"{key} is not in basket {self.basket_id}")
        return found
