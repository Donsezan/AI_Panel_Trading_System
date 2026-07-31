"""Feed fetching and parsing, fully offline through `httpx.MockTransport`.

The compliance behaviours are tested as behaviours, not as intentions: `robots.txt` is honoured,
an unreachable `robots.txt` means we do *not* fetch, conditional GET is replayed, and a real
`User-Agent` is sent. These are PLAN §3.3 obligations, not optimizations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import (
    ConfigError,
    RateLimitedError,
    SourceDisallowedError,
    VenueError,
)
from tradebot.news.http import DEFAULT_USER_AGENT, FeedFetcher
from tradebot.news.rss import FEEDS, RssNewsSource, build_sources

FEED_URL = "https://news.example.com/rss"

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example</title>
  <item>
    <title>Bitcoin ETF approved</title>
    <link>https://news.example.com/a?utm_source=rss</link>
    <description>&lt;p&gt;Regulators &lt;b&gt;approved&lt;/b&gt; the filing.&lt;/p&gt;</description>
    <pubDate>Sun, 01 Mar 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Ether upgrade ships</title>
    <link>https://news.example.com/b</link>
    <description>Shipped today.</description>
    <pubDate>Sun, 01 Mar 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Solana outage resolved</title>
    <link href="https://news.example.com/c"/>
    <updated>2026-03-01T08:00:00Z</updated>
    <content type="html">Validators restarted.</content>
  </entry>
</feed>"""


class Recorder:
    """Serves canned responses and records what was requested."""

    def __init__(self, *, robots: str = "User-agent: *\nAllow: /", body: str = RSS) -> None:
        self.robots = robots
        self.robots_status = 200
        self.body = body
        self.status = 200
        self.headers: dict[str, str] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(self.robots_status, text=self.robots)
        if "ETag" in self.headers and request.headers.get("If-None-Match") == self.headers["ETag"]:
            return httpx.Response(304)
        return httpx.Response(self.status, text=self.body, headers=self.headers)


def fetcher(recorder: Recorder, clock: ManualClock, **kwargs: object) -> FeedFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return FeedFetcher(client, clock, **kwargs)  # type: ignore[arg-type]


class TestRobots:
    async def test_a_disallowed_path_is_never_fetched(self, clock: ManualClock) -> None:
        recorder = Recorder(robots="User-agent: *\nDisallow: /rss")
        with pytest.raises(SourceDisallowedError, match=r"robots\.txt disallows"):
            await fetcher(recorder, clock).fetch(FEED_URL)
        assert [r.url.path for r in recorder.requests] == ["/robots.txt"]

    async def test_an_allowed_path_is_fetched(self, clock: ManualClock) -> None:
        recorder = Recorder()
        assert await fetcher(recorder, clock).fetch(FEED_URL) == RSS

    async def test_a_missing_robots_file_permits_access(self, clock: ManualClock) -> None:
        """RFC 9309: a 4xx on robots.txt means no restrictions are published."""
        recorder = Recorder()
        recorder.robots_status = 404
        assert await fetcher(recorder, clock).fetch(FEED_URL) is not None

    async def test_an_unreachable_robots_file_means_we_do_not_fetch(
        self, clock: ManualClock
    ) -> None:
        """Fail closed on the legal question: 5xx is 'rules unknown', so we do not crawl."""
        recorder = Recorder()
        recorder.robots_status = 503
        with pytest.raises(SourceDisallowedError):
            await fetcher(recorder, clock).fetch(FEED_URL)

    async def test_a_robots_transport_failure_also_blocks(self, clock: ManualClock) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        client = httpx.AsyncClient(transport=httpx.MockTransport(explode))
        with pytest.raises(SourceDisallowedError):
            await FeedFetcher(client, clock).fetch(FEED_URL)

    async def test_robots_is_cached_for_its_ttl(self, clock: ManualClock) -> None:
        """Re-fetching robots.txt on every poll is exactly the rudeness it exists to prevent."""
        recorder = Recorder()
        client = fetcher(recorder, clock)
        await client.fetch(FEED_URL)
        await client.fetch(FEED_URL)
        assert sum(1 for r in recorder.requests if r.url.path == "/robots.txt") == 1


class TestConditionalGet:
    async def test_validators_are_replayed_and_304_yields_nothing(self, clock: ManualClock) -> None:
        recorder = Recorder()
        recorder.headers = {"ETag": '"abc"'}
        client = fetcher(recorder, clock)
        assert await client.fetch(FEED_URL) == RSS
        assert await client.fetch(FEED_URL) is None
        assert recorder.requests[-1].headers["If-None-Match"] == '"abc"'

    async def test_a_real_user_agent_identifies_the_client(self, clock: ManualClock) -> None:
        recorder = Recorder()
        await fetcher(recorder, clock).fetch(FEED_URL)
        assert recorder.requests[-1].headers["User-Agent"] == DEFAULT_USER_AGENT
        assert "Mozilla" not in DEFAULT_USER_AGENT


