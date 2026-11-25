# decision_lab/tests/test_archive_summarize_pass.py
"""The pass over a store: resumable, idempotent, batched, and honest about what it refused.

Every test here drives `StubLLMProvider` with scripted replies, so the suite is offline and free.
`provider.calls` is what the §16.2 contamination tests later read; it is used here for the
cheaper question of *how many* calls a pass made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.archive.summarize import prompt_digest, summarize_store, system_for
from tradebot.core.clock import ManualClock
from tradebot.decision.providers.stub import FAIL, StubLLMProvider

GOOD = json.dumps(
    {
        "summary": "Kraken received EMI authorisation from the FCA.",
        "event_type": "REGULATORY",
        "entities": ["Kraken", "FCA"],
        "assets": ["NONE"],
        "magnitudes": [],
        "modality": "CONFIRMED",
    }
)

POLICY = ArchivePolicy(provider_id="stub", model="stub-summary")


def row(
    n: int, *, body: str = "The FCA granted Kraken an EMI licence today.", summary: str = ""
) -> RawArticle:
    return RawArticle(
        source_id="cryptoslate",
        url=f"https://x.test/{n}",
        title=f"Story {n}",
        body=body,
        abstract="Securing EMI authorization empowers Kraken to innovate.",
        published_at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        fetched_at=datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
        summary=summary,
    )


def store_with(tmp_path: Path, rows: tuple[RawArticle, ...]) -> RawStore:
    store = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate")
    for article in rows:
        store.append(article)
    return store


def clock() -> ManualClock:
    return ManualClock(datetime(2026, 10, 5, tzinfo=UTC))


@pytest.mark.asyncio
async def test_a_pass_summarises_every_bodyless_row_and_writes_the_fields(tmp_path: Path) -> None:
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.requested, report.summarised, report.refused) == (2, 2, 0)
    back = store.read_all()
    assert all(a.is_summarised for a in back)
    assert back[0].event_type is EventType.REGULATORY
    assert back[0].modality is Modality.CONFIRMED
    assert back[0].entities == ("Kraken", "FCA")


@pytest.mark.asyncio
async def test_a_second_pass_asks_for_nothing(tmp_path: Path) -> None:
    """Resumable and idempotent, the property `archive build` has and for the same reason: a pass
    interrupted at row 9 000 must not re-pay for the first 9 000."""
    store = store_with(tmp_path, (row(1), row(2)))
    first = StubLLMProvider([GOOD])
    await summarize_store(first, store, POLICY, clock(), pause=timedelta(0))

    second = StubLLMProvider([GOOD])
    report = await summarize_store(second, store, POLICY, clock(), pause=timedelta(0))

    assert second.calls == []
    assert (report.already, report.requested) == (2, 0)


@pytest.mark.asyncio
async def test_limit_commits_a_batch_and_leaves_the_rest(tmp_path: Path) -> None:
    """`rewrite` replaces the store from rows read at the start of the pass, so a long pass risks
    everything it collected — the same reason `backfill` is run in `--limit` batches."""
    store = store_with(tmp_path, (row(1), row(2), row(3)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), limit=2, pause=timedelta(0))

    assert report.summarised == 2
    assert sum(1 for a in store.read_all() if a.is_summarised) == 2


@pytest.mark.asyncio
async def test_a_blank_body_is_never_sent_and_never_summarised(tmp_path: Path) -> None:
    """Review Focus 5. A model given nothing invents something, and the 418-character minimum in
    this archive was *measured* rather than guaranteed by the type."""
    store = store_with(tmp_path, (row(1, body="   \n "), row(2)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.bodyless, report.summarised) == (1, 1)
    assert len(provider.calls) == 1
    blank = next(a for a in store.read_all() if a.url.endswith("/1"))
    assert blank.summary == "" and not blank.is_summarised


@pytest.mark.asyncio
async def test_a_malformed_reply_gets_one_repair_attempt_then_is_refused(tmp_path: Path) -> None:
    """Review Focus 1. Prose, a fence, a truncation or a refusal — every real run meets one in
    the first hundred rows. The bot's own `REPAIR_INSTRUCTION` is reused, never restated."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["I cannot summarise this.", "Still cannot."])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.refused, report.summarised) == (1, 0)
    assert len(provider.calls) == 2
    assert "rejected" in provider.calls[1].user


