"""Folding two panels' verdicts out of the log.

The arithmetic is the point: an agreement rate computed over the wrong denominator, or a cost
attributed to the wrong panel, would make a panel look better or worse than it was — and panel
selection is what this whole harness exists to inform.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, CycleOutcome, SizeHint
from tradebot.core.events import EventFactory
from tradebot.persistence.store import EventStore
from tradebot.validation.comparison import Comparison

BTC = "sim:BTC/USDT"
ETH = "sim:ETH/USDT"


def a_decision(key: str, action: Action, conviction: str = "0.7") -> Decision:
    return Decision(
        instrument_key=key,
        action=action,
        conviction=Decimal(conviction),
        size_hint=SizeHint.HALF if action.is_tradable else SizeHint.NONE,
    )


async def a_compared_cycle(
    store: EventStore,
    clock: ManualClock,
    *,
    cycle_id: str,
    champion: tuple[Decision, ...],
    challenger: tuple[Decision, ...],
    champion_cost: str = "0.02",
    challenger_cost: str = "0.05",
    error: str = "",
    panel_id: str = "chall",
) -> None:
    events = EventFactory(clock=clock, basket_id="demo", cycle_id=cycle_id)
    await store.append(events.cycle_started((), "sim"))
    for decision in champion:
        await store.append(events.decision_made(decision))
    await store.append(
        events.shadow_evaluated(panel_id, challenger, Decimal(challenger_cost), error=error)
    )
    await store.append(events.cycle_completed(CycleOutcome.NO_ACTION, Decimal(champion_cost)))


class TestPairing:
    async def test_agreement_is_counted_over_instruments_both_panels_ruled_on(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY), a_decision(ETH, Action.WAIT)),
            challenger=(a_decision(BTC, Action.BUY), a_decision(ETH, Action.HOLD)),
        )

        comparison = Comparison.gather(store)
        assert len(comparison.pairings) == 2
        assert comparison.agreements == 1
        assert comparison.agreement_pct == Decimal(50)
        assert [p.instrument_key for p in comparison.disagreements] == [ETH]

    async def test_an_instrument_only_one_panel_ruled_on_is_unpaired_not_guessed(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY), a_decision(ETH, Action.WAIT)),
            challenger=(a_decision(BTC, Action.BUY),),
        )

        comparison = Comparison.gather(store)
        assert [p.instrument_key for p in comparison.pairings] == [BTC]
        assert comparison.unpaired == 1

    async def test_a_cycle_with_no_challenger_contributes_nothing(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """A basket with no shadow panel must not drag the denominators around."""
        events = EventFactory(clock=clock, basket_id="demo", cycle_id="lonely")
        await store.append(events.cycle_started((), "sim"))
        await store.append(events.decision_made(a_decision(BTC, Action.BUY)))
        await store.append(events.cycle_completed(CycleOutcome.NO_ACTION, Decimal("0.02")))

        comparison = Comparison.gather(store)
        assert not comparison.ran
        assert comparison.pairings == ()
        assert comparison.champion_cost == Decimal(0)


class TestDivergence:
    async def test_only_a_difference_in_tradability_would_have_moved_money(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """HOLD versus WAIT is a research signal; BUY versus WAIT is a position."""
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.HOLD), a_decision(ETH, Action.BUY)),
            challenger=(a_decision(BTC, Action.WAIT), a_decision(ETH, Action.WAIT)),
        )

        comparison = Comparison.gather(store)
        assert len(comparison.disagreements) == 2
        assert [p.instrument_key for p in comparison.tradable_divergences] == [ETH]

    async def test_the_matrix_shows_where_the_disagreement_lives(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY), a_decision(ETH, Action.BUY)),
            challenger=(a_decision(BTC, Action.WAIT), a_decision(ETH, Action.WAIT)),
        )

        assert Comparison.gather(store).matrix == {("BUY", "WAIT"): 2}

    async def test_conviction_spread_is_signed_so_a_bolder_challenger_is_visible(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY, "0.60"), a_decision(ETH, Action.BUY, "0.50")),
            challenger=(a_decision(BTC, Action.BUY, "0.80"), a_decision(ETH, Action.BUY, "0.90")),
        )

        comparison = Comparison.gather(store)
        assert comparison.conviction_gap_mean == Decimal("0.30")
        assert comparison.conviction_gap_abs_mean == Decimal("0.30")

    async def test_opposite_gaps_cancel_in_the_signed_mean_but_not_the_absolute_one(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY, "0.50"), a_decision(ETH, Action.BUY, "0.90")),
            challenger=(a_decision(BTC, Action.BUY, "0.90"), a_decision(ETH, Action.BUY, "0.50")),
        )

        comparison = Comparison.gather(store)
        assert comparison.conviction_gap_mean == Decimal(0)
        assert comparison.conviction_gap_abs_mean == Decimal("0.40")


class TestFailuresAndCost:
    async def test_a_failed_challenger_is_listed_and_excluded_from_the_arithmetic(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """Dropping failures silently would overstate agreement exactly when it was least real."""
        await a_compared_cycle(
            store,
            clock,
            cycle_id="ok",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.BUY),),
        )
        clock.advance(timedelta(hours=1).total_seconds())
        await a_compared_cycle(
            store,
            clock,
            cycle_id="broken",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(),
            challenger_cost="0",
            error="ProviderError: everything is down",
        )

        comparison = Comparison.gather(store)
        assert comparison.compared_cycles == 1
        assert [f.cycle_id for f in comparison.failures] == ["broken"]
        assert len(comparison.pairings) == 1
        assert comparison.ran

    async def test_each_side_is_costed_against_its_own_spend(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.BUY),),
            champion_cost="0.02",
            challenger_cost="0.10",
        )

        comparison = Comparison.gather(store)
        assert comparison.champion_cost == Decimal("0.02")
        assert comparison.challenger_cost == Decimal("0.10")
        assert comparison.cost_per_decision(comparison.challenger_cost) == Decimal("0.10")

    async def test_a_window_narrows_to_the_cycles_inside_it(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_compared_cycle(
            store,
            clock,
            cycle_id="early",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.WAIT),),
        )
        clock.advance(timedelta(days=2).total_seconds())
        boundary = clock.now()
        await a_compared_cycle(
            store,
            clock,
            cycle_id="late",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.BUY),),
        )

        comparison = Comparison.gather(store, since=boundary)
        assert comparison.compared_cycles == 1
        assert comparison.agreements == 1

    async def test_two_challenger_panels_in_one_window_are_both_named(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """The report warns on this: they are different experiments sharing a total."""
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c1",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.BUY),),
            panel_id="chall-a",
        )
        clock.advance(3600)
        await a_compared_cycle(
            store,
            clock,
            cycle_id="c2",
            champion=(a_decision(BTC, Action.BUY),),
            challenger=(a_decision(BTC, Action.BUY),),
            panel_id="chall-b",
        )

        assert Comparison.gather(store).challenger_panels == ("chall-a", "chall-b")


class TestDefensiveReading:
    async def test_an_unreadable_action_is_skipped_rather_than_guessed(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """A payload written by a future build must not become a fabricated verdict."""
        events = EventFactory(clock=clock, basket_id="demo", cycle_id="c1")
        await store.append(events.cycle_started((), "sim"))
        await store.append(events.decision_made(a_decision(BTC, Action.BUY)))
        shadow = events.shadow_evaluated("chall", (), Decimal("0.01"))
        await store.append(
            shadow.model_copy(
                update={"payload": {**shadow.payload, "decisions": [{"instrument_key": BTC}]}}
            )
        )
        await store.append(events.cycle_completed(CycleOutcome.NO_ACTION, Decimal("0.02")))

        comparison = Comparison.gather(store)
        assert comparison.pairings == ()
        assert comparison.unpaired == 1
