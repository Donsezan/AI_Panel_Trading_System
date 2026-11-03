# ruff: noqa: F811 - the imported fixtures are used by name as test parameters
"""What the period page claims and what it was built from (task 8, fix round 1).

Coverage, the news a panel could see, the month buckets and the page's headline figures, each
against a real run read back through `analyse_period`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import analysis, render
from decision_lab import scoring as sc
from decision_lab.archive.published import PublishedArchive
from decision_lab.calibration_days import Pool
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.records import CycleRecord
from decision_lab.regimes import EventWindow, RegimeLabel
from decision_lab.spikes import SpikeEvent
from decision_lab.tests.test_analysis_period import analysed, scoring_for, stretch_ids
from decision_lab.tests.test_newsfeed import FLOOR, archive, item
from decision_lab.tests.test_period_pause import (  # noqa: F401 - pytest fixtures
    Scenarios,
    candidate_over,
    dropping_data,
    flat_data,
    scenarios,
)
from tradebot.core.snapshot import NewsItemView
from tradebot.news.relevance import KeywordRelevanceFilter


def feed_of(*archives: PublishedArchive) -> ArchiveNewsFeed:
    return ArchiveNewsFeed(
        archives, KeywordRelevanceFilter(), window=timedelta(hours=48), floor=FLOOR
    )


def at(month: int, day: int, hour: int = 0) -> datetime:
    # Month 0 is December of the year before, so "wholly before" needs no second helper.
    return (
        datetime(2024, month, day, hour, tzinfo=UTC)
        if month
        else datetime(2023, 12, day, hour, tzinfo=UTC)
    )


def spike(start: datetime, end: datetime) -> SpikeEvent:
    return SpikeEvent(
        start=start,
        end=end,
        direction=RegimeLabel.SHOCK_UP,
        move_pct=Decimal(5),
        peak_volatility=Decimal(1),
    )


def story(day: int, title: str, month: int = 1):  # type: ignore[no-untyped-def]
    return item(
        at=at(month, day), title=title, summary=f"{title} summary", url=f"https://x/{title}"
    )


def second_source(*items) -> PublishedArchive:  # type: ignore[no-untyped-def]
    base = archive(*items)
    return PublishedArchive(
        header=base.header.model_copy(update={"source": "bitcoincom"}), items=base.items
    )


async def covered(
    scenarios: Scenarios,
    flat_data: Path,
    monkeypatch: pytest.MonkeyPatch,
    feed: ArchiveNewsFeed | None,
) -> bool:
    ran = await scenarios.paused_in_january()
    # The event needs [Jan 8, Jan 11]: its 48-hour lead-in and the event itself (spec 7.3).
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: (spike(at(1, 10), at(1, 11)),))
    folded = analysis.fold_period(
        ran.meta,
        (),
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=feed,
    )
    return folded.event_news[0].covered


async def test_coverage_requires_one_sources_span_to_contain_the_lead_in_and_the_event(
    scenarios: Scenarios, flat_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def case(*archives: PublishedArchive) -> bool:
        return await covered(scenarios, flat_data, monkeypatch, feed_of(*archives))

    assert not await covered(scenarios, flat_data, monkeypatch, None), "no archive"
    assert not await case(archive()), "an empty archive measured nothing"
    assert not await case(archive(story(1, "a", 0), story(5, "b"))), "wholly before"
    assert not await case(archive(story(11, "a"), story(28, "b"))), "begins as the event ends"
    assert not await case(archive(story(1, "a", 0), story(10, "b"))), "ends as the event starts"
    gap = (archive(story(1, "a"), story(5, "b")), second_source(story(20, "c"), story(25, "d")))
    assert not await case(*gap), "the event falls between two sources' spans"
    assert await case(archive(story(1, "a"), story(25, "b"))), "one source really covers it"


async def test_a_source_starting_inside_the_lead_in_does_not_cover_the_event(
    scenarios: Scenarios, flat_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 8's surviving mutant: `covers_span(e.start, e.end)` would call this covered.

    The event is [Jan 10, Jan 11] and its lead-in starts Jan 8. A source whose first row is Jan 9
    measured the event but only half the 48 hours that answer "did it predict" (spec 7.3).
    """
    late_start = archive(story(9, "a"), story(25, "b"))
    assert not await covered(scenarios, flat_data, monkeypatch, feed_of(late_start))
    assert feed_of(late_start).covers_span(at(1, 10), at(1, 11)), (
        "the source must cover the event itself, or this asserts nothing about the lead-in"
    )


