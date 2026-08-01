"""The shadow A/B comparison, folded out of the log like every other report (ADR 0016).

Two panels were run on the *same frozen snapshot* every cycle, so the market is not a variable
between them: every difference in this report is a difference in how the two panels read one
identical packet of evidence. That is the whole reason the harness exists — a few weeks of
forward PnL cannot tell two panels apart, and running them in different weeks compares markets.

What is counted, and why each one:

* **agreement rate** — how often the two reached the same action. Interesting mostly when it is
  very high (the challenger is not a different experiment) or very low (one of them is unstable).
* **tradable divergence** — the cycles where one side would have placed an order and the other
  would not. The only disagreement that would have moved money, and the one an operator reads.
* **conviction spread** — signed and absolute, so a challenger that agrees on every action while
  being systematically bolder is visible. Conviction feeds the Tier-1 floor and sizing, so two
  panels that always agree on *action* are still two different position sizes.
* **cost per decision** — each side against its own spend, over the cycles where both ran.

Failure semantics: this module only reads. Cycles where the challenger failed are counted and
listed rather than dropped — a comparison silently computed over the cycles that happened to
succeed would overstate agreement exactly when the challenger was least reliable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradebot.core.enums import Action, Mode
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO, divide, multiply, to_decimal
from tradebot.persistence.store import EventStore
from tradebot.validation.payload import money, nested, rows, text

#: What a comparison reads. As narrow as `evidence.REPORT_TYPES`, and for the same reason: a
#: soak's log is mostly snapshots and transcripts, and no report needs either.
COMPARISON_TYPES: tuple[EventType, ...] = (
    EventType.CYCLE_STARTED,
    EventType.CYCLE_COMPLETED,
    EventType.DECISION_MADE,
    EventType.SHADOW_EVALUATED,
)

HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Pairing:
    """One instrument in one cycle, as each panel saw it."""

    cycle_id: str
    basket_id: str
    at: datetime
    instrument_key: str
    champion: Action
    challenger: Action
    champion_conviction: Decimal = ZERO
    challenger_conviction: Decimal = ZERO

    @property
    def agreed(self) -> bool:
        return self.champion is self.challenger

    @property
    def diverged_tradably(self) -> bool:
        """Whether exactly one of them asked for an order. The disagreement that costs money."""
        return self.champion.is_tradable is not self.challenger.is_tradable

    @property
    def conviction_gap(self) -> Decimal:
        """Challenger minus champion. Positive means the challenger was the bolder of the two."""
        return self.challenger_conviction - self.champion_conviction


@dataclass(frozen=True, slots=True)
class ShadowFailure:
    """A cycle whose challenger could not be evaluated. The champion's cycle was unaffected."""

    cycle_id: str
    at: datetime
    error: str


@dataclass(frozen=True, slots=True)
class Comparison:
    """Everything the log says about two panels judged on one stream of snapshots."""

    since: datetime | None
    until: datetime | None
    challenger_panels: tuple[str, ...] = ()
    pairings: tuple[Pairing, ...] = ()
    failures: tuple[ShadowFailure, ...] = ()
    compared_cycles: int = 0
    #: Instruments one side ruled on and the other did not — a challenger that answered for fewer
    #: instruments than it was asked about. Counted rather than paired against a guess.
    unpaired: int = 0
    champion_cost: Decimal = ZERO
    challenger_cost: Decimal = ZERO

    @classmethod
    def gather(
        cls, store: EventStore, *, since: datetime | None = None, until: datetime | None = None
    ) -> Comparison:
        """Fold the log's champion decisions and challenger evaluations into one summary."""
        fold = _Fold()
        for event in store.read_types(*COMPARISON_TYPES, since=since, until=until):
            fold.apply(event)
        return fold.finish(since, until)

    @property
    def ran(self) -> bool:
        """Whether the window contains a shadow evaluation at all."""
        return bool(self.compared_cycles or self.failures)

    @property
    def agreements(self) -> int:
        return sum(1 for pairing in self.pairings if pairing.agreed)

    @property
    def agreement_pct(self) -> Decimal:
        return _pct(Decimal(self.agreements), Decimal(len(self.pairings)))

    @property
    def disagreements(self) -> tuple[Pairing, ...]:
        return tuple(pairing for pairing in self.pairings if not pairing.agreed)

    @property
    def tradable_divergences(self) -> tuple[Pairing, ...]:
        return tuple(pairing for pairing in self.pairings if pairing.diverged_tradably)

    @property
    def matrix(self) -> dict[tuple[str, str], int]:
        """Champion action × challenger action. Where the disagreement actually lives."""
        return dict(
            Counter((pairing.champion.value, pairing.challenger.value) for pairing in self.pairings)
        )

    @property
    def conviction_gap_mean(self) -> Decimal:
        """Signed mean, so a systematically bolder challenger is visible as a sign, not a size."""
        total = sum((pairing.conviction_gap for pairing in self.pairings), start=ZERO)
        return divide(total, Decimal(len(self.pairings))) if self.pairings else ZERO

    @property
    def conviction_gap_abs_mean(self) -> Decimal:
        total = sum((abs(pairing.conviction_gap) for pairing in self.pairings), start=ZERO)
        return divide(total, Decimal(len(self.pairings))) if self.pairings else ZERO

    def cost_per_decision(self, total: Decimal) -> Decimal:
        return divide(total, Decimal(len(self.pairings))) if self.pairings else ZERO


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """A comparison, stamped with when and against which database it was produced."""

    mode: Mode
    generated_at: datetime
    comparison: Comparison


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return divide(multiply(part, HUNDRED), whole) if whole else ZERO


