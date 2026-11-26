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
import json
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Final

from pydantic import field_validator, model_validator

from decision_lab.archive.collect import DEFAULT_PAUSE
from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.store import RawArticle, RawStore, StoreLock
from decision_lab.providers import claude_subscription
from tradebot.core.clock import Clock
from tradebot.core.errors import ProviderError, SchemaViolationError
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO, divide
from tradebot.core.schema import DomainModel, Money
from tradebot.decision.seat import REPAIR_INSTRUCTION, _extract_object
from tradebot.interfaces.llm import CompletionRequest, CompletionResult, LLMProvider

logger = get_logger(__name__)

#: §6.5: the text either names an asset or it does not. Inferring one is the relevance filter's
#: job and that filter is the bot's, so the vocabulary here is deliberately tiny — a model writing
#: "SOL" is answering a question the archive did not ask, and nothing downstream reads this field
#: for relevance.
ALLOWED_ASSETS: Final = ("BTC", "ETH", "OTHER", "NONE")

#: Delimit the worked example so a test can read it alone and prove it names neither traded asset
#: (subscription-summariser spec §6 item 7): an example about BTC or ETH would tilt the asset field.
EXAMPLE_START: Final = "<example>"
EXAMPLE_END: Final = "</example>"


def summary_user(*, title: str, body: str) -> str:
    """The user message. Two strings, and that is §6.5's second closure made structural.

    There is no instrument parameter, no market-data parameter and no sibling-article parameter,
    so the model cannot tilt toward BTC because it does not know we trade BTC. §16.2 asserts this
    on the recorded call arguments.
    """
    return f"TITLE:\n{title.strip()}\n\nARTICLE:\n{body.strip()}\n"


#: Invented, not taken from the archive, so it can be neither a memorised outcome nor a row the
#: traceability sample later scores. It shows the four habits the prompt asks for at once: the
#: evaluative remark dropped, a magnitude copied, "OTHER" for a named non-traded asset, and
#: entities in their shortest common name. Built by `summary_user`, so the example is laid out
#: exactly as every real request is. The dropped remark is the writer's own: attributed to
#: someone it would be their claim, which the attribution rule says to keep.
_EXAMPLE_ARTICLE: Final = summary_user(
    title="Lumen hit by $41M hot-wallet hack",
    body=(
        "Singapore-based exchange Lumen said on Tuesday it had suspended withdrawals after "
        "attackers drained $41 million in SOL and USDC from one of its hot wallets. The company "
        "said customer balances would be covered from its reserves. The breach is a devastating "
        "blow to the young exchange."
    ),
).rstrip("\n")
_EXAMPLE_REPLY: Final = json.dumps(
    {
        "summary": (
            "Lumen suspended withdrawals after attackers drained $41 million in SOL and USDC from "
            "one of its hot wallets; it said customer balances would be covered from its reserves."
        ),
        "event_type": EventType.SECURITY_INCIDENT.value,
        "entities": ["Lumen"],
        "assets": ["OTHER"],
        "magnitudes": ["$41 million"],
        "modality": Modality.CONFIRMED.value,
    }
)

