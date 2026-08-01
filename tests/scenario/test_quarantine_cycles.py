"""Rung 3: a quarantined scope over several cycles — data keeps arriving, nothing is traded.

The unit tests prove the rule refuses one proposal. This proves the property the operator
actually asked for holds *over time*: the loop keeps running, the snapshot keeps being frozen
with fresh quotes and computed indicators, and no order is ever placed — which is what makes a
quarantine something you can watch and then release on evidence, rather than a pause that blinds
you to the instrument you are trying to make up your mind about (ADR 0022).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from tests.scenario.harness import Harness

from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket
from tradebot.core.enums import CycleOutcome
from tradebot.core.events import Event, EventType
from tradebot.decision.providers import DEFAULT_RESPONSE
from tradebot.marketdata.replay import ReplayMarketData

pytestmark = pytest.mark.scenario

CYCLES = 3


def quarantined(basket: Basket, *, whole_basket: bool = False) -> Basket:
    """The same basket with its only instrument — or all of it — excluded by the operator."""
    keys = () if whole_basket else (basket.instruments[0].key,)
    return basket.model_copy(
        update={
            "risk_policy": basket.risk_policy.model_copy(
                update={"quarantined": whole_basket, "quarantined_instruments": keys}
            )
        }
    )


async def run(harness: Harness, cycles: int = CYCLES) -> list[CycleOutcome]:
    return [(await harness.runner.run_once()).outcome for _ in range(cycles)]


def snapshots(harness: Harness) -> list[dict[str, object]]:
    return [
        json.loads(json.dumps(event.payload["snapshot"]))
        for event in harness.store.read_all()
        if event.type is EventType.SNAPSHOT_FROZEN
    ]


def events_of(harness: Harness, event_type: EventType) -> list[Event]:
    return [event for event in harness.store.read_all() if event.type is event_type]


@pytest.fixture
async def instrument_quarantined(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData
) -> AsyncIterator[Harness]:
    built = Harness(quarantined(basket), clock, market_data, [DEFAULT_RESPONSE] * CYCLES)
    await built.start()
    yield built
    built.close()


@pytest.fixture
async def basket_quarantined(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData
) -> AsyncIterator[Harness]:
    built = Harness(
        quarantined(basket, whole_basket=True), clock, market_data, [DEFAULT_RESPONSE] * CYCLES
    )
    await built.start()
    yield built
    built.close()


class TestQuarantinedInstrument:
    async def test_every_cycle_is_vetoed_and_none_places_an_order(
        self, instrument_quarantined: Harness
    ) -> None:
        outcomes = await run(instrument_quarantined)

        assert outcomes == [CycleOutcome.RISK_VETOED] * CYCLES
        assert not events_of(instrument_quarantined, EventType.ORDER_SUBMITTED)

    async def test_the_position_stays_flat_across_every_cycle(
        self, instrument_quarantined: Harness
    ) -> None:
        await run(instrument_quarantined)

        key = instrument_quarantined.basket.instruments[0].key
        assert instrument_quarantined.ledger.position(key).is_flat

    async def test_the_data_keeps_arriving(self, instrument_quarantined: Harness) -> None:
        """The whole difference from a pause: one frozen snapshot per cycle, with indicators."""
        await run(instrument_quarantined)

        frozen = snapshots(instrument_quarantined)
        assert len(frozen) == CYCLES
        for snapshot in frozen:
            context = snapshot["instruments"][0]  # type: ignore[index]
            assert context["indicators"]
            assert Decimal(str(context["quote"]["last"])) > 0

    async def test_the_panel_still_deliberates_and_the_decision_is_recorded(
        self, instrument_quarantined: Harness
    ) -> None:
        """The documented cost of a per-instrument quarantine: the model call is still made.

        Recorded deliberately rather than optimized away — the research record of what the panel
        would have done while an instrument was under review is the reason to keep cycling it.
        """
        await run(instrument_quarantined)

        assert len(events_of(instrument_quarantined, EventType.SEAT_RESPONDED)) == CYCLES
        assert len(events_of(instrument_quarantined, EventType.DECISION_MADE)) == CYCLES

    async def test_the_veto_names_the_rule_in_the_log(
        self, instrument_quarantined: Harness
    ) -> None:
        """An auditable Tier-1 verdict, not a silent skip: the log says why nothing happened."""
        await run(instrument_quarantined, cycles=1)

        checks = [
            check
            for event in events_of(instrument_quarantined, EventType.RISK_CHECKED)
            for check in event.payload["checks"]
        ]
        blocked = [c for c in checks if c["rule"] == "quarantine" and c["decision"] == "veto"]
        assert len(blocked) == 1
        assert "quarantined" in blocked[0]["detail"]


class TestQuarantinedBasket:
    async def test_every_cycle_ends_as_quarantined_with_no_panel_run(
        self, basket_quarantined: Harness
    ) -> None:
        """A whole basket the operator excluded is not worth a model call to have vetoed."""
        outcomes = await run(basket_quarantined)

        assert outcomes == [CycleOutcome.QUARANTINED] * CYCLES
        assert not events_of(basket_quarantined, EventType.SEAT_RESPONDED)
        assert not events_of(basket_quarantined, EventType.ORDER_SUBMITTED)

    async def test_the_snapshot_is_still_frozen_every_cycle(
        self, basket_quarantined: Harness
    ) -> None:
        await run(basket_quarantined)

        assert len(snapshots(basket_quarantined)) == CYCLES

    async def test_the_cycle_is_recorded_rather_than_skipped(
        self, basket_quarantined: Harness
    ) -> None:
        """A basket that stops appearing in the log is a basket nobody can audit."""
        await run(basket_quarantined, cycles=1)

        completed = events_of(basket_quarantined, EventType.CYCLE_COMPLETED)
        assert [event.payload["outcome"] for event in completed] == ["quarantined"]
        assert basket_quarantined.projections()["cycles"]