@dataclass(slots=True)
class _CycleFold:
    """One cycle's two verdicts, accumulated as its events go past."""

    cycle_id: str
    basket_id: str = ""
    at: datetime | None = None
    champion_cost: Decimal = ZERO
    challenger_cost: Decimal = ZERO
    champion: dict[str, tuple[Action, Decimal]] = field(default_factory=dict)
    challenger: dict[str, tuple[Action, Decimal]] = field(default_factory=dict)
    panel_id: str = ""
    error: str = ""
    evaluated: bool = False


class _Fold:
    """Accumulates one pass over the log. Dispatch is a table, never a chain of `if`s."""

    def __init__(self) -> None:
        self._cycles: dict[str, _CycleFold] = {}
        self._handlers = {
            EventType.CYCLE_STARTED: self._cycle_started,
            EventType.CYCLE_COMPLETED: self._cycle_completed,
            EventType.DECISION_MADE: self._decision_made,
            EventType.SHADOW_EVALUATED: self._shadow_evaluated,
        }

    def apply(self, event: Event) -> None:
        handler = self._handlers.get(event.type)
        if handler is not None:
            handler(event)

    def _cycle(self, event: Event) -> _CycleFold:
        cycle_id = event.cycle_id or event.aggregate_id
        cycle = self._cycles.get(cycle_id)
        if cycle is None:
            cycle = _CycleFold(cycle_id=cycle_id, basket_id=event.basket_id or "", at=event.ts)
            self._cycles[cycle_id] = cycle
        return cycle

    def _cycle_started(self, event: Event) -> None:
        cycle = self._cycle(event)
        cycle.at = event.ts
        cycle.basket_id = event.basket_id or text(event, "basket_id")

    def _cycle_completed(self, event: Event) -> None:
        self._cycle(event).champion_cost = money(event, "cost_usd")

    def _decision_made(self, event: Event) -> None:
        verdict = _verdict(
            {
                "instrument_key": nested(event, "decision", "instrument_key"),
                "action": nested(event, "decision", "action"),
                "conviction": nested(event, "decision", "conviction"),
            }
        )
        if verdict is not None:
            key, value = verdict
            self._cycle(event).champion[key] = value

    def _shadow_evaluated(self, event: Event) -> None:
        cycle = self._cycle(event)
        cycle.evaluated = True
        cycle.panel_id = text(event, "panel_id")
        cycle.error = text(event, "error")
        cycle.challenger_cost = money(event, "cost_usd")
        for row in rows(event, "decisions"):
            verdict = _verdict(row)
            if verdict is not None:
                key, value = verdict
                cycle.challenger[key] = value

    def finish(self, since: datetime | None, until: datetime | None) -> Comparison:
        compared = [c for c in self._cycles.values() if c.evaluated and not c.error]
        return Comparison(
            since=since,
            until=until,
            challenger_panels=tuple(
                sorted({c.panel_id for c in self._cycles.values() if c.panel_id})
            ),
            pairings=tuple(pairing for cycle in compared for pairing in _pair(cycle)),
            failures=tuple(
                ShadowFailure(cycle_id=c.cycle_id, at=c.at or _UNDATED, error=c.error)
                for c in self._cycles.values()
                if c.error
            ),
            compared_cycles=len(compared),
            unpaired=sum(len(set(c.champion) ^ set(c.challenger)) for c in compared),
            champion_cost=sum((c.champion_cost for c in compared), start=ZERO),
            challenger_cost=sum((c.challenger_cost for c in compared), start=ZERO),
        )


def _pair(cycle: _CycleFold) -> tuple[Pairing, ...]:
    """Only instruments both panels ruled on. The rest are counted as `unpaired`, never assumed."""
    return tuple(
        Pairing(
            cycle_id=cycle.cycle_id,
            basket_id=cycle.basket_id,
            at=cycle.at or _UNDATED,
            instrument_key=key,
            champion=cycle.champion[key][0],
            challenger=cycle.challenger[key][0],
            champion_conviction=cycle.champion[key][1],
            challenger_conviction=cycle.challenger[key][1],
        )
        for key in sorted(set(cycle.champion) & set(cycle.challenger))
    )


def _verdict(row: dict[str, Any]) -> tuple[str, tuple[Action, Decimal]] | None:
    """One decision as `(instrument_key, (action, conviction))`, or nothing it can be read as."""
    key = row.get("instrument_key")
    try:
        action = Action(str(row.get("action")))
    except ValueError:
        return None
    if not isinstance(key, str) or not key:
        return None
    conviction = row.get("conviction")
    return key, (action, to_decimal(conviction) if conviction is not None else ZERO)


#: Stands in for a pairing whose cycle never recorded a start — a window that opened mid-cycle.
_UNDATED = datetime(1970, 1, 1, tzinfo=UTC)
