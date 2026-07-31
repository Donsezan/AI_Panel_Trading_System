"""The per-cycle spend ceiling for panel deliberation.

A cost budget is a safety control, not an accounting nicety: an LLM panel that debates without a
ceiling is one bug away from spending unbounded money on a decision it will not act on (R6).
DESIGN §6.5 says exceeding the budget *truncates the debate early and resolves with whatever
rounds completed* — which is safe, because fewer rounds can only ever make the panel more likely
to fail to reach a qualified majority, and a panel without a majority produces `WAIT`.

**The scope is the cycle, not the panel run.** A five-instrument basket in `per_asset` mode runs
five panels against one ceiling, so one runaway instrument cannot spend the other four's budget
and one budget cannot be silently multiplied by the basket size.

**The rule is check-before-round, priced from the last round.** A round may begin only if the
remaining budget covers what the previous round actually cost; the first round is always allowed,
since a cycle that never asks anything is not a cheaper cycle, it is a broken one. Charging a
round in flight is therefore impossible to overrun by more than the estimate's error, and the
common case — free models, priced at zero — is never truncated at all.

Failure semantics: this class cannot fail closed or open, because it never decides anything on
its own. It answers "is there room" and records what was spent; the protocol decides to stop.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.money import ZERO


class CycleBudget:
    """Tracks deliberation spend against one cycle's ceiling."""

    __slots__ = ("_limit", "_spent", "_truncated")

    def __init__(self, limit: Decimal = ZERO) -> None:
        if limit < ZERO:
            raise ValueError(f"a cost ceiling cannot be negative, got {limit}")
        self._limit = limit
        self._spent = ZERO
        self._truncated = False

    @property
    def limit(self) -> Decimal:
        return self._limit

    @property
    def spent(self) -> Decimal:
        return self._spent

    @property
    def remaining(self) -> Decimal:
        return max(self._limit - self._spent, ZERO)

    @property
    def truncated(self) -> bool:
        """True once a round has been refused for want of budget."""
        return self._truncated

    def spend(self, amount: Decimal) -> None:
        """Record what a completed round cost. Spending past the limit is recorded, not rejected —
        the round already happened, and hiding it would understate the cycle's true cost."""
        self._spent += max(amount, ZERO)

    def can_afford(self, estimate: Decimal) -> bool:
        """Whether another round priced at `estimate` fits. Latches `truncated` when it does not."""
        if estimate <= self.remaining:
            return True
        self._truncated = True
        return False
