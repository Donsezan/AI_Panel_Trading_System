"""§10.4's profit arithmetic, in isolation.

"Total profit" is the number a reader will trust without checking, and it is the one this design
says is easiest to get wrong: `Evidence.realized_pnl` sums *closed* round trips only, so a run
ending with an open position would report a profit that ignores it — the same class of error as a
drawdown gate measuring cost basis (ADR 0027).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from decision_lab import longrun
from tradebot.core.errors import ConfigError
from tradebot.risk.aggregate import PortfolioAggregate

AT = datetime(2026, 1, 1, tzinfo=UTC)


class FakeEvidence:
    """Only the three properties `profit_of` and `veto_breakdown` read."""

    def __init__(
        self,
        realized: str = "0",
        cost: str = "0",
        risk_events: Mapping[str, int] | None = None,
    ) -> None:
        self.realized_pnl = Decimal(realized)
        self.cost_usd = Decimal(cost)
        self.risk_events = risk_events or {}


def valued(equity: str) -> PortfolioAggregate:
    return PortfolioAggregate(equity=Decimal(equity), gross_exposure=Decimal(0), as_of=AT)


def frozen(reason: str) -> PortfolioAggregate:
    return PortfolioAggregate(
        equity=Decimal(0), gross_exposure=Decimal(0), as_of=AT, frozen_reason=reason
    )


def test_total_profit_is_mark_to_market_not_realized_pnl() -> None:
    """A run ending with an open position: realized is 50, the position is worth 30 more."""
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="50"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(80)
    assert profit.realized == Decimal(50)
    assert profit.unrealized == Decimal(30)


def test_both_halves_are_always_present_never_just_the_total() -> None:
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="50"), start_equity=Decimal(1000)
    )
    assert profit.realized + profit.unrealized == profit.total


def test_net_profit_subtracts_what_the_deliberation_cost() -> None:
    """A panel that made $80 on $120 of tokens lost money. Nothing else in this design says so."""
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="80", cost="120"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(80)
    assert profit.net == Decimal(-40)


def test_a_loss_is_reported_as_a_loss() -> None:
    profit = longrun.profit_of(
        valued("900"), FakeEvidence(realized="-100"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(-100)
    assert profit.unrealized == Decimal(0)


def test_a_frozen_aggregate_reports_unvaluable_and_no_figure() -> None:
    """§10.4: freezing is ignorance, and a number produced in ignorance is worse than its
    absence — including a partial one."""
    profit = longrun.profit_of(
        frozen("no mark for binance:BTC/USDT"),
        FakeEvidence(realized="50", cost="10"),
        start_equity=Decimal(1000),
    )
    assert profit.unvaluable
    assert profit.freeze_reason == "no mark for binance:BTC/USDT"
    assert profit.total == Decimal(0)
    assert profit.realized == Decimal(0)
    assert profit.net == Decimal(0)


def test_the_veto_breakdown_splits_the_rule_from_the_action() -> None:
    """`Evidence.risk_events` is already keyed `rule/action_taken` — the breakdown reads it
    rather than re-folding the log."""
    rows = longrun.veto_breakdown(
        FakeEvidence(risk_events={"cooldown/veto": 4, "price_collar/veto": 1, "drawdown/warn": 2})
    )
    assert rows[0] == longrun.VetoRow(rule="cooldown", action_taken="veto", count=4)
    assert [row.count for row in rows] == [4, 2, 1], "most frequent first"


def test_a_malformed_risk_event_key_is_kept_not_dropped() -> None:
    """A row nobody can parse is still a refusal that happened; dropping it would understate the
    breakdown."""
    assert longrun.veto_breakdown(FakeEvidence(risk_events={"weird": 3})) == (
        longrun.VetoRow(rule="weird", action_taken="", count=3),
    )


def test_the_window_ends_at_the_dataset_and_runs_backwards() -> None:
    start, end = longrun.window_bounds(AT, "6m")
    assert end == AT
    assert end - start == timedelta(days=182)


def test_an_unknown_window_refuses_naming_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="6m"):
        longrun.window_bounds(AT, "7m")
