"""`archive build` on the CLI (spec §6.3, §13).

Deliberately **not** in `LOCKED`. The lock exists to keep two writers off the workspace, and this
command writes beside the dataset rather than into the workspace — the same reason `dataset` is
absent. It is also the longest-running command in the tool: a year is ~5 700 articles at a polite
pace, and a crawl holding the workspace lock would block every sweep and report for hours.

The transport is injected through `build_fetcher`, so this test reaches no network. That is also
what lets it assert the thing a crawl must never do — ask for a path `robots.txt` disallows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab.archive.config import DEFAULT_POLICY_FILE
from decision_lab.archive.store import RAW_DIR, RawArticle, RawStore
from decision_lab.tests.test_archive_coindesk import LD_JSON, article_page
from decision_lab.tests.test_archive_collect import FakeFetcher, listing, url_for

INSIDE = url_for("2024-06-20", "inside")
OUTSIDE = url_for("2024-06-20", "outside")


@pytest.fixture
def pages() -> dict[str, str]:
    rows = ((INSIDE, "Inside", "2024-06-20"), (OUTSIDE, "Outside", "2024-11-02"))
    built = {"https://www.coindesk.com/sitemap/archive/2024": listing(*rows)}
    for url, _title, listed in rows:
        built[url] = article_page(
            LD_JSON.replace("2024-07-16T15:32:00.326Z", f"{listed}T09:30:00Z")
        )
    return built


def test_archive_build_collects_the_window_into_the_raw_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]
) -> None:
    fetcher = FakeFetcher(pages)
    monkeypatch.setattr(cli, "build_fetcher", lambda clock: fetcher)

    code = cli.main(
        [
            "archive",
            "build",
            "--data",
            str(tmp_path),
            "--year",
            "2024",
            "--since",
            "2024-06-01",
            "--until",
            "2024-06-30",
            "--pause",
            "0",
        ]
    )

    assert code == 0
    stored = RawStore.open(tmp_path / "news-raw", source_id="coindesk").read_all()
    assert [row.url for row in stored] == [INSIDE], "only the window should be collected"
    assert stored[0].body, "the body is the whole point of the raw store"
    assert stored[0].summary == "", "summarising is a later pass"


def test_archive_build_routes_a_sitemap_source_to_its_own_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two source shapes are addressed differently and must not share a walk.

    CoinDesk is a dated year listing; CryptoSlate is a Yoast sitemap. Pointing the listing walk at
    a sitemap index finds no rows at all and reports a clean, empty, entirely wrong success.
    """
    from decision_lab.archive.sites import PROFILES
    from decision_lab.tests.test_archive_collect_site import pages as site_pages

    fetcher = FakeFetcher(site_pages())
    monkeypatch.setattr(cli, "build_fetcher", lambda clock: fetcher)

    code = cli.main(
        [
            "archive",
            "build",
            "--data",
            str(tmp_path),
            "--source",
            "cryptoslate",
            "--year",
            "2025",
            "--pause-ms",
            "0",
        ]
    )

    assert code == 0
    stored = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate").read_all()
    assert len(stored) == 2, "both articles in the 2025 chunk should have been collected"
    assert all(row.source_id == "cryptoslate" for row in stored)
    assert PROFILES["cryptoslate"].sitemap_index in fetcher.asked


def test_archive_backfill_fills_bodies_into_rows_collected_without_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metered 2024 crawl's repair pass: 5 678 complete rows that lack only their text."""
    from decision_lab.tests.test_archive_backfill import stored

    store = RawStore.open(tmp_path / "news-raw", source_id="coindesk")
    store.append(stored(url=INSIDE))
    fetcher = FakeFetcher({INSIDE: article_page()})
    monkeypatch.setattr(cli, "build_fetcher", lambda clock: fetcher)

    code = cli.main(["archive", "backfill", "--data", str(tmp_path), "--pause-ms", "0"])

    assert code == 0
    (row,) = RawStore.open(tmp_path / "news-raw", source_id="coindesk").read_all()
    assert "72 cents mid-morning Tuesday" in row.body


