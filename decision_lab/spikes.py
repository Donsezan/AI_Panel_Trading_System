"""Spike events: the dates the market moved hard, and what the panel could have known (spec §7).

`regimes.py` labels each bar and is not touched here. An event is a run of same-direction shock
bars, merged across short gaps so one violent week reads as one entry rather than a dozen.

**What the panel could see and what was published later are two lists and are never mixed.**
`could_see` is read off the frozen snapshots' own `news` — the items actually served, never a
re-selection against the archive, so it cannot drift from what the seats read. `published_later`
comes from the archive and exists to explain the move; merging it in would be look-ahead wearing
the costume of evidence.

**No coverage is not a quiet market** (§6.8.3). `covered` is passed in by the caller, who knows the
archive's span; with no news run recorded it is `False`, and the page says "not covered" rather
than "nothing happened".

Failure semantics: pure functions over values already in memory. Nothing here reads a file, the
clock or the venue, so there is no dependency to be down; an empty input yields an empty result,
with `clipped` true when there is no history to say the lead-in was complete.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from decision_lab.archive.published import ArchiveItem
from decision_lab.records import CycleRecord
from decision_lab.regimes import BarLabel, EventWindow, RegimeLabel
from decision_lab.scoring import ScoredDecision
from tradebot.core.money import multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import NewsItemView

#: §7.1 [builder's call]: non-spike bars tolerated inside one event.
MERGE_GAP_BARS: Final = 6

#: §7.3: the lead-in, the news window, and how far past an event "published later" reaches.
LEAD_IN: Final = timedelta(hours=48)

_HUNDRED: Final = Decimal(100)


class SpikeEvent(DomainModel):
    """One dated event. `move_pct` is a percentage, signed, and `peak_volatility` a magnitude."""

    start: UtcDatetime
    end: UtcDatetime
    direction: RegimeLabel
    move_pct: Money
    peak_volatility: Money
    window_name: str = ""


def find_events(
    bars: Sequence[BarLabel], *, windows: Sequence[EventWindow] = ()
) -> tuple[SpikeEvent, ...]:
    """Fold labelled bars into events, in time order.

    The label is read as given: a `NORMAL` bar is a gap, an opposite-direction shock closes the
    run (up and down are never pooled, §8), and a same-direction shock within
    `MERGE_GAP_BARS` of the last one extends it.
    """
    runs: list[list[BarLabel]] = []
    open_run: list[BarLabel] | None = None
    gap = 0
    for bar in bars:
        if bar.label is RegimeLabel.NORMAL:
            gap += 1
            if open_run is not None and gap > MERGE_GAP_BARS:
                open_run = None
            continue
        if open_run is not None and open_run[-1].label is bar.label and gap <= MERGE_GAP_BARS:
            open_run.append(bar)
        else:
            open_run = [bar]
            runs.append(open_run)
        gap = 0
    return tuple(_event_of(run, windows) for run in runs)


def _event_of(run: list[BarLabel], windows: Sequence[EventWindow]) -> SpikeEvent:
    peak = max(run, key=lambda bar: bar.volatility)
    start, end = run[0].close_time, run[-1].close_time
    # `BarLabel` carries no prices, only the trailing return, so the move is that return at the
    # strongest bar — the best figure the label alone supports, signed like the direction.
    name = next((w.name for w in windows if w.from_ <= end and start < w.to), "")
    return SpikeEvent(
        start=start,
        end=end,
        direction=run[0].label,
        move_pct=multiply(peak.window_return_, _HUNDRED),
        peak_volatility=peak.volatility,
        window_name=name,
    )


@dataclass(frozen=True, slots=True)
class EventStretches:
    """Decisions around one event. `clipped` says the lead-in ran into the edge of the data."""

    lead_in: tuple[ScoredDecision, ...]
    during: tuple[ScoredDecision, ...]
    after: tuple[ScoredDecision, ...]
    clipped: bool


def stretches(event: SpikeEvent, decisions: Iterable[ScoredDecision]) -> EventStretches:
    """Sort decisions into lead-in, during and after, by time and never by their regime label.

    A decision taken just before a spike is labelled `NORMAL` yet is graded on that spike, and the
    lag in a trailing-window label means it is exactly the one that answers "did it predict" (§7.3).
    Decisions earlier than the lead-in are out of this event's view.
    """
    ordered = sorted(decisions, key=lambda d: d.as_of)
    lead_start = event.start - LEAD_IN
    after_end = event.end + LEAD_IN
    clipped = not ordered or ordered[0].as_of > lead_start
    return EventStretches(
        lead_in=tuple(d for d in ordered if lead_start <= d.as_of < event.start),
        during=tuple(d for d in ordered if event.start <= d.as_of <= event.end),
        after=tuple(d for d in ordered if event.end < d.as_of <= after_end),
        clipped=clipped,
    )


@dataclass(frozen=True, slots=True)
class EventNews:
    could_see: tuple[NewsItemView, ...]
    published_later: tuple[ArchiveItem, ...]
    covered: bool


def _served_key(source: str, title: str, observed_at: datetime) -> tuple[str, str, datetime]:
    """A served view carries no url hash, so identity is what `ArchiveNewsFeed` copied across."""
    return (source, title, observed_at)


def news_for(
    event: SpikeEvent,
    records: Iterable[CycleRecord],
    archive_items: Iterable[ArchiveItem],
    *,
    coverage: bool,
) -> EventNews:
    """The two news lists for one event, kept apart (§7.3).

    `could_see` is the union of what the snapshots from the lead-in through the event served, each
    item once. `published_later` is archive items observed after the last of those decisions and
    no later than `LEAD_IN` past the event, deduplicated by `url_hash`, and never anything served.
    """
    window = [r for r in records if event.start - LEAD_IN <= r.as_of <= event.end]
    last_decision = max((r.as_of for r in window), default=event.end)

    seen: dict[tuple[str, str, datetime], NewsItemView] = {}
    for record in sorted(window, key=lambda r: r.as_of):
        for view in record.snapshot.news:
            seen.setdefault(_served_key(view.source, view.title, view.observed_at), view)
    could_see = tuple(sorted(seen.values(), key=lambda v: v.observed_at))

    later: dict[str, ArchiveItem] = {}
    for archived in sorted(archive_items, key=lambda i: i.observed_at):
        if not last_decision < archived.observed_at <= event.end + LEAD_IN:
            continue
        if _served_key(archived.source_id, archived.title, archived.observed_at) in seen:
            continue
        later.setdefault(archived.url_hash, archived)
    return EventNews(could_see=could_see, published_later=tuple(later.values()), covered=coverage)
