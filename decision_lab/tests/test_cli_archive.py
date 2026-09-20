"""`archive build` on the CLI (spec §6.3, §13).

Deliberately **not** in `LOCKED`. The lock exists to keep two writers off the workspace, and this
command writes beside the dataset rather than into the workspace — the same reason `dataset` is
absent. It is also the longest-running command in the tool: a year is ~5 700 articles at a polite
pace, and a crawl holding the workspace lock would block every sweep and report for hours.

The transport is injected through `build_fetcher`, so this test reaches no network. That is also
what lets it assert the thing a crawl must never do — ask for a path `robots.txt` disallows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab.archive.store import RawStore
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