#: `{max_chars}` is a literal placeholder, substituted by the pass with `str.replace`. It is not a
#: `.format()` field: the body is JSON, and `format` would raise on every brace in it.
#:
#: Everything the original §6.5 prompt said is still here. One phrase moved: "a price direction"
#: became "a price direction of your own", because §4.3 now keeps a move the article states with
#: its cause, and the unqualified rule would forbid that. What was added is subscription-summariser
#: spec §6: entities as a cluster key, attribution, own knowledge, the register to strip, §4.3's
#: price rule, §6.1's commentary mark, edge cases, one example. Rules carry their reason where one
#: fits on a line, because a model follows a reason better than a bare rule.
SUMMARY_SYSTEM: Final = f"""\
You compress one news article into a short factual record. You are not an analyst.

Restate only what the article's own text says. You must not offer an outlook, a market
implication, a price direction of your own, a sentiment, or any reference to events after this
article was published. If the article states something as expected, proposed or rumoured, record
it that way rather than as fact.

You may know what happened after this article was published. Do not use any of it. Add no fact
the text does not state, from your own knowledge or anywhere else, however sure you are of it.

Reply with JSON only, matching this schema exactly. No prose, no code fences.

{{
  "summary": "<at most {{max_chars}} characters, plain factual prose, no evaluation>",
  "event_type": "<one of: {" | ".join(m.value for m in EventType)}>",
  "entities": ["<at most three: the actor first, then whom it acted on>"],
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
- Use "NONE" in assets when the text names no crypto asset. Do not infer one from context. "OTHER"
  means the text names a crypto asset other than BTC or ETH.
- Entities: at most three, the actor first, then whom it acted on. Give each its shortest common
  name ("SEC", "BlackRock", "Binance"), with no titles, and name a person only when a person is
  the actor. Two articles about one event are matched on these names, so both must list the
  same names.
- Inside an article about an event, a prediction, forecast or opinion is a claim its author made.
  Record it as that claim ("the bank said it expects ..."), never as the event.
- Strip praising or alarming words: "empowers", "innovative", "plunges", "soars" judge the news
  rather than state it. Say what happened instead.
- A purely evaluative remark that nobody is named as holding (the writer's own "a devastating
  blow") states no event and no one's claim. Drop it.
- A price move whose cause the article states is kept: the move, its figures exactly as stated,
  and the cause. A cause the article only attributes to others ("analysts say ...") is recorded as
  their claim, with modality RUMOURED.
- Pure market commentary - technical analysis, price targets or predictions, or a recap of moves
  that states no cause - is event_type MARKET_COMMENTARY. Its summary is one neutral line saying
  what kind of piece it is ("Technical analysis of support levels"), with no levels, targets or
  predictions in it. Its entities and magnitudes are empty and its modality is CONFIRMED.
- An article whose substance is a prediction, a price target or an analysis is MARKET_COMMENTARY
  even when it is attributed to someone. Recording a claim, as above, is only for an opinion
  reported inside an article about an event.
- When the title and the body disagree, the body wins.
- A roundup of several stories is recorded as its headline event alone.
- Add no keys beyond the schema.

One worked example. The article is invented: follow its form, not its subject.
{EXAMPLE_START}
{_EXAMPLE_ARTICLE}

{_EXAMPLE_REPLY}
{EXAMPLE_END}
"""


def prompt_digest(system: str) -> str:
    """A third of `archive_digest` (§6.6), so an edited prompt is a different archive."""
    return hashlib.blake2s(system.encode("utf-8"), digest_size=16).hexdigest()


def digest_for(policy: ArchivePolicy) -> str:
    """The prompt digest a row is stamped with under `policy`.

    The subscription binding's CLI prepends its own identity preamble and is versioned by the
    pinned CLI build; both decide what the model reads, so both are in the archive identity.
    Every other provider is digested over the system text alone, unchanged.
    """
    system = system_for(policy)
    if policy.provider_id == claude_subscription.PROVIDER_ID:
        return prompt_digest(
            f"{claude_subscription.IDENTITY_PREAMBLE}\n"
            f"[claude-code {claude_subscription.PINNED_CLI_VERSION}]\n{system}"
        )
    return prompt_digest(system)


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

    @model_validator(mode="after")
    def _commentary_carries_no_figures_or_names(self) -> SummaryResponse:
        # §6.1: a commentary row is the record that a piece was seen, not an event. A level or a
        # target is the prediction the mark exists to keep out, and a name would give a row the
        # feed drops anyway a cluster key it must never contribute to.
        if self.event_type is EventType.MARKET_COMMENTARY:
            carried = [n for n in ("entities", "magnitudes") if getattr(self, n)]
            if carried:
                raise ValueError(
                    f"MARKET_COMMENTARY carries {' and '.join(carried)}, which must be empty. "
                    "Commentary is recorded as the kind of piece it was, with no levels, targets "
                    "or names in it"
                )
        return self


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


#: Attempts per row, including the repair. Two: one call, one repair, then the row is refused and
#: counted. A third would triple the spend on a model that is answering in prose today.
DEFAULT_SUMMARY_RETRIES: Final = 2


class SummaryReport(DomainModel):
    """What one pass did. `refused` against `summarised` is what the report prints.

    `already` is what makes a batched run legible: a third invocation reporting
    `already: 9000, requested: 1000` is resuming, not re-paying.
    """

    rows: int = 0
    already: int = 0
    requested: int = 0
    summarised: int = 0
    #: Of `summarised`, how many were marked `MARKET_COMMENTARY` (§6.1). A subset, not a third
    #: outcome beside `refused`: the row parsed and was paid for, so `per_row_cost` divides by it.
    commentary: int = 0
    refused: int = 0
    bodyless: int = 0
    cost_usd: Money = ZERO
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def added(self, **counts: int) -> SummaryReport:
        return self.model_copy(update={k: getattr(self, k) + v for k, v in counts.items()})

    @property
    def per_row_cost(self) -> Money:
        """Measured, never projected. `ZERO` when nothing was summarised - a per-row cost over
        zero rows is not a small number, it is no number."""
        if not self.summarised:
            return ZERO
        return divide(self.cost_usd, Decimal(self.summarised))


