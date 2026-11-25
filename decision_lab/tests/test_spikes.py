"""A spike is a dated event; its news is split by what the panel could have read (§7)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from decision_lab import spikes
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveItem
from decision_lab.calibration_days import Pool
from decision_lab.records import CycleRecord
from decision_lab.regimes import BarLabel
from decision_lab.scoring import ScoredDecision, Verdict
from decision_lab.tests import factories as f
from tradebot.core.enums import Action
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle
from tradebot.core.snapshot import NewsItemView

SHOCK_UP = Pool.SHOCK_UP
SHOCK_DOWN = Pool.SHOCK_DOWN
NORMAL = Pool.NORMAL
T0 = datetime(2025, 3, 1, tzinfo=UTC)


def utc(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def bars(pattern: list[Pool], *, vol: str = "0.01") -> tuple[BarLabel, ...]:
    return tuple(
        BarLabel(
            close_time=T0 + timedelta(hours=i),
            label=label,
            volatility=Decimal(vol) + Decimal(i) / 1000,
            window_return_=Decimal("0.05") if label is SHOCK_UP else Decimal("-0.05"),
        )
        for i, label in enumerate(pattern)
    )


def candles_for(
    labelled: Sequence[BarLabel], closes: Sequence[str] | None = None
) -> tuple[Candle, ...]:
    """Candles on the bars' own close times; flat 100 unless closes are given."""
    values = closes or ["100"] * len(labelled)
    return tuple(
        Candle(
            open_time=bar.close_time - timedelta(hours=1),
            close_time=bar.close_time,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            volume=Decimal(1),
        )
        for bar, c in zip(labelled, values, strict=True)
    )


def events_of(
    pattern: list[Pool], closes: Sequence[str] | None = None, **kw: Any
) -> tuple[spikes.SpikeEvent, ...]:
    labelled = bars(pattern)
    return spikes.find_events(labelled, candles_for(labelled, closes), **kw)


def test_consecutive_shock_bars_of_one_direction_are_one_event() -> None:
    events = events_of(
        [NORMAL, SHOCK_UP, SHOCK_UP, SHOCK_UP, NORMAL], ["100", "100", "105", "110", "110"]
    )
    assert len(events) == 1
    event = events[0]
    assert event.direction is SHOCK_UP
    assert event.start == T0 + timedelta(hours=1)
    assert event.end == T0 + timedelta(hours=3)
    assert event.peak_volatility == Decimal("0.013")
    assert event.move_pct == Decimal("10")


def test_a_gap_of_six_merges_and_seven_splits() -> None:
    six = [SHOCK_UP, *[NORMAL] * 6, SHOCK_UP]
    seven = [SHOCK_UP, *[NORMAL] * 7, SHOCK_UP]
    assert len(events_of(six)) == 1
    assert len(events_of(seven)) == 2


def test_an_opposite_direction_never_merges() -> None:
    events = events_of([SHOCK_UP, NORMAL, SHOCK_DOWN])
    assert [e.direction for e in events] == [SHOCK_UP, SHOCK_DOWN]
    assert events_of([SHOCK_UP, SHOCK_DOWN])[1].direction is SHOCK_DOWN


def test_no_shock_bars_is_no_events() -> None:
    assert events_of([NORMAL, NORMAL]) == ()


def test_a_named_window_attaches_its_name_to_an_overlapping_event() -> None:
    from decision_lab.regimes import EventWindow

    window = EventWindow(name="crash", **{"from": T0, "to": T0 + timedelta(hours=2)})
    event = events_of([NORMAL, SHOCK_DOWN], windows=(window,))[0]
    assert event.window_name == "crash"


def test_move_is_the_events_own_price_change_not_the_trailing_return() -> None:
    # The bars all carry a 5% trailing return; the event's real move is 100 -> 90.
    event = events_of([NORMAL, SHOCK_DOWN, NORMAL, SHOCK_DOWN], ["100", "95", "93", "90"])[0]
    assert event.move_pct == Decimal("-10")


def test_a_series_that_opens_inside_the_event_measures_from_its_first_close() -> None:
    event = events_of([SHOCK_UP, SHOCK_UP], ["100", "120"])[0]
    assert event.move_pct == Decimal("20")


def test_a_missing_close_refuses() -> None:
    labelled = bars([NORMAL, SHOCK_UP])
    with pytest.raises(ConfigError):
        spikes.find_events(labelled, candles_for(labelled)[:1])


def event_at(start: datetime, direction: Pool, *, hours: int = 3) -> spikes.SpikeEvent:
    return spikes.SpikeEvent(
        start=start,
        end=start + timedelta(hours=hours),
        direction=direction,
        move_pct=Decimal("-5"),
        peak_volatility=Decimal("0.02"),
    )


def scored(*, as_of: datetime, regime: Pool = NORMAL) -> ScoredDecision:
    return ScoredDecision(
        cycle_id=f"c-{as_of.isoformat()}",
        as_of=as_of,
        instrument_key="binance:BTC/USDT",
        regime=regime,
        action=Action.WAIT,
        conviction=Decimal("0.5"),
        asked_for_an_order=False,
        holding=False,
        verdict=Verdict.CORRECT,
    )


def test_a_decision_labelled_normal_before_the_spike_is_in_its_lead_in() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    d = scored(as_of=utc(2025, 3, 9), regime=NORMAL)
    assert spikes.stretches(event, [d], period_start=T0).lead_in == (d,)


