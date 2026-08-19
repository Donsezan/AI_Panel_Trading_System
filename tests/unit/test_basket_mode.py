"""`basket` decision mode: one panel run, an assessment per instrument.

The richer mode DESIGN §4 asks for — the panel sees cross-asset structure instead of judging each
instrument in isolation. It also introduces the one place where a single provider call answers
for several instruments, so the two things tested hardest here are that a hallucinated symbol
cannot become a vote, and that one call's cost is counted once.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from tests.doubles import ScriptedLLM

from tradebot.core.budget import CycleBudget
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.decision import BasketAssessment
from tradebot.core.enums import Action, DecisionMode
from tradebot.core.errors import SchemaViolationError
from tradebot.core.instrument import Instrument
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.prompts import build_system_prompt, build_user_prompt, symbols_requested
from tradebot.decision.protocols import BlindThenDebateProtocol
from tradebot.decision.providers import DEFAULT_RESPONSE, StubLLMProvider
from tradebot.decision.seat import SeatRunner, parse_assessments
from tradebot.interfaces.debate import PanelRequest

BTC = "sim:BTC/USDT"
ETH = "sim:ETH/USDT"
SYMBOLS = {"BTC/USDT": BTC, "ETH/USDT": ETH, BTC.upper(): BTC, ETH.upper(): ETH}


def assessment(action: str, thesis: str = "t") -> dict[str, object]:
    return {
        "action": action,
        "conviction": 4,
        "size_hint": "half" if action in ("BUY", "SELL") else "none",
        "thesis": thesis,
        "key_risks": [],
        "invalidation": "none",
    }


def basket_response(**by_symbol: str) -> str:
    return json.dumps(
        {
            "assessments": {symbol: assessment(action) for symbol, action in by_symbol.items()},
            "basket_view": "The two move together; sizing both at once doubles one bet.",
        }
    )


BOTH_BUY = basket_response(**{"BTC/USDT": "BUY", "ETH/USDT": "BUY"})
SPLIT = basket_response(**{"BTC/USDT": "BUY", "ETH/USDT": "HOLD"})


@pytest.fixture
def seats() -> tuple[SeatConfig, ...]:
    return (
        SeatConfig(seat_id="a", role="Technical", provider_id="scripted", model="model-a"),
        SeatConfig(seat_id="b", role="News", provider_id="scripted", model="model-b"),
    )


@pytest.fixture
def basket_of_two(
    instrument: Instrument, second_instrument: Instrument, seats: tuple[SeatConfig, ...]
) -> Basket:
    return Basket(
        basket_id="b1",
        name="two-asset basket",
        instruments=(instrument, second_instrument),
        panel=PanelConfig(panel_id="p", seats=seats, protocol="blind_then_debate", max_rounds=2),
        decision_mode=DecisionMode.BASKET,
    )


class TestParsing:
    def test_one_response_becomes_a_vote_per_instrument(self) -> None:
        votes = parse_assessments(SPLIT, SYMBOLS)
        assert votes[BTC].action is Action.BUY
        assert votes[ETH].action is Action.HOLD

    def test_the_qualified_instrument_key_is_also_accepted(self) -> None:
        """A model echoing back `venue:symbol` is being helpful, not wrong."""
        votes = parse_assessments(basket_response(**{"sim:BTC/USDT": "BUY"}), SYMBOLS)
        assert votes[BTC].action is Action.BUY

    def test_an_instrument_we_never_asked_about_is_a_schema_violation(self) -> None:
        """A confident opinion on an absent instrument is a hallucination, not an opinion."""
        with pytest.raises(SchemaViolationError, match="DOGE"):
            parse_assessments(basket_response(**{"DOGE/USDT": "BUY"}), SYMBOLS)

    def test_a_missing_instrument_is_not_a_violation(self) -> None:
        """One omission must not discard the assessments that were sound."""
        votes = parse_assessments(basket_response(**{"BTC/USDT": "BUY"}), SYMBOLS)
        assert set(votes) == {BTC}

    def test_an_empty_assessment_map_is_rejected(self) -> None:
        with pytest.raises(SchemaViolationError):
            parse_assessments('{"assessments": {}, "basket_view": "nothing"}', SYMBOLS)

    def test_a_per_asset_response_does_not_satisfy_the_basket_contract(self) -> None:
        with pytest.raises(SchemaViolationError):
            parse_assessments(json.dumps(assessment("BUY")), SYMBOLS)

    def test_an_incoherent_vote_inside_the_map_fails_the_whole_response(self) -> None:
        payload = {"assessments": {"BTC/USDT": {**assessment("BUY"), "size_hint": "none"}}}
        with pytest.raises(SchemaViolationError):
            parse_assessments(json.dumps(payload), SYMBOLS)

    def test_an_overlong_basket_view_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="basket_view"):
            BasketAssessment.model_validate(
                {
                    "assessments": {"BTC/USDT": assessment("BUY")},
                    "basket_view": "word " * 400,
                }
            )


class TestPrompt:
    def test_the_prompt_lists_exactly_the_symbols_to_assess(
        self, two_instrument_snapshot: ContextSnapshot, seats: tuple[SeatConfig, ...]
    ) -> None:
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)
        prompt = build_user_prompt(two_instrument_snapshot, seats[0], request)
        assert "Assess exactly these symbols" in prompt
        assert "BTC/USDT, ETH/USDT" in prompt

    def test_the_schema_asks_for_a_map_keyed_by_symbol(self, seats: tuple[SeatConfig, ...]) -> None:
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)
        system = build_system_prompt(seats[0], request)
        assert '"assessments"' in system
        assert '"basket_view"' in system

    def test_per_asset_mode_asks_for_a_single_assessment(
        self, seats: tuple[SeatConfig, ...]
    ) -> None:
        system = build_system_prompt(seats[0], PanelRequest.for_instrument(BTC))
        assert '"assessments"' not in system


class TestSeatRunnerInBasketMode:
    async def test_one_call_answers_for_every_instrument(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        seats: tuple[SeatConfig, ...],
    ) -> None:
        provider = ScriptedLLM({"model-a": [BOTH_BUY]}, cost_usd=Decimal("0.03"))
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)

        responses = await SeatRunner({"scripted": provider}, clock).run(
            seats[0], two_instrument_snapshot, request
        )

        assert len(responses) == 2
        assert len(provider.calls) == 1, "one call, not one per instrument"
        assert len({r.call_id for r in responses}) == 1, "shared call id is what de-dups the cost"

    async def test_an_unassessed_instrument_abstains_alone(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        seats: tuple[SeatConfig, ...],
    ) -> None:
        provider = ScriptedLLM({"model-a": [basket_response(**{"BTC/USDT": "BUY"})]})
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)

        by_key = {
            r.instrument_key: r
            for r in await SeatRunner({"scripted": provider}, clock).run(
                seats[0], two_instrument_snapshot, request
            )
        }
        assert by_key[BTC].vote is not None
        assert by_key[ETH].abstained

    async def test_a_hallucinated_symbol_costs_the_seat_every_vote(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        seats: tuple[SeatConfig, ...],
    ) -> None:
        """One repair attempt, then the whole seat abstains — fail closed, not partially trusted."""
        bad = basket_response(**{"BTC/USDT": "BUY", "DOGE/USDT": "BUY"})
        provider = ScriptedLLM({"model-a": [bad, bad, BOTH_BUY]})
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)

        responses = await SeatRunner({"scripted": provider}, clock).run(
            seats[0], two_instrument_snapshot, request
        )
        assert all(r.abstained for r in responses)
        assert len(provider.calls) == 2, "the original call plus exactly one repair"


class TestEngineModes:
    async def test_basket_mode_yields_one_decision_per_instrument_from_one_run(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        basket_of_two: Basket,
    ) -> None:
        provider = ScriptedLLM({"model-a": [SPLIT], "model-b": [SPLIT]}, cost_usd=Decimal("0.02"))
        engine = DecisionEngine(SeatRunner({"scripted": provider}, clock))

        outcome = await engine.deliberate(two_instrument_snapshot, basket_of_two)

        assert len(outcome.deliberations) == 1, "one panel run covers the basket"
        assert [d.instrument_key for d in outcome.decisions] == [BTC, ETH]
        assert {d.action for d in outcome.decisions} == {Action.BUY, Action.HOLD}

    async def test_basket_mode_counts_each_call_once(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        basket_of_two: Basket,
    ) -> None:
        """Two seats, one round, one call each — 0.04, not 0.08 for four responses."""
        provider = ScriptedLLM(
            {"model-a": [BOTH_BUY], "model-b": [BOTH_BUY]}, cost_usd=Decimal("0.02")
        )
        engine = DecisionEngine(SeatRunner({"scripted": provider}, clock))

        outcome = await engine.deliberate(two_instrument_snapshot, basket_of_two)

        assert len(outcome.responses) == 4
        assert outcome.cost_usd == Decimal("0.04")

    async def test_per_asset_mode_runs_one_panel_per_instrument(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        basket_of_two: Basket,
    ) -> None:
        single = json.dumps(assessment("BUY"))
        provider = ScriptedLLM({"model-a": [single], "model-b": [single]})
        engine = DecisionEngine(SeatRunner({"scripted": provider}, clock))

        outcome = await engine.deliberate(
            two_instrument_snapshot,
            basket_of_two.model_copy(update={"decision_mode": DecisionMode.PER_ASSET}),
        )

        assert len(outcome.deliberations) == 2
        assert [d.instrument_key for d in outcome.decisions] == [BTC, ETH]

    async def test_both_modes_share_one_cycle_budget(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        basket_of_two: Basket,
    ) -> None:
        """A basket must not multiply the ceiling by its instrument count."""
        single = json.dumps(assessment("BUY"))
        provider = ScriptedLLM(
            {"model-a": [single, json.dumps(assessment("SELL"))], "model-b": [single]},
            cost_usd=Decimal("0.30"),
        )
        panel = basket_of_two.panel.model_copy(update={"max_cost_usd_per_cycle": Decimal("1.00")})
        engine = DecisionEngine(SeatRunner({"scripted": provider}, clock))

        outcome = await engine.deliberate(
            two_instrument_snapshot,
            basket_of_two.model_copy(
                update={"decision_mode": DecisionMode.PER_ASSET, "panel": panel}
            ),
        )
        assert outcome.budget_truncated, "the second instrument inherits the first's spend"


class TestPanelRequest:
    def test_per_asset_covers_exactly_one_instrument(self) -> None:
        with pytest.raises(ValueError, match="exactly one instrument"):
            PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.PER_ASSET)

    def test_an_instrument_cannot_appear_twice(self) -> None:
        with pytest.raises(ValueError, match="only once"):
            PanelRequest(instrument_keys=(BTC, BTC), decision_mode=DecisionMode.BASKET)

    def test_a_run_covers_something(self) -> None:
        with pytest.raises(ValueError, match="at least one instrument"):
            PanelRequest(instrument_keys=(), decision_mode=DecisionMode.BASKET)


async def test_a_basket_transcript_says_which_instrument_an_argument_was_about(
    two_instrument_snapshot: ContextSnapshot, clock: ManualClock, basket_of_two: Basket
) -> None:
    """Without the symbol, round 1 reads as three opinions about nothing in particular."""
    provider = ScriptedLLM({"model-a": [SPLIT], "model-b": [BOTH_BUY]})
    protocol = BlindThenDebateProtocol(SeatRunner({"scripted": provider}, clock))

    await protocol.deliberate(
        two_instrument_snapshot,
        basket_of_two.panel,
        PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET),
        CycleBudget(Decimal("1.00")),
    )

    transcript = provider.calls_for("model-a")[1].user
    assert "Analyst B on BTC/USDT" in transcript
    assert "Analyst B on ETH/USDT" in transcript


class TestTheStubPanelAnswersInBasketMode:
    """The default panel is the stub, so a stub that can only answer per-asset makes `basket`
    mode unusable: every seat violates the schema, the repair replays the same canned text, and
    the panel resolves `WAIT (PANEL_DEGRADED)` on every cycle forever. A real model reads the
    schema out of its prompt; the stub has to do the same or it is easier to satisfy in one mode
    than the other, which is exactly what the contract suite exists to prevent.
    """

    async def test_an_unscripted_stub_assesses_every_symbol_the_prompt_asked_for(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        seats: tuple[SeatConfig, ...],
    ) -> None:
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)
        runner = SeatRunner({"scripted": StubLLMProvider(provider_id="scripted")}, clock)

        responses = await runner.run(seats[0], two_instrument_snapshot, request)

        assert [r.abstain_reason for r in responses] == [None, None]
        assert {r.instrument_key for r in responses} == {BTC, ETH}

    async def test_an_unscripted_stub_still_answers_a_per_asset_prompt(
        self, snapshot: ContextSnapshot, clock: ManualClock, seats: tuple[SeatConfig, ...]
    ) -> None:
        runner = SeatRunner({"scripted": StubLLMProvider(provider_id="scripted")}, clock)

        responses = await runner.run(seats[0], snapshot, PanelRequest.for_instrument(BTC))

        assert responses[0].vote is not None
        assert responses[0].raw_text == DEFAULT_RESPONSE

    async def test_a_scripted_response_is_never_rewritten_to_fit_the_mode(
        self,
        two_instrument_snapshot: ContextSnapshot,
        clock: ManualClock,
        seats: tuple[SeatConfig, ...],
    ) -> None:
        """Scripting a per-asset vote into a basket run is the rung-3 fault injection. Adapting
        it would delete the only way to assert that a malformed answer fails closed."""
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)
        stub = StubLLMProvider([DEFAULT_RESPONSE], provider_id="scripted")

        responses = await SeatRunner({"scripted": stub}, clock).run(
            seats[0], two_instrument_snapshot, request
        )

        assert all(r.abstained for r in responses)

    def test_the_stub_reads_the_symbols_out_of_the_prompt_the_builder_writes(
        self, two_instrument_snapshot: ContextSnapshot, seats: tuple[SeatConfig, ...]
    ) -> None:
        """Two-sided, because the coupling is a string: the builder writes the line and the
        reader parses it, so a reworded prompt breaks here rather than in a sim run."""
        request = PanelRequest(instrument_keys=(BTC, ETH), decision_mode=DecisionMode.BASKET)

        prompt = build_user_prompt(two_instrument_snapshot, seats[0], request)

        assert symbols_requested(prompt) == ("BTC/USDT", "ETH/USDT")

    def test_a_per_asset_prompt_asks_for_no_symbol_list(
        self, snapshot: ContextSnapshot, seats: tuple[SeatConfig, ...]
    ) -> None:
        prompt = build_user_prompt(snapshot, seats[0], PanelRequest.for_instrument(BTC))

        assert symbols_requested(prompt) == ()