@pytest.mark.asyncio
async def test_a_repair_that_succeeds_is_counted_as_summarised(tmp_path: Path) -> None:
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["not json at all", GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.summarised, report.refused) == (1, 0)


@pytest.mark.asyncio
async def test_a_refused_row_never_falls_back_to_the_publishers_abstract(tmp_path: Path) -> None:
    """§6.5: the abstract is the *baseline the pass is measured against*. Using it on failure
    makes the measurement circular — and on CryptoSlate it imports the evaluative register the
    pass exists to strip ("empowers", "innovative")."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["no.", "no."])

    await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    stored = store.read_all()[0]
    assert stored.summary == ""
    assert "empowers" not in stored.summary


@pytest.mark.asyncio
async def test_a_provider_failure_is_retried_within_a_budget(tmp_path: Path) -> None:
    """`ProviderError` is a `RetryableError`, and the class *is* the handling instruction. The
    first full-year crawl died on a single timeout at its 1 430th row; this one does not."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider([FAIL, GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.summarised == 1


@pytest.mark.asyncio
async def test_a_provider_down_past_the_budget_refuses_the_row_without_ending_the_pass(
    tmp_path: Path,
) -> None:
    """One dead row must not end a 14 000-row pass. A refusal is a tally, not an exception —
    `collect`'s rule, and the store is rewritten with what did succeed."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([FAIL, FAIL, FAIL, GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.refused == 1
    assert report.summarised == 1


@pytest.mark.asyncio
async def test_the_report_totals_what_the_pass_spent(tmp_path: Path) -> None:
    """The cost projection on the CLI is this number over a sample, and a pass that cannot say
    what it spent cannot inform the decision to run the next one."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD], cost_usd=Decimal("0.0004"))

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.cost_usd == Decimal("0.0008")
    assert report.prompt_tokens > 0


@pytest.mark.asyncio
async def test_the_system_prompt_carries_the_policys_character_limit(tmp_path: Path) -> None:
    """`max_summary_chars` is policy and rides in the digest, so the model is told the number the
    parser will hold it to — otherwise every row is refused for an overrun nobody asked about."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider([GOOD])

    await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert "280" in provider.calls[0].system
    assert "{max_chars}" not in provider.calls[0].system


def test_system_for_substitutes_without_format() -> None:
    """The prompt body is full of JSON braces; `.format()` would raise on every one."""
    rendered = system_for(ArchivePolicy(provider_id="stub", model="m", max_summary_chars=200))
    assert "200" in rendered and "{max_chars}" not in rendered


def test_the_two_enum_fields_distinguish_never_summarised_from_classified_other() -> None:
    """`None` means the pass never ran on this row; `OTHER` means it ran and classified. One is
    the absence of a measurement and the other is a measurement, and `is_summarised` is what
    keeps them apart — a sentinel member would make a 14 484-row store indistinguishable from a
    summarised one."""
    assert not row(1).is_summarised
    summarised = row(1).model_copy(
        update={"summary": "x", "event_type": EventType.OTHER, "modality": Modality.CONFIRMED}
    )
    assert summarised.is_summarised


def test_every_already_collected_row_still_validates(tmp_path: Path) -> None:
    """The five fields are additive and defaulted. 14 484 rows are on disk, written before they
    existed, and a non-defaulted addition would refuse the whole store on the first read."""
    legacy = {
        "source_id": "cryptoslate",
        "url": "https://x.test/legacy",
        "title": "Story",
        "body": "Body text.",
        "published_at": "2025-03-04T09:02:00Z",
        "fetched_at": "2025-03-04T10:00:00Z",
    }
    article = RawArticle.model_validate(legacy)
    assert article.event_type is None and not article.is_summarised


@pytest.mark.asyncio
async def test_each_summarised_row_records_the_binding_that_produced_it(tmp_path: Path) -> None:
    """B10. A publish step can then refuse a store whose rows disagree with each other or with the
    policy, instead of mixing two prompts' output under one archive digest."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD])

    await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    for article in store.read_all():
        assert article.summary_provider == "stub"
        assert article.summary_model == "stub-summary"
        assert article.summary_prompt_digest == prompt_digest(system_for(POLICY))
    assert row(1).summary_provider is None