_PARTIAL_REPORT: Final = "summary_report"


def partial_report(error: BaseException) -> SummaryReport | None:
    """What the batch `error` ended had done, as `summarize_store` attached it; else `None`."""
    report = getattr(error, _PARTIAL_REPORT, None)
    return report if isinstance(report, SummaryReport) else None


def system_for(policy: ArchivePolicy) -> str:
    """`SUMMARY_SYSTEM` with the policy's character limit substituted.

    `str.replace`, never `.format()`: the prompt body is full of JSON braces and `format` would
    raise on every one of them. The *substituted* text is what `prompt_digest` is taken over, so
    changing `max_summary_chars` mints a new archive identity — correct, because it changes what
    the model was asked for.
    """
    return SUMMARY_SYSTEM.replace("{max_chars}", str(policy.max_summary_chars))


class Projection(DomainModel):
    """What a pass would ask for, measured without asking (§10.6's cost projection, one tool over).

    No dollar figure, deliberately. This tool has no price table - every cost in it comes from a
    provider's own response, which is why `total_cost` de-duplicates by `call_id` rather than
    multiplying a rate. The dollars come from a measured batch: run `--limit 50` and read
    `per_row_cost`. A projected rate would be a number with no provenance.
    """

    rows: int = 0
    pending: int = 0
    bodyless: int = 0
    body_chars: int = 0
    prompt_tokens: int = 0
    #: Summarised rows written under a different binding or prompt than the policy in force.
    #: Reported always; requested only under `resummarise_stale`.
    stale: int = 0


def is_stale(article: RawArticle, policy: ArchivePolicy) -> bool:
    """Summarised, but not by the (provider, model, prompt) the policy in force names.

    `archive publish` refuses such rows, so without a way to redo them a model swap or a prompt
    edit strands paid output."""
    return article.is_summarised and (
        article.summary_provider,
        article.summary_model,
        article.summary_prompt_digest,
    ) != (policy.provider_id, policy.model, digest_for(policy))


def _pending(
    rows: Sequence[RawArticle], policy: ArchivePolicy, resummarise_stale: bool
) -> list[RawArticle]:
    return [a for a in rows if not a.is_summarised or (resummarise_stale and is_stale(a, policy))]


def project(
    store: RawStore, policy: ArchivePolicy, *, resummarise_stale: bool = False
) -> Projection:
    """What `summarize_store` would request. Reads the store and nothing else."""
    rows = store.read_all()
    pending = _pending(rows, policy, resummarise_stale)
    sendable = [a for a in pending if a.body.strip()]
    system = system_for(policy)
    # Four characters to a token: exact enough to tell a 3 000-call pass from a 30 000-call one,
    # which is the decision this number informs.
    chars = sum(len(system) + len(summary_user(title=a.title, body=a.body)) for a in sendable)
    return Projection(
        rows=len(rows),
        pending=len(sendable),
        bodyless=len(pending) - len(sendable),
        body_chars=sum(len(a.body) for a in sendable),
        prompt_tokens=chars // 4,
        stale=sum(1 for a in rows if is_stale(a, policy)),
    )


