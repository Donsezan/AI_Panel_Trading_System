"""§16.2: the point-in-time and hindsight guards, asserted against the real failure.

Nine rows in the spec's table. The third is the one that matters most and it runs through the
real `ContextBuilder` rather than against the feed in isolation - a test of the feed proves the
feed, and what is at stake is the snapshot. `build_sim` wires the builder exactly as a corpus pass
does, so reading `CorpusEntry.snapshot` back out of the event log is reading what the panel read.

Everything is offline and under `tmp_path`: no row here reaches a network, and none touches the
operator's `data\\` or `decision_lab/workspace/`.
"""

from __future__ import annotations

import ast
import inspect
import re
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import newsfeed as nf
from decision_lab.archive import summarize as summ
from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.tests import factories as f
from decision_lab.tests.test_slice_e_end_to_end import (
    SOURCES,
    corpus_ids,
    prepared_dataset,
    write_news,
)
from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.decision.providers.stub import StubLLMProvider
from tradebot.indicators.library import DEFAULT_INDICATORS
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.news.relevance import KeywordRelevanceFilter

NOW = datetime(2026, 8, 23, tzinfo=UTC)
FLOOR = Decimal("0.25")
BTC = Instrument(
    symbol="BTC/USDT",
    venue="binance",
    asset_class=AssetClass.CRYPTO,
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
)

#: The stub panel's pass auto-pauses on its loss streak after roughly Jan 6, and the indicators'
#: warm-up consumes the first two days. January 4th is inside both, so every cycle the rows below
#: reason about exists.
DAY = datetime(2024, 1, 4, tzinfo=UTC)


def row(
    *,
    at: datetime,
    title: str,
    summary: str,
    event: EventType = EventType.REGULATORY,
    modality: Modality = Modality.CONFIRMED,
    entities: tuple[str, ...] = ("Kraken",),
    source: str = "cryptoslate",
    link: str | None = None,
) -> ArchiveItem:
    url = link or f"https://{source}.test/{title.replace(' ', '-')}"
    return ArchiveItem(
        source_id=source,
        url=url,
        url_hash=f"h{abs(hash(url)) % 1_000_000}",
        title=title,
        summary=summary,
        event_type=event,
        entities=entities,
        assets=("BTC",),
        magnitudes=(),
        modality=modality,
        published_at=at - timedelta(minutes=12),
        observed_at=at,
    )


def feed_over(*items: ArchiveItem, window: timedelta = timedelta(hours=48)) -> ArchiveNewsFeed:
    ordered = tuple(sorted(items, key=lambda i: i.observed_at))
    header = ArchiveHeader(
        source="cryptoslate",
        capture="sitemap",
        captured_at=NOW,
        observed_at_policy="published_at+00:12:00",
        summarizer_provider="stub",
        summarizer_model="stub-summary",
        prompt_digest="abc",
        news_window_seconds=int(window.total_seconds()),
        cluster_floor=FLOOR,
        rows=len(ordered),
        archive_digest="digest-contamination",
    )
    return ArchiveNewsFeed(
        (PublishedArchive(header=header, items=ordered),),
        KeywordRelevanceFilter(),
        window=window,
        floor=FLOOR,
    )


@pytest.fixture
async def verified(tmp_path: Path) -> Path:
    """Ten days of hourly bars, audited clean - `test_corpus`'s own fixture."""
    clock = ManualClock(NOW)
    data = tmp_path / "history"
    f.write_dataset(
        data, {(f.instrument(), "1h"): f.shocked_walk(days=10, shock_up=(3,), shock_down=(6,))}
    )
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    return data


async def corpus_over(data: Path, workspace: Path, feed: ArchiveNewsFeed) -> cp.Corpus:
    return await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="stub",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
        news_feed=feed,
        wall_clock=ManualClock(NOW),
    )


