"""§6.5's summarising pass: a compressor, not an analyst.

The job is **de-editorialising and genuine compression**, not merely fitting 280 characters, and
that framing is measured rather than assumed (§6.5, revised 2026-09-22). Bitcoin.com's abstract is
the body's first sentence verbatim — a lede is whatever the writer opened with, not a summary.
CryptoSlate's is written and it *evaluates*: "Securing EMI authorization **empowers** Kraken to
introduce **innovative** crypto-focused financial products." That is the fourteenth seat this
section exists to prevent, arriving from the publisher instead of from our model, and it arrives
on one source only — so the two registers are not comparable and a seat could learn the publisher
rather than the news.

What a summary carries is facts stated in the article: the event as a classification, the named
entities, the assets the text names, magnitudes exactly as stated, and the modality. What it never
carries is sentiment polarity, price direction, market implication, or anything about what
happened after publication.

**The hindsight channel is closed four ways, and only one is checkable.** By prompt (restate only
what the text says). By what the model is *not given* — `summary_user` takes a title and a body,
and there is no parameter through which an instrument, the day's market data or a sibling article
could arrive, so that closure is a property of this signature rather than of a sentence somebody
could edit. By binding, so the stub serves it in tests. And by traceability, asserted on a sample
by n-gram overlap against the source body — the only one that produces a number.

`_extract_object` is imported from the bot's `seat.py` deliberately, private name and all.
Tolerating a code fence or a sentence of prose is provider-habit handling, not domain logic, and
§2.4 says nothing about how the bot reads a model's reply is restated here. The bot's extractor is
greedy from the first brace to the last precisely so a nested object survives surrounding prose; a
tidier second one would quietly refuse answers the bot accepts.

Failure semantics: every malformed reply is `SchemaViolationError` — the bot's own class for
exactly this — and the pass tallies it as a refusal rather than propagating. A refusal is a
*number on the report*, never a quiet substitution of the publisher's abstract: that abstract is
the baseline the pass is measured against, and using it as a fallback would make the measurement
circular.
"""

from __future__ import annotations

import hashlib
from typing import Final

from pydantic import field_validator

from decision_lab.archive.fields import EventType, Modality
from tradebot.core.errors import SchemaViolationError
from tradebot.core.schema import DomainModel
from tradebot.decision.seat import _extract_object

#: §6.5: the text either names an asset or it does not. Inferring one is the relevance filter's
#: job and that filter is the bot's, so the vocabulary here is deliberately tiny — a model writing
#: "SOL" is answering a question the archive did not ask, and nothing downstream reads this field
#: for relevance.
ALLOWED_ASSETS: Final = ("BTC", "ETH", "OTHER", "NONE")

#: `{max_chars}` is a literal placeholder, substituted by the pass with `str.replace`. It is not a
#: `.format()` field: the body is JSON, and `format` would raise on every brace in it.
SUMMARY_SYSTEM: Final = f"""\
You compress one news article into a short factual record. You are not an analyst.

Restate only what the article's own text says. You must not offer an outlook, a market
implication, a price direction, a sentiment, or any reference to events after this article was
published. If the article states something as expected, proposed or rumoured, record it that way
rather than as fact.

Reply with JSON only, matching this schema exactly. No prose, no code fences.

{{
  "summary": "<at most {{max_chars}} characters, plain factual prose, no evaluation>",
  "event_type": "<one of: {" | ".join(m.value for m in EventType)}>",
  "entities": ["<who acted, and on whom, as the article names them>"],
  "assets": ["<only those the text names, from: {" | ".join(ALLOWED_ASSETS)}>"],
  "magnitudes": ["<figures exactly as stated, e.g. \\"$2.2B inflow\\"; omit if none>"],
  "modality": "<one of: {" | ".join(m.value for m in Modality)}>"
}}

Rules you must follow:
- A magnitude is copied, never characterised. "$2.2B inflow" is in the article; "large inflow" is
  a judgement about it.
- CONFIRMED means the article reports the event as having happened. PROPOSED means announced or
  intended. RUMOURED means unconfirmed or sourced to speculation. DENIED means a party denies it.
  CORRECTED means this article corrects or walks back an earlier claim.
- Use "NONE" in assets when the text names no crypto asset. Do not infer one from context.
- Add no keys beyond the schema.
"""


def prompt_digest(system: str) -> str:
    """A third of `archive_digest` (§6.6), so an edited prompt is a different archive."""
    return hashlib.blake2s(system.encode("utf-8"), digest_size=16).hexdigest()


def summary_user(*, title: str, body: str) -> str:
    """The user message. Two strings, and that is §6.5's second closure made structural.

    There is no instrument parameter, no market-data parameter and no sibling-article parameter,
    so the model cannot tilt toward BTC because it does not know we trade BTC. §16.2 asserts this
    on the recorded call arguments.
    """
    return f"TITLE:\n{title.strip()}\n\nARTICLE:\n{body.strip()}\n"


class SummaryResponse(DomainModel):
    """One summarised article, validated. `DomainModel` forbids extras, which is what keeps
    §6.5's "what it never carries" list enforced rather than merely requested: a model
    volunteering `sentiment` or `expected_impact` is refused, not ignored."""

    summary: str
    event_type: EventType
    entities: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    magnitudes: tuple[str, ...] = ()
    modality: Modality

    @field_validator("summary")
    @classmethod
    def _summary_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "summary is empty. An empty excerpt reads to the panel as a headline with no "
                "content rather than as a missing row"
            )
        return value.strip()

    @field_validator("assets")
    @classmethod
    def _assets_are_named_not_inferred(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(ALLOWED_ASSETS))
        if unknown:
            raise ValueError(
                f"assets names {unknown}, which is outside {list(ALLOWED_ASSETS)}. The text "
                "either names an asset or it does not; inferring one is the relevance filter's "
                "job and that filter is the bot's"
            )
        return value


def parse_summary(text: str, *, max_chars: int) -> SummaryResponse:
    """Extract and validate one summariser reply.

    `max_chars` is checked here rather than on the model because it is *policy* — `archive.toml`
    sets it and it rides in the digest — while the model's own constraints are facts about the
    schema. The over-length refusal names the length: the cut has to be visible and tallied, not
    `NewsItem.view` quietly carrying an overrun into the panel's slot.

    `_extract_object` raises `SchemaViolationError` itself for a reply with no JSON object or a
    truncated one, so there is nothing to translate on that side.
    """
    payload = _extract_object(text)
    try:
        parsed = SummaryResponse.model_validate(payload)
    except ValueError as exc:
        raise SchemaViolationError(f"reply does not match the summary schema: {exc}") from exc
    if len(parsed.summary) > max_chars:
        raise SchemaViolationError(
            f"summary is {len(parsed.summary)} characters, over the {max_chars} the panel's slot "
            "holds. Refused rather than truncated, so the cut is ours and counted"
        )
    return parsed
