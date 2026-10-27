# decision_lab/tests/test_archive_publish.py
"""Raw rows → the §6.6 archive: `observed_at` derived, the body dropped, the digest stamped.

The §6.4 boundary is crossed here and only here, and only in the direction that drops data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.publish import archive_path, publish_archive
from decision_lab.archive.published import PublishedArchive
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.archive.summarize import prompt_digest, system_for
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.news.normalize import url_hash

POLICY = ArchivePolicy(provider_id="stub", model="stub-summary")
DIGEST = prompt_digest(system_for(POLICY))


def summarised(n: int, **overrides: object) -> RawArticle:
    base: dict[str, object] = {
        "source_id": "cryptoslate",
        "url": f"https://x.test/{n}?utm_source=twitter",
        "title": f"Story {n}",
        "body": "The FCA granted Kraken an EMI licence today.",
        "abstract": "An abstract.",
        "published_at": datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        "fetched_at": datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
        "summary": f"Summary {n}.",
        "event_type": EventType.REGULATORY,
        "entities": ("Kraken",),
        "assets": ("NONE",),
        "modality": Modality.CONFIRMED,
        "summary_provider": POLICY.provider_id,
        "summary_model": POLICY.model,
        "summary_prompt_digest": DIGEST,
    }
    return RawArticle.model_validate(base | overrides)


def store_with(tmp_path: Path, rows: tuple[RawArticle, ...]) -> RawStore:
    store = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate")
    for article in rows:
        store.append(article)
    return store


def clock() -> ManualClock:
    return ManualClock(datetime(2026, 10, 5, tzinfo=UTC))


def test_publishing_derives_observed_at_from_the_policys_lag(tmp_path: Path) -> None:
    """§6.7: never `published_at` itself, which grants the panel the headline at the instant of
    publication — optimistic in the one direction that inflates a score."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    item = PublishedArchive.read(archive_path(out, "cryptoslate")).items[0]
    assert item.observed_at == item.published_at + timedelta(minutes=12)
    assert item.observed_at > item.published_at


def test_the_published_row_carries_no_body_and_the_summary_is_the_excerpt(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    raw_text = archive_path(out, "cryptoslate").read_text(encoding="utf-8")
    assert "The FCA granted Kraken an EMI licence today." not in raw_text
    assert "Summary 2." in raw_text


def test_the_url_is_canonicalised_and_hashed_with_the_bots_own_functions(tmp_path: Path) -> None:
    """§2.4: a second canonicalisation would key the same story two ways, so a cross-source
    duplicate would stop being recognisable as one."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    item = PublishedArchive.read(archive_path(out, "cryptoslate")).items[0]
    assert "utm_source" not in item.url
    assert item.url_hash == url_hash(item.url)


def test_an_unsummarised_row_is_skipped_and_counted(tmp_path: Path) -> None:
    """Publishing it blank would put a title-only item into one of eight slots and then fail to
    collapse it, having no cluster key. The count is how an operator learns it is unfinished."""
    store = store_with(
        tmp_path, (summarised(2), summarised(3, summary="", event_type=None, modality=None))
    )
    out = tmp_path / "out"

    report = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    assert (report.published, report.unsummarised) == (1, 1)


def test_publishing_a_store_with_nothing_summarised_is_refused(tmp_path: Path) -> None:
    """An archive of zero rows is a NEWS-BLIND corpus wearing a digest, which is worse than no
    archive: §6.9 is the honest way to run without news, and it says so on the report."""
    store = store_with(tmp_path, (summarised(3, summary="", event_type=None, modality=None),))

    with pytest.raises(ConfigError, match="summarise"):
        publish_archive(store, POLICY, clock(), out_dir=tmp_path / "out", capture="sitemap")


def test_the_header_records_the_binding_the_prompt_and_the_reading_policy(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    report = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    header = PublishedArchive.read(archive_path(out, "cryptoslate")).header

    assert header.summarizer_model == "stub-summary"
    assert header.observed_at_policy == "published_at+00:12:00"
    assert header.news_window_seconds == 172_800
    assert header.prompt_digest and header.archive_digest == report.archive_digest


def test_republishing_the_same_rows_yields_the_same_digest(tmp_path: Path) -> None:
    """It feeds `corpus_id`. A digest that moved on a re-publish would renumber a corpus whose
    evidence is byte-identical."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    first = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    second = publish_archive(
        store,
        POLICY,
        ManualClock(datetime(2026, 12, 1, tzinfo=UTC)),
        out_dir=out,
        capture="sitemap",
    )

    assert first.archive_digest == second.archive_digest


def test_a_different_lag_is_a_different_archive(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    base = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    other = publish_archive(
        store,
        POLICY.model_copy(update={"observed_at_lag_seconds": 1800}),
        clock(),
        out_dir=out,
        capture="sitemap",
    )

    assert base.archive_digest != other.archive_digest


def test_rows_are_published_in_observed_at_order(tmp_path: Path) -> None:
    """The feed binary-searches this file's order. Shard order is month order, and a month's rows
    are in crawl order — which is not time order, because a resumed crawl appends."""
    store = store_with(tmp_path, (summarised(5), summarised(2), summarised(9)))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    items = PublishedArchive.read(archive_path(out, "cryptoslate")).items
    assert [i.observed_at for i in items] == sorted(i.observed_at for i in items)


def test_a_store_summarised_by_two_models_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """B10: the header names one summariser. Two models' output under one digest is a header that
    describes rows it does not hold."""
    store = store_with(tmp_path, (summarised(2), summarised(3, summary_model="other-model")))
    out = tmp_path / "out"

    with pytest.raises(
        ConfigError, match=r"1 rows? summarised by stub/stub-summary.*1 by stub/other-model"
    ):
        publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    assert not archive_path(out, "cryptoslate").exists()


def test_a_policy_that_disagrees_with_the_rows_is_refused(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    edited = POLICY.model_copy(update={"model": "stub-summary-v2"})
    out = tmp_path / "out"

    with pytest.raises(ConfigError, match=r"(?i)stub-summary-v2.*re-summarise"):
        publish_archive(store, edited, clock(), out_dir=out, capture="sitemap")

    assert not archive_path(out, "cryptoslate").exists()


def test_a_changed_prompt_is_a_disagreement_too(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2, summary_prompt_digest="0" * 32),))

    with pytest.raises(ConfigError, match="prompt"):
        publish_archive(store, POLICY, clock(), out_dir=tmp_path / "out", capture="sitemap")


def test_the_header_is_stamped_from_the_agreed_triple(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    header = PublishedArchive.read(archive_path(out, "cryptoslate")).header

    assert (header.summarizer_provider, header.summarizer_model, header.prompt_digest) == (
        "stub",
        "stub-summary",
        DIGEST,
    )
