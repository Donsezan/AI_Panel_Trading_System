"""The Tier-2 watchdog and the kill switch — the one big red button (DESIGN §6.6).

The watchdog is not part of a cycle. It reads the reconciled portfolio independently and can
pause baskets or stop everything without waiting for a basket to get round to asking, because
the breaches it watches for — a drawdown, a reconciliation mismatch — do not politely wait for
a cycle boundary.

The switch trips on exactly three things, and every one of them is tested:

1. **Drawdown** past `max_drawdown_pct` of the flow-adjusted high-water mark.
2. **A reconciliation mismatch** the reconciler could not explain.
3. **A human**, through `tradebot risk` (and the dashboard's Control page).

Its effect is to cancel working orders and halt every runner. It does **not** liquidate:
`flatten_on_kill` defaults to false because flattening into a broken market is frequently the
worse outcome, and that call belongs to the operator. Re-arming requires a typed phrase.

The daily-loss limit is deliberately *not* a kill: it halts new orders for the rest of the day
and lets existing protective legs do their job, which is a different and lesser response.

Failure semantics: the watchdog fails closed in both directions. It cannot un-trip anything, and
if it cannot compute a baseline it leaves the system as it found it rather than assuming safety.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import BasketStatus, KillSwitchState, RiskTier
from tradebot.core.errors import FailClosedError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument, base_currencies_of
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.interfaces.broker import TradingCalendar
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import ExternalFlow
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate, value_cash
from tradebot.risk.state import RiskState, RiskStateStore, rolled_over, start_of_day

logger = get_logger(__name__)

DRAWDOWN_RULE = "max_drawdown"
DAILY_LOSS_RULE = "max_daily_loss"


@dataclass(frozen=True, slots=True)
class WatchdogVerdict:
    """What the watchdog decided this sweep."""

    state: RiskState
    tripped: bool = False
    day_halted: bool = False
    #: The portfolio could not be valued, so no limit could be evaluated. Distinct from a halt:
    #: nothing is wrong with the *system*, and it clears on its own when marks return.
    frozen: bool = False
    reason: str = ""

    @property
    def may_trade(self) -> bool:
        return self.state.may_trade and not self.day_halted and not self.frozen


class Watchdog:
    """Continuous Tier-2 enforcement over the aggregate portfolio."""

    def __init__(
        self,
        policy: GlobalRiskPolicy,
        states: RiskStateStore,
        store: EventStore,
        clock: Clock,
        *,
        calendar: TradingCalendar | None = None,
        marks: Marks | None = None,
        notional_currency: str = "USDT",
    ) -> None:
        self._policy = policy
        self._states = states
        self._store = store
        self._clock = clock
        #: The same cache `aggregate` reads, so a flow is converted by the rule that values the
        #: balance it lands in — one ladder, not two that can disagree (PHASE_12 §3.7).
        self._marks = marks if marks is not None else Marks()
        self._notional_currency = notional_currency
        #: Refreshed through `use_universe`, never captured here.
        self._position_currencies: frozenset[str] = frozenset()
        #: Whose "day" the daily-loss baseline rolls over on. Absent means the UTC date, which is
        #: correct for crypto and wrong for equities: a US session ends at 20:00 UTC and its
        #: after-hours prints land the next UTC day, so a UTC rollover would reset the baseline
        #: in the middle of a session (DESIGN §6.6).
        self._calendar = calendar

    def use_policy(self, policy: GlobalRiskPolicy) -> None:
        """Adopt a newly published Tier-2 policy (DESIGN §6.6).

        The watchdog outlives every cycle, so unlike Tier-2's per-cycle engine it cannot be
        rebuilt from the pinned configuration — a policy fixed at construction would leave it
        enforcing the drawdown limit the process started with, hours after the dashboard changed
        it. Swapped at a cycle boundary by the supervisor, never mid-sweep.
        """
        self._policy = policy

    def _events(self) -> EventFactory:
        return EventFactory(clock=self._clock, basket_id="global", cycle_id="watchdog")

    async def check(self, valuation: PortfolioAggregate) -> WatchdogVerdict:
        """Evaluate the drawdown and daily-loss baselines against current equity.

        A **frozen** valuation is ignorance, not a breach: nothing is tripped, no baseline moves,
        and no state is written — but no new order may be sent either. Rolling the day or raising
        the mark against a number the system has just said it cannot compute would persist a
        fiction that outlives the outage, and tripping would spend the operator's typed re-arm
        phrase on a feed that will recover by itself (PHASE_12 §3.4, ADR 0027).

        A freeze spanning midnight therefore leaves `day_start_equity` at yesterday's, measuring
        the daily loss from an older and generally higher baseline. That is the conservative
        direction, and it is chosen rather than incidental.
        """
        if valuation.frozen:
            return WatchdogVerdict(
                state=self._states.load(), frozen=True, reason=valuation.frozen_reason
            )
        equity = valuation.equity
        state = await self._roll_day(self._states.load(), equity)
        if not state.may_trade:
            return WatchdogVerdict(state=state, reason=state.reason)

        drawdown = state.drawdown_pct(equity)
        if drawdown > self._policy.max_drawdown_pct:
            detail = (
                f"equity {equity} is {drawdown}% below the high-water mark "
                f"{state.high_water_mark}, limit {self._policy.max_drawdown_pct}%"
            )
            return WatchdogVerdict(
                state=await self.trip(DRAWDOWN_RULE, detail), tripped=True, reason=detail
            )

        daily_loss = state.daily_loss_pct(equity)
        if daily_loss > self._policy.max_daily_loss_pct:
            detail = (
                f"equity {equity} is {daily_loss}% below day-start {state.day_start_equity}, "
                f"limit {self._policy.max_daily_loss_pct}%"
            )
            await self._store.append(
                self._events().risk_event(
                    tier=RiskTier.TIER2,
                    rule=DAILY_LOSS_RULE,
                    scope="portfolio",
                    action="orders_halted_for_the_day",
                    detail=detail,
                )
            )
            return WatchdogVerdict(state=state, day_halted=True, reason=detail)

        return WatchdogVerdict(state=await self._raise_mark(state, equity))

    async def trip(self, rule: str, detail: str) -> RiskState:
        """Stop everything. Idempotent — tripping an already-tripped switch changes nothing."""
        state = self._states.load()
        if not state.may_trade:
            return state
        tripped = await self._states.save(
            state.model_copy(
                update={"kill_switch": KillSwitchState.TRIPPED, "reason": f"{rule}: {detail}"}
            )
        )
        events = self._events()
        await self._store.append(
            events.kill_switch_changed(KillSwitchState.TRIPPED, detail, actor=rule),
            events.risk_event(
                tier=RiskTier.TIER2,
                rule=rule,
                scope="portfolio",
                action="kill_switch_tripped",
                detail=detail,
            ),
        )
        logger.error("kill switch tripped", extra={"rule": rule, "detail": detail})
        return tripped

    async def rearm(self, equity: Decimal, *, actor: str) -> RiskState:
        """Re-arm after a human has typed the phrase. The baselines restart from here.

        Resetting the high-water mark is deliberate: re-arming is an assertion that the operator
        has looked at what happened and accepts the current equity as the new starting point.
        Keeping the old mark would trip the switch again on the next sweep.
        """
        state = await self._states.save(
            RiskState(
                kill_switch=KillSwitchState.ARMED,
                reason=f"re-armed by {actor}",
                high_water_mark=equity,
                day_start_equity=equity,
                day_started_on=start_of_day(self._clock.now()),
                updated_at=self._clock.now(),
            )
        )
        await self._store.append(
            self._events().kill_switch_changed(
                KillSwitchState.ARMED, "re-armed with typed confirmation", actor=actor
            )
        )
        logger.warning("kill switch re-armed", extra={"actor": actor})
        return state

    async def halt_basket(self, basket_id: str, reason: str) -> BasketStatus:
        """Stop one basket. Only a human clears it (DESIGN §6.1)."""
        status = await self._states.set_basket_status(basket_id, BasketStatus.HALTED, reason)
        await self._store.append(
            self._events().basket_status_changed(basket_id, BasketStatus.HALTED, reason)
        )
        logger.error("basket halted", extra={"basket_id": basket_id, "reason": reason})
        return status

    async def resume_basket(self, basket_id: str, *, actor: str) -> BasketStatus:
        status = await self._states.set_basket_status(
            basket_id, BasketStatus.ACTIVE, f"un-halted by {actor}"
        )
        await self._store.append(
            self._events().basket_status_changed(
                basket_id, BasketStatus.ACTIVE, f"un-halted by {actor}"
            )
        )
        return status

    def use_universe(self, instruments: Iterable[Instrument]) -> None:
        """Adopt the configured instrument set, so a flow is converted against current truth.

        Not captured at construction: it moves whenever a basket adds an instrument, and a set
        fixed at boot is the same defect ADR 0021 fixed for the Tier-2 cap (PHASE_12 §3.7).
        """
        self._position_currencies = base_currencies_of(instruments)

    async def record_flow(self, flow: ExternalFlow) -> RiskState:
        """Move both baselines by an external deposit or withdrawal, in the notional currency.

        Without this a withdrawal reads as a drawdown and trips the switch, and a deposit masks a
        real loss (DESIGN §6.6, R16). The adjustment is the flow itself, never a re-derivation from
        current equity, which would launder a genuine loss into the baseline.

        **The currency is not decoration.** `startup.py` used to drop it and add the bare amount to
        baselines denominated in the notional currency, so a 9,000 USDC deposit raised the
        high-water mark by 9,000 while contributing nothing at all to equity — a guaranteed
        spurious kill-switch trip on the very next check (PHASE_12 Finding 4). A flow this cannot
        value refuses: a baseline adjusted by a number in the wrong unit is worse than no
        adjustment, and the caller turns the refusal into a halted process.
        """
        amount = value_cash(
            flow.currency,
            flow.amount,
            self._marks,
            notional_currency=self._notional_currency,
            position_currencies=self._position_currencies,
            now=self._clock.now(),
            tolerance=self._policy.mark_tolerance,
        )
        if amount is None:
            raise FailClosedError(
                f"an external flow of {flow.amount} {flow.currency} cannot be valued in "
                f"{self._notional_currency}, so the drawdown baselines cannot be adjusted for it; "
                "adjusting them by a number in the wrong unit would fabricate a drawdown"
            )
        state = self._states.load()
        adjusted = await self._states.save(
            state.model_copy(
                update={
                    "high_water_mark": max(state.high_water_mark + amount, ZERO),
                    "day_start_equity": max(state.day_start_equity + amount, ZERO),
                }
            )
        )
        logger.info(
            "risk baselines flow-adjusted",
            extra={
                "amount": str(amount),
                "currency": flow.currency,
                "original": str(flow.amount),
                "why": flow.reason,
            },
        )
        return adjusted

    async def _session_day(self) -> str:
        now = self._clock.now()
        if self._calendar is None:
            return start_of_day(now)
        return await self._calendar.session_day(now)

    async def _roll_day(self, state: RiskState, equity: Decimal) -> RiskState:
        today = await self._session_day()
        if not rolled_over(state, today):
            return state
        return await self._states.save(
            state.model_copy(update={"day_start_equity": equity, "day_started_on": today})
        )

    async def _raise_mark(self, state: RiskState, equity: Decimal) -> RiskState:
        if equity <= state.high_water_mark:
            return state
        return await self._states.save(state.model_copy(update={"high_water_mark": equity}))
