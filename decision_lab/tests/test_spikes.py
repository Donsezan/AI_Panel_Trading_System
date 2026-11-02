"""A spike is a dated event; its news is split by what the panel could have read (§7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from decision_lab import spikes
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveItem
from decision_lab.calibration_days import Pool
from decision_lab.records import CycleRecord
from decision_lab.regimes import BarLabel
from decision_lab.scoring import ScoredDecision, Verdict
from decision_lab.tests import factories as f
from tradebot.core.enums import Action
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


def test_consecutive_shock_bars_of_one_direction_are_one_event() -> None:
    events = spikes.find_events(bars([NORMAL, SHOCK_UP, SHOCK_UP, SHOCK_UP, NORMAL]))
    assert len(events) == 1
    event = events[0]
    assert event.direction is SHOCK_UP
    assert event.start == T0 + timedelta(hours=1)
    assert event.end == T0 + timedelta(hours=3)
    assert event.peak_volatility == Decimal("0.013")
    assert event.move_pct == Decimal("5.00")


def test_a_gap_of_six_merges_and_seven_splits() -> None:
    six = [SHOCK_UP, *[NORMAL] * 6, SHOCK_UP]
    seven = [SHOCK_UP, *[NORMAL] * 7, SHOCK_UP]
    assert len(spikes.find_events(bars(six))) == 1
    assert len(spikes.find_events(bars(seven))) == 2


def test_an_opposite_direction_never_merges() -> None:
    events = spikes.find_events(bars([SHOCK_UP, NORMAL, SHOCK_DOWN]))
    assert [e.direction for e in events] == [SHOCK_UP, SHOCK_DOWN]
    assert spikes.find_events(bars([SHOCK_UP, SHOCK_DOWN]))[1].direction is SHOCK_DOWN


def test_no_shock_bars_is_no_events() -> None:
    assert spikes.find_events(bars([NORMAL, NORMAL])) == ()


def test_a_named_window_attaches_its_name_to_an_overlapping_event() -> None:
    from decision_lab.regimes import EventWindow

    window = EventWindow(name="crash", **{"from": T0, "to": T0 + timedelta(hours=2)})
    event = spikes.find_events(bars([NORMAL, SHOCK_DOWN]), windows=(window,))[0]
    assert event.window_name == "crash"


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
    assert spikes.stretches(event, [d]).lead_in == (d,)


def test_decisions_fall_into_exactly_one_stretch() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    early = scored(as_of=utc(2025, 3, 5))
    lead = scored(as_of=utc(2025, 3, 9))
    during = scored(as_of=utc(2025, 3, 10, 2), regime=SHOCK_DOWN)
    after = scored(as_of=utc(2025, 3, 11))
    result = spikes.stretches(event, [after, during, lead, early])
    assert result.lead_in == (lead,)
    assert result.during == (during,)
    assert result.after == (after,)


def test_a_lead_in_clipped_by_the_period_edge_says_so() -> None:
    event = event_at(utc(2025, 3, 1, 6), SHOCK_DOWN)
    assert spikes.stretches(event, [scored(as_of=utc(2025, 3, 1, 2))]).clipped


def test_a_lead_in_with_history_behind_it_is_not_clipped() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    assert not spikes.stretches(event, [scored(as_of=utc(2025, 3, 1))]).clipped


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


def test_published_later_is_deduplicated_by_url_hash() -> None:
    twice = (
        *ITEMS,
        item("later", utc(2025, 3, 10, 6)).model_copy(update={"url_hash": "hash-later"}),
    )
    n = spikes.news_for(EVENT, records_with_news(), twice, coverage=True)
    assert [i.url_hash for i in n.published_later] == ["hash-later"]


def test_no_coverage_and_a_quiet_window_differ() -> None:
    outside = spikes.news_for(EVENT, (), (), coverage=False)
    inside = spikes.news_for(EVENT, (), (), coverage=True)
    assert not outside.covered
    assert inside.covered
    assert outside.could_see == inside.could_see == ()
