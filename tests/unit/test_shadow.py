"""The A/B challenger: what it records, what it costs, and what it must never do.

The contract under test is narrow and absolute — the challenger is deliberated on the champion's
own snapshot, its verdict goes into the log, and *nothing about it* can reach an order or change
what the cycle decided (ADR 0018).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, ProviderSettings, SeatConfig
from tradebot.core.enums import Action, ProviderKind
from tradebot.core.events import EventFactory, EventType
from tradebot.core.instrument import Instrument
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.providers import DEFAULT_RESPONSE, FAIL, StubLLMProvider
from tradebot.decision.seat import SeatRunner
from tradebot.decision.shadow import ShadowEvaluator
from tradebot.persistence.store import EventStore

HOLD_RESPONSE = """{
  "action": "HOLD",
  "conviction": 2,
  "size_hint": "none",
  "thesis": "The position is fine where it is; nothing in this packet argues for adding.",
  "key_risks": ["a breakout would leave us underweight"],
  "invalidation": "a close above the range high"
}"""


def a_panel(panel_id: str, provider_id: str = "stub") -> PanelConfig:
    return PanelConfig(
        panel_id=panel_id,
        seats=(
            SeatConfig(
                seat_id="analyst", role="Analyst", provider_id=provider_id, model=f"{panel_id}-m"
            ),
        ),
    )


def a_basket(
    instrument: Instrument, *, champion: PanelConfig, shadow: PanelConfig | None
) -> Basket:
    return Basket(
        basket_id="b1",
        name="test basket",
        instruments=(instrument,),
        panel=champion,
        shadow_panel=shadow,
    )


def events_for(clock: ManualClock) -> EventFactory:
    return EventFactory(clock=clock, basket_id="b1", cycle_id="c1")


def evaluator(clock: ManualClock, store: EventStore, *responses: str) -> ShadowEvaluator:
    engine = DecisionEngine(SeatRunner({"stub": StubLLMProvider(list(responses))}, clock))
    return ShadowEvaluator(engine, store)


class TestBasketConfiguration:
    def test_no_shadow_panel_means_no_challenger(self, instrument: Instrument) -> None:
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=None)

        assert basket.challenger is None
        assert basket.panels == (basket.panel,)

    def test_a_challenger_inherits_the_instruments_and_mode(self, instrument: Instrument) -> None:
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))

        challenger = basket.challenger
        assert challenger is not None
        assert challenger.panel.panel_id == "chall"
        assert challenger.instruments == basket.instruments
        assert challenger.decision_mode is basket.decision_mode
        # It is not itself shadowed; a challenger of a challenger would recurse forever.
        assert challenger.shadow_panel is None

    def test_the_two_panels_must_be_distinguishable(self, instrument: Instrument) -> None:
        """A report whose two sides carry the same name cannot be read."""
        with pytest.raises(ValidationError, match="repeats the champion's id"):
            a_basket(instrument, champion=a_panel("same"), shadow=a_panel("same"))

    def test_a_shared_provider_id_must_carry_identical_settings(
        self, instrument: Instrument
    ) -> None:
        """One wiring serves both panels, so two meanings for one id would misprice a budget."""
        champion = a_panel("champ").model_copy(
            update={
                "providers": (
                    ProviderSettings(provider_id="p", kind=ProviderKind.STUB),
                    ProviderSettings(provider_id="stub", kind=ProviderKind.STUB),
                ),
            }
        )
        shadow = a_panel("chall").model_copy(
            update={
                "providers": (
                    ProviderSettings(
                        provider_id="p",
                        kind=ProviderKind.OPENAI_COMPAT,
                        base_url="https://elsewhere.example/v1",
                    ),
                    ProviderSettings(provider_id="stub", kind=ProviderKind.STUB),
                ),
            }
        )
        with pytest.raises(ValidationError, match="declare p differently"):
            a_basket(instrument, champion=champion, shadow=shadow)

    def test_an_identically_declared_provider_is_shared_happily(
        self, instrument: Instrument
    ) -> None:
        shared = ProviderSettings(provider_id="stub", kind=ProviderKind.STUB)
        champion = a_panel("champ").model_copy(update={"providers": (shared,)})
        shadow = a_panel("chall").model_copy(update={"providers": (shared,)})

        basket = a_basket(instrument, champion=champion, shadow=shadow)
        assert len(basket.panels) == 2


class TestEvaluation:
    async def test_a_basket_without_a_challenger_records_nothing(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=None)

        await evaluator(clock, store).evaluate(snapshot, basket, events_for(clock))

        assert store.count() == 0

    async def test_the_challenger_s_verdict_reaches_the_log(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))

        await evaluator(clock, store, HOLD_RESPONSE).evaluate(snapshot, basket, events_for(clock))

        (event,) = store.read_types(EventType.SHADOW_EVALUATED)
        assert event.payload["panel_id"] == "chall"
        assert event.payload["error"] == ""
        assert [d["action"] for d in event.payload["decisions"]] == [Action.HOLD.value]

    async def test_a_challenger_failure_is_recorded_and_never_raised(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        """The champion's cycle is already complete; a research failure may not disturb it."""
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))
        broken = ShadowEvaluator(_ExplodingEngine(), store)

        await broken.evaluate(snapshot, basket, events_for(clock))

        (event,) = store.read_types(EventType.SHADOW_EVALUATED)
        assert "the provider is on fire" in event.payload["error"]
        assert event.payload["decisions"] == []

    async def test_an_abstaining_challenger_still_records_a_verdict(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        """A degraded challenger is a WAIT, not a gap: the comparison must be able to see it."""
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))

        await evaluator(clock, store, FAIL).evaluate(snapshot, basket, events_for(clock))

        (event,) = store.read_types(EventType.SHADOW_EVALUATED)
        assert event.payload["error"] == ""
        assert [d["action"] for d in event.payload["decisions"]] == [Action.WAIT.value]

    async def test_the_challenger_s_cost_is_recorded_against_the_challenger(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        """`$/decision` for the panel that traded stays true only if this stays separate."""
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))
        engine = DecisionEngine(
            SeatRunner(
                {"stub": StubLLMProvider([DEFAULT_RESPONSE], cost_usd=Decimal("0.02"))}, clock
            )
        )

        await ShadowEvaluator(engine, store).evaluate(snapshot, basket, events_for(clock))

        (event,) = store.read_types(EventType.SHADOW_EVALUATED)
        assert Decimal(event.payload["cost_usd"]) == Decimal("0.02")
        # Nothing about the cycle itself was written: the runner owns that record.
        assert store.read_types(EventType.CYCLE_COMPLETED) == ()

    async def test_nothing_but_the_shadow_event_is_written(
        self,
        instrument: Instrument,
        snapshot: ContextSnapshot,
        clock: ManualClock,
        store: EventStore,
    ) -> None:
        """No decision, no risk check, no order — the challenger is a record, not a proposal."""
        basket = a_basket(instrument, champion=a_panel("champ"), shadow=a_panel("chall"))

        await evaluator(clock, store, DEFAULT_RESPONSE).evaluate(
            snapshot, basket, events_for(clock)
        )

        assert [event.type for event in store.read_all()] == [EventType.SHADOW_EVALUATED]


class _ExplodingEngine:
    """A decision engine that fails the way a dead provider chain does."""

    async def deliberate(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the provider is on fire")
