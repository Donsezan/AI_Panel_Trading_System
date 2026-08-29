"""Was the decision right? (spec §9.)

Per (candidate, snapshot, instrument): take `p0` — the quote in the snapshot, what the panel
actually saw — and `atr`, read **off the frozen snapshot** rather than recomputed. The band is
`k × atr`, so it is derived from exactly the evidence the panel had rather than from a better
view of the same market. Then compare against `pH`, the close `H` bars later.

**The truth label is long-only aware, and this is the rule easiest to get backwards.** Tier-1
refuses a short, so standing aside from a fall while flat is *correct*, not a missed opportunity.
Scoring it as a miss would systematically punish the conservative behaviour the bot is built for,
and would make `SHOCK_DOWN` a period the bot is doomed to score badly in rather than a test it
can pass.

Every decision gets a verdict, and there are exactly three ways to be unscorable: the ATR lookback
or the forward window crosses a known hole, the forward window runs off the end of the dataset, or
the snapshot carries no ATR for the scoring timeframe. Each is counted with its reason on every
table — a run that quietly dropped them would report accuracy over a subset it chose after the
fact.

Everything here is `Decimal`. `decision_lab/tests/test_discipline.py` asserts that structurally.

Failure semantics: this module computes and never fetches. Bad input raises `ValueError`; a
missing input is a verdict, not an exception.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from decision_lab.calibration_days import Pool
from decision_lab.dataset import CoverageAudit, read_series, series_key
from decision_lab.params import DEFAULT_BAND_K, DEFAULT_HORIZON_BARS
from decision_lab.records import CycleRecord
from decision_lab.regimes import RegimeIndex
from tradebot.core.decision import Decision
from tradebot.core.enums import Action
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import InstrumentContext

# The flag `reach_consensus` sets on a WAIT from a panel that could not answer, taken from its
# own module rather than through `records`: a symbol reached through a second module's import
# list stops resolving the moment that module stops needing it.
from tradebot.decision.consensus import PANEL_DEGRADED
from tradebot.indicators.library import REGISTRY
from tradebot.marketdata.recorder import ReplayDataset


class Truth(StrEnum):
    """What the market went on to do, expressed as what the right call would have been."""

    BUY = "BUY"
    STAND_ASIDE = "STAND_ASIDE"
    ADD = "ADD"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Verdict(StrEnum):
    """§9.4. Three unscored reasons, and adding a fourth is a spec change."""

    CORRECT = "CORRECT"
    WRONG = "WRONG"
    UNSCORED_GAP = "UNSCORED (gap)"
    UNSCORED_HORIZON = "UNSCORED (horizon)"
    UNSCORED_NO_ATR = "UNSCORED (no ATR)"

    @property
    def is_scored(self) -> bool:
        return self in (Verdict.CORRECT, Verdict.WRONG)


#: §9.3's fourth column, as data. `HOLD` is correct for `ADD` because a position already on is
#: already exposed to the move — the panel is not required to press a winner to be right about it.
CORRECT_ACTIONS: dict[Truth, frozenset[Action]] = {
    Truth.BUY: frozenset({Action.BUY}),
    Truth.STAND_ASIDE: frozenset({Action.WAIT, Action.HOLD}),
    Truth.ADD: frozenset({Action.BUY, Action.HOLD}),
    Truth.EXIT: frozenset({Action.SELL}),
    Truth.HOLD: frozenset({Action.HOLD, Action.WAIT}),
}


def truth_for(*, holding: bool, move: Decimal, band: Decimal) -> Truth:
    """§9.3's truth table. Long-only: a fall while flat cost nothing and demanded nothing."""
    if band <= ZERO:
        raise ValueError(f"scoring needs a positive band, got {band}")
    if not holding:
        return Truth.BUY if move > band else Truth.STAND_ASIDE
    if move > band:
        return Truth.ADD
    if move < -band:
        return Truth.EXIT
    return Truth.HOLD


class ScoringParams(DomainModel):
    """The four numbers a verdict depends on, printed on every report (§14)."""

    #: Defaults to the dataset's shortest timeframe; `ReplayDataset.timeframes` is shortest-first.
    timeframe: str
    band_k: Money = DEFAULT_BAND_K
    horizon_bars: int = DEFAULT_HORIZON_BARS
    #: Bars the ATR reading in the snapshot was averaged over, plus one for the true range's
    #: previous close. Read from the registry so a change to the indicator moves this with it.
    atr_lookback_bars: int = REGISTRY["ATR"].period + 1


class Forward(DomainModel):
    """What the market did over the horizon, from the price the panel saw."""

    p0: Money
    p_h: Money
    move: Money
    #: Maximum favourable and adverse excursion over the same window — recorded alongside because
    #: `move` alone cannot distinguish a straight climb from a round trip through a drawdown.
    mfe: Money
    mae: Money


