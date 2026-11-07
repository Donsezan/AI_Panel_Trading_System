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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from decision_lab.archive.published import ArchiveItem
from decision_lab.records import CycleRecord
from decision_lab.regimes import BarLabel, EventWindow, RegimeLabel
from decision_lab.scoring import ScoredDecision
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle
from tradebot.core.money import MONEY_CONTEXT, ONE, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import NewsItemView

#: §7.1 [builder's call]: non-spike bars tolerated inside one event.
MERGE_GAP_BARS: Final = 6

#: §7.3: the lead-in, and the news window before an event.
LEAD_IN: Final = timedelta(hours=48)

#: How far past an event the "after" stretch and the later news reach. The spec names no length;
#: 48 hours matches the lead-in and is this tool's choice.
AFTER: Final = timedelta(hours=48)

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
    bars: Sequence[BarLabel],
    candles: Sequence[Candle],
    *,
    windows: Sequence[EventWindow] = (),
) -> tuple[SpikeEvent, ...]:
    """Fold labelled bars into events, in time order.

    The label is read as given: a `NORMAL` bar is a gap, an opposite-direction shock closes the
    run (up and down are never pooled, §8), and a same-direction shock within
    `MERGE_GAP_BARS` of the last one extends it. `candles` are the same series the bars were
    labelled from, read for the event's own price change (§7.1, §7.3).

    Raises `ConfigError` when a bar the move needs has no close in `candles`: a zero would read
    as a market that did not move.
    """
    closes = {candle.close_time: candle.close for candle in candles}
    runs: list[list[int]] = []
    open_run: list[int] | None = None
    gap = 0
    for index, bar in enumerate(bars):
        if bar.label is RegimeLabel.NORMAL:
            gap += 1
            if open_run is not None and gap > MERGE_GAP_BARS:
                open_run = None
            continue
        if open_run is not None and bars[open_run[-1]].label is bar.label and gap <= MERGE_GAP_BARS:
            open_run.append(index)
        else:
            open_run = [index]
            runs.append(open_run)
        gap = 0
    return tuple(_event_of(bars, run, closes, windows) for run in runs)


def _close_at(closes: Mapping[datetime, Decimal], moment: datetime) -> Decimal:
    try:
        return closes[moment]
    except KeyError:
        raise ConfigError(
            f"no close for the bar ending {moment.isoformat()}; a spike's move cannot be measured"
        ) from None


def _event_of(
    bars: Sequence[BarLabel],
    run: list[int],
    closes: Mapping[datetime, Decimal],
    windows: Sequence[EventWindow],
) -> SpikeEvent:
    members = [bars[i] for i in run]
    peak = max(members, key=lambda bar: bar.volatility)
    start, end = members[0].close_time, members[-1].close_time
    # The event's own price change, from the close before it began to its last bar's close. The
    # label's trailing return can start before the event or span part of it. A series that opens
    # inside the event has no earlier bar, so the first spike bar's close is the base.
    before = bars[run[0] - 1].close_time if run[0] > 0 else start
    base = _close_at(closes, before)
    move = multiply(MONEY_CONTEXT.subtract(divide(_close_at(closes, end), base), ONE), _HUNDRED)
    name = next((w.name for w in windows if w.from_ <= end and start < w.to), "")
    return SpikeEvent(
        start=start,
        end=end,
        direction=members[0].label,
        move_pct=move,
        peak_volatility=peak.volatility,
        window_name=name,
    )


@dataclass(frozen=True, slots=True)
class EventStretches:
    """Decisions around one event. `clipped` says the lead-in ran past the period's start."""

    lead_in: tuple[ScoredDecision, ...]
    during: tuple[ScoredDecision, ...]
    after: tuple[ScoredDecision, ...]
    clipped: bool


def stretches(
    event: SpikeEvent, decisions: Iterable[ScoredDecision], *, period_start: datetime
) -> EventStretches:
    """Sort decisions into lead-in, during and after, by time and never by their regime label.

    A decision taken just before a spike is labelled `NORMAL` yet is graded on that spike, and the
    lag in a trailing-window label means it is exactly the one that answers "did it predict" (§7.3).
    `clipped` is a fact about the period, not the decisions: the lead-in is cut short exactly when
    the 48 hours before the event reach back past the period's start. A paused or sparse lead-in
    is complete and merely empty.
    """
    ordered = sorted(decisions, key=lambda d: d.as_of)
    lead_start = event.start - LEAD_IN
    after_end = event.end + AFTER
    return EventStretches(
        lead_in=tuple(d for d in ordered if lead_start <= d.as_of < event.start),
        during=tuple(d for d in ordered if event.start <= d.as_of <= event.end),
        after=tuple(d for d in ordered if event.end < d.as_of <= after_end),
        clipped=lead_start < period_start,
    )


@dataclass(frozen=True, slots=True)
class EventNews:
    """`published_later` is published after the event's last decision, never served by it."""

    could_see: tuple[NewsItemView, ...]
    published_later: tuple[ArchiveItem, ...]
    covered: bool


def news_for(
    event: SpikeEvent,
    records: Iterable[CycleRecord],
    archive_items: Iterable[ArchiveItem],
    *,
    coverage: bool,
) -> EventNews:
    """The two news lists for one event, kept apart (§7.3).

    `could_see` is the union of what the snapshots from the lead-in through the event served, each
    item once, read off `snapshot.news` and never re-selected. `published_later` is archive items
    published after the event's last decision (one in the after stretch may have served some of
    them) and no later than `AFTER` past the event, deduplicated by `url_hash`. With no decision
    at or before the event's end nothing was served, so every item from the start of the lead-in
    on is later news.

    The time bound is what keeps the lists apart: a served item has
    `observed_at <= as_of <= last_decision`, so nothing after `last_decision` can have been
    served. An item observed before that but not served appears in neither list.
    """
    window = [r for r in records if event.start - LEAD_IN <= r.as_of <= event.end]
    last_decision = max((r.as_of for r in window), default=None)

    seen: dict[tuple[str, str, datetime], NewsItemView] = {}
    for record in sorted(window, key=lambda r: r.as_of):
        for view in record.snapshot.news:
            seen.setdefault((view.source, view.title, view.observed_at), view)
    could_see = tuple(sorted(seen.values(), key=lambda v: v.observed_at))

    def is_later(moment: datetime) -> bool:
        if last_decision is None:
            return event.start - LEAD_IN <= moment <= event.end + AFTER
        return last_decision < moment <= event.end + AFTER

    later: dict[str, ArchiveItem] = {}
    for archived in sorted(archive_items, key=lambda i: i.observed_at):
        if is_later(archived.observed_at):
            later.setdefault(archived.url_hash, archived)
    return EventNews(could_see=could_see, published_later=tuple(later.values()), covered=coverage)