@pytest.mark.asyncio
async def test_a_repaired_row_reports_the_cost_of_both_calls(tmp_path: Path) -> None:
    """The bad reply was billed too; the cost projection extrapolates from this figure."""
    single = await summarize_store(
        StubLLMProvider([GOOD], cost_usd=Decimal("0.0004")),
        store_with(tmp_path / "a", (row(1),)),
        POLICY,
        clock(),
        pause=timedelta(0),
    )
    repaired = await summarize_store(
        StubLLMProvider(["not json at all", GOOD], cost_usd=Decimal("0.0004")),
        store_with(tmp_path / "b", (row(1),)),
        POLICY,
        clock(),
        pause=timedelta(0),
    )

    assert repaired.summarised == 1
    assert repaired.cost_usd == single.cost_usd * 2 == Decimal("0.0008")
    assert repaired.prompt_tokens > single.prompt_tokens
    assert repaired.completion_tokens >= single.completion_tokens


@pytest.mark.asyncio
async def test_a_refused_row_still_reports_both_calls_cost(tmp_path: Path) -> None:
    report = await summarize_store(
        StubLLMProvider(["no.", "no."], cost_usd=Decimal("0.0004")),
        store_with(tmp_path, (row(1),)),
        POLICY,
        clock(),
        pause=timedelta(0),
    )

    assert report.refused == 1
    assert report.cost_usd == Decimal("0.0008")
    assert report.prompt_tokens > 0


def _stale(article: RawArticle) -> RawArticle:
    return article.model_copy(
        update={
            "summary": "Old summary.",
            "event_type": EventType.REGULATORY,
            "modality": Modality.CONFIRMED,
            "summary_provider": "stub",
            "summary_model": "retired-model",
            "summary_prompt_digest": "0ld",
        }
    )


@pytest.mark.asyncio
async def test_stale_rows_are_skipped_without_the_flag_and_redone_with_it(tmp_path: Path) -> None:
    store = store_with(tmp_path, (_stale(row(1)), _stale(row(2))))

    quiet = StubLLMProvider([GOOD])
    report = await summarize_store(quiet, store, POLICY, clock(), pause=timedelta(0))
    assert quiet.calls == [] and report.already == 2

    loud = StubLLMProvider([GOOD])
    report = await summarize_store(
        loud, store, POLICY, clock(), pause=timedelta(0), resummarise_stale=True
    )
    assert (report.requested, report.summarised) == (2, 2)
    assert {a.summary_model for a in store.read_all()} == {POLICY.model}


class _Interrupts(StubLLMProvider):
    def __init__(self, *, at: int) -> None:
        super().__init__([GOOD])
        self._at = at
        self._n = 0

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self._n += 1
        if self._n == self._at:
            raise KeyboardInterrupt
        return await super().complete(request)


@pytest.mark.asyncio
async def test_an_interrupt_flushes_the_rows_already_summarised(tmp_path: Path) -> None:
    store = store_with(tmp_path, (row(1), row(2), row(3), row(4)))

    with pytest.raises(KeyboardInterrupt):
        await summarize_store(_Interrupts(at=3), store, POLICY, clock(), pause=timedelta(0))

    assert [a.is_summarised for a in store.read_all()] == [True, True, False, False]


COMMENTARY = json.dumps(
    {
        "summary": "Technical analysis of support and resistance levels.",
        "event_type": "MARKET_COMMENTARY",
        "entities": [],
        "assets": ["NONE"],
        "magnitudes": [],
        "modality": "CONFIRMED",
    }
)


@pytest.mark.asyncio
async def test_the_pass_counts_commentary_inside_summarised(tmp_path: Path) -> None:
    """§6.1: a commentary row is summarised (it is the record that we saw it) and also counted,
    so `per_row_cost` still divides by every row that was paid for and parsed."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD, COMMENTARY])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.summarised, report.commentary, report.refused) == (2, 1, 0)
    assert store.read_all()[1].event_type is EventType.MARKET_COMMENTARY
