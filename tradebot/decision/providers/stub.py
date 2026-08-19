"""A scripted LLM provider. No network, no cost, and no non-determinism it was not asked for.

This is what makes the walking skeleton runnable and the whole suite free and repeatable. It is
also the fault-injection point for rung-3 chaos tests: script malformed JSON, a schema-violating
vote, or a provider outage and assert that the panel degrades to `WAIT` rather than trading on
junk (PLAN §7).

An *unscripted* stub answers in the schema the prompt asked for, because a real model does:
it reads the schema out of its system prompt. A stub that always answered in the per-asset
schema would fail `BasketAssessment` on every seat of a `basket`-mode basket, replay the same
canned text for the repair attempt, and leave the panel resolving `WAIT (PANEL_DEGRADED)` on
every cycle forever — the default panel being unable to run one of the two decision modes.
A *scripted* response is returned verbatim, always: scripting a per-asset vote into a basket
run is the rung-3 fault injection, and adapting it would delete the only way to assert that a
malformed answer fails closed.

**The stub serves two model families, and the seat's model name picks between them.** A model id
only means something to the provider serving it, and this provider is not a vendor — so the names
are ours to define:

* `stub-*` answers with `DEFAULT_RESPONSE`, every time. The zero-configuration demo and the whole
  suite run on it, and it must stay byte-identical.
* `varied-*` draws a vote at random from `stub_responses.json` for each instrument it is asked
  about, and renders it the way a real completion arrives — sometimes bare, sometimes fenced,
  sometimes behind a sentence of prose.

The second family exists because one canned answer means every cycle reaches the same qualified
majority at the same conviction. The disagreement branches of `decision/consensus.py` — no
qualified majority, a conviction that is a mean of differing ratings, a size clamped to the most
conservative agreeing seat — are unreachable from a panel that always says the same thing, and so
is the debate itself: `has_converged` stops `blind_then_debate` after the blind round when every
seat already agrees. Which of the two a seat uses is *panel data*, so it is versioned, pinned per
cycle (ADR 0013), and editable in the dashboard rather than being a process-wide flag that the
log could not distinguish.

The catalogue is a committed JSON file rather than a constant so that changing what a simulated
panel argues about needs no code change. It is read once per process and cached; an edit takes
effect on the next start.

Failure semantics: raises `ProviderError` when scripted to, exactly as a real provider would on
a timeout, so the seat's fallback-then-abstain path is exercised by the same code path. A
catalogue that is missing or is not a set of valid votes raises `ConfigError` naming the file —
loudly, because the alternative is a run of silent abstentions nobody can explain.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from decimal import Decimal
from itertools import cycle
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError, model_validator

from tradebot.core.decision import SeatVote
from tradebot.core.errors import ConfigError, ProviderError
from tradebot.core.schema import DomainModel
from tradebot.decision.prompts import symbols_requested
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

DEFAULT_RESPONSE = """{
  "action": "BUY",
  "conviction": 4,
  "size_hint": "half",
  "thesis": "Momentum is constructive and the position is well within budget.",
  "key_risks": ["momentum can reverse without warning"],
  "invalidation": "RSI closing back below 45 on the 1h timeframe"
}"""

#: The cross-asset half of a `basket`-mode answer. The per-instrument half is `DEFAULT_RESPONSE`
#: itself, so the stub says the same thing about an instrument in either decision mode and a
#: scenario's outcome does not depend on which mode its basket is in.
DEFAULT_BASKET_VIEW = "These legs are one momentum view; taken together they double a single bet."

#: Sentinel entry: a scripted response equal to this raises instead of returning.
FAIL = "<<PROVIDER_FAILURE>>"

#: Model-name prefix that selects a random draw from the catalogue. A seat bound to
#: `varied-technical` argues; one bound to `stub-technical` recites.
VARIED_PREFIX: Final = "varied-"

#: The committed catalogue. Edited by hand — it is the one artifact here meant to be.
CATALOGUE_PATH: Final = Path(__file__).with_name("stub_responses.json")

#: How a drawn answer reaches the seat. Real completions are not bare JSON: they arrive fenced,
#: and often with a sentence in front of or behind the object. `seat.parse_vote` calls that "a
#: provider habit, not a contract violation" and tolerates it — but nothing on the default path
#: ever produced one, so that tolerance was exercised only by a hand-written test. Drawing the
#: style puts it on every simulated cycle. Each template is `str.format`ted, which reads
#: placeholders from the *template* only, so the braces in the JSON body are left alone.
RENDERINGS: Final[tuple[str, ...]] = (
    "{body}",
    "```json\n{body}\n```",
    "Here is my assessment.\n\n```json\n{body}\n```",
    "```json\n{body}\n```\n\nHappy to revisit this if the panel reads it differently.",
)


class StubResponseCatalogue(DomainModel):
    """The answers a `varied-*` seat draws from, as read from `stub_responses.json`.

    Every entry is a `SeatVote`, held to the same contract a real model's answer is (DESIGN [L8]).
    Validating at load rather than at use is deliberate: a hand-edited file that no longer parses
    should say so by name, not become a seat that abstains on every cycle for reasons an operator
    would have to read the event log to discover.
    """

    responses: tuple[SeatVote, ...]
    #: Cross-asset commentary for `basket` mode, drawn alongside the per-instrument votes.
    basket_views: tuple[str, ...] = (DEFAULT_BASKET_VIEW,)
    #: What the file is and how to edit it, carried in the artifact rather than only in the code
    #: that reads it. Unused here; present so the document round-trips and `extra="forbid"` does
    #: not reject the guidance written for whoever opens it.
    notes: str = ""

    @model_validator(mode="after")
    def _check_not_empty(self) -> StubResponseCatalogue:
        if not self.responses:
            raise ValueError("a stub response catalogue needs at least one vote to draw from")
        if not self.basket_views:
            raise ValueError("a stub response catalogue needs at least one basket view")
        return self


#: Read once per process. An edit therefore takes effect at the next start, which is the same
#: contract the simulated venue's committed rule set has.
_CACHE: dict[Path, StubResponseCatalogue] = {}


def load_catalogue(path: Path = CATALOGUE_PATH) -> StubResponseCatalogue:
    """Read the committed catalogue. Raises `ConfigError` when it is not one."""
    cached = _CACHE.get(path)
    if cached is not None:
        return cached
    try:
        catalogue = StubResponseCatalogue.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"the stub's response catalogue is missing from {path}: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path} is not a usable stub response catalogue: {exc}") from exc
    _CACHE[path] = catalogue
    return catalogue


def _default_response(request: CompletionRequest) -> str:
    """The answer an unscripted `stub-*` seat gives, in whichever schema the prompt asked for.

    Reading the symbols back out of the prompt is what a model does with the same text, and it
    is the only source of them: `parse_assessments` refuses a symbol that was not in the
    snapshot, so a placeholder key would be a hallucination and abstain the seat.
    """
    symbols = symbols_requested(request.user)
    if not symbols:
        return DEFAULT_RESPONSE
    vote = json.loads(DEFAULT_RESPONSE)
    return json.dumps(
        {"assessments": dict.fromkeys(symbols, vote), "basket_view": DEFAULT_BASKET_VIEW},
        indent=2,
    )


def _varied_response(request: CompletionRequest, rng: random.Random) -> str:
    """The answer an unscripted `varied-*` seat gives: a fresh draw per instrument.

    Per instrument, not per call. One draw shared across a basket's symbols would make every
    basket-mode answer internally unanimous, which is the same flatness this exists to remove —
    one level down.
    """
    catalogue = load_catalogue()
    symbols = symbols_requested(request.user)
    if not symbols:
        body = json.dumps(_draw(rng, catalogue), indent=2)
    else:
        body = json.dumps(
            {
                "assessments": {symbol: _draw(rng, catalogue) for symbol in symbols},
                "basket_view": rng.choice(catalogue.basket_views),
            },
            indent=2,
        )
    return rng.choice(RENDERINGS).format(body=body)


def _draw(rng: random.Random, catalogue: StubResponseCatalogue) -> dict[str, Any]:
    return rng.choice(catalogue.responses).model_dump(mode="json")


class StubLLMProvider:
    """Replays scripted responses in order, repeating the sequence once exhausted."""

    provider_id = "stub"

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        provider_id: str = "stub",
        cost_usd: Decimal = Decimal("0"),
        latency_ms: int = 0,
        rng: random.Random | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._responses = cycle(list(responses)) if responses else None
        self._cost_usd = cost_usd
        self._latency_ms = latency_ms
        #: Only ever consulted for a `varied-*` model. Unseeded by default, so a simulated run is
        #: genuinely varied; tests pass a seeded one, because a flaky suite proves nothing.
        #: S311 is about cryptographic use: this picks which canned opinion an offline stub
        #: recites, reaches no venue, and guards nothing — a CSPRNG here would say otherwise.
        self._rng = random.Random() if rng is None else rng  # noqa: S311
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        text = self._text_for(request)
        if text == FAIL:
            raise ProviderError(f"scripted failure from {self.provider_id}")
        # Held to the same contract as a real provider, so the contract suite can run against
        # this one too and the stub cannot drift into being easier to satisfy than reality.
        if not text.strip():
            raise ProviderError(f"{self.provider_id} returned an empty completion")
        return CompletionResult(
            text=text,
            model_fingerprint=f"{self.provider_id}:{request.model}",
            prompt_tokens=len(request.user) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=self._latency_ms,
            cost_usd=self._cost_usd,
        )

    def _text_for(self, request: CompletionRequest) -> str:
        """A script outranks the model name — scripting is the fault-injection path."""
        if self._responses is not None:
            return next(self._responses)
        if request.model.startswith(VARIED_PREFIX):
            return _varied_response(request, self._rng)
        return _default_response(request)
