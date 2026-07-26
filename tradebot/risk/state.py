"""Risk state that outlives the process: the kill switch, halted baskets, and the baselines.

A restart must never silently un-halt anything (DESIGN §6.6, §8.2). That is the whole reason
this state lives in the database rather than in memory: the failure mode it defends against is
a crash loop where each restart forgets why the last one stopped and resumes trading into the
same problem.

Two baselines live here too, and they are **flow-adjusted**:

* the **high-water mark**, against which drawdown is measured, and
* **day-start equity**, against which the daily loss limit is measured.

A deposit raises both and a withdrawal lowers both, by the exact amount of the flow. Without
that, withdrawing your own money reads as a drawdown and trips the kill switch, and depositing
masks a real loss (DESIGN §6.6, R16).

Failure semantics: every read fails *closed*. An unreadable or absent state row is treated as
"kill switch tripped", because the alternative — assuming armed — lets a corrupted database
resume trading.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Engine, select

from tradebot.core.clock import Clock
from tradebot.core.enums import BasketStatus, KillSwitchState
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import basket_status, risk_state, upsert

#: Typed by a human to re-arm the kill switch. Deliberately awkward (PLAN §2.4).
REARM_PHRASE = "RE-ARM TRADING"

_SINGLETON = "global"


class RiskState(DomainModel):
    """The persisted risk posture of the whole system."""

    kill_switch: KillSwitchState = KillSwitchState.TRIPPED
    reason: str = ""
    high_water_mark: Money = Decimal(0)
    day_start_equity: Money = Decimal(0)
    day_started_on: str = ""
    updated_at: UtcDatetime

    @property
    def may_trade(self) -> bool:
        return self.kill_switch.may_trade

    def drawdown_pct(self, equity: Decimal) -> Decimal:
        """Loss from the high-water mark, 0–100. Zero before a mark has been established."""
        if self.high_water_mark <= ZERO:
            return ZERO
        return _pct_drop(self.high_water_mark, equity)

    def daily_loss_pct(self, equity: Decimal) -> Decimal:
        if self.day_start_equity <= ZERO:
            return ZERO
        return _pct_drop(self.day_start_equity, equity)


def _pct_drop(baseline: Decimal, equity: Decimal) -> Decimal:
    """Percentage fall from `baseline` to `equity`, on the 0–100 scale. A gain reads as zero."""
    drop = baseline - equity
    if drop <= ZERO:
        return ZERO
    return divide(multiply(drop, Decimal(100)), baseline)


class RiskStateStore:
    """Reads and writes risk state through the single writer that owns the database."""

    def __init__(self, engine: Engine, writer: SingleWriter, clock: Clock) -> None:
        self._engine = engine
        self._writer = writer
        self._clock = clock

    def load(self) -> RiskState:
        """Current posture. An absent row means "never initialised" — which means tripped."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(risk_state).where(risk_state.c.scope == _SINGLETON)
            ).one_or_none()
        if row is None:
            return RiskState(
                reason="no persisted risk state; the system has never been armed",
                updated_at=self._clock.now(),
            )
        return RiskState(
            kill_switch=KillSwitchState(row.kill_switch),
            reason=row.reason or "",
            high_water_mark=row.high_water_mark,
            day_start_equity=row.day_start_equity,
            day_started_on=row.day_started_on or "",
            updated_at=row.updated_at,
        )

    def halted_baskets(self) -> dict[str, str]:
        """Basket id → why it is not trading. Halted baskets need a human to clear them."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(basket_status).where(basket_status.c.status != BasketStatus.ACTIVE.value)
            ).all()
        return {row.basket_id: row.reason or "" for row in rows}

    def status_of(self, basket_id: str) -> BasketStatus:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(basket_status).where(basket_status.c.basket_id == basket_id)
            ).one_or_none()
        return BasketStatus(row.status) if row else BasketStatus.ACTIVE

    async def save(self, state: RiskState) -> RiskState:
        stamped = state.model_copy(update={"updated_at": self._clock.now()})
        values: dict[str, object] = {
            "scope": _SINGLETON,
            "kill_switch": stamped.kill_switch.value,
            "reason": stamped.reason,
            "high_water_mark": stamped.high_water_mark,
            "day_start_equity": stamped.day_start_equity,
            "day_started_on": stamped.day_started_on,
            "updated_at": stamped.updated_at,
        }
        await self._writer.run(lambda connection: upsert(connection, risk_state, values, ["scope"]))
        return stamped

    async def set_basket_status(
        self, basket_id: str, status: BasketStatus, reason: str
    ) -> BasketStatus:
        values: dict[str, object] = {
            "basket_id": basket_id,
            "status": status.value,
            "reason": reason,
            "updated_at": self._clock.now(),
        }
        await self._writer.run(
            lambda connection: upsert(connection, basket_status, values, ["basket_id"])
        )
        return status


def rolled_over(state: RiskState, now: datetime) -> bool:
    """Whether `now` starts a new risk day.

    UTC for crypto, which is what v1 trades. Equity sessions arrive with the trading calendars
    in Phase 5; hard-coding a market close here would be wrong for the venue we actually run.
    """
    return state.day_started_on != now.date().isoformat()


def start_of_day(now: datetime) -> str:
    return now.date().isoformat()


def assert_rearm_phrase(phrase: str | None) -> None:
    """Refuse to re-arm without the exact typed confirmation."""
    if phrase != REARM_PHRASE:
        raise ConfigError(
            f"re-arming the kill switch requires the exact phrase {REARM_PHRASE!r}; "
            "this is a deliberate human act, never an automatic recovery"
        )
