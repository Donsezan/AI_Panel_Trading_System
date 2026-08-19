"""The offline stub's varied answers: the catalogue, the draw, and what a panel does with it.

The point of these is not that the stub says different things — it is that a panel *fed*
different things exercises the consensus rule. A single canned answer means every cycle reaches
the same qualified majority at the same conviction, so the disagreement paths in
`decision/consensus.py` have never run outside a hand-scripted test.

Two properties are load-bearing and asserted here rather than assumed:

* **A `stub-*` model still answers with `DEFAULT_RESPONSE`, byte for byte.** The whole suite and
  the zero-configuration demo run on it; variety is opt-in through the model name and must not
  leak into the seats that did not ask for it.
* **A scripted response is still returned verbatim**, whatever the model is called. Scripting is
  the rung-3 fault injection, and a stub that adapted a script to the model would delete the only
  way to assert that a malformed answer fails closed.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, RiskPolicy, SeatConfig
from tradebot.core.decision import SeatVote
from tradebot.core.enums import Action, DecisionMode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.presets import SIM_PANEL
from tradebot.decision.prompts import build_user_prompt
from tradebot.decision.providers.stub import (
    CATALOGUE_PATH,
    DEFAULT_RESPONSE,
    FAIL,
    RENDERINGS,
    VARIED_PREFIX,
    StubLLMProvider,
    load_catalogue,
)
from tradebot.decision.seat import SeatRunner, parse_vote
from tradebot.interfaces.debate import PanelRequest
from tradebot.interfaces.llm import CompletionRequest

#: Enough cycles for a three-way draw over fifteen entries to visit every consensus outcome, and
#: small enough to stay a unit test. Deterministic under the seeded rng below.
CYCLES = 60


def a_request(model: str, user: str = "As of: now") -> CompletionRequest:
    return CompletionRequest(model=model, system="you are a seat", user=user)


def varied_seat(seat_id: str, role: str, *, devils_advocate: bool = False) -> SeatConfig:
    return SeatConfig(
        seat_id=seat_id,
        role=role,
        provider_id="stub",
        model=f"{VARIED_PREFIX}{seat_id}",
        evidence=("indicators", "position"),
        devils_advocate=devils_advocate,
    )


class TestTheShippedCatalogue:
    """The file is the artifact an operator edits, so its contents are asserted, not assumed."""

    def test_it_loads_and_every_entry_is_a_valid_vote(self) -> None:
        catalogue = load_catalogue()

        assert len(catalogue.responses) == 15
        assert all(isinstance(vote, SeatVote) for vote in catalogue.responses)

    def test_it_covers_every_action_a_seat_may_take(self) -> None:
        """The spread is the whole reason the file exists.

        A catalogue of nothing but BUY would load, validate, and leave the non-consensus and
        HOLD paths as unexercised as one canned answer does.
        """
        actions = {vote.action for vote in load_catalogue().responses}

        assert actions == {Action.BUY, Action.SELL, Action.HOLD, Action.WAIT}

    def test_it_spans_the_conviction_scale(self) -> None:
        """`_conviction` normalizes over 1–5; a catalogue clustered at one rating never moves it."""
        assert {vote.conviction for vote in load_catalogue().responses} == {1, 2, 3, 4, 5}

    def test_the_shipped_file_is_where_the_loader_looks(self) -> None:
        assert CATALOGUE_PATH.name == "stub_responses.json"
        assert CATALOGUE_PATH.is_file()


class TestARefusedCatalogue:
    """A hand-edited file fails by name. Silent abstentions at 03:00 are the alternative."""

    def test_a_missing_file_is_refused_by_name(self, tmp_path: Path) -> None:
        absent = tmp_path / "nope.json"

        with pytest.raises(ConfigError, match=re.escape("nope.json")):
            load_catalogue(absent)

    def test_unparseable_json_is_refused_by_name(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text('{"responses": [', encoding="utf-8")

        with pytest.raises(ConfigError, match=re.escape("broken.json")):
            load_catalogue(broken)

    def test_an_entry_that_is_not_a_vote_is_refused(self, tmp_path: Path) -> None:
        """The same contract a real model's answer is held to (DESIGN [L8])."""
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps({"responses": [{"action": "BUY", "conviction": 9, "size_hint": "half"}]}),
            encoding="utf-8",
        )

        with pytest.raises(ConfigError, match=re.escape("bad.json")):
            load_catalogue(bad)

    def test_an_empty_catalogue_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"responses": []}), encoding="utf-8")

        with pytest.raises(ConfigError, match=re.escape("empty.json")):
            load_catalogue(empty)