# --- §16.2 row 1 ----------------------------------------------------------------------------
def test_the_feed_holds_no_fetcher_and_no_http_client() -> None:
    """Also asserted in `test_newsfeed.py`; repeated here because this file is where a reader
    looks for the contamination guarantees, and a guarantee split across two files is one
    somebody deletes half of. Two halves: no object the feed owns could reach out, and nothing
    the module imports could be made to."""
    built = feed_over(row(at=DAY, title="Bitcoin ETF approved", summary="Approved."))

    names = {type(v).__name__.lower() for v in vars(built).values()}
    assert not any("client" in n or "fetcher" in n or "transport" in n for n in names)
    assert not any(
        word in parameter
        for parameter in inspect.signature(ArchiveNewsFeed.__init__).parameters
        for word in ("fetch", "client", "transport", "hub", "source")
    )

    imported: set[str] = set()
    for module in (nf, __import__("decision_lab.clustering", fromlist=["x"])):
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    forbidden = {"httpx", "aiohttp", "requests", "socket", "urllib.request", "ccxt"}
    assert not {m for m in imported if m.split(".")[0] in {x.split(".")[0] for x in forbidden}}
    assert not {m for m in imported if m.startswith(("tradebot.news.http", "tradebot.news.rss"))}


# --- §16.2 row 2 ----------------------------------------------------------------------------
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a corpus build reached for the network")

    for client in (httpx.Client, httpx.AsyncClient):
        monkeypatch.setattr(client, "send", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.mark.asyncio
async def test_a_corpus_cycle_completes_behind_a_transport_that_raises_on_any_request(
    verified: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No network call happens on the real path, not merely in principle. A pass that completes
    proves nothing reached out; a pass that fails names which component did.

    The trap is armed first and shown to fire, so a pass that "completes behind" a guard that
    never worked is not a pass that proves anything."""
    _forbid_network(monkeypatch)
    with pytest.raises(AssertionError, match="reached for the network"):
        httpx.Client().get("http://example.invalid/")

    feed = feed_over(row(at=DAY + timedelta(hours=5), title="Bitcoin ETF approved", summary="s"))
    built = await corpus_over(verified, tmp_path / "ws", feed)

    assert built.entries, "the pass ran cycles"
    assert any(e.snapshot.news for e in built.entries), "and the panel was shown the archive"


# --- §16.2 row 3 — the one that matters most -------------------------------------------------
@pytest.mark.asyncio
async def test_the_snapshot_carries_the_earlier_item_and_not_the_later_one(
    verified: Path, tmp_path: Path
) -> None:
    """The exact scenario §2.2 describes, through the real `ContextBuilder` output. One item
    observed before the replay instant and one after it; the snapshot frozen at that instant
    carries the first and not the second.

    It fails if anyone reintroduces a fetch into the replay - including by wiring a live
    `NewsHub` back in, which is the failure mode no amount of care inside this package can
    prevent from outside."""
    before = row(
        at=DAY + timedelta(hours=5),
        title="Bitcoin ETF approved",
        summary="The SEC approved a bitcoin ETF.",
        entities=("SEC",),
    )
    after = row(
        at=DAY + timedelta(hours=9),
        title="Bitcoin crashes on exchange outage",
        summary="Bitcoin fell sharply during an outage.",
        entities=("Exchange",),
        event=EventType.SECURITY_INCIDENT,
    )

    built = await corpus_over(verified, tmp_path / "ws", feed_over(before, after))

    between = [e for e in built.entries if before.observed_at <= e.as_of < after.observed_at]
    assert between, "at least one cycle sits between the two observations"
    for entry in between:
        titles = [item.title for item in entry.snapshot.news]
        assert titles == [before.title], f"{entry.as_of}: {titles}"
    # Not vacuous: the later item is servable once its instant has passed, so its absence above
    # is the point-in-time cut and not the feed having lost it.
    later = [e for e in built.entries if e.as_of >= after.observed_at]
    assert later
    assert {i.title for i in later[0].snapshot.news} == {before.title, after.title}
    earlier = [e for e in built.entries if e.as_of < before.observed_at]
    assert earlier
    assert all(e.snapshot.news == () for e in earlier)


# --- §16.2 rows 4 and 5 ---------------------------------------------------------------------
def test_the_summarizer_is_never_called_during_a_corpus_sweep_or_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.5's reuse promise, and that a sweep's spend is the panel's alone. The archive is
    published first with a working summariser; every entry point into summarising is then replaced
    with one that records and raises, and `corpus build`, `calibrate normal`, `calibrate shock`
    and `sweep` are run over it.

    Recorded as well as raised, so a broad `except` somewhere between the trap and the test could
    not turn a call into a pass."""
    shocks: dict[str, object] = {"shock_up": (3, 11, 19, 27), "shock_down": (7, 15, 23)}
    data = prepared_dataset(tmp_path, monkeypatch, days=60, **shocks)
    assert cli.main(["dataset", "days", "--data", str(data)]) == cli.EXIT_OK
    write_news(data)

    called: list[str] = []

    def trap(name: str) -> Any:
        def tripped(*_: Any, **__: Any) -> Any:
            called.append(name)
            raise AssertionError(f"the summariser was reached through {name}")

        return tripped

    for name in ("summarize_store", "_summarise_one", "summary_user", "parse_summary"):
        monkeypatch.setattr(summ, name, trap(name))
    monkeypatch.setattr(cli, "summarize_store", trap("cli.summarize_store"))

    args = ["corpus", "build", "--data", str(data), "--every", "8h", "--reference-panel", "sim"]
    for source in SOURCES:
        args += ["--news-source", source]
    assert cli.main(args) == cli.EXIT_OK
    (corpus_id,) = corpus_ids(tmp_path / "workspace")
    assert cp.load(corpus_id, workspace=tmp_path / "workspace").meta.news_blind is False

    for scenario in ("normal", "shock"):
        out = tmp_path / f"{scenario}.md"
        code = cli.main(
            [
                *("calibrate", scenario, "--corpus", corpus_id),
                *("--configs", str(cd.STUB_MATRIX), "--budget", "1", "--out", str(out)),
            ]
        )
        assert code == cli.EXIT_OK
    sweep = ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    assert cli.main(sweep) == cli.EXIT_OK

    assert called == []


def _three_rows(tmp_path: Path) -> RawStore:
    store = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate")
    for n, (title, body) in enumerate(
        (
            ("Zephyr exchange lists a token", "Zephyr said the token begins trading on Monday."),
            ("Quillon fund files a prospectus", "Quillon filed a prospectus with its regulator."),
            ("Marlow miner relocates a facility", "Marlow will move a mining site to Norway."),
        )
    ):
        store.append(
            RawArticle(
                source_id="cryptoslate",
                url=f"https://x.test/{n}",
                title=title,
                body=body,
                published_at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
                fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
        )
    return store


@pytest.mark.asyncio
async def test_the_summarizers_prompt_is_given_no_instrument_no_market_data_no_sibling(
    tmp_path: Path,
) -> None:
    """Asserted on the recorded call arguments rather than on the prompt text, per §16.2: each
    `user` carries that row's own title and body and no other row's, and neither `user` nor
    `system` carries an instrument symbol or an indicator name.

    `system` legitimately lists `BTC` among the closed asset vocabulary and names "a price
    direction" among the things the model must not offer, so what is forbidden is the *shape* of
    an instrument (`BASE/QUOTE`) and a market reading (an indicator), not every word."""
    store = _three_rows(tmp_path)
    policy = ArchivePolicy(provider_id="stub", model="stub-summary")
    reply = (
        '{"summary": "A thing happened.", "event_type": "OTHER", "entities": [], '
        '"assets": ["NONE"], "magnitudes": [], "modality": "CONFIRMED"}'
    )
    provider = StubLLMProvider([reply])

    await summ.summarize_store(provider, store, policy, ManualClock(NOW), pause=timedelta(0))

    rows = store.read_all()
    assert len(provider.calls) == len(rows) == 3
    symbol = re.compile(r"\b[A-Z0-9]{2,6}/[A-Z0-9]{2,6}\b")
    indicator = re.compile(rf"\b({'|'.join(DEFAULT_INDICATORS)})\b")
    for article, call in zip(rows, provider.calls, strict=True):
        assert article.title in call.user and article.body in call.user
        for other in rows:
            if other is not article:
                assert other.title not in call.user and other.body not in call.user
        for text in (call.user, call.system):
            assert not symbol.search(text), "an instrument symbol reached the summariser"
            assert not indicator.search(text), "a market reading reached the summariser"
    assert {call.system for call in provider.calls} == {summ.system_for(policy)}, (
        "one constant system prompt - nothing per-row or per-instrument rides in it"
    )


# --- §16.2 row 6 ----------------------------------------------------------------------------
def _trigrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9$%.]+", text.lower())
    if len(words) < 3:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def traceable_share(summary: str, body: str, *, clause_floor: Decimal = Decimal("0.5")) -> Decimal:
    """The share of a summary's clauses whose 3-grams trace to the body's.

    A clause is traceable when at least `clause_floor` of its 3-grams occur in the body. Clauses
    are cut on sentence and comma boundaries, so a summary that is faithful in one clause and
    invents in the next scores half rather than passing on the strength of the first.
    """
    clauses = [c for c in re.split(r"[.;,]\s+|[.;,]$", summary) if c.strip()]
    available = _trigrams(body)
    traced = 0
    for clause in clauses:
        grams = _trigrams(clause)
        if grams and Decimal(len(grams & available)) / Decimal(len(grams)) >= clause_floor:
            traced += 1
    return Decimal(traced) / Decimal(len(clauses))


def test_a_sampled_summarys_clauses_trace_to_ngrams_of_its_own_body() -> None:
    """§6.5's traceability check - the only one of the four closures that produces a number
    rather than an argument. A summary asserting something the article does not say fails, and it
    fails identically whether the claim came from hindsight, from priors, or from invention.

    The *mechanism* is asserted on handmade pairs. The number over the real archive is a
    measurement for the operator to take after the real pass, not a test fixture."""
    body = (
        "The FCA granted Kraken an electronic money licence on Tuesday. The exchange said it will "
        "begin offering e-money accounts to UK customers in the spring."
    )

    faithful = "The FCA granted Kraken an electronic money licence on Tuesday."
    assert traceable_share(faithful, body) == Decimal(1)

    hindsight = "Analysts expect further upside after the licence."
    assert traceable_share(hindsight, body) == Decimal(0)

    half = "The FCA granted Kraken an electronic money licence on Tuesday, shares later doubled."
    assert traceable_share(half, body) == Decimal("0.5")


# --- §16.2 rows 7, 8 and 9 ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_claim_and_its_correction_yield_two_slots() -> None:
    """Covered in `test_clustering.py`; re-asserted here through `snapshot_news` so the guarantee
    holds at the seam the panel actually reads. §6.8.2 says the collapse-to-first implementation
    passes every other test in that file."""
    claim = row(
        at=DAY + timedelta(hours=1),
        title="Bitcoin backer says he is enthusiastic",
        summary="A public figure said he is enthusiastic about bitcoin.",
        entities=("Figure",),
    )
    walkback = row(
        at=DAY + timedelta(hours=2),
        title="Bitcoin backer walks back his enthusiasm",
        summary="A public figure said he is enthusiastic about bitcoin, his office now says.",
        entities=("Figure",),
        modality=Modality.CORRECTED,
    )

    views, _ = await feed_over(claim, walkback).snapshot_news((BTC,), DAY + timedelta(hours=3))

    assert {v.title for v in views} == {claim.title, walkback.title}
    assert not any("reports=" in v.summary for v in views), "neither was folded into the other"


@pytest.mark.asyncio
async def test_cluster_counts_for_a_cycle_ignore_rows_observed_after_it() -> None:
    """Forward-only. A count over the whole archive is look-ahead with no model in it: it would
    tell the panel how much coverage an event *eventually* attracted."""
    text = "Kraken gained an EMI licence from the FCA today."
    reports = [
        row(
            at=DAY + timedelta(hours=h),
            title=f"Bitcoin exchange Kraken licensed, report {h}",
            summary=text,
            source=f"outlet{h}",
        )
        for h in (1, 3, 9)
    ]
    feed = feed_over(*reports)

    async def counted(hours: int) -> str:
        views, _ = await feed.snapshot_news((BTC,), DAY + timedelta(hours=hours))
        (only,) = views
        return only.summary

    assert "reports=" not in await counted(2)
    assert "(reports=2)" in await counted(4)
    assert "(reports=3)" in await counted(10)


@pytest.mark.asyncio
async def test_no_archive_row_is_absent_from_the_reconstruction_of_a_collapsed_cycle() -> None:
    """ "Annotates, never deletes", asserted on the reconstruction rather than on the renderer:
    for a cycle whose window collapsed twelve rows into one slot, `feed.in_window(as_of)` must
    still return all twelve."""
    reports = [
        row(
            at=DAY + timedelta(minutes=10 * n),
            title=f"Bitcoin exchange Kraken licensed, report {n}",
            summary="Kraken gained an EMI licence from the FCA today.",
            source=f"outlet{n}",
        )
        for n in range(12)
    ]
    feed = feed_over(*reports)
    as_of = DAY + timedelta(hours=3)

    views, _ = await feed.snapshot_news((BTC,), as_of)

    assert len(views) == 1 and "(reports=12)" in views[0].summary
    assert {i.url for i in feed.in_window(as_of)} == {r.url for r in reports}
