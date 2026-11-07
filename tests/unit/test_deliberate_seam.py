"""The second seam a replay needs: who answers for the panel, and the doors it must not open.

An answer that did not come from the configured panel is a substitution only a replay may make. A
paper soak is the evidence `report promotion` reads (ADR 0016, ADR 0020) and a live order sized by a
canned answer is real money, so paper and live are refused the way `test_news_feed_seam.py` refuses
them a substituted feed: `_assemble` before it builds anything, and `build_paper`/`build_live` by
not having the parameter at all.

Everything below the panel is untouched by construction. The substitute is asked only what
`DecisionEngine.deliberate` was, and the risk tiers and the venue still decide what happens.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import tradebot.app as app_module
from tradebot.app import BrokerChoice, _assemble, build, build_live, build_paper, build_sim
from tradebot.control.basket_runner import Deliberate
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket
from tradebot.core.decision import Decision, PanelOutcome
from tradebot.core.enums import Action, CycleOutcome, Mode
from tradebot.core.errors import ConfigError
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine

PAST = datetime(2024, 3, 11, tzinfo=UTC)


class Substitute:
    """Answers WAIT with a reason nothing else writes, and records what it was asked."""

    REASON = "answered by the substitute"

    def __init__(self) -> None:
        self.asked: list[tuple[ContextSnapshot, Basket]] = []

    async def __call__(self, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome:
        self.asked.append((snapshot, basket))
        return PanelOutcome(
            decisions=tuple(
                Decision(
                    instrument_key=instrument.key,
                    action=Action.WAIT,
                    reasoning_summary=self.REASON,
                )
                for instrument in basket.instruments
            )
        )


def _explode(*_: object, **__: object) -> Any:
    raise AssertionError("nothing may be built after the refusal")


def test_build_paper_and_build_live_carry_no_deliberate_parameter() -> None:
    for builder in (build_paper, build_live):
        assert "deliberate" not in inspect.signature(builder).parameters, builder.__name__


def test_build_sim_does_carry_it() -> None:
    """The guard above must not pass by the parameter simply never having been added."""
    assert "deliberate" in inspect.signature(build_sim).parameters


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_assemble_refuses_a_substitute_outside_sim(
    mode: Mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is the first statement: the database bomb proves nothing past the refusal ran."""
    monkeypatch.setattr(app_module, "create_database", _explode)
    substitute: Deliberate = Substitute()

    with pytest.raises(ConfigError, match=mode.value):
        await _assemble(
            ManualClock(PAST),
            mode=mode,
            db_path=None,
            broker_choice=BrokerChoice.BINANCE,
            start_equity=Decimal(10_000),
            baskets=None,
            global_policy=None,
            feed=app_module.PriceFeed(),
            news_sources=(),
            panel_id="stub",
            deliberate=substitute,
        )


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_the_dispatch_door_is_shut_too(mode: Mode) -> None:
    with pytest.raises(TypeError):
        await build(mode, deliberate=Substitute())


async def test_a_substitute_replaces_the_engines_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*_: object) -> PanelOutcome:
        raise AssertionError("the engine answered although a substitute was given")

    monkeypatch.setattr(DecisionEngine, "deliberate", never)
    substitute = Substitute()
    application = await build_sim(clock=ManualClock(PAST), deliberate=substitute)
    try:
        await application.recover()
        results = await application.supervisor.run_once()
    finally:
        await application.shutdown()

    assert len(substitute.asked) == len(results)
    assert [result.outcome for result in results] == [CycleOutcome.NO_ACTION] * len(results)
    assert all(
        decision.reasoning_summary == Substitute.REASON
        for result in results
        for decision in result.decisions
    )


class FalsySubstitute(Substitute):
    """A substitute that reads as false, as an empty container or a stateful wrapper might."""

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0


async def test_a_falsy_substitute_still_replaces_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam reads `is not None`: a truthiness test would hand a falsy substitute's cycles to
    the real engine, silently, and a replay would then pay for answers it was meant to recite."""

    async def never(*_: object) -> PanelOutcome:
        raise AssertionError("the engine answered although a substitute was given")

    monkeypatch.setattr(DecisionEngine, "deliberate", never)
    substitute = FalsySubstitute()
    assert not substitute, "the substitute must read as false for this test to bite"
    application = await build_sim(clock=ManualClock(PAST), deliberate=substitute)
    try:
        await application.recover()
        results = await application.supervisor.run_once()
    finally:
        await application.shutdown()

    assert results
    assert len(substitute.asked) == len(results)
    assert [result.outcome for result in results] == [CycleOutcome.NO_ACTION] * len(results)


async def test_the_engine_is_still_built_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A substitute must not be how a panel the bot would refuse gets to run."""
    refusal = "this panel names an unknown protocol"

    def refuse(_: DecisionEngine, __: Basket) -> None:
        raise ConfigError(refusal)

    monkeypatch.setattr(DecisionEngine, "validate", refuse)
    substitute = Substitute()
    application = await build_sim(clock=ManualClock(PAST), deliberate=substitute)
    try:
        await application.recover()
        await application.supervisor.run_once()
    finally:
        await application.shutdown()

    assert not substitute.asked


async def test_without_one_the_engine_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression net: the default is the old behaviour, a call to the engine."""
    real = DecisionEngine.deliberate
    asked: list[ContextSnapshot] = []

    async def spy(self: DecisionEngine, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome:
        asked.append(snapshot)
        return await real(self, snapshot, basket)

    monkeypatch.setattr(DecisionEngine, "deliberate", spy)
    application = await build_sim(clock=ManualClock(PAST))
    try:
        await application.recover()
        results = await application.supervisor.run_once()
    finally:
        await application.shutdown()

    assert results
    assert len(asked) == len(results)
