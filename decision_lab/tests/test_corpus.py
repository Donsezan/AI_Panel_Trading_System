"""The corpus is the frozen evidence every candidate is judged on (spec §5).

Read out of the event log rather than written to a new format: every cycle already appends
`SNAPSHOT_FROZEN` carrying the whole snapshot body, so there is no second persistence format and
no second rendering path.

The reference pass exists for one reason — positions. A corpus built against an empty ledger makes
SELL and HOLD unreachable, so the panel only ever chooses between BUY and WAIT and half the action
space goes unmeasured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


@pytest.fixture
async def verified(tmp_path: Path) -> Path:
    """Ten days of hourly bars, audited clean, ready to build a corpus from."""
    clock = ManualClock(NOW)
    data = tmp_path / "history"
    f.write_dataset(
        data, {(f.instrument(), "1h"): f.shocked_walk(days=10, shock_up=(3,), shock_down=(6,))}
    )
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    return data


async def build(
    data: Path,
    workspace: Path,
    *,
    reference_panel: str = "stub",
    cadence_seconds: int = 4 * 3600,
    start_equity: Decimal = Decimal(10_000),
) -> cp.Corpus:
    return await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel=reference_panel,
        cadence_seconds=cadence_seconds,
        start_equity=start_equity,
        wall_clock=ManualClock(NOW),
    )


async def test_a_corpus_holds_one_entry_per_cycle(verified: Path, tmp_path: Path) -> None:
    built = await build(verified, tmp_path / "ws")

    assert built.entries, "the reference pass produced no snapshots"
    assert all(entry.snapshot.instruments for entry in built.entries)
    assert [e.seq for e in built.entries] == sorted(e.seq for e in built.entries)
    assert built.meta.ran_cycles == len(built.entries)


async def test_entries_carry_the_indicator_readings_scoring_will_need(
    verified: Path, tmp_path: Path
) -> None:
    """§9.2 reads ATR off the frozen snapshot rather than recomputing it, so it has to be there."""
    built = await build(verified, tmp_path / "ws")

    context = built.entries[0].snapshot.instruments[0]
    assert context.indicator("ATR", "1h") is not None


async def test_the_identity_moves_with_the_cadence(verified: Path, tmp_path: Path) -> None:
    """§5.5: cadence is a corpus property, so a cadence comparison is N runs, not one."""
    four = await build(verified, tmp_path / "a", cadence_seconds=4 * 3600)
    eight = await build(verified, tmp_path / "b", cadence_seconds=8 * 3600)
    assert four.meta.corpus_id != eight.meta.corpus_id


async def test_the_identity_moves_with_the_reference_panel(verified: Path, tmp_path: Path) -> None:
    stub = await build(verified, tmp_path / "a")
    sim = await build(verified, tmp_path / "b", reference_panel="sim")
    assert stub.meta.corpus_id != sim.meta.corpus_id


async def test_a_corpus_round_trips(verified: Path, tmp_path: Path) -> None:
    """§16 round-trip row: written and re-read yields identical snapshot digests."""
    built = await build(verified, tmp_path / "ws")

    reloaded = cp.load(built.meta.corpus_id, workspace=tmp_path / "ws")

    assert reloaded.meta == built.meta
    assert [e.snapshot.digest for e in reloaded.entries] == [
        e.snapshot.digest for e in built.entries
    ]


async def test_entries_are_indexable_by_day(verified: Path, tmp_path: Path) -> None:
    """§10 scores a pinned day, so the corpus has to be able to hand over exactly that day."""
    built = await build(verified, tmp_path / "ws")
    day = built.entries[0].day

    for_day = built.for_day(day)

    assert for_day, "the day the first entry falls on holds at least that entry"
    assert all(entry.day == day for entry in for_day)
    assert built.for_days([day]) == for_day


async def test_the_meta_carries_the_reference_basket(verified: Path, tmp_path: Path) -> None:
    """Slice B replays `reach_consensus` over recorded votes and needs the panel that produced
    them (§9.7 swing rate)."""
    built = await build(verified, tmp_path / "ws")
    assert built.meta.reference_basket.panel.seats


async def test_a_corpus_is_news_blind_until_slice_e(verified: Path, tmp_path: Path) -> None:
    """§6.9: the snapshot records no sources rather than letting the panel read silence as calm."""
    built = await build(verified, tmp_path / "ws")
    assert built.meta.news_blind
    assert built.entries[0].snapshot.news == ()


async def test_an_unverified_dataset_refuses(tmp_path: Path) -> None:
    """Fail closed (§4.4, §15): a corpus is the basis of every number downstream."""
    data = tmp_path / "history"
    f.write_dataset(data, {(f.instrument(), "1h"): f.walk(["100"] * 240)})

    with pytest.raises(ConfigError, match="dataset verify"):
        await build(data, tmp_path / "ws")


def bot_data_files() -> list[str]:
    """What the bot's `data/` holds. Sync, so ruff's ASYNC rules stay happy about the listing."""
    root = Path("data")
    return sorted(entry.name for entry in root.iterdir()) if root.is_dir() else []


async def test_the_corpus_never_writes_to_a_bot_database(verified: Path, tmp_path: Path) -> None:
    """§2.1: every write lands under the workspace, never in `data/`."""
    before = bot_data_files()

    built = await build(verified, tmp_path / "ws")

    assert (tmp_path / "ws" / built.meta.corpus_id / cp.CORPUS_DB).is_file()
    assert bot_data_files() == before


async def test_rebuilding_an_identical_corpus_reuses_it(verified: Path, tmp_path: Path) -> None:
    """§11's premise: identical parameters are one experiment, not two.

    Re-running must not append a second reference pass into the same log — that would double
    every entry and leave the corpus describing two passes as if they were one.
    """
    first = await build(verified, tmp_path / "ws")

    second = await build(verified, tmp_path / "ws")

    assert second.meta == first.meta
    assert len(second.entries) == len(first.entries)


async def test_a_narrowed_window_under_the_same_identity_refuses(
    verified: Path, tmp_path: Path
) -> None:
    """§5.4 leaves the window out of `corpus_id`, so two windows would collide on one id.

    Silently returning the existing corpus would answer a question about March with evidence from
    six months; overwriting it would do the reverse to whoever built it first.
    """
    await build(verified, tmp_path / "ws")

    with pytest.raises(ConfigError, match="window"):
        await cp.build(
            data_dir=verified,
            workspace=tmp_path / "ws",
            reference_panel="stub",
            cadence_seconds=4 * 3600,
            start_equity=Decimal(10_000),
            since=datetime(2024, 1, 3, tzinfo=UTC),
            wall_clock=ManualClock(NOW),
        )


def test_a_compacted_snapshot_refuses_by_name() -> None:
    """A compacted `SNAPSHOT_FROZEN` keeps `snapshot_id` and `digest` and drops the body
    (`maintenance/compaction._drop_snapshot`). Reading it as an empty context would produce a
    corpus of blanks that scores perfectly and means nothing."""
    payload = {"snapshot_id": "s", "digest": "d", "compacted": {"archive": "…"}}

    with pytest.raises(ConfigError, match="compacted"):
        cp.entry_from_payload(seq=1, cycle_id="c", basket_id="b", payload=payload)