class TestTheFixedStubIsUntouched:
    """Variety is opt-in through the model name. Everything else must be byte-identical."""

    async def test_a_stub_model_still_answers_with_the_fixed_response(self) -> None:
        provider = StubLLMProvider()

        result = await provider.complete(a_request("stub-technical"))

        assert result.text == DEFAULT_RESPONSE

    async def test_a_scripted_varied_model_still_returns_its_script_verbatim(self) -> None:
        """Scripting outranks the model name: it is the fault-injection path."""
        scripted = "I would probably buy some."
        provider = StubLLMProvider([scripted])

        result = await provider.complete(a_request(f"{VARIED_PREFIX}technical"))

        assert result.text == scripted

    async def test_a_scripted_failure_still_raises_for_a_varied_model(self) -> None:
        provider = StubLLMProvider([FAIL])

        with pytest.raises(Exception, match="scripted failure"):
            await provider.complete(a_request(f"{VARIED_PREFIX}technical"))


class TestTheVariedDraw:
    async def test_a_varied_model_answers_with_a_vote_from_the_catalogue(self) -> None:
        provider = StubLLMProvider(rng=random.Random(1))

        result = await provider.complete(a_request(f"{VARIED_PREFIX}technical"))

        assert parse_vote(result.text) in load_catalogue().responses

    async def test_repeated_calls_do_not_all_say_the_same_thing(self) -> None:
        """The defect this feature exists to fix, asserted directly."""
        provider = StubLLMProvider(rng=random.Random(1))

        texts = [
            (await provider.complete(a_request(f"{VARIED_PREFIX}technical"))).text
            for _ in range(CYCLES)
        ]

        assert len({parse_vote(text).action for text in texts}) > 1

    async def test_every_rendering_style_parses_back_to_a_vote(self) -> None:
        """Real completions arrive fenced and wrapped in prose; `_JSON_BLOCK` tolerates it.

        Nothing on the default path produced one before, so that tolerance was exercised only by
        a hand-written test. Now it is on every sim cycle — which is the point, but only if every
        style this emits actually survives the parser.
        """
        vote = load_catalogue().responses[0]
        body = json.dumps(vote.model_dump(mode="json"), indent=2)

        for rendering in RENDERINGS:
            assert parse_vote(rendering.format(body=body)) == vote

    async def test_a_basket_prompt_draws_each_symbol_independently(
        self, two_instrument_snapshot: ContextSnapshot, seat: SeatConfig
    ) -> None:
        """One shared draw per call would make a basket-mode panel unanimous by construction."""
        request = PanelRequest(
            instrument_keys=tuple(c.instrument.key for c in two_instrument_snapshot.instruments),
            decision_mode=DecisionMode.BASKET,
        )
        user = build_user_prompt(two_instrument_snapshot, seat, request, (), "")
        provider = StubLLMProvider(rng=random.Random(1))

        disagreed = False
        for _ in range(CYCLES):
            result = await provider.complete(a_request(f"{VARIED_PREFIX}technical", user))
            votes = json.loads(result.text[result.text.index("{") : result.text.rindex("}") + 1])
            actions = {vote["action"] for vote in votes["assessments"].values()}
            disagreed = disagreed or len(actions) > 1

        assert disagreed

    async def test_a_basket_prompt_still_covers_every_symbol_it_was_asked_about(
        self, two_instrument_snapshot: ContextSnapshot, seat: SeatConfig, clock: ManualClock
    ) -> None:
        """A drawn answer is held to the same contract as the fixed one: no instrument abstains
        merely because the seat is varied."""
        keys = tuple(c.instrument.key for c in two_instrument_snapshot.instruments)
        request = PanelRequest(instrument_keys=keys, decision_mode=DecisionMode.BASKET)
        runner = SeatRunner({"stub": StubLLMProvider(rng=random.Random(1))}, clock)

        responses = await runner.run(
            varied_seat("technical", "Technical"), two_instrument_snapshot, request
        )

        assert [r.abstain_reason for r in responses] == [None, None]
        assert {r.instrument_key for r in responses} == set(keys)


