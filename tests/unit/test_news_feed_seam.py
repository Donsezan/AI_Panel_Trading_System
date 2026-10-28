"""The one injection seam a replay needs, and the doors it must not open.

`NewsHub.snapshot_news` calls `refresh()` on every invocation. Wired into a replay under a
`ManualClock` it would fetch *today's* RSS and store each item with `observed_at = <the replayed
instant>` — and since selection filters on `observed_at`, a 2026 headline would be served into a
2024 decision as though it had been known at the time. That is the look-ahead bug DESIGN [L12]
exists to prevent, and no amount of care outside this module can prevent it from outside.

So `build_sim` takes an optional `NewsFeed`. Paper and live are refused alongside each other: a
paper soak is the evidence `report promotion` reads (ADR 0016, ADR 0020), and a soak deciding on
archived news is not a soak of the system that would trade.

These tests assert behaviour, not private state: the injected feed is shown to be the one asked
for news, and the hub builder is shown not to run. The last test is a *characterisation* test. It
records what the news pipeline does today — the mechanism that makes this seam necessary — so a
future change to `observed_at` stamping surfaces in this file rather than silently in a replay.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine

from tradebot.app import BrokerChoice, _assemble, build, build_live, build_paper, build_sim
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.snapshot import NewsCoverage, NewsItemView
from tradebot.marketdata.synthetic import SyntheticMarketData
from tradebot.news.hub import NewsHub
from tradebot.news.relevance import KeywordRelevanceFilter
from tradebot.news.rss import RssNewsSource
from tradebot.news.store import NewsStore
from tradebot.news.vectorstore import SqliteVectorStore
from tradebot.persistence.database import SingleWriter

PAST = datetime(2024, 3, 11, tzinfo=UTC)


class StubFeed:
    """Satisfies the `NewsFeed` protocol, and records that it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    async def snapshot_news(
        self, instruments: tuple[Instrument, ...], as_of: datetime, limit: int
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        self.asked += 1
        return (), NewsCoverage(sources_ok=("stub",))


def _explode(*_: object, **__: object) -> Any:
    raise AssertionError("build_news_hub must not be called when a feed is injected")


def test_build_paper_and_build_live_carry_no_news_feed_parameter() -> None:
    """The structural closure. Fails loudly if a future refactor adds one for convenience."""
    for builder in (build_paper, build_live):
        assert "news_feed" not in inspect.signature(builder).parameters, builder.__name__


def test_build_sim_does_carry_it() -> None:
    """The guard above must not pass by the parameter simply never having been added."""
    assert "news_feed" in inspect.signature(build_sim).parameters


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_assemble_refuses_a_substituted_feed_outside_sim(
    mode: Mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defensive closure, which survives somebody adding the parameter to `build_paper`.

    It is the first statement: nothing is built, so no credential or network is reached. The
    `build_news_hub` bomb and the missing stack factory prove nothing past the refusal ran."""
    import tradebot.app as app_module

    monkeypatch.setattr(app_module, "build_news_hub", _explode)
    monkeypatch.setattr(app_module, "create_database", _explode)

    with pytest.raises(ConfigError, match=mode.value):
        await _assemble(
            ManualClock(PAST),
            mode=mode,
            db_path=None,
            broker_choice=BrokerChoice.BINANCE,
            start_equity=Decimal(10_000),
            baskets=None,
            global_policy=None,
            feed=app_module.PriceFeed(),
            news_sources=(),
            panel_id="stub",
            news_feed=StubFeed(),
        )


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_the_dispatch_door_is_shut_too(mode: Mode) -> None:
    """`build(mode, **kwargs)` must not quietly accept what the builder does not take."""
    with pytest.raises(TypeError):
        await build(mode, news_feed=StubFeed())


async def test_build_sim_uses_the_feed_given_and_never_builds_a_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam does what it says, observed through behaviour: the injected object is asked for
    news by a real cycle, and `build_news_hub` is not called at all — not called-and-discarded."""
    import tradebot.app as app_module

    monkeypatch.setattr(app_module, "build_news_hub", _explode)
    feed = StubFeed()
    clock = ManualClock(PAST)

    application = await build_sim(clock=clock, db_path=None, news_feed=feed)
    try:
        await application.recover()
        assert await application.supervisor.run_once()
        assert feed.asked > 0
    finally:
        await application.shutdown()


async def test_build_sim_with_sources_and_no_feed_still_wires_a_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression net. The default path is byte-for-byte the old behaviour, so the seam
    cannot silently change what a sim run with configured sources does."""
    import tradebot.app as app_module

    real = app_module.build_news_hub
    calls: list[tuple[object, ...]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_news_hub", spy)
    application = await build_sim(clock=ManualClock(PAST), news_sources=("cointelegraph",))
    try:
        assert len(calls) == 1
        assert calls[0][3] == ("cointelegraph",)
    finally:
        await application.shutdown()


async def test_paper_news_is_unchanged_by_this_design(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper still builds its hub from configured sources. The hub builder is replaced by a spy
    that stops the build, so the test needs no network: it asks only what `_assemble` called.
    (Live shares `_assemble` but cannot be constructed offline — it needs venue credentials.)"""
    import tradebot.app as app_module

    seen: list[tuple[object, ...]] = []

    class ReachedError(Exception):
        pass

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(args)
        raise ReachedError

    monkeypatch.setattr(app_module, "build_news_hub", spy)
    with pytest.raises(ReachedError):
        await build_paper(
            clock=ManualClock(PAST),
            market_data=SyntheticMarketData(ManualClock(PAST)),
            news_sources=("cointelegraph",),
        )
    assert seen
    assert seen[0][3] == ("cointelegraph",)


class _Fetcher:
    """One RSS document with no dates, so the item's stamp can only come from the clock."""

    async def fetch(self, url: str) -> str | None:
        return (
            '<rss version="2.0"><channel><title>t</title><item>'
            "<title>Bitcoin rallies</title><link>https://example.test/btc</link>"
            "<description>Bitcoin is up.</description></item></channel></rss>"
        )


async def test_a_news_hub_on_a_past_clock_stamps_items_with_that_instant(
    database: tuple[Engine, SingleWriter], instrument: Instrument
) -> None:
    """Characterisation, not aspiration. This is the mechanism the seam exists to avoid: a hub
    built on a `ManualClock` set to a past instant stamps whatever it fetched *now* with *then*,
    and selection filters on that field — so today's headline is served into a past decision."""
    engine, writer = database
    clock = ManualClock(PAST)
    hub = NewsHub(
        (
            RssNewsSource(
                "example",
                "https://example.test/feed",
                _Fetcher(),  # type: ignore[arg-type]  # only `fetch` is ever called
                clock,
            ),
        ),
        NewsStore(engine, writer),
        SqliteVectorStore(engine, writer, clock),
        KeywordRelevanceFilter(),
        clock,
    )

    items, coverage = await hub.snapshot_news((instrument,), PAST + timedelta(minutes=1), 5)

    assert coverage.sources_ok == ("example",)
    assert [item.title for item in items] == ["Bitcoin rallies"]
    stored = NewsStore(engine, writer).select(PAST + timedelta(minutes=1))
    assert [item.observed_at for item in stored] == [PAST]
