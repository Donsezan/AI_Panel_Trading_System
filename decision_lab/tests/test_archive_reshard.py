"""Migrating a flat store to month shards (spec §6.3.1).

The three stores were collected before sharding existed and hold 71 MB between them. Migration is
therefore an explicit command rather than something `open` does on the way past: 71 MB of
collected article text must never be reorganised as a side effect of being read.

Two properties carry the safety:

* **Every row survives, in the same order.** Asserted by comparing the whole round trip, not a
  count — a count passes while rows swap months.
* **The flat file is renamed, never deleted.** Collection cost hours and cannot be repeated for
  CoinDesk without the session; the migration leaves the original beside the shards under
  `.premigration` so a mistake is recoverable by renaming it back.

Failure semantics: a store already sharded is a no-op that says so, not an error — re-running a
migration must be safe. A flat file that will not parse raises `ConfigError` naming the line, as
every other read of this store does, and writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from decision_lab.archive.reshard import reshard
from decision_lab.archive.store import SUFFIX, RawArticle, RawStore

FETCHED = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def row(published: datetime) -> RawArticle:
    return RawArticle(
        source_id="coindesk",
        url=f"https://www.coindesk.com/markets/{published:%Y/%m/%d}/story-{published:%d%H%M}",
        title="A headline",
        body="The article text.",
        abstract="The publisher's own sentence.",
        published_at=published,
        fetched_at=FETCHED,
    )


def flat(tmp_path: Path, rows: tuple[RawArticle, ...]) -> Path:
    path = tmp_path / f"coindesk{SUFFIX}"
    path.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")
    return path


def test_every_row_survives_the_migration(tmp_path: Path) -> None:
    rows = tuple(row(datetime(2024, m, 5, 9, 0, tzinfo=UTC)) for m in range(1, 13))
    flat(tmp_path, rows)

    reshard(tmp_path, source_id="coindesk")

    assert RawStore.open(tmp_path, source_id="coindesk").read_all() == rows


def test_rows_land_in_the_shard_for_their_month(tmp_path: Path) -> None:
    flat(tmp_path, (row(datetime(2024, 3, 11, tzinfo=UTC)), row(datetime(2024, 7, 16, tzinfo=UTC))))

    reshard(tmp_path, source_id="coindesk")

    assert sorted(p.name for p in (tmp_path / "coindesk").glob("*.jsonl")) == [
        "2024-03.jsonl",
        "2024-07.jsonl",
    ]


def test_the_flat_file_is_kept_under_premigration(tmp_path: Path) -> None:
    """Hours of collection, and for CoinDesk not repeatable without the session. Never deleted."""
    original = flat(tmp_path, (row(datetime(2024, 3, 11, tzinfo=UTC)),))
    before = original.read_text(encoding="utf-8")

    reshard(tmp_path, source_id="coindesk")

    assert not original.exists(), "the flat file must move, or `open` keeps refusing"
    kept = tmp_path / f"coindesk{SUFFIX}.premigration"
    assert kept.read_text(encoding="utf-8") == before


def test_an_already_sharded_store_is_left_alone(tmp_path: Path) -> None:
    """Re-running a migration must be safe, so it reports nothing to do rather than raising."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(row(datetime(2024, 3, 11, tzinfo=UTC)))

    report = reshard(tmp_path, source_id="coindesk")

    assert report.migrated == 0
    assert len(RawStore.open(tmp_path, source_id="coindesk").read_all()) == 1


def test_the_report_counts_rows_and_shards(tmp_path: Path) -> None:
    flat(
        tmp_path,
        (
            row(datetime(2024, 3, 11, tzinfo=UTC)),
            row(datetime(2024, 3, 12, tzinfo=UTC)),
            row(datetime(2024, 7, 16, tzinfo=UTC)),
        ),
    )

    report = reshard(tmp_path, source_id="coindesk")

    assert (report.migrated, report.shards) == (3, 2)