class TestWhatAPanelDoesWithIt:
    """The reason for all of the above: the consensus rule's branches actually run."""

    async def test_a_three_seat_panel_reaches_every_consensus_outcome(
        self, snapshot: ContextSnapshot, instrument: Instrument, clock: ManualClock
    ) -> None:
        panel = PanelConfig(
            panel_id="varied",
            protocol="blind_then_debate",
            max_rounds=3,
            seats=(
                varied_seat("technical", "Technical Analyst"),
                varied_seat("news", "News Analyst"),
                varied_seat("skeptic", "Macro Skeptic", devils_advocate=True),
            ),
        )
        basket = Basket(
            basket_id="b1",
            name="varied",
            instruments=(instrument,),
            panel=panel,
            risk_policy=RiskPolicy(),
        )
        engine = DecisionEngine(SeatRunner({"stub": StubLLMProvider(rng=random.Random(7))}, clock))

        seen: Counter[str] = Counter()
        for _ in range(CYCLES):
            outcome = await engine.deliberate(snapshot, basket)
            for decision in outcome.decisions:
                label = decision.action.value
                if "no qualified majority" in decision.reasoning_summary:
                    label = "NO_MAJORITY"
                seen[label] += 1

        assert seen["BUY"] and seen["SELL"], f"no tradable majority in {CYCLES} cycles: {seen}"
        assert seen["HOLD"], f"the HOLD path never ran: {seen}"
        assert seen["NO_MAJORITY"], f"the panel always agreed, which it should not: {seen}"

    async def test_conviction_varies_between_cycles(
        self, snapshot: ContextSnapshot, instrument: Instrument, clock: ManualClock
    ) -> None:
        """`_conviction` folds the agreeing seats' ratings. One canned rating pins it forever."""
        panel = PanelConfig(
            panel_id="varied",
            seats=(varied_seat("technical", "Technical"), varied_seat("news", "News")),
        )
        basket = Basket(
            basket_id="b1",
            name="varied",
            instruments=(instrument,),
            panel=panel,
            risk_policy=RiskPolicy(),
        )
        engine = DecisionEngine(SeatRunner({"stub": StubLLMProvider(rng=random.Random(3))}, clock))

        convictions = set()
        for _ in range(CYCLES):
            outcome = await engine.deliberate(snapshot, basket)
            convictions.update(decision.conviction for decision in outcome.decisions)

        assert len(convictions) > 1


class TestTheSimPanel:
    def test_every_seat_draws_from_the_catalogue(self) -> None:
        assert SIM_PANEL.seats
        assert all(seat.model.startswith(VARIED_PREFIX) for seat in SIM_PANEL.seats)

    def test_it_debates_rather_than_answering_once(self) -> None:
        """A single round over one seat is what `stub` already is; this panel exists to argue."""
        assert SIM_PANEL.protocol == "blind_then_debate"
        assert SIM_PANEL.max_rounds > 1
        assert len(SIM_PANEL.seats) >= 3

    def test_its_seats_are_distinguishable(self) -> None:
        """Two seats on one fingerprint trip PANEL_HOMOGENEOUS on every cycle."""
        fingerprints = {(seat.provider_id, seat.model) for seat in SIM_PANEL.seats}

        assert len(fingerprints) == len(SIM_PANEL.seats)
