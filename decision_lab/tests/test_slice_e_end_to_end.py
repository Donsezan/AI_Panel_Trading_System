"""Slice E end to end, offline: crawl → summarise → publish → corpus → report.

One pass, on the stub provider and the seeded `sim` panel (stub seats), over a handmade dataset
and a handmade raw store. It asserts the loop closes and that the page says what it measured -
not that any model's judgement was measured, which is what the real pass is for.

Assert on the markup only the deriving branch emits, never on a bare value. That rule was learned
expensively in pass 2: three assertions on that branch were vacuous substring matches against a
whole rendered page, including its own exit criterion.

The module also owns the offline fixtures the contamination suite reuses (`write_news`,
`prepared_dataset`), because a second pipeline written for `test_contamination.py` would be one
these tests agree with and the tool does not.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab.archive.config import DEFAULT_POLICY_FILE, load_policy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.publish import archive_path
from decision_lab.archive.published import PublishedArchive
from decision_lab.archive.store import RAW_DIR, RawArticle, RawStore
from decision_lab.archive.summarize import summarize_store
from decision_lab.params import CORPUS_META
from decision_lab.tests import factories as f
from decision_lab.tests.test_slice_b_end_to_end import seeded_stub
from tradebot.core.clock import ManualClock
from tradebot.decision.providers.stub import StubLLMProvider

REPO = Path(__file__).resolve().parents[2]
SOURCES = ("cryptoslate", "bitcoincom")


@dataclass(frozen=True, slots=True)
class Spec:
    """One article and the reply a scripted summariser gives for it."""

    source: str
    published: datetime
    title: str
    body: str
    summary: str
    event: EventType
    entities: tuple[str, ...]
    modality: Modality = Modality.CONFIRMED


def _at(day: int, hour: int) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


#: Twelve rows over two months-worth of January, two sources. Every title names bitcoin so the
#: bot's own keyword filter scores it - relevance is not the thing under test, and an irrelevant
#: row would be dropped before the clustering this file wants to see.
#:
#: The ETF pair is one event in two publishers' words and must fold into one slot; the Kraken
#: pair is a claim and its walk-back and must not (§6.8.2). Everything else is a distinct event,
#: spread so no 48h window holds more than the panel's eight slots.
SPECS: tuple[Spec, ...] = (
    Spec(
        "cryptoslate",
        _at(12, 9),
        "Bitcoin ETF inflows reach a record",
        "Issuer filings published on Friday show the largest single day of inflows.",
        "Spot bitcoin ETFs recorded record inflows on Friday, issuer filings show.",
        EventType.MARKET_STRUCTURE,
        ("Spot bitcoin ETFs",),
    ),
    Spec(
        "bitcoincom",
        _at(12, 10),
        "Bitcoin ETFs log record inflows",
        "Filings from the issuers confirm a record day of inflows into the funds.",
        "Spot bitcoin ETFs recorded record inflows on Friday, issuer filings show today.",
        EventType.MARKET_STRUCTURE,
        ("Spot bitcoin ETFs",),
    ),
    Spec(
        "cryptoslate",
        _at(14, 10),
        "Kraken halts bitcoin withdrawals after a security incident",
        "The exchange paused bitcoin withdrawals while it investigates a possible breach.",
        "Kraken halted bitcoin withdrawals after a security incident.",
        EventType.SECURITY_INCIDENT,
        ("Kraken",),
    ),
    Spec(
        "bitcoincom",
        _at(14, 16),
        "Kraken says the bitcoin withdrawal halt was a scheduled outage",
        "The exchange now states there was no breach and the pause was planned maintenance.",
        "Kraken now says the bitcoin withdrawal halt was a scheduled outage, not an incident.",
        EventType.SECURITY_INCIDENT,
        ("Kraken",),
        Modality.CORRECTED,
    ),
    Spec(
        "cryptoslate",
        _at(17, 8),
        "Bitcoin steady as the Fed holds rates",
        "The central bank kept its policy rate unchanged on Wednesday.",
        "The Federal Reserve held its policy rate unchanged on Wednesday.",
        EventType.MACRO,
        ("Federal Reserve",),
    ),
    Spec(
        "bitcoincom",
        _at(18, 8),
        "Bitcoin custody firm lists a new vault product",
        "A custody provider announced a vault product for institutional clients.",
        "A custody provider launched a vault product for institutional clients.",
        EventType.LISTING,
        ("Anchorage",),
    ),
    Spec(
        "cryptoslate",
        _at(19, 8),
        "Bitcoin developers merge a mempool relay change",
        "Core maintainers accepted a patch altering how nodes pass along cheap transactions.",
        "Maintainers merged a change to how nodes relay low-fee transactions.",
        EventType.TECHNOLOGY,
        ("Bitcoin Core",),
    ),
    Spec(
        "bitcoincom",
        _at(20, 8),
        "Bitcoin miner appoints a new chief executive",
        "The miner named a new chief executive after its founder stepped down.",
        "A bitcoin miner named a new chief executive after its founder stepped down.",
        EventType.PERSONNEL,
        ("Marathon",),
    ),
    Spec(
        "cryptoslate",
        _at(22, 8),
        "Bitcoin adoption widens as a retailer accepts payments",
        "A national retailer said customers can now pay at the till in bitcoin.",
        "A national retailer began accepting bitcoin payments at the till.",
        EventType.ADOPTION,
        ("Retailer",),
    ),
    Spec(
        "bitcoincom",
        _at(23, 8),
        "Court rules on a bitcoin mixer operator",
        "The operator of a bitcoin mixing service received a ruling from a district judge.",
        "A district court ruled on the operator of a bitcoin mixing service.",
        EventType.LEGAL,
        ("District court",),
    ),
    Spec(
        "cryptoslate",
        _at(25, 8),
        "Bitcoin exchange publishes proof of reserves",
        "The exchange published a proof of reserves covering customer balances.",
        "An exchange published a proof of reserves covering customer balances.",
        EventType.MARKET_STRUCTURE,
        ("Bitstamp",),
    ),
    Spec(
        "bitcoincom",
        _at(26, 8),
        "Bitcoin regulator opens a consultation on custody rules",
        "The regulator opened a consultation on how custodians must hold client assets.",
        "A regulator opened a consultation on custody rules for client assets.",
        EventType.REGULATORY,
        ("Regulator",),
    ),
)


def reply(spec: Spec) -> str:
    return json.dumps(
        {
            "summary": spec.summary,
            "event_type": spec.event.value,
            "entities": list(spec.entities),
            "assets": ["BTC"],
            "magnitudes": [],
            "modality": spec.modality.value,
        }
    )


def raw_article(spec: Spec) -> RawArticle:
    return RawArticle(
        source_id=spec.source,
        url=f"https://{spec.source}.test/{spec.published:%Y%m%d%H}/{abs(hash(spec.title)) % 9999}",
        title=spec.title,
        body=spec.body,
        abstract="The publisher's own evaluative blurb.",
        published_at=spec.published,
        fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


def prepared_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, days: int, **shocks: object
) -> Path:
    """A verified dataset with every workspace redirected under `tmp_path`.

    The rebindings are `calibrated_corpus`'s, for the same reason: a module that imported
    `workspace_root` into its own namespace needs its own redirect, and a missing one is not a
    failing test but one that writes into the operator's real `decision_lab/workspace/`.
    """
    from decision_lab import gate as gate_module
    from decision_lab import longrun as longrun_module
    from decision_lab import registry

    workspace = tmp_path / "workspace"
    for module in (cp, registry, gate_module, longrun_module):
        monkeypatch.setattr(module, "workspace_root", lambda: workspace)
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(workspace))
    seeded_stub(monkeypatch)
    data = tmp_path / "history"
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=days, **shocks)})  # type: ignore[arg-type]
    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    return data


def write_news(data: Path) -> dict[str, tuple[RawArticle, ...]]:
    """Raw store → scripted summarise → `archive publish`, for both sources.

    The summariser is driven as a function, not through the CLI: the CLI's stub refusal is Task
    5's test. It is *scripted*, not default - an unscripted stub answers a summary prompt with a
    trading decision, so every row would be refused. A script outranks the model name, which makes
    a valid canned `SummaryResponse` exactly what an offline test should hand it.
    """
    policy = load_policy(DEFAULT_POLICY_FILE)
    clock = ManualClock(datetime(2026, 10, 5, tzinfo=UTC))
    written: dict[str, tuple[RawArticle, ...]] = {}
    for source in SOURCES:
        specs = {raw_article(s).url: s for s in SPECS if s.source == source}
        store = RawStore.open(data / RAW_DIR, source_id=source)
        for spec in specs.values():
            store.append(raw_article(spec))
        # The script is consumed in call order, which is the store's read order.
        script = [reply(specs[a.url]) for a in store.read_all()]
        # `cli.main` below owns its own event loop, so this one cannot be a coroutine of the test.
        report = asyncio.run(
            summarize_store(StubLLMProvider(script), store, policy, clock, pause=timedelta(0))
        )
        assert (report.summarised, report.refused) == (len(specs), 0)
        assert cli.main(["archive", "publish", "--data", str(data), "--source", source]) == 0
        written[source] = store.read_all()
    return written


def build_args(data: Path, *extra: str) -> list[str]:
    """`sim` rather than `stub` as the reference panel: the one-seat stub always votes the same
    way, so its pass auto-pauses on the loss streak within a few days and leaves a corpus too short
    to meet the archive. `prepared_dataset` seeds the `varied-*` draws for the usual reason."""
    return [
        "corpus",
        "build",
        "--data",
        str(data),
        "--every",
        "4h",
        "--reference-panel",
        "sim",
        *extra,
    ]


def corpus_ids(workspace: Path) -> set[str]:
    return {p.name for p in workspace.iterdir() if (p / CORPUS_META).is_file()}


def test_the_loop_closes_from_a_raw_store_to_a_news_fed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice's exit criterion.

    Step 7 is the load-bearing one: steps 1-6 can all pass over an archive the panel never saw.
    """
    data = prepared_dataset(tmp_path, monkeypatch, days=30)
    workspace = tmp_path / "workspace"

    raw = write_news(data)

    # 4. Two archives, and nothing of an article's text in either.
    for source in SOURCES:
        text = archive_path(data, source).read_text(encoding="utf-8")
        for article in raw[source]:
            assert article.body not in text
            assert article.abstract not in text
        archive = PublishedArchive.read(archive_path(data, source))
        assert len(archive.items) == sum(1 for s in SPECS if s.source == source)
        assert all("body" not in json.loads(line) for line in text.splitlines()[1:])

    # 5. Blind and news-fed are different experiments.
    assert cli.main(build_args(data)) == cli.EXIT_OK
    (blind_id,) = corpus_ids(workspace)
    news_args = build_args(data, "--news-source", "cryptoslate", "--news-source", "bitcoincom")
    assert cli.main(news_args) == cli.EXIT_OK
    (fed_id,) = corpus_ids(workspace) - {blind_id}
    assert fed_id != blind_id
    fed = cp.load(fed_id, workspace=workspace)
    assert fed.meta.news_blind is False
    assert [a.source for a in fed.meta.news_archives] == list(SOURCES)

    # 6. The page states what was measured: both §6.7 banners, the plumbing label (the archive
    #    was stub-summarised), and not the blind one.
    page = tmp_path / "fed.md"
    assert cli.main(["report", "--corpus", fed_id, "--out", str(page)]) == cli.EXIT_OK
    text = page.read_text(encoding="utf-8")
    assert "**SUMMARIZED NEWS** — the excerpts in every snapshot are model-generated" in text
    assert "(model=`stub-summary`, prompt=" in text
    assert "**RECONSTRUCTED NEWS** — `observed_at` is synthetic" in text
    assert "(policy=`published_at+00:12:00`)" in text
    assert "**PLUMBING CHECK — NOT AN EVALUATION.**" in text
    assert "NEWS-BLIND RUN" not in text

    blind_page = tmp_path / "blind.md"
    assert cli.main(["report", "--corpus", blind_id, "--out", str(blind_page)]) == cli.EXIT_OK
    blind_text = blind_page.read_text(encoding="utf-8")
    assert "**NEWS-BLIND RUN**" in blind_text
    assert "**SUMMARIZED NEWS**" not in blind_text

    # 7. The panel saw news. Read off the frozen snapshots, i.e. out of the event log.
    seen = [entry.snapshot.news for entry in fed.entries if entry.snapshot.news]
    assert seen, "every step above can pass over an archive the panel never saw"
    assert all(
        entry.snapshot.news == () for entry in cp.load(blind_id, workspace=workspace).entries
    )
    # The ETF pair is one slot carrying its count; the claim and its walk-back are two.
    assert any("(reports=2)" in item.summary for news in seen for item in news)
    assert any(
        {"Kraken halts bitcoin withdrawals after a security incident"}
        | {"Kraken says the bitcoin withdrawal halt was a scheduled outage"}
        <= {item.title for item in news}
        for news in seen
    )


def _changed_under_tradebot(base: str) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", base, "--", "tradebot/"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def test_the_bot_diff_is_exactly_the_licensed_surface() -> None:
    """The slice's structural exit criterion: the only `tradebot/` file this branch may change is
    `app.py` (§2.2), and its guards live in `tests/`, which this check does not cover.

    Diffed against the fork point rather than `main` itself, and as a *subset*: once this branch
    merges, `main` contains the seam and an equality against it would fail forever. A branch that
    changed nothing in `tradebot/` also passes - the licence is a ceiling, not a quota.
    """
    try:
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "main"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git or `main` is not resolvable here (a shallow checkout, say)")

    assert _changed_under_tradebot(base) <= {"tradebot/app.py"}