@dataclass(frozen=True, slots=True)
class PriceIndex:
    """The scoring timeframe's bars per instrument, plus the holes not to score across."""

    timeframe: str
    candles: dict[str, tuple[Candle, ...]]
    holes: dict[str, tuple[tuple[datetime, datetime], ...]]

    def _bar_index(self, instrument_key: str, as_of: datetime) -> int:
        closes = [candle.close_time for candle in self.candles[instrument_key]]
        return bisect.bisect_right(closes, as_of) - 1

    def forward(self, instrument_key: str, as_of: datetime, *, horizon: int) -> Forward | None:
        """`p0`, `pH`, the move, and the excursions — or `None` when the window runs off the end."""
        bars = self.candles[instrument_key]
        start = self._bar_index(instrument_key, as_of)
        if start < 0 or start + horizon >= len(bars):
            return None
        window = bars[start + 1 : start + horizon + 1]
        p0 = bars[start].close
        p_h = window[-1].close
        return Forward(
            p0=p0,
            p_h=p_h,
            move=p_h - p0,
            mfe=max(bar.high for bar in window) - p0,
            mae=min(bar.low for bar in window) - p0,
        )

    def crosses_hole(self, instrument_key: str, as_of: datetime, params: ScoringParams) -> bool:
        """Does the ATR lookback or the forward window touch a known hole? (§4.4)"""
        interval = timeframe_interval(self.timeframe)
        since = as_of - interval * params.atr_lookback_bars
        until = as_of + interval * params.horizon_bars
        return any(
            hole_from < until and hole_to > since
            for hole_from, hole_to in self.holes.get(instrument_key, ())
        )


async def build_price_index(
    dataset: ReplayDataset, audit: CoverageAudit, params: ScoringParams
) -> PriceIndex:
    """Load the scoring timeframe for every instrument, with its known holes attached."""
    candles: dict[str, tuple[Candle, ...]] = {}
    holes: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    for instrument in dataset.instruments:
        series = await read_series(dataset, instrument, params.timeframe)
        candles[instrument.key] = series.candles
        holes[instrument.key] = tuple(
            (hole.from_, hole.to)
            for hole in audit.holes_for(series_key(instrument.key, params.timeframe))
        )
    return PriceIndex(timeframe=params.timeframe, candles=candles, holes=holes)


def band_for(context: InstrumentContext, params: ScoringParams) -> Decimal | None:
    """`k × ATR`, read off the frozen snapshot. `None` when the snapshot has no ATR (§9.1)."""
    reading = context.indicator("ATR", params.timeframe)
    if reading is None or reading.value <= ZERO:
        return None
    return multiply(params.band_k, reading.value)


class ScoredDecision(DomainModel):
    """One (cycle, instrument) verdict, with everything the report needs to explain it."""

    cycle_id: str
    as_of: UtcDatetime
    instrument_key: str
    regime: Pool
    #: The named episode covering this instant, or `""`. Reported on its own row *and* inside the
    #: regime aggregate, never instead of it (§8.2).
    window_name: str = ""
    action: Action
    conviction: Money
    asked_for_an_order: bool
    holding: bool
    degraded: bool = False
    truth: Truth | None = None
    verdict: Verdict
    band: Money | None = None
    move: Money | None = None
    mfe: Money | None = None
    mae: Money | None = None
    #: `oracle − panel`, in band units so instruments are comparable. A ranking aid, explicitly
    #: unreachable: an oracle exits at the high of every window and no risk-managed system can.
    regret: Money | None = None
    cost_usd: Money = ZERO


def score_decision(
    *,
    cycle_id: str,
    as_of: datetime,
    context: InstrumentContext,
    decision: Decision,
    forward: Forward | None,
    band: Decimal | None,
    regime: Pool,
    window_name: str,
    degraded: bool = False,
    cost_usd: Decimal = ZERO,
    crossed_hole: bool = False,
) -> ScoredDecision:
    """One verdict. Unscorable is a verdict, never an exception and never a drop."""
    holding = context.position is not None
    common = {
        "cycle_id": cycle_id,
        "as_of": as_of,
        "instrument_key": context.instrument.key,
        "regime": regime,
        "window_name": window_name,
        "action": decision.action,
        "conviction": decision.conviction,
        "asked_for_an_order": decision.action.is_tradable,
        "holding": holding,
        "degraded": degraded,
        "cost_usd": cost_usd,
    }
    if crossed_hole:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_GAP)
    if band is None:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_NO_ATR)
    if forward is None:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_HORIZON, band=band)

    truth = truth_for(holding=holding, move=forward.move, band=band)
    return ScoredDecision(
        **common,
        truth=truth,
        verdict=Verdict.CORRECT if decision.action in CORRECT_ACTIONS[truth] else Verdict.WRONG,
        band=band,
        move=forward.move,
        mfe=forward.mfe,
        mae=forward.mae,
        regret=_regret(decision.action, forward, band, holding=holding),
    )


def _regret(action: Action, forward: Forward, band: Decimal, *, holding: bool) -> Decimal:
    """Oracle capture minus the panel's, in band units (§9.5).

    The oracle is long-only too and exits at the window's high, so its capture is `max(mfe, 0)`
    whether or not a position was already on. The panel captures the move only if its decision
    left it exposed: BUY, or HOLD while already holding. Standing aside captures nothing, which is
    exactly right — and is why regret is a *ranking aid* rather than a score: a system that never
    trades has maximal regret and may still be the correct system for the period.
    """
    exposed = action is Action.BUY or (holding and action is Action.HOLD)
    return divide(max(forward.mfe, ZERO) - (forward.move if exposed else ZERO), band)