def with_news(record: CycleRecord, title: str) -> CycleRecord:
    view = NewsItemView(
        source="x",
        title=title,
        summary="s",
        published_at=record.as_of,
        observed_at=record.as_of,
        relevance=Decimal(1),
    )
    return record.model_copy(
        update={"snapshot": record.snapshot.model_copy(update={"news": (view,)})}
    )


async def test_could_see_counts_cycles_whose_panel_ran_contaminated_included_quarantined_not(
    scenarios: Scenarios, flat_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran, result = await analysed(scenarios, flat_data)
    stretch = stretch_ids(result.records)
    decided = max(
        (r for r in result.records if r.decisions and r.as_of <= at(1, 22)), key=lambda r: r.as_of
    )
    quarantined = next(r for r in result.records if r.cycle_id in stretch and r.as_of >= at(1, 24))
    substituted = decided.model_copy(
        update={
            "responses": tuple(
                x.model_copy(update={"model": "substitute"}) for x in decided.responses
            )
        }
    )
    swapped = {
        decided.cycle_id: with_news(substituted, "seen by a contaminated panel"),
        quarantined.cycle_id: with_news(quarantined, "snapshot of a cycle that ran no panel"),
    }
    records = tuple(swapped.get(r.cycle_id, r) for r in result.records)
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: (spike(at(1, 20), at(1, 25)),))
    # Observed after the last panel that ran and before the quarantined cycle: it is later news
    # only if a cycle that ran no panel has not been allowed to move the last decision past it.
    later = story(24, "published later")

    folded = analysis.fold_period(
        ran.meta,
        records,
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=feed_of(archive(story(1, "early"), later, story(28, "late"))),
    )

    assert folded.contaminated == 1
    shown = folded.event_news[0]
    titles = {view.title for view in shown.could_see}
    assert "seen by a contaminated panel" in titles
    assert "snapshot of a cycle that ran no panel" not in titles
    assert [i.title for i in shown.published_later] == ["published later"]


async def test_by_month_holds_each_months_own_decisions(
    scenarios: Scenarios, flat_data: Path
) -> None:
    _ran, result = await analysed(scenarios, flat_data)

    assert list(result.by_month) == ["2024-01", "2024-02", "2024-03"]
    assert sum(len(v) for v in result.by_month.values()) == len(result.scored)
    for label, decisions in result.by_month.items():
        assert decisions, label
        assert {d.as_of.strftime("%Y-%m") for d in decisions} == {label}


async def test_a_named_window_adds_no_regime_row(scenarios: Scenarios, flat_data: Path) -> None:
    ran, result = await analysed(scenarios, flat_data)
    scoring = await scoring_for(flat_data)
    window = EventWindow.model_validate(
        {"name": "the january episode", "from": at(1, 1), "to": at(2, 1)}
    )
    named = replace(scoring, regimes=scoring.regimes.with_windows([window]))

    folded = analysis.fold_period(
        ran.meta,
        result.records,
        named,
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
    )

    assert any(d.window_name for d in folded.scored), "the window must actually label decisions"
    assert [row.regime for row in folded.regimes] == [p.value for p in Pool]


async def test_the_pages_headline_figures_are_the_runs_own_counts(
    scenarios: Scenarios, flat_data: Path, tmp_path: Path
) -> None:
    _ran, result = await analysed(scenarios, flat_data)
    graded = sum(1 for d in result.scored if d.verdict.is_scored)
    correct = sum(1 for d in result.scored if d.verdict is sc.Verdict.CORRECT)
    assert graded > 0 and correct > 0, "the fixture must grade something for this to bite"

    page = render.write_period(
        result,
        tmp_path / "p.md",
        generated_at=datetime(2026, 11, 1, tzinfo=UTC),
        panel_models=(),
    ).read_text(encoding="utf-8")

    total = len(result.scored)
    assert f"{graded} of {total} ({correct} correct); {total - graded} unscored" in page
    assert "## Seats" in page and "| NORMAL " in page


async def test_a_page_with_no_decisions_still_has_three_regime_rows_and_no_seat_table(
    scenarios: Scenarios, flat_data: Path, tmp_path: Path
) -> None:
    ran = await scenarios.paused_in_january()
    empty = analysis.fold_period(
        ran.meta,
        (),
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
    )

    page = render.write_period(
        empty,
        tmp_path / "p.md",
        generated_at=datetime(2026, 11, 1, tzinfo=UTC),
        panel_models=(),
    ).read_text(encoding="utf-8")

    for regime in ("NORMAL", "SHOCK_UP", "SHOCK_DOWN"):
        assert f"| {regime} " in page
    assert "no scored decision" in page
    assert "single_round" not in page, "an empty block must not claim a protocol (vacuous truth)"