def test_decisions_fall_into_exactly_one_stretch() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    early = scored(as_of=utc(2025, 3, 5))
    lead = scored(as_of=utc(2025, 3, 9))
    during = scored(as_of=utc(2025, 3, 10, 2), regime=SHOCK_DOWN)
    after = scored(as_of=utc(2025, 3, 11))
    result = spikes.stretches(event, [after, during, lead, early], period_start=T0)
    assert result.lead_in == (lead,)
    assert result.during == (during,)
    assert result.after == (after,)


def test_an_event_24h_into_the_period_is_clipped() -> None:
    event = event_at(utc(2025, 3, 2), SHOCK_DOWN)
    assert spikes.stretches(event, [], period_start=utc(2025, 3, 1)).clipped


def test_a_sparse_lead_in_well_inside_the_period_is_not_clipped() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    late = scored(as_of=utc(2025, 3, 9, 20))  # a paused basket: nothing earlier in the lead-in
    result = spikes.stretches(event, [late], period_start=utc(2025, 3, 1))
    assert not result.clipped


def test_no_decisions_well_inside_the_period_is_an_empty_unclipped_lead_in() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    result = spikes.stretches(event, [], period_start=utc(2025, 3, 1))
    assert result.lead_in == ()
    assert not result.clipped


def view(title: str, observed_at: datetime) -> NewsItemView:
    return NewsItemView(
        source="src",
        title=title,
        summary="s",
        published_at=observed_at,
        observed_at=observed_at,
        relevance=Decimal("0.5"),
    )


def item(title: str, observed_at: datetime) -> ArchiveItem:
    return ArchiveItem(
        source_id="src",
        url=f"https://x/{title}",
        url_hash=f"hash-{title}",
        title=title,
        summary="s",
        event_type=next(iter(EventType)),
        modality=next(iter(Modality)),
        published_at=observed_at,
        observed_at=observed_at,
    )


def record(as_of: datetime, *news: NewsItemView) -> CycleRecord:
    snapshot = f.snapshot_at(as_of).model_copy(update={"news": news})
    return CycleRecord(
        cycle_id=f"c-{as_of.isoformat()}", basket_id="b", as_of=as_of, snapshot=snapshot
    )


EVENT = event_at(utc(2025, 3, 10), SHOCK_DOWN)
LAST = utc(2025, 3, 10, 2)


def records_with_news() -> tuple[CycleRecord, ...]:
    seen = view("seen", utc(2025, 3, 9, 20))
    return (
        record(utc(2025, 3, 9, 22), seen),
        record(LAST, seen, view("also", utc(2025, 3, 10, 1))),
    )


ITEMS = (
    item("seen", utc(2025, 3, 9, 20)),
    item("also", utc(2025, 3, 10, 1)),
    item("later", utc(2025, 3, 10, 5)),
    item("far", utc(2025, 3, 20)),
)


def test_could_see_is_only_what_the_snapshots_served() -> None:
    n = spikes.news_for(EVENT, records_with_news(), ITEMS, coverage=True)
    assert {v.title for v in n.could_see} == {"seen", "also"}
    assert all(v.observed_at <= LAST for v in n.could_see)


def test_published_later_never_appears_in_could_see() -> None:
    n = spikes.news_for(EVENT, records_with_news(), ITEMS, coverage=True)
    assert {i.title for i in n.published_later} == {"later"}
    served = {v.title for v in n.could_see}
    assert served.isdisjoint({i.title for i in n.published_later})


def test_published_later_keeps_commentary() -> None:
    commentary = item("opinion", utc(2025, 3, 10, 6)).model_copy(
        update={"event_type": EventType.MARKET_COMMENTARY}
    )
    n = spikes.news_for(EVENT, records_with_news(), (*ITEMS, commentary), coverage=True)
    assert "opinion" in {i.title for i in n.published_later}


def test_published_later_is_deduplicated_by_url_hash() -> None:
    twice = (
        *ITEMS,
        item("later", utc(2025, 3, 10, 6)).model_copy(update={"url_hash": "hash-later"}),
    )
    n = spikes.news_for(EVENT, records_with_news(), twice, coverage=True)
    assert [i.url_hash for i in n.published_later] == ["hash-later"]


def test_with_no_decisions_every_item_in_the_window_is_published_later() -> None:
    n = spikes.news_for(EVENT, (), ITEMS, coverage=True)
    assert n.could_see == ()
    assert {i.title for i in n.published_later} == {"seen", "also", "later"}


def test_a_record_outside_the_news_window_is_excluded() -> None:
    stale = record(utc(2025, 3, 5), view("stale", utc(2025, 3, 5)))
    after = record(utc(2025, 3, 11), view("after", utc(2025, 3, 11)))
    n = spikes.news_for(EVENT, (*records_with_news(), stale, after), ITEMS, coverage=True)
    assert {v.title for v in n.could_see} == {"seen", "also"}


def test_an_item_served_before_the_lead_in_is_in_neither_list() -> None:
    # Served by a decision too early to be this event's, and observed before its last decision:
    # the time bound keeps it out of published_later, and the window keeps it out of could_see.
    early = view("early", utc(2025, 3, 5))
    records = (*records_with_news(), record(utc(2025, 3, 5), early))
    n = spikes.news_for(EVENT, records, (*ITEMS, item("early", utc(2025, 3, 5))), coverage=True)
    assert "early" not in {v.title for v in n.could_see}
    assert "early" not in {i.title for i in n.published_later}


def test_no_coverage_and_a_quiet_window_differ() -> None:
    outside = spikes.news_for(EVENT, (), (), coverage=False)
    inside = spikes.news_for(EVENT, (), (), coverage=True)
    assert not outside.covered
    assert inside.covered
    assert outside.could_see == inside.could_see == ()