class RegimeMetrics(DomainModel):
    """§9.5, for one regime or one named window. Every field is `Decimal` or a count."""

    regime: str
    decisions: int = 0
    scored: int = 0
    correct: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_gap: Money = ZERO
    regret_total: Money = ZERO
    regret_per_decision: Money = ZERO
    degradation_rate: Money = ZERO
    cost_usd: Money = ZERO
    cost_per_scored: Money = ZERO
    unscored: dict[str, int] = Field(default_factory=dict)


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    """Zero rather than a refusal on an empty denominator: an empty regime is a row of zeroes,
    and `§8.3` requires the row to exist so 'never happened' does not read as 'not measured'."""
    return divide(Decimal(numerator), Decimal(denominator)) if denominator else ZERO


def _mean(values: Sequence[Decimal]) -> Decimal:
    return divide(sum(values, start=ZERO), Decimal(len(values))) if values else ZERO


def summarise(decisions: Sequence[ScoredDecision], *, regime: str) -> RegimeMetrics:
    """Fold one regime's decisions into §9.5's metrics."""
    scored = [d for d in decisions if d.verdict.is_scored]
    correct = [d for d in scored if d.verdict is Verdict.CORRECT]
    wrong = [d for d in scored if d.verdict is Verdict.WRONG]
    acted = [d for d in scored if d.asked_for_an_order]
    acted_correct = [d for d in acted if d.verdict is Verdict.CORRECT]
    regrets = [d.regret for d in scored if d.regret is not None]
    cost = sum((d.cost_usd for d in decisions), start=ZERO)
    unscored: dict[str, int] = {}
    for decision in decisions:
        if not decision.verdict.is_scored:
            unscored[decision.verdict.value] = unscored.get(decision.verdict.value, 0) + 1

    return RegimeMetrics(
        regime=regime,
        decisions=len(decisions),
        scored=len(scored),
        correct=len(correct),
        accuracy=_ratio(len(correct), len(scored)),
        action_rate=_ratio(len(acted), len(scored)),
        precision_on_action=_ratio(len(acted_correct), len(acted)),
        # Zero when either side is empty: a panel with no wrong calls has no *gap*, and reporting
        # its correct-side mean as one would flatter it.
        mean_conviction_gap=(
            _mean([d.conviction for d in correct]) - _mean([d.conviction for d in wrong])
            if correct and wrong
            else ZERO
        ),
        regret_total=sum(regrets, start=ZERO),
        regret_per_decision=_mean(regrets),
        # Over *every* decision, not the scored ones: degradation is the reason a decision is
        # missing, so measuring it against what survived would hide it.
        degradation_rate=_ratio(sum(1 for d in decisions if d.degraded), len(decisions)),
        cost_usd=cost,
        cost_per_scored=_ratio(cost, len(scored)),
        unscored=unscored,
    )


def by_regime(decisions: Sequence[ScoredDecision]) -> tuple[RegimeMetrics, ...]:
    """The three regimes, always all three, then one row per named window (§8.3, §8.2).

    `SHOCK_UP` and `SHOCK_DOWN` are never pooled: they ask opposite questions of a long-only
    system, and a blended figure averages "did the seats catch the move" with "did the seats
    protect capital" and hides both.
    """
    rows = [
        summarise([d for d in decisions if d.regime is pool], regime=pool.value) for pool in Pool
    ]
    windows = sorted({d.window_name for d in decisions if d.window_name})
    rows += [
        summarise([d for d in decisions if d.window_name == name], regime=name) for name in windows
    ]
    return tuple(rows)


def score_records(
    records: Sequence[CycleRecord],
    *,
    index: PriceIndex,
    regimes: RegimeIndex,
    params: ScoringParams,
) -> tuple[ScoredDecision, ...]:
    """Score every (cycle, instrument) of the reference pass."""
    results: list[ScoredDecision] = []
    for record in records:
        # `basket` mode answers for N instruments in one provider call, so the cycle's cost is
        # already de-duplicated by `total_cost` and is split evenly across the instruments it
        # answered for rather than counted once per instrument.
        per_instrument = _ratio(record.cost_usd, len(record.snapshot.instruments))
        for context in record.snapshot.instruments:
            decision = record.decision_for(context.instrument.key)
            if decision is None:
                continue
            window = regimes.window_at(record.as_of)
            results.append(
                score_decision(
                    cycle_id=record.cycle_id,
                    as_of=record.as_of,
                    context=context,
                    decision=decision,
                    forward=index.forward(
                        context.instrument.key, record.as_of, horizon=params.horizon_bars
                    ),
                    band=band_for(context, params),
                    regime=regimes.label_at(context.instrument.key, record.as_of),
                    window_name=window.name if window else "",
                    degraded=PANEL_DEGRADED in decision.flags,
                    cost_usd=per_instrument,
                    crossed_hole=index.crosses_hole(context.instrument.key, record.as_of, params),
                )
            )
    return tuple(results)