class TestErrorClassification:
    async def test_a_rate_limit_carries_the_publishers_retry_after(
        self, clock: ManualClock
    ) -> None:
        recorder = Recorder()
        recorder.status = 429
        recorder.headers = {"Retry-After": "30"}
        with pytest.raises(RateLimitedError) as caught:
            await fetcher(recorder, clock).fetch(FEED_URL)
        assert caught.value.retry_after_seconds == 30.0

    async def test_a_server_error_is_retryable(self, clock: ManualClock) -> None:
        recorder = Recorder()
        recorder.status = 503
        with pytest.raises(VenueError, match="HTTP 503"):
            await fetcher(recorder, clock).fetch(FEED_URL)

    @pytest.mark.parametrize("status", [401, 403])
    async def test_being_blocked_stops_us_asking(self, clock: ManualClock, status: int) -> None:
        recorder = Recorder()
        recorder.status = status
        with pytest.raises(SourceDisallowedError):
            await fetcher(recorder, clock).fetch(FEED_URL)

    async def test_a_404_is_a_configuration_defect(self, clock: ManualClock) -> None:
        recorder = Recorder()
        recorder.status = 404
        with pytest.raises(ConfigError, match="feed URL looks wrong"):
            await fetcher(recorder, clock).fetch(FEED_URL)

    async def test_a_timeout_is_retryable(self, clock: ManualClock) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            raise httpx.ReadTimeout("too slow")

        client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        with pytest.raises(VenueError, match="timed out"):
            await FeedFetcher(client, clock).fetch(FEED_URL)

    async def test_an_oversized_response_is_refused(self, clock: ManualClock) -> None:
        """A hostile or broken feed must not be able to exhaust memory."""
        recorder = Recorder(body="x" * 5000)
        with pytest.raises(ConfigError, match="above the"):
            await fetcher(recorder, clock, max_bytes=1000).fetch(FEED_URL)


class TestRssParsing:
    async def test_rss_entries_become_raw_items(self, clock: ManualClock) -> None:
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(), clock), clock)
        items = await source.fetch_latest()
        assert [entry.title for entry in items] == ["Bitcoin ETF approved", "Ether upgrade ships"]
        assert items[0].source_id == "example"

    async def test_html_is_stripped_from_the_body(self, clock: ManualClock) -> None:
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(), clock), clock)
        items = await source.fetch_latest()
        assert items[0].body == "Regulators approved the filing."

    async def test_the_published_date_is_parsed_as_utc(self, clock: ManualClock) -> None:
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(), clock), clock)
        items = await source.fetch_latest()
        assert items[0].published_at == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

    async def test_observed_at_is_our_clock_not_the_publishers(self, clock: ManualClock) -> None:
        """`observed_at` is the only field a point-in-time filter may trust (DESIGN [L12])."""
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(), clock), clock)
        items = await source.fetch_latest()
        assert all(entry.observed_at == clock.now() for entry in items)

    async def test_atom_feeds_parse_too(self, clock: ManualClock) -> None:
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(body=ATOM), clock), clock)
        items = await source.fetch_latest()
        assert items[0].title == "Solana outage resolved"
        assert items[0].published_at == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)

    async def test_an_undated_entry_falls_back_to_when_we_saw_it(self, clock: ManualClock) -> None:
        undated = RSS.replace("<pubDate>Sun, 01 Mar 2026 09:00:00 GMT</pubDate>", "")
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(body=undated), clock), clock)
        items = await source.fetch_latest()
        assert items[0].published_at == clock.now()

    async def test_an_unchanged_feed_yields_no_items(self, clock: ManualClock) -> None:
        recorder = Recorder()
        recorder.headers = {"ETag": '"abc"'}
        client = fetcher(recorder, clock)
        source = RssNewsSource("example", FEED_URL, client, clock)
        await source.fetch_latest()
        assert await source.fetch_latest() == ()

    async def test_a_feed_with_no_entries_is_a_venue_error(self, clock: ManualClock) -> None:
        empty = "<rss version='2.0'><channel><title>x</title></channel></rss>"
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(body=empty), clock), clock)
        with pytest.raises(VenueError, match="no entries"):
            await source.fetch_latest()

    async def test_entries_missing_a_link_are_skipped(self, clock: ManualClock) -> None:
        """Without a URL there is no identity, and dedup would count the story twice."""
        linkless = RSS.replace("<link>https://news.example.com/b</link>", "")
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(body=linkless), clock), clock)
        items = await source.fetch_latest()
        assert [entry.title for entry in items] == ["Bitcoin ETF approved"]

    async def test_a_feed_of_only_unusable_entries_is_a_venue_error(
        self, clock: ManualClock
    ) -> None:
        broken = RSS.replace("<link>", "<nolink>").replace("</link>", "</nolink>")
        source = RssNewsSource("example", FEED_URL, fetcher(Recorder(body=broken), clock), clock)
        with pytest.raises(VenueError, match="none usable"):
            await source.fetch_latest()

    async def test_the_entry_count_is_capped(self, clock: ManualClock) -> None:
        source = RssNewsSource(
            "example", FEED_URL, fetcher(Recorder(), clock), clock, max_entries=1
        )
        assert len(await source.fetch_latest()) == 1


class TestSourceRegistry:
    def test_built_in_feeds_resolve(self, clock: ManualClock) -> None:
        sources = build_sources(("cointelegraph",), fetcher(Recorder(), clock), clock)
        assert sources[0].url == FEEDS["cointelegraph"]

    def test_an_unknown_source_is_a_config_error(self, clock: ManualClock) -> None:
        with pytest.raises(ConfigError, match="unknown news source"):
            build_sources(("nope",), fetcher(Recorder(), clock), clock)

    def test_config_can_add_a_feed(self, clock: ManualClock) -> None:
        sources = build_sources(
            ("mine",), fetcher(Recorder(), clock), clock, extra={"mine": FEED_URL}
        )
        assert sources[0].source_id == "mine"

    def test_a_non_http_url_is_refused(self, clock: ManualClock) -> None:
        with pytest.raises(ConfigError, match="must be http"):
            RssNewsSource("x", "file:///etc/passwd", fetcher(Recorder(), clock), clock)

    def test_every_built_in_feed_is_https(self) -> None:
        """RSS over plain HTTP is an injection surface for text that reaches a prompt."""
        assert all(url.startswith("https://") for url in FEEDS.values())
