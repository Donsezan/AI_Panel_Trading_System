"""The blotter's rows: baskets, and the instruments under them.

Pure assembly over already-fetched projections, so the shape an operator reads is testable
without a browser and without a database. The route fetches; this decides what a row *says*.

Two facts about this view are easy to misread, and both are properties of the domain rather than
of the UI:

* **A position belongs to the portfolio, not to a basket** (DESIGN §4). Two baskets listing the
  same instrument therefore show the *same* holding on both rows — there is one position, and
  pretending otherwise is how two baskets independently size against the same exposure.
* **A decision belongs to the basket that made it.** Those are per basket *and* instrument, which
  is why `Queries.latest_decisions` partitions by both.

The row's state is one label chosen by precedence, and the precedence is the point: a **halt** is
the system refusing, a **pause** is the operator's intent, a **quarantine** still cycles and only
refuses the order (ADR 0022). Showing the mildest of them when a stronger one is in force would
tell an operator the bot is doing something it is not.

Failure semantics: nothing here reads a store, so nothing here fails. A basket with no positions,
no decisions and no cycles renders as a basket with nothing under it, which is what a freshly
configured basket is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradebot.control.config_store import ConfigRecord
from tradebot.core.config import Basket
from tradebot.core.instrument import Instrument
from tradebot.dashboard.scope import Scope

__all__ = ["BasketRow", "InstrumentRow", "build"]

ACTIVE = "active"
HALTED = "halted"
QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    """One instrument as it appears under its basket."""

    basket_id: str
    instrument: Instrument
    #: The portfolio's holding, or `None` when flat. A projection row, rendered by the filters.
    position: Any | None
    #: What this basket's panel last concluded here, or `None` before it has ever decided.
    decision: Any | None
    quarantined: bool
    selected: bool

    @property
    def key(self) -> str:
        return self.instrument.key

    @property
    def scope(self) -> Scope:
        return Scope(self.basket_id, self.instrument.key)

    @property
    def held(self) -> bool:
        return self.position is not None and self.position.qty > 0

    @property
    def state(self) -> str:
        return QUARANTINED if self.quarantined else ACTIVE


@dataclass(frozen=True, slots=True)
class BasketRow:
    """One basket, its counters, and the instruments it may trade."""

    record: ConfigRecord[Basket]
    instruments: tuple[InstrumentRow, ...]
    cycles_today: int
    trades_today: int
    #: When this basket next cycles. `None` whenever nothing is waiting to cycle it — supervision
    #: stopped, the basket paused, or halted — which is a different fact from "not soon".
    next_due: datetime | None
    #: Why the *system* stopped this basket, or empty. Never the operator's own pause.
    halted_reason: str
    selected: bool

    @property
    def basket(self) -> Basket:
        return self.record.document

    @property
    def basket_id(self) -> str:
        return self.record.ref.config_id

    @property
    def scope(self) -> Scope:
        return Scope(self.basket_id)

    @property
    def trade_cap(self) -> int:
        return self.basket.risk_policy.max_trades_per_day

    @property
    def at_cap(self) -> bool:
        """Whether the daily trade cap is spent — the rule that silently ends a basket's day."""
        return self.trades_today >= self.trade_cap

    @property
    def quarantined(self) -> bool:
        return self.basket.risk_policy.quarantined

    @property
    def held(self) -> bool:
        return any(row.held for row in self.instruments)

    @property
    def state(self) -> str:
        """The strongest thing currently true of this basket, as one label.

        Ordered strongest first: a halted basket that is also quarantined is *halted*, because
        that is the one an operator has to clear before anything else matters.
        """
        for condition, label in (
            (bool(self.halted_reason), HALTED),
            (not self.basket.status.may_trade, self.basket.status.value),
            (self.quarantined, QUARANTINED),
        ):
            if condition:
                return label
        return ACTIVE


def build(
    records: Sequence[ConfigRecord[Basket]],
    *,
    positions: Sequence[Any],
    decisions: Sequence[Any],
    halted: dict[str, str],
    cycles_today: dict[str, int],
    trades_today: dict[str, int],
    next_due: Callable[[str], datetime | None],
    scope: Scope | None = None,
) -> tuple[BasketRow, ...]:
    """Every basket in service, with its instruments, counters and selection state."""
    held = {row.instrument_key: row for row in positions if row.qty > 0}
    decided = {(row.basket_id, row.instrument_key): row for row in decisions}
    return tuple(
        _basket_row(record, held, decided, halted, cycles_today, trades_today, next_due, scope)
        for record in records
    )


def _basket_row(
    record: ConfigRecord[Basket],
    held: dict[str, Any],
    decided: dict[tuple[str, str], Any],
    halted: dict[str, str],
    cycles_today: dict[str, int],
    trades_today: dict[str, int],
    next_due: Callable[[str], datetime | None],
    scope: Scope | None,
) -> BasketRow:
    basket_id = record.ref.config_id
    policy = record.document.risk_policy
    return BasketRow(
        record=record,
        instruments=tuple(
            InstrumentRow(
                basket_id=basket_id,
                instrument=instrument,
                position=held.get(instrument.key),
                decision=decided.get((basket_id, instrument.key)),
                quarantined=policy.excludes(instrument.key),
                selected=_selects(scope, basket_id, instrument.key),
            )
            for instrument in record.document.instruments
        ),
        cycles_today=cycles_today.get(basket_id, 0),
        trades_today=trades_today.get(basket_id, 0),
        next_due=next_due(basket_id),
        halted_reason=halted.get(basket_id, ""),
        selected=_selects(scope, basket_id, None),
    )


def _selects(scope: Scope | None, basket_id: str, instrument_key: str | None) -> bool:
    """Whether the URL's selection names exactly this row — the basket, or one instrument in it."""
    if scope is None or scope.basket_id != basket_id:
        return False
    return scope.instrument_key == instrument_key


def realized(rows: Sequence[Any]) -> Decimal:
    """Realized PnL across the given position rows, totalled in Python.

    Never `SUM` in SQL: money is TEXT precisely because SQLite's numeric affinity rounds through
    an IEEE-754 double (`queries.py`).
    """
    return sum((row.realized_pnl for row in rows), Decimal(0))
