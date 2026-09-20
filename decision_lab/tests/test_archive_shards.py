"""One file per publication month, so a read costs the months it asked for (spec §6.3.1).

A single file per source reached 22–28 MB, and `read_all` parses all of it into memory on every
call — every backfill batch, every `held` check for resume, and every point-in-time read a replay
will make. A cycle deciding on 2024-03-15 has no use for December.

The rule is deliberately unconditional: `published_at[:7]` is the filename, always. No threshold,
no "sparse months go elsewhere", no target year the store has to know about. A reader computes the
path rather than consulting a rule, and `ArchiveNewsFeed` needs to know nothing but the date it is
asking for. The cost is a tail of near-empty shards where a sitemap chunk straddled a year —
around 85 of them across the 2025 sources — and that is accepted as the price of one rule.

`published_at` is the right key because it is already the only thing that dates a row (§6.7).
`fetched_at` would scatter one month's articles across every shard the crawl happened to run in.

Failure semantics: a legacy flat `<source>.jsonl` beside the directory is a **refusal**, not a
silent second store. Writing shards while the old file holds the rows would leave `held` blind to
everything already collected, and the next crawl would re-ask the publisher for all of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab.archive.store import RawArticle, RawStore
from tradebot.core.errors import ConfigError

FETCHED = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def article(published: datetime, *, url: str = "", body: str = "text") -> RawArticle:
    return RawArticle(
        source_id="coindesk",
        url=url or f"https://www.coindesk.com/markets/{published:%Y/%m/%d}/story-{published:%d%H}",
        title="A headline",
        body=body,
        published_at=published,
        fetched_at=FETCHED,
    )


def test_an_article_lands_in_the_shard_for_its_publication_month(tmp_path: Path) -> None:
    store = RawStore.open(tmp_path, source_id="coindesk")

    store.append(article(datetime(2024, 3, 11, 9, 2, tzinfo=UTC)))

    assert (tmp_path / "coindesk" / "2024-03.jsonl").exists()


def test_rows_are_read_back_across_shards_in_month_order(tmp_path: Path) -> None:
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(article(datetime(2024, 12, 1, tzinfo=UTC)))
    store.append(article(datetime(2024, 1, 5, tzinfo=UTC)))
    store.append(article(datetime(2024, 6, 9, tzinfo=UTC)))

    months = [
        row.published_at.month for row in RawStore.open(tmp_path, source_id="coindesk").read_all()
    ]

    assert months == [1, 6, 12], "a concatenated read must be chronological, not directory order"


def test_resume_sees_rows_in_every_shard(tmp_path: Path) -> None:
    """`held` blind to an earlier month would re-ask the publisher for a year of articles."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    first = article(datetime(2024, 1, 5, tzinfo=UTC))
    second = article(datetime(2024, 9, 5, tzinfo=UTC))
    store.append(first)
    store.append(second)

    held = RawStore.open(tmp_path, source_id="coindesk").held()

    assert held == {store.identity(first.url), store.identity(second.url)}


def test_a_rewrite_leaves_untouched_shards_alone(tmp_path: Path) -> None:
    """The point of sharding: a batch that filled March must not rewrite all twelve months."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    march = article(datetime(2024, 3, 11, tzinfo=UTC), body="")
    july = article(datetime(2024, 7, 16, tzinfo=UTC))
    store.append(march)
    store.append(july)
    july_shard = tmp_path / "coindesk" / "2024-07.jsonl"
    before = july_shard.stat().st_mtime_ns

    store.rewrite((march.model_copy(update={"body": "filled in"}), july))

    assert july_shard.stat().st_mtime_ns == before, "an unchanged shard must not be rewritten"
    assert "filled in" in (tmp_path / "coindesk" / "2024-03.jsonl").read_text(encoding="utf-8")


def test_a_rewrite_that_empties_a_month_removes_its_shard(tmp_path: Path) -> None:
    """A shard left behind with stale rows would be read back by the next `read_all`."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    march = article(datetime(2024, 3, 11, tzinfo=UTC))
    july = article(datetime(2024, 7, 16, tzinfo=UTC))
    store.append(march)
    store.append(july)

    store.rewrite((july,))

    assert not (tmp_path / "coindesk" / "2024-03.jsonl").exists()
    assert len(RawStore.open(tmp_path, source_id="coindesk").read_all()) == 1


def test_only_the_months_asked_for_are_read(tmp_path: Path) -> None:
    """What a replay actually wants: a cycle in March must not parse December."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    for month in (1, 3, 4, 12):
        store.append(article(datetime(2024, month, 5, tzinfo=UTC)))

    rows = store.read_months("2024-03", "2024-04")

    assert [row.published_at.month for row in rows] == [3, 4]


def test_a_legacy_flat_file_is_refused_rather_than_shadowed(tmp_path: Path) -> None:
    """Sharding beside the old file leaves `held` blind and re-crawls everything it already has."""
    (tmp_path / "coindesk.jsonl").write_text(
        article(datetime(2024, 3, 11, tzinfo=UTC)).model_dump_json() + "\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="reshard"):
        RawStore.open(tmp_path, source_id="coindesk")
