"""Scenario 3 — six months of long exposure, with its own ledger (spec §10.4).

A different *kind* of instrument from scenarios 1 and 2, and §10.1 keeps them apart for a reason:
positions compound, so a candidate's cycle 400 happens in a market its own cycle 12 helped create.
Its numbers are not on the same scale as a snapshot-scored accuracy and must never be ranked
against one — §10.5 prints both rankings and names every position where they disagree.

`BacktestHarness` runs unchanged, in a workspace database, from a declared starting balance. No
`tradebot` seam is needed: `build_sim(start_equity=…)` already threads to `SimBroker.balances`.

Failure semantics: a frozen aggregate at the window's end reports `UNVALUABLE` and no figure at
all — freezing is ignorance, and a number produced in ignorance is worse than its absence. An
unknown `--window` refuses, naming the ones that exist. Nothing here writes to a bot database or
constructs a venue broker; its orders reach `SimBroker` only, in a workspace database.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from decision_lab.params import WINDOW_DAYS
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money
from tradebot.risk.aggregate import PortfolioAggregate


class ProfitEvidence(Protocol):
    """What `profit_of` and `veto_breakdown` need of `validation.Evidence`.

    A Protocol rather than the class itself, so the arithmetic can be driven by a stand-in without
    building a whole event log — and so this module states exactly which three of `Evidence`'s
    properties it depends on.
    """

    @property
    def realized_pnl(self) -> Decimal: ...

    @property
    def cost_usd(self) -> Decimal: ...

    @property
    def risk_events(self) -> Mapping[str, int]: ...


class Profit(DomainModel):
    """§10.4's headline, with both halves and never just the total."""

    #: A frozen aggregate. Every figure below is zero and none of them means anything.
    unvaluable: bool = False
    freeze_reason: str = ""
    equity: Money = ZERO
    start_equity: Money = ZERO
    #: `equity − start_equity`. Mark-to-market, so an open position at the window's end counts.
    total: Money = ZERO
    realized: Money = ZERO
    unrealized: Money = ZERO
    cost_usd: Money = ZERO
    #: `total − cost_usd`. The ask is efficient, not merely profitable.
    net: Money = ZERO


class VetoRow(DomainModel):
    """One rule's refusals over the run.

    §10.4: a panel that is right often but only in ways the price collar, the cooldown or the
    daily cap refuse is not an improvement — and a corpus sweep can never discover that.
    """

    rule: str
    action_taken: str = ""
    count: int = 0


def profit_of(
    aggregate: PortfolioAggregate, evidence: ProfitEvidence, *, start_equity: Decimal
) -> Profit:
    """What the run made, decomposed. `UNVALUABLE` when the portfolio cannot be valued.

    The total is **not** `Evidence.realized_pnl`: that property sums closed round trips only, so a
    run ending with an open position would report a profit that ignores it (§10.4). Realized is
    reported beside the mark-to-market total and unrealized is the difference — both halves always
    printed, because "made $80" reads very differently when $80 of it is still at risk.
    """
    if aggregate.frozen:
        # Every figure stays zero. Quoting realized alone would be the cost-basis fallback ADR
        # 0027 forbids, arriving as a partial answer instead of a wrong one.
        return Profit(unvaluable=True, freeze_reason=aggregate.frozen_reason)
    total = aggregate.equity - start_equity
    realized = evidence.realized_pnl
    return Profit(
        equity=aggregate.equity,
        start_equity=start_equity,
        total=total,
        realized=realized,
        unrealized=total - realized,
        cost_usd=evidence.cost_usd,
        net=total - evidence.cost_usd,
    )


def veto_breakdown(evidence: ProfitEvidence) -> tuple[VetoRow, ...]:
    """Which rule refused what, most frequent first.

    `Evidence.risk_events` is already keyed `rule/action_taken`, so this splits rather than
    re-folds the log. A key with no separator is kept under an empty action: a refusal nobody can
    parse is still a refusal that happened, and dropping it would understate the breakdown.
    """
    rows = []
    for key, count in evidence.risk_events.items():
        rule, _, action = key.partition("/")
        rows.append(VetoRow(rule=rule, action_taken=action, count=count))
    return tuple(sorted(rows, key=lambda row: (-row.count, row.rule, row.action_taken)))


def window_bounds(dataset_end: datetime, window: str) -> tuple[datetime, datetime]:
    """The window to replay, ending at the dataset's last covered instant.

    Backwards from the end rather than forwards from the start: the most recent history is the
    most relevant, and it is also the half most likely to be complete after a §4.3 repair.
    """
    if window not in WINDOW_DAYS:
        raise ConfigError(
            f"{window!r} is not a window this tool measures; it has "
            f"{', '.join(sorted(WINDOW_DAYS))}"
        )
    return dataset_end - timedelta(days=WINDOW_DAYS[window]), dataset_end