async def summarize_store(
    provider: LLMProvider,
    store: RawStore,
    policy: ArchivePolicy,
    clock: Clock,
    *,
    limit: int | None = None,
    pause: timedelta = DEFAULT_PAUSE,
    resummarise_stale: bool = False,
) -> SummaryReport:
    """Summarise every unsummarised row with a body. Resumable, batched, and never silent.

    Keyed on `RawArticle.is_summarised`, so a second invocation asks the provider for nothing and
    a pass interrupted at row 9 000 does not re-pay for the first 9 000 — the resume property
    `archive build` has, for the same reason.

    `limit` commits a batch. `rewrite` replaces the store from rows read at the *start* of a pass,
    so a single long pass risks everything it collected to one failure near the end; a 14 000-row
    job is a sequence of `--limit` batches, each of which commits. `StoreLock` is what stops two
    of them running at once: the second would read the same pre-batch state and write it back over
    the first's gains, both reporting success.

    A row is refused — and counted — rather than ending the pass. `ProviderError` is a
    `RetryableError` and the class *is* the handling instruction, so a transient failure is
    absorbed within `DEFAULT_SUMMARY_RETRIES`; a `SchemaViolationError` gets one repair attempt
    through the bot's own `REPAIR_INSTRUCTION` and is then a tally. Nothing falls back to the
    publisher's abstract: that is the baseline this pass is measured against (§6.5), and on
    CryptoSlate it carries exactly the evaluative register the pass exists to strip.
    """
    system = system_for(policy)
    digest = digest_for(policy)
    with StoreLock(store):
        rows = store.read_all()
        report = SummaryReport(rows=len(rows))
        pending = _pending(rows, policy, resummarise_stale)
        report = report.added(already=len(rows) - len(pending))

        updated: dict[str, RawArticle] = {}
        # Flushed in a `finally`: a KeyboardInterrupt, a cancellation or a FatalError at row N
        # must not discard the N-1 rows already paid for. The same write path, then re-raise.
        try:
            asked = 0
            for article in pending:
                if limit is not None and asked >= limit:
                    break
                if not article.body.strip():
                    # Review Focus 5: a model given nothing invents something, and the measured
                    # 418-character minimum in this archive is a measurement, not a guarantee.
                    report = report.added(bodyless=1)
                    continue
                asked += 1
                report = report.added(requested=1)
                parsed, spent = await _summarise_one(
                    provider, policy, system, article, clock, pause
                )
                # Every billed call counts, whether it parsed, was repaired or was refused: the cost
                # projection extrapolates from this figure, and counting only the winner understates
                # spend by up to 2x on a model that answers in prose.
                report = report.model_copy(
                    update={"cost_usd": report.cost_usd + spent.cost_usd}
                ).added(
                    prompt_tokens=spent.prompt_tokens, completion_tokens=spent.completion_tokens
                )
                if parsed is None:
                    report = report.added(refused=1)
                    continue
                updated[article.url] = article.model_copy(
                    update={
                        "summary": parsed.summary,
                        "event_type": parsed.event_type,
                        "entities": parsed.entities,
                        "assets": parsed.assets,
                        "magnitudes": parsed.magnitudes,
                        "modality": parsed.modality,
                        "summary_provider": policy.provider_id,
                        "summary_model": policy.model,
                        "summary_prompt_digest": digest,
                    }
                )
                report = report.added(
                    summarised=1,
                    commentary=int(parsed.event_type is EventType.MARKET_COMMENTARY),
                )
        except BaseException as exc:
            # A quota or contract stop ends the batch by propagating, so without this the only
            # record of what it summarised and refused is a diff of the store. Attached rather
            # than logged here so the caller's one stop line carries it with its own provenance.
            setattr(exc, _PARTIAL_REPORT, report)
            raise
        finally:
            if updated:
                # Only the six §6.5 fields and their provenance move. Every other field was
                # accepted at collection, and rewriting one would silently change the archive's
                # record of what was published - `backfill`'s rule, shared with this pass.
                store.rewrite(tuple(updated.get(a.url, a) for a in rows))
    logger.info("archive summarised", extra=report.model_dump(mode="json"))
    return report


class _Spend(DomainModel):
    """What one row's calls cost, summed over every attempt that returned a result."""

    cost_usd: Money = ZERO
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def plus(self, result: CompletionResult) -> _Spend:
        return _Spend(
            cost_usd=self.cost_usd + (result.cost_usd or ZERO),
            prompt_tokens=self.prompt_tokens + result.prompt_tokens,
            completion_tokens=self.completion_tokens + result.completion_tokens,
        )


async def _summarise_one(
    provider: LLMProvider,
    policy: ArchivePolicy,
    system: str,
    article: RawArticle,
    clock: Clock,
    pause: timedelta,
) -> tuple[SummaryResponse | None, _Spend]:
    """One row, up to `DEFAULT_SUMMARY_RETRIES` attempts. `None` means refused and counted.

    The spend is returned on both paths: a refused row was still billed for what came back.
    """
    spent = _Spend()
    user = summary_user(title=article.title, body=article.body)
    message = user
    for attempt in range(DEFAULT_SUMMARY_RETRIES):
        if attempt:
            await clock.sleep(pause.total_seconds())
        try:
            result = await provider.complete(
                CompletionRequest(model=policy.model, system=system, user=message)
            )
        except ProviderError as exc:
            logger.warning("summariser call failed", extra={"url": article.url, "error": str(exc)})
            message = user
            continue
        spent = spent.plus(result)
        try:
            return parse_summary(result.text, max_chars=policy.max_summary_chars), spent
        except SchemaViolationError as exc:
            logger.warning(
                "summariser reply refused", extra={"url": article.url, "error": str(exc)}
            )
            message = f"{user}\n{REPAIR_INSTRUCTION.format(error=exc)}"
    return None, spent