def test_archive_backfill_limit_bounds_what_the_probe_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--limit 1` answers "does this session get bodies" for one request, not 5 678."""
    from decision_lab.tests.test_archive_backfill import stored

    store = RawStore.open(tmp_path / "news-raw", source_id="coindesk")
    store.append(stored(url=INSIDE))
    store.append(stored(url=OUTSIDE))
    fetcher = FakeFetcher({INSIDE: article_page(), OUTSIDE: article_page()})
    monkeypatch.setattr(cli, "build_fetcher", lambda clock: fetcher)

    code = cli.main(
        ["archive", "backfill", "--data", str(tmp_path), "--pause-ms", "0", "--limit", "1"]
    )

    assert code == 0
    assert fetcher.asked == [INSIDE]


def test_an_authenticated_backfill_without_a_cookie_refuses_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back to anonymous would spend thousands of requests to gain three bodies.

    The refusal must also come *before* the transport is built, which is what `asked == []`
    pins: a pass that discovered the problem on its first 401 would already have crawled.
    """
    from decision_lab.archive.auth import COOKIE_VARIABLE
    from decision_lab.tests.test_archive_backfill import stored

    store = RawStore.open(tmp_path / "news-raw", source_id="coindesk")
    store.append(stored(url=INSIDE))
    fetcher = FakeFetcher({INSIDE: article_page()})
    monkeypatch.setattr(cli, "build_fetcher", lambda clock: fetcher)
    monkeypatch.delenv(COOKIE_VARIABLE, raising=False)

    code = cli.main(
        ["archive", "backfill", "--data", str(tmp_path), "--pause-ms", "0", "--authenticated"]
    )

    assert code == 3, "a refusal about the evidence we were pointed at"
    assert fetcher.asked == []


def summarisable_row(
    n: int, *, body: str = "The FCA granted Kraken an EMI licence today."
) -> RawArticle:
    return RawArticle(
        source_id="cryptoslate",
        url=f"https://x.test/{n}",
        title=f"Story {n}",
        body=body,
        abstract="An abstract.",
        published_at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        fetched_at=datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
    )


def _refusal(args: list[str], caplog: pytest.LogCaptureFixture) -> tuple[int, str]:
    # `main` reconfigures logging and detaches caplog's handler (see test_cli_calibrate), so the
    # handler is driven directly, as the sweep tests do.
    parsed = cli.parse_args(args)
    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        code = asyncio.run(cli.archive_summarise(parsed))
    return code, " ".join(r.getMessage() + str(getattr(r, "reason", "")) for r in caplog.records)


def test_summarise_runs_the_pass_and_exits_zero(tmp_path: Path) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    code = cli.main(
        [
            "archive",
            "summarise",
            "--data",
            str(data),
            "--source",
            "cryptoslate",
            "--policy",
            str(DEFAULT_POLICY_FILE),
            "--pause-ms",
            "0",
            "--plumbing-check",
        ]
    )

    assert code == cli.EXIT_OK
    # The stub answers an unscripted prompt with a trading decision, so every row is refused.
    assert not store.read_all()[0].is_summarised


def test_dry_run_makes_no_provider_calls_and_reports_what_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))
    store.append(summarisable_row(2, body="   "))

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("a projection must not construct a provider")

    monkeypatch.setattr(cli, "build_providers", explode)

    code = cli.main(
        ["archive", "summarise", "--data", str(data), "--source", "cryptoslate", "--dry-run"]
    )

    assert code == cli.EXIT_OK
    assert not store.read_all()[0].is_summarised


def test_project_counts_pending_bodyless_and_prompt_tokens(tmp_path: Path) -> None:
    from decision_lab.archive.config import load_policy
    from decision_lab.archive.summarize import project

    store = RawStore.open(tmp_path / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))
    store.append(summarisable_row(2, body="   "))

    projection = project(store, load_policy(DEFAULT_POLICY_FILE))

    assert (projection.rows, projection.pending, projection.bodyless) == (2, 1, 1)
    assert projection.prompt_tokens > 0


def test_per_row_cost_is_zero_when_nothing_was_summarised() -> None:
    from decimal import Decimal

    from decision_lab.archive.summarize import SummaryReport

    assert SummaryReport().per_row_cost == 0
    report = SummaryReport(summarised=4, cost_usd=Decimal("0.10"))
    assert report.per_row_cost == Decimal("0.025")


def test_summarise_refuses_a_missing_policy_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    RawStore.open(data / RAW_DIR, source_id="cryptoslate")

    code, text = _refusal(
        [
            "archive",
            "summarise",
            "--data",
            str(data),
            "--source",
            "cryptoslate",
            "--policy",
            str(tmp_path / "absent.toml"),
        ],
        caplog,
    )

    assert code == cli.EXIT_CANDIDATE
    assert "archive" in text and "absent.toml" in text


def test_summarise_refuses_before_any_spend_when_the_key_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))
    policy = tmp_path / "real.toml"
    policy.write_text(
        '[summarizer]\nprovider = "openrouter"\nmodel = "x/y:free"\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code, text = _refusal(
        [
            "archive",
            "summarise",
            "--data",
            str(data),
            "--source",
            "cryptoslate",
            "--policy",
            str(policy),
        ],
        caplog,
    )

    assert code == cli.EXIT_CANDIDATE
    assert "OPENROUTER_API_KEY" in text


def test_a_stub_binding_refuses_without_the_acknowledgement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    code, text = _refusal(
        ["archive", "summarise", "--data", str(data), "--source", "cryptoslate"], caplog
    )

    assert code == cli.EXIT_CANDIDATE
    assert "--plumbing-check" in text
    assert not store.read_all()[0].is_summarised


def test_dry_run_is_not_blocked_by_the_stub_refusal(tmp_path: Path) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    code = cli.main(
        ["archive", "summarise", "--data", str(data), "--source", "cryptoslate", "--dry-run"]
    )

    assert code == cli.EXIT_OK


def test_summarise_is_absent_from_locked() -> None:
    assert ("archive", "summarise") not in cli.LOCKED
    assert ("archive", "publish") not in cli.LOCKED


def _publishable(data: Path) -> None:
    from decision_lab.archive.config import load_policy
    from decision_lab.archive.fields import EventType, Modality
    from decision_lab.archive.summarize import prompt_digest, system_for

    policy = load_policy(DEFAULT_POLICY_FILE)
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(
        summarisable_row(1).model_copy(
            update={
                "summary": "Summary.",
                "event_type": EventType.REGULATORY,
                "modality": Modality.CONFIRMED,
                "summary_provider": policy.provider_id,
                "summary_model": policy.model,
                "summary_prompt_digest": prompt_digest(system_for(policy)),
            }
        )
    )


def test_publish_writes_the_archive_and_exits_zero(tmp_path: Path) -> None:
    data = tmp_path / "history"
    _publishable(data)

    code = cli.main(["archive", "publish", "--data", str(data), "--source", "cryptoslate"])

    assert code == cli.EXIT_OK
    assert (data / "news" / "cryptoslate" / "news.jsonl").exists()


def test_publish_refuses_an_unsummarised_store_with_exit_candidate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    RawStore.open(data / RAW_DIR, source_id="cryptoslate").append(summarisable_row(1))

    parsed = cli.parse_args(["archive", "publish", "--data", str(data), "--source", "cryptoslate"])
    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        code = asyncio.run(cli.archive_publish(parsed))

    assert code == cli.EXIT_CANDIDATE
    assert "summarise" in " ".join(
        r.getMessage() + str(getattr(r, "reason", "")) for r in caplog.records
    )
    assert not (data / "news").exists()


def _stale_store(data: Path) -> RawStore:
    from decision_lab.archive.fields import EventType, Modality

    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(
        summarisable_row(1).model_copy(
            update={
                "summary": "Old.",
                "event_type": EventType.REGULATORY,
                "modality": Modality.CONFIRMED,
                "summary_provider": "stub",
                "summary_model": "retired-model",
                "summary_prompt_digest": "0ld",
            }
        )
    )
    return store


def test_dry_run_reports_stale_rows_and_resummarise_counts_them_pending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    _stale_store(data)
    base = ["archive", "summarise", "--data", str(data), "--source", "cryptoslate", "--dry-run"]

    def run(extra: list[str]) -> dict[str, object]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="decision_lab.cli"):
            assert asyncio.run(cli.archive_summarise(cli.parse_args(base + extra))) == cli.EXIT_OK
        (record,) = [r for r in caplog.records if r.getMessage() == "archive summarise projection"]
        return {"stale": record.stale, "pending": record.pending}  # type: ignore[attr-defined]

    assert run([]) == {"stale": 1, "pending": 0}
    assert run(["--resummarise-stale"]) == {"stale": 1, "pending": 1}


def test_summarise_with_the_flag_redoes_stale_rows_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    from tradebot.decision.providers.stub import StubLLMProvider

    good = json.dumps(
        {
            "summary": "Kraken got a licence.",
            "event_type": "REGULATORY",
            "entities": ["Kraken"],
            "assets": ["NONE"],
            "magnitudes": [],
            "modality": "CONFIRMED",
        }
    )
    provider = StubLLMProvider([good])
    monkeypatch.setattr(
        cli, "build_providers", lambda *_a, **_k: SimpleNamespace(providers={"stub": provider})
    )
    data = tmp_path / "history"
    store = _stale_store(data)
    args = ["archive", "summarise", "--data", str(data), "--source", "cryptoslate"]
    args += ["--pause-ms", "0", "--plumbing-check"]

    assert cli.main(args) == cli.EXIT_OK
    assert provider.calls == [] and store.read_all()[0].summary_model == "retired-model"

    assert cli.main([*args, "--resummarise-stale"]) == cli.EXIT_OK
    assert store.read_all()[0].summary_model == "stub-summary"


def test_the_publish_refusal_names_the_resummarise_flag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "history"
    _stale_store(data)
    parsed = cli.parse_args(["archive", "publish", "--data", str(data), "--source", "cryptoslate"])
    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        assert asyncio.run(cli.archive_publish(parsed)) == cli.EXIT_CANDIDATE
    assert "--resummarise-stale" in " ".join(
        r.getMessage() + str(getattr(r, "reason", "")) for r in caplog.records
    )


def test_a_repeated_news_source_is_refused_before_anything_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parsed = cli.parse_args(
        [
            *["corpus", "build", "--data", str(tmp_path), "--every", "4h"],
            *["--news-source", "cryptoslate", "--news-source", "cryptoslate"],
        ]
    )
    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        code = asyncio.run(cli.corpus_build(parsed))

    assert code == cli.EXIT_CANDIDATE
    assert "repeats a source" in " ".join(str(getattr(r, "reason", "")) for r in caplog.records)
