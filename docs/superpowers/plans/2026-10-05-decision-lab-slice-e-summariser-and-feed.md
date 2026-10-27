# decision_lab Slice E — the summariser, the seam and `ArchiveNewsFeed`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the collected raw news archive into point-in-time evidence a replayed panel can read — a summarising pass that compresses each stored body into a 280-character excerpt plus five extracted fields, a travelling archive that carries no body, the one licensed `build_sim` seam, and an `ArchiveNewsFeed` that selects and clusters without ever touching the network.

**Architecture:** Three stages, two stores. `RawStore` (staging, bodies, gitignored) already exists and holds 14 484 collected rows. A **summarising pass** reads each row's body through one declared `(provider, model)` binding and writes a `summary` plus `event_type`, `entities`, `assets`, `magnitudes` and `modality` back onto the same row. A **publish** step projects those rows into the §6.6 travelling archive — `news.jsonl`, header line plus item lines, **no body field** — deriving `observed_at = published_at + lag` and computing the `archive_digest` that feeds `corpus_id`. `ArchiveNewsFeed` loads that file once, indexes it in memory, and answers `snapshot_news` by selecting `observed_at <= as_of`, clustering on the extracted fields behind a similarity floor, and scoring through the bot's own `KeywordRelevanceFilter`. It reaches `BasketWorker` through one new optional parameter on `build_sim`, refused in every other mode.

**Tech Stack:** Python 3.12, pydantic v2 (`DomainModel`), `tomllib`, `pytest`, `ruff`, `mypy`. No new dependencies. Everything is proven offline against `StubLLMProvider` with scripted responses.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §2.2, §2.3, §2.4, §6.4, §6.5, §6.6, §6.7, §6.8, §6.9, §16.2. Read §6.5 and §6.8.2.1 before Task 1; they are the two sections this plan argues from hardest.

**Scope:** Code only. Every task is proven offline on the stub provider. The real summarising run (~11 894 rows, needs `OPENROUTER_API_KEY`), the corpus over `data\history-2025`, and its calibration are **deliberately out of scope** — they are spend and risk decisions, and Task 5 ships the cost projection that informs the first of them.

## Global Constraints

Copied verbatim from the spec and the repository's own guards. Every task's requirements implicitly include this section.

- **No `float`, anywhere in `decision_lab`.** `test_discipline.py` walks every non-test module with an AST and checks *annotations* too — `def band(k: float) -> float` is the same defect one syntax node over. `FLOAT_EXEMPT` is currently empty and must stay empty. The similarity floor is a `Decimal`, a pause is a `timedelta`, and a CLI delay is `--pause-ms` in integer milliseconds.
- **Nothing under `tradebot/` may name `decision_lab`** — not an import, not an attribute, not a string. `test_separation.py` checks `ast.Constant` too, so a docstring mentioning the tool fails. The seam's parameter is typed against the existing `NewsFeed` protocol and its docstring says "a substituted feed", never "the lab".
- **`git diff --stat main -- tradebot/` may touch exactly two things:** `tradebot/app.py` (the seam) and `tests/` (its guards). Slice E is the one sanctioned exception to the empty-diff rule, and the seam plus its guard tests are **one commit, never two**.
- **The travelling archive row model has no `body` field** (§6.4, §16 structural row). Enforced structurally so there is nowhere for one to be written.
- **Money is `Decimal`**, from `tradebot.core.money`. Never `float`, never `Decimal(some_float)`.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never `datetime.now()` in library code; `freezegun` cannot freeze `loop.time()`.
- **Errors are classified**: `RetryableError` / `FailClosedError` / `FatalError`. A malformed config is `ConfigError`. A bare `except: pass` is a defect.
- **`DEFAULT_EXCERPT_CHARS = 280`** is the panel's slot (`tradebot/news/normalize.py`). A summary longer than it is our refusal, never the view's silent truncation.
- **The summariser prompt is given title and body only** — never an instrument, never market data, never a sibling article (§6.5's hindsight closure).
- **`archive_digest` feeds `corpus_id`** (§5.4, §6.6). Changing the summariser model, the prompt, the `observed_at` lag, the news window or the cluster floor must change it. `corpus_identity` stays exactly four components.
- **Reused, never reimplemented** (§2.4). `KeywordRelevanceFilter`, `canonical_url`, `url_hash`, `embed`, `similarity`, `REPAIR_INSTRUCTION`, `SchemaViolationError` and the provider registry are imported from `tradebot`. Nothing about how the bot decides is restated.
- **Exit codes are the existing set** (`decision_lab/cli.py:86-92`): `0` ok, `2` misuse, `3` dataset, `4` candidate, `5` budget, `6` gate, `7` busy. A new refusal reuses one; no new code is minted in this plan.
- **Both gates must pass**: `.\decision_lab\check.ps1` and the root `.\check.ps1`.

## Review Focus

Five input classes the spec implies but no task's own happy path exercises, most likely to bite first. Each has its test placed in the task that owns the code.

1. **A summariser reply that is prose, fenced, truncated, or a refusal.** Every real run meets this within the first hundred rows. One repair attempt, then the row is **refused and tallied** — never summarised from the publisher's abstract as a quiet fallback, because an abstract that evaluates is the fourteenth seat §6.5 exists to prevent. *Test in Task 4.*
2. **A summary longer than 280 characters.** `NewsItem.view` would carry it and the panel's slot would hold a model's overrun. The cut must be **ours and visible** — refused at parse time naming the length, not truncated. *Test in Task 3.*
3. **A replay window the archive does not cover.** A 2024 corpus pointed at the 2025 archive, or a cycle before the archive's first row. The feed must answer *no coverage*, never an empty list that the panel reads as a quiet market (§6.9's rule, arriving through the feed instead of the wiring). *Test in Task 8.*
4. **An `event_type` or `modality` the model invented.** §6.8.2's correction rule reads `modality` and the cluster key reads `event_type`; a free-text value makes the key unstable and silently stops collapsing corrections. Closed vocabularies, refused on anything else. *Test in Task 3.*
5. **An article with an empty or whitespace-only body.** It must never be sent to the provider and must never yield a summary — a model given nothing invents something, and this archive has 418-character minimum bodies only because they were measured, not because the type guarantees it. *Test in Task 4.*

---

## File Structure

**New, under `decision_lab/archive/`:**

| File | Responsibility |
|---|---|
| `fields.py` | the two closed vocabularies — `EventType`, `Modality` — and nothing else, so the travelling archive and the summariser cannot disagree about them |
| `config.py` | `archive.toml` → `ArchivePolicy`: the summariser binding, the `observed_at` lag, the news window, the cluster floor, and `prompt_digest` |
| `summarize.py` | the prompt, the response contract, `parse_summary`, and `summarize_store` — the pass itself |
| `published.py` | `ArchiveItem` (no body field), `ArchiveHeader`, `archive_digest`, and the read/write of §6.6's `news.jsonl` |
| `publish.py` | raw rows → `ArchiveItem`s: `observed_at` derivation, body dropped, digest computed |

**New, under `decision_lab/`:**

| File | Responsibility |
|---|---|
| `newsfeed.py` | `ArchiveNewsFeed` — the `NewsFeed` implementation, point-in-time selection, the coverage line |
| `clustering.py` | the §6.8.2 fold: field key, similarity floor, forward-only counts, corrections never collapsed |
| `config/archive.toml` | the shipped policy, with a stub binding so the suite is offline |

**Modified:**

| File | Change |
|---|---|
| `decision_lab/archive/store.py` | `RawArticle` gains five extraction fields, all defaulted |
| `decision_lab/archive/__init__.py` | export the new surfaces |
| `decision_lab/cli.py` | `archive summarise` and `archive publish`; both absent from `LOCKED` |
| `decision_lab/corpus.py` | `build(..., news_feed=…)` threads the feed and its digest |
| `decision_lab/render.py` | the two §6.7 banners |
| `tradebot/app.py` | `build_sim(news_feed=…)` and `_assemble`'s refusal — **the only bot change** |

**New tests:**

`decision_lab/tests/`: `test_archive_fields.py`, `test_archive_config.py`, `test_archive_summarize.py`, `test_archive_published.py`, `test_archive_publish.py`, `test_newsfeed.py`, `test_clustering.py`, `test_contamination.py`, `test_slice_e_end_to_end.py`, plus additions to `test_cli_archive.py` and `test_discipline.py`.

`tests/unit/`: `test_news_feed_seam.py` — §2.3's seven guards, in the **bot's** suite.

---

## Task 1: The closed vocabularies and the travelling archive

**Files:**
- Create: `decision_lab/archive/fields.py`
- Create: `decision_lab/archive/published.py`
- Test: `decision_lab/tests/test_archive_fields.py`
- Test: `decision_lab/tests/test_archive_published.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `EventType` (StrEnum): `REGULATORY`, `LISTING`, `SECURITY_INCIDENT`, `MARKET_STRUCTURE`, `ADOPTION`, `MACRO`, `LEGAL`, `TECHNOLOGY`, `PERSONNEL`, `OTHER`
  - `Modality` (StrEnum): `CONFIRMED`, `PROPOSED`, `RUMOURED`, `DENIED`, `CORRECTED`; property `Modality.qualifies -> bool`, which is `True` for `DENIED` and `CORRECTED` and `False` for the other three
  - `ArchiveItem(DomainModel)`: `source_id: str`, `url: str`, `url_hash: str`, `title: str`, `summary: str`, `event_type: EventType`, `entities: tuple[str, ...]`, `assets: tuple[str, ...]`, `magnitudes: tuple[str, ...]`, `modality: Modality`, `published_at: UtcDatetime`, `observed_at: UtcDatetime`
  - `ArchiveHeader(DomainModel)`: `source: str`, `capture: str`, `captured_at: UtcDatetime`, `observed_at_policy: str`, `summarizer_provider: str`, `summarizer_model: str`, `prompt_digest: str`, `news_window_seconds: int`, `cluster_floor: Money`, `rows: int`, `archive_digest: str`
  - `archive_digest(header_without_digest: ArchiveHeader, items: tuple[ArchiveItem, ...]) -> str`
  - `PublishedArchive`: `write(path, header, items) -> None`, `read(path) -> PublishedArchive`, attributes `.header`, `.items`
  - `ARCHIVE_FILE: Final = "news.jsonl"`, `ARCHIVE_DIR: Final = "news"`

**Why one file per vocabulary.** The cluster key is `(event_type, entities, assets)` and the correction rule reads `modality` (§6.8.2.1, §6.8.2). Both the summariser's response contract and the travelling archive's row model validate against them, and two copies of a ten-member vocabulary that drift are a key that stops matching halfway through an archive. `fields.py` depends on nothing, so neither side can be the owner.

**Why §6.6's literal single file, not month shards.** `archive reshard` exists because `RawStore.read_all` was re-parsed on every backfill batch and every `held` check during a multi-hour crawl. This file is read **once per corpus build** and indexed in memory — 11 894 bodyless rows is about 7 MB — so the sharding argument does not reach it, and keeping §6.6's declared format means the header travels on line 1 where the spec puts it.

- [ ] **Step 1: Write the failing test for the vocabularies**

```python
# decision_lab/tests/test_archive_fields.py
"""The two closed vocabularies the cluster key and the correction rule read (spec §6.8.2.1).

Closed rather than free text, because `event_type` is a third of the cluster key and a model
inventing "regulatory-approval" beside "REGULATORY" is a key that silently stops matching. The
same argument applies to `modality` one step harder: §6.8.2's correction rule *dispatches* on it,
so an unrecognised value does not merely fail to cluster — it collapses a retraction behind the
claim it retracts.
"""

from __future__ import annotations

import pytest

from decision_lab.archive.fields import EventType, Modality


def test_every_modality_is_one_of_the_five_the_spec_names() -> None:
    assert {m.value for m in Modality} == {
        "CONFIRMED",
        "PROPOSED",
        "RUMOURED",
        "DENIED",
        "CORRECTED",
    }


def test_only_a_denial_or_a_correction_qualifies_its_cluster() -> None:
    """§6.8.2: a member that qualifies, corrects or denies is never collapsed at all.

    `PROPOSED` and `RUMOURED` are deliberately *not* qualifiers. "SEC is expected to approve" is
    a different fact from "SEC approves" and the summary says so, but it does not retract
    anything — treating it as a qualifier would give every speculative rewrite its own slot and
    reintroduce the flooding §6.8.2.1 exists to prevent.
    """
    assert [m for m in Modality if m.qualifies] == [Modality.DENIED, Modality.CORRECTED]


def test_an_unknown_value_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ValueError):
        Modality("SPECULATIVE")
    with pytest.raises(ValueError):
        EventType("regulatory-approval")


def test_event_type_carries_a_catch_all_so_a_real_article_always_classifies() -> None:
    """A vocabulary with no escape hatch refuses rows for being ordinary."""
    assert EventType.OTHER in set(EventType)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_fields.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.archive.fields'`

- [ ] **Step 3: Write `fields.py`**

```python
# decision_lab/archive/fields.py
"""The closed vocabularies the extracted fields use (spec §6.5, §6.8.2.1).

They live in their own module, depending on nothing, because two sides validate against them: the
summariser's response contract (`summarize.py`) and the travelling archive's row model
(`published.py`). Either one owning them makes the other a client of its import graph, and two
copies that drift are a cluster key that stops matching partway through an archive.

Closed rather than free text for the reason §6.8.2.1 gives: `event_type` is a third of the cluster
key, so "regulatory-approval" beside "REGULATORY" splits one event into two slots. `modality` is
harder still — §6.8.2's correction rule *dispatches* on it, and an unrecognised value collapses a
retraction behind the claim it retracts, which is the one failure that section exists to prevent.

Failure semantics: pure data. An unknown value raises `ValueError` from `StrEnum` itself, which
pydantic converts into a field error naming the path — so a malformed model response is refused
at the boundary and tallied, never stored.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """What kind of thing happened, as a classification (§6.5).

    Classification rather than span extraction: financial events are overlong and discontinuous,
    so a span is neither a stable key nor a readable summary field. `OTHER` is deliberate — a
    vocabulary with no escape hatch refuses rows for being ordinary, and a refused row is a gap
    in coverage rather than a tidier archive.
    """

    REGULATORY = "REGULATORY"
    LISTING = "LISTING"
    SECURITY_INCIDENT = "SECURITY_INCIDENT"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    ADOPTION = "ADOPTION"
    MACRO = "MACRO"
    LEGAL = "LEGAL"
    TECHNOLOGY = "TECHNOLOGY"
    PERSONNEL = "PERSONNEL"
    OTHER = "OTHER"


class Modality(StrEnum):
    """Whether the article states the event as fact, intent, rumour, denial or correction.

    "SEC approves" and "SEC is expected to approve" are different facts, both present in the text,
    and an editorial abstract flattens them (§6.5). Load-bearing past the prompt: §6.8.2 reads
    this to refuse to collapse a correction behind the claim it corrects.
    """

    CONFIRMED = "CONFIRMED"
    PROPOSED = "PROPOSED"
    RUMOURED = "RUMOURED"
    DENIED = "DENIED"
    CORRECTED = "CORRECTED"

    @property
    def qualifies(self) -> bool:
        """True for a member that must never be collapsed into its cluster (§6.8.2).

        `PROPOSED` and `RUMOURED` are excluded on purpose. They state a weaker claim, not a
        retraction of one, so giving each its own slot would hand every speculative rewrite of a
        story a slot of its own — the flooding §6.8.2.1 exists to prevent, arriving from the
        qualifier rule instead of from a missing one.
        """
        return self in (Modality.DENIED, Modality.CORRECTED)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_fields.py -q`
Expected: PASS, 4 tests

- [ ] **Step 5: Write the failing test for the travelling archive**

```python
# decision_lab/tests/test_archive_published.py
"""The §6.6 travelling archive: no body, a header that declares provenance, a digest that binds.

Three properties, and the first is structural: the row model has **no body field**, so §6.4's
"the article body is never written to disk" is enforced by there being nowhere to write one
(§16's structural row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import (
    ARCHIVE_FILE,
    ArchiveHeader,
    ArchiveItem,
    PublishedArchive,
    archive_digest,
)
from tradebot.core.errors import ConfigError


def item(*, url: str = "https://x.test/a", summary: str = "Kraken gained an EMI licence.") -> ArchiveItem:
    return ArchiveItem(
        source_id="cryptoslate",
        url=url,
        url_hash="h" + url[-1],
        title="Kraken licensed in the UK",
        summary=summary,
        event_type=EventType.REGULATORY,
        entities=("Kraken", "FCA"),
        assets=("BTC",),
        magnitudes=(),
        modality=Modality.CONFIRMED,
        published_at=datetime(2025, 3, 4, 9, 2, tzinfo=UTC),
        observed_at=datetime(2025, 3, 4, 9, 14, tzinfo=UTC),
    )


def header(**overrides: object) -> ArchiveHeader:
    fields: dict[str, object] = {
        "source": "cryptoslate",
        "capture": "sitemap",
        "captured_at": datetime(2026, 10, 5, tzinfo=UTC),
        "observed_at_policy": "published_at+00:12:00",
        "summarizer_provider": "stub",
        "summarizer_model": "stub-summary",
        "prompt_digest": "abc123",
        "news_window_seconds": 172_800,
        "cluster_floor": Decimal("0.25"),
        "rows": 1,
        "archive_digest": "",
    }
    return ArchiveHeader.model_validate(fields | overrides)


def test_the_row_model_has_no_body_field() -> None:
    """§6.4, enforced structurally. Asserted on the model rather than on a writer, because a
    writer that drops the field can be changed and a field that does not exist cannot be set."""
    assert "body" not in ArchiveItem.model_fields


def test_a_body_passed_anyway_is_refused_rather_than_ignored() -> None:
    """pydantic's default would silently drop an extra key. `DomainModel` forbids extras, so an
    upstream step that starts carrying a body fails here instead of quietly succeeding."""
    with pytest.raises(ValueError):
        ArchiveItem.model_validate(item().model_dump(mode="json") | {"body": "the whole article"})


def test_write_then_read_round_trips_items_and_header(tmp_path: Path) -> None:
    path = tmp_path / ARCHIVE_FILE
    items = (item(url="https://x.test/a"), item(url="https://x.test/b"))
    PublishedArchive.write(path, header(rows=2), items)

    back = PublishedArchive.read(path)

    assert back.items == items
    assert back.header.observed_at_policy == "published_at+00:12:00"
    assert back.header.cluster_floor == Decimal("0.25")


def test_the_digest_changes_with_the_model_the_prompt_the_lag_the_window_and_the_floor() -> None:
    """§6.6: `archive_digest` feeds `corpus_id`, so each of these is a *different experiment*
    rather than a silent re-run of the same one. The floor and the window are in here for the
    same reason `observed_at_policy` already is — both are reading policy, not file content."""
    base = header()
    rows = (item(),)
    baseline = archive_digest(base, rows)

    for field, value in (
        ("summarizer_model", "stub-other"),
        ("prompt_digest", "def456"),
        ("observed_at_policy", "published_at+00:30:00"),
        ("news_window_seconds", 86_400),
        ("cluster_floor", Decimal("0.30")),
    ):
        assert archive_digest(header(**{field: value}), rows) != baseline, field


def test_the_digest_changes_with_the_row_set() -> None:
    base = header()
    one = archive_digest(base, (item(url="https://x.test/a"),))
    two = archive_digest(base, (item(url="https://x.test/a"), item(url="https://x.test/b")))
    assert one != two


def test_the_digest_ignores_captured_at_so_republishing_is_not_a_new_experiment() -> None:
    """When was it published is provenance; what does it contain is identity. A digest moving on
    a clock read would renumber the corpus of an archive whose rows are byte-identical."""
    rows = (item(),)
    early = archive_digest(header(captured_at=datetime(2026, 10, 5, tzinfo=UTC)), rows)
    late = archive_digest(header(captured_at=datetime(2026, 11, 1, tzinfo=UTC)), rows)
    assert early == late


def test_a_truncated_line_names_the_file_and_the_line(tmp_path: Path) -> None:
    """A staging store that silently dropped rows would under-report coverage in a way no later
    step could detect — `RawStore.read_month`'s rule, applied to the archive that travels."""
    path = tmp_path / ARCHIVE_FILE
    PublishedArchive.write(path, header(), (item(),))
    path.write_text(path.read_text(encoding="utf-8") + '{"kind": "item", "title"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"news\.jsonl:3"):
        PublishedArchive.read(path)


def test_a_file_whose_first_line_is_not_a_header_is_refused(tmp_path: Path) -> None:
    """The header is line 1 by §6.6's format. A file without one has no declared provenance, and
    an archive whose `observed_at` policy is unknown cannot be read point-in-time at all."""
    path = tmp_path / ARCHIVE_FILE
    path.write_text(item().model_dump_json() + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="header"):
        PublishedArchive.read(path)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_published.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.archive.published'`

- [ ] **Step 7: Write `published.py`**

```python
# decision_lab/archive/published.py
"""The §6.6 archive that travels: header line, item lines, and no body anywhere (spec §6.4).

Two stores, two rules. `store.py` is staging — raw articles as crawled, bodies included,
gitignored, never distributed, and never an input to `archive_digest`. This is the archive a
corpus is built against, and it keeps §6.4's rule structurally: `ArchiveItem` has no `body` field,
so there is nowhere for one to be written. `DomainModel` forbids extra keys, so an upstream step
that starts carrying a body is refused here rather than quietly dropped.

One file per source rather than month shards, deliberately, and the reasoning is the inverse of
`archive reshard`'s. That migration happened because `RawStore.read_all` was re-parsed on every
backfill batch and every `held` check of a multi-hour crawl. This file is read **once per corpus
build** and indexed in memory by `ArchiveNewsFeed` — 11 894 bodyless rows is roughly 7 MB — so
re-parsing never happens, and §6.6's declared format keeps the header on line 1 where the spec
puts it.

`archive_digest` covers the source, the capture mode, the `observed_at` policy, the summariser's
model and prompt digest, the news window, the cluster floor, and the row set. It feeds `corpus_id`
(§5.4), so re-summarising with a different model — or reading the same rows through a different
window or floor — yields a different corpus rather than silently mixing two experiments. It
deliberately ignores `captured_at`: when an archive was written is provenance, what it contains is
identity, and a digest that moved on a clock read would renumber the corpus of an archive whose
rows are byte-identical.

Failure semantics: the archive is a file. An unreadable line, a missing header, or a header that
is not line 1 raises `ConfigError` naming the file and the line. Nothing is skipped — an archive
that silently dropped rows would under-report coverage in a way no later step could detect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from decision_lab.archive.fields import EventType, Modality
from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel, Money, UtcDatetime, canonical_json

#: Beside the dataset, in its own directory, so the travelling archive is never confused with the
#: gitignored staging store `RAW_DIR` holds.
ARCHIVE_DIR: Final = "news"

#: §6.6's filename. One per source directory.
ARCHIVE_FILE: Final = "news.jsonl"

_HEADER: Final = "header"
_ITEM: Final = "item"


class ArchiveItem(DomainModel):
    """One archived article as a replay reads it. No body, by construction (§6.4).

    `summary` is §6.5's output and is what reaches `NewsItem.excerpt` and therefore the panel's
    280-character slot. The four extracted fields beside it are not decoration: `event_type`,
    `entities` and `assets` are the §6.8.2.1 cluster key, and `modality` is what stops a
    correction being collapsed behind the claim it corrects.

    `observed_at` is derived (§6.7) and the header declares how. It is the **only** field a
    point-in-time filter may use: filtering on `published_at` would let an item we did not have at
    the time into the decision, which is the look-ahead bug DESIGN [L12] exists to prevent.
    """

    source_id: str
    url: str
    url_hash: str
    title: str
    summary: str
    event_type: EventType
    entities: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    magnitudes: tuple[str, ...] = ()
    modality: Modality
    published_at: UtcDatetime
    observed_at: UtcDatetime


class ArchiveHeader(DomainModel):
    """§6.6's provenance line. Everything a reader needs to know how to read the rows below it.

    `archive_digest` is stored here as well as computed, so a reader can state the identity of the
    archive it loaded without recomputing it over every row — and `PublishedArchive.read` checks
    the two agree, which is how a hand-edited archive is caught.
    """

    source: str
    capture: str
    captured_at: UtcDatetime
    observed_at_policy: str
    summarizer_provider: str
    summarizer_model: str
    prompt_digest: str
    news_window_seconds: int
    cluster_floor: Money
    rows: int
    archive_digest: str = ""


def archive_digest(header: ArchiveHeader, items: tuple[ArchiveItem, ...]) -> str:
    """§6.6. Everything that decides what a replay reads, and nothing about when it was written."""
    payload = {
        "source": header.source,
        "capture": header.capture,
        "observed_at_policy": header.observed_at_policy,
        "summarizer_provider": header.summarizer_provider,
        "summarizer_model": header.summarizer_model,
        "prompt_digest": header.prompt_digest,
        "news_window_seconds": header.news_window_seconds,
        "cluster_floor": str(header.cluster_floor),
        "items": [item.model_dump(mode="json") for item in items],
    }
    return hashlib.blake2s(canonical_json(payload).encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedArchive:
    """One source's travelling archive, in memory."""

    header: ArchiveHeader
    items: tuple[ArchiveItem, ...]

    @staticmethod
    def write(path: Path, header: ArchiveHeader, items: tuple[ArchiveItem, ...]) -> None:
        """Write the header line and the item lines, stamping the digest over the rows given."""
        stamped = header.model_copy(
            update={"rows": len(items), "archive_digest": archive_digest(header, items)}
        )
        lines = [json.dumps({"kind": _HEADER} | stamped.model_dump(mode="json"))]
        lines += [json.dumps({"kind": _ITEM} | item.model_dump(mode="json")) for item in items]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Self:
        """Parse the whole file. Refuses a missing header, a bad line, or a digest that disagrees."""
        if not path.is_file():
            raise ConfigError(
                f"no published news archive at {path}. Run `archive publish --data <dir> "
                "--source <id>` after `archive summarise`"
            )
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise ConfigError(f"{path} is empty; it carries neither a header nor any rows")

        head = _decode(path, 1, lines[0])
        if head.pop("kind", None) != _HEADER:
            raise ConfigError(
                f"{path}:1 is not a §6.6 header. The header is line 1 and declares the "
                "`observed_at` policy; without it the rows below cannot be read point-in-time"
            )
        header = _validate(path, 1, ArchiveHeader, head)

        items = []
        for number, line in enumerate(lines[1:], 2):
            body = _decode(path, number, line)
            if body.pop("kind", None) != _ITEM:
                raise ConfigError(f"{path}:{number} is not an item row")
            items.append(_validate(path, number, ArchiveItem, body))
        rows = tuple(items)

        recomputed = archive_digest(header, rows)
        if header.archive_digest and header.archive_digest != recomputed:
            raise ConfigError(
                f"{path} declares archive_digest {header.archive_digest} but its rows hash to "
                f"{recomputed}. The file has been edited since it was published; re-publish it "
                "rather than trusting either number — the digest feeds corpus_id (§6.6)"
            )
        return cls(header=header, items=rows)


def _decode(path: Path, number: int, line: str) -> dict[str, Any]:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name}:{number} is not readable JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ConfigError(f"{path.name}:{number} is not a JSON object")
    return decoded


def _validate[T: DomainModel](path: Path, number: int, model: type[T], body: dict[str, Any]) -> T:
    try:
        return model.model_validate(body)
    except ValueError as exc:
        raise ConfigError(f"{path.name}:{number} is not a readable {model.__name__}: {exc}") from exc
```

- [ ] **Step 8: Run both files to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_published.py decision_lab/tests/test_archive_fields.py -q`
Expected: PASS, 12 tests

- [ ] **Step 9: Commit**

```bash
git add decision_lab/archive/fields.py decision_lab/archive/published.py \
        decision_lab/tests/test_archive_fields.py decision_lab/tests/test_archive_published.py
git commit -m "feat(decision_lab): the travelling archive and its closed vocabularies

The §6.6 archive a replay reads: header line, item lines, and no body field
anywhere, so §6.4's rule is enforced by there being nowhere to write one.
`archive_digest` covers the summariser binding, the observed_at policy, the
news window and the cluster floor — each of those is a different experiment
— and ignores captured_at, because when an archive was written is provenance
and what it contains is identity.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: `archive.toml` — the summariser binding and the reading policy

**Files:**
- Create: `decision_lab/archive/config.py`
- Create: `decision_lab/config/archive.toml`
- Test: `decision_lab/tests/test_archive_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ArchivePolicy(DomainModel)`: `provider_id: str`, `model: str`, `observed_at_lag_seconds: int`, `news_window_seconds: int`, `cluster_floor: Money`, `max_summary_chars: int`, and properties `lag -> timedelta`, `window -> timedelta`, `observed_at_policy -> str`, `is_stub -> bool`
  - `load_policy(path: Path) -> ArchivePolicy`
  - `DEFAULT_POLICY_FILE: Final[Path]` — `decision_lab/config/archive.toml`
  - `DEFAULT_LAG_SECONDS: Final = 720`, `DEFAULT_WINDOW_SECONDS: Final = 172_800`, `DEFAULT_CLUSTER_FLOOR: Final = Decimal("0.25")`

**The floor's value, and where it came from.** `Decimal("0.25")` is the operating point measured on
2026-10-05 over 401 likely-duplicate pairs against 300 random controls: normalised short text
separates same-event pairs (0.308) from unrelated ones (0.177) by 1.74×, against bodies' 1.08×.
It is the **best** lexical operating point and it still false-merges 15% of unrelated pairs, which
is why §6.8.2.1 keys on fields and uses the floor only as a guard. It is configuration, not a
constant, and it rides in `archive_digest` so two floors are two corpora.

**Why the lag is 12 minutes and never zero.** §6.7 rejects `observed_at = published_at` outright:
it grants the panel the headline at the instant of publication, which is optimistic in the one
direction that inflates a score. Twelve minutes is the figure §6.6's own example header carries.

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_archive_config.py
"""`archive.toml`: the summariser binding, and the three numbers that ride in `archive_digest`.

Every refusal here happens before a single provider call, for the same reason `candidates.py`
validates a matrix before any spend: a pass that discovers its binding is wrong on row 9 000 has
spent 9 000 calls finding out.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import (
    DEFAULT_CLUSTER_FLOOR,
    DEFAULT_POLICY_FILE,
    ArchivePolicy,
    load_policy,
)
from tradebot.core.errors import ConfigError

GOOD = """
[summarizer]
provider = "stub"
model = "stub-summary"

[archive]
observed_at_lag_seconds = 720
news_window_seconds = 172800
cluster_floor = "0.25"
max_summary_chars = 280
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "archive.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_file_loads_and_binds_the_stub() -> None:
    """The suite stays offline and free, consistent with the bot's own rule that real models are
    off unless asked for. A shipped file binding a real endpoint would make `pytest` spend money."""
    policy = load_policy(DEFAULT_POLICY_FILE)
    assert policy.is_stub
    assert policy.cluster_floor == DEFAULT_CLUSTER_FLOOR


def test_a_loaded_policy_renders_the_observed_at_policy_string(tmp_path: Path) -> None:
    """§6.6's header field, and §6.7's banner reads it. The format is the spec's own."""
    policy = load_policy(write(tmp_path, GOOD))
    assert policy.observed_at_policy == "published_at+00:12:00"
    assert policy.lag == timedelta(minutes=12)
    assert policy.window == timedelta(hours=48)


def test_a_zero_lag_is_refused_by_name(tmp_path: Path) -> None:
    """§6.7 rejects `observed_at = published_at`: it grants the panel the headline at the instant
    of publication, which is optimistic in the one direction that inflates a score."""
    with pytest.raises(ConfigError, match="observed_at_lag_seconds"):
        load_policy(write(tmp_path, GOOD.replace("= 720", "= 0")))


def test_a_floor_outside_zero_to_one_is_refused(tmp_path: Path) -> None:
    """A similarity is a cosine. A floor of 1.4 clusters nothing and reads as "clustering is on"."""
    for bad in ("0", "1.4", "-0.2"):
        with pytest.raises(ConfigError, match="cluster_floor"):
            load_policy(write(tmp_path, GOOD.replace('"0.25"', f'"{bad}"')))


def test_a_float_floor_is_refused_naming_the_path(tmp_path: Path) -> None:
    """`tomllib` parses `0.25` as a Python float, and a binary-rounded floor would make the
    digest — and therefore `corpus_id` — depend on a value nobody can type exactly. Quoted
    strings only, the rule `matrices.dumps` already applies one package over."""
    with pytest.raises(ConfigError, match="cluster_floor"):
        load_policy(write(tmp_path, GOOD.replace('"0.25"', "0.25")))


def test_an_unknown_provider_is_refused_before_any_spend(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nosuchprovider"):
        load_policy(write(tmp_path, GOOD.replace('"stub"', '"nosuchprovider"')))


def test_a_missing_file_names_the_shipped_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="archive.toml"):
        load_policy(tmp_path / "absent.toml")


def test_malformed_toml_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_policy(write(tmp_path, "[summarizer\nprovider = "))


def test_a_max_summary_above_the_panels_slot_is_refused(tmp_path: Path) -> None:
    """`DEFAULT_EXCERPT_CHARS = 280` is the slot `NewsItem.view` renders. A policy permitting 600
    would mean the cut happens in the bot's renderer, silently, instead of in our refusal."""
    with pytest.raises(ConfigError, match="max_summary_chars"):
        load_policy(write(tmp_path, GOOD.replace("= 280", "= 600")))


def test_defaults_apply_when_the_archive_table_is_absent(tmp_path: Path) -> None:
    """A policy file naming only the binding is complete: the three reading numbers have
    defaults, and the digest records whichever were in force."""
    policy = load_policy(write(tmp_path, '[summarizer]\nprovider = "stub"\nmodel = "stub-summary"\n'))
    assert policy.cluster_floor == DEFAULT_CLUSTER_FLOOR
    assert policy.lag == timedelta(minutes=12)


def test_the_policy_model_rejects_an_unknown_key(tmp_path: Path) -> None:
    """`DomainModel` forbids extras, so a mistyped `cluster_flor` is a refusal rather than a
    silently-ignored line that leaves the measured default in force."""
    with pytest.raises(ConfigError):
        load_policy(write(tmp_path, GOOD.replace("cluster_floor", "cluster_flor")))


def test_a_policy_built_directly_still_validates() -> None:
    with pytest.raises(ValueError):
        ArchivePolicy(
            provider_id="stub",
            model="stub-summary",
            observed_at_lag_seconds=-1,
            news_window_seconds=172_800,
            cluster_floor=Decimal("0.25"),
            max_summary_chars=280,
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.archive.config'`

- [ ] **Step 3: Write `config.py`**

```python
# decision_lab/archive/config.py
"""`archive.toml` — the summariser binding and the three numbers a replay reads an archive by.

Four things live here because all four end up inside `archive_digest` (§6.6) and therefore inside
`corpus_id`: which model wrote the summaries, which prompt it was given, how `observed_at` was
derived, and the window and floor the feed reads them through. Two archives differing in any one
of them are two experiments, and the digest is what stops them being compared as one.

Every refusal happens before a single provider call, for the reason `candidates.py` validates a
matrix before any spend: a pass that discovers its binding is wrong on row 9 000 has spent 9 000
calls finding out. The binding is checked against the bot's own `PRESETS`, so an unknown provider
id is a refusal here rather than a `KeyError` at the first call.

The floor's value is measured, not chosen: `Decimal("0.25")` is the operating point from the
2026-10-05 measurement in §6.8.2.1 — normalised short text separates same-event pairs at 0.308
from unrelated ones at 0.177. It still false-merges 15% of unrelated pairs, which is exactly why
§6.8.2.1 keys clustering on the extracted fields and uses the floor only as a guard.

Failure semantics: every fault is `ConfigError` naming the field. Nothing here degrades to a
default on a malformed value — a summarising pass is a measuring instrument, and an instrument
that quietly substituted a number would produce an archive whose digest describes a policy that
was not applied.
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import field_validator

from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel, Money, parse_money
from tradebot.decision.providers.registry import PRESETS
from tradebot.news.normalize import DEFAULT_EXCERPT_CHARS

#: §6.6's example header carries `published_at+00:12:00`, and §6.7 rejects a zero lag outright.
DEFAULT_LAG_SECONDS: Final = 720

#: §6.8.1's window. 48 hours, because a replay measuring a panel the bot does not run measures
#: the wrong panel — `NewsStore.select`'s own `DEFAULT_LOOKBACK`.
DEFAULT_WINDOW_SECONDS: Final = 172_800

#: §6.8.2.1, measured 2026-10-05. See the module docstring.
DEFAULT_CLUSTER_FLOOR: Final = Decimal("0.25")

#: The shipped policy. Binds the stub, so the suite is offline and costs nothing.
DEFAULT_POLICY_FILE: Final = Path(__file__).parent.parent / "config" / "archive.toml"


class ArchivePolicy(DomainModel):
    """How one archive was summarised, and how a replay reads it."""

    provider_id: str
    model: str
    observed_at_lag_seconds: int = DEFAULT_LAG_SECONDS
    news_window_seconds: int = DEFAULT_WINDOW_SECONDS
    cluster_floor: Money = DEFAULT_CLUSTER_FLOOR
    max_summary_chars: int = DEFAULT_EXCERPT_CHARS

    @field_validator("observed_at_lag_seconds")
    @classmethod
    def _lag_is_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "observed_at_lag_seconds must be positive. §6.7 rejects observed_at = "
                "published_at: it grants the panel the headline at the instant of publication, "
                "which is optimistic in the one direction that inflates a score"
            )
        return value

    @field_validator("news_window_seconds")
    @classmethod
    def _window_is_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("news_window_seconds must be positive; a zero window selects nothing")
        return value

    @field_validator("cluster_floor")
    @classmethod
    def _floor_is_a_similarity(cls, value: Money) -> Money:
        if not Decimal(0) < value <= Decimal(1):
            raise ValueError(
                f"cluster_floor must be in (0, 1]; {value} is not a cosine similarity. A floor "
                "above 1 clusters nothing while reading as though clustering were on"
            )
        return value

    @field_validator("max_summary_chars")
    @classmethod
    def _summary_fits_the_panels_slot(cls, value: int) -> int:
        if not 0 < value <= DEFAULT_EXCERPT_CHARS:
            raise ValueError(
                f"max_summary_chars must be in (0, {DEFAULT_EXCERPT_CHARS}]; {value} is outside "
                "the slot NewsItem.view renders, so the cut would happen in the bot's renderer "
                "rather than in our refusal"
            )
        return value

    @property
    def lag(self) -> timedelta:
        return timedelta(seconds=self.observed_at_lag_seconds)

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.news_window_seconds)

    @property
    def observed_at_policy(self) -> str:
        """§6.6's header field, in the spec's own format."""
        hours, rest = divmod(self.observed_at_lag_seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"published_at+{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def is_stub(self) -> bool:
        """A stub binding makes the pass a plumbing check, and the *binding* decides — never a
        flag. The same argument §7.2 makes about a matrix: a flag leaves an archive whose digest
        records a real model beside rows that are canned text."""
        return self.provider_id == "stub"


def load_policy(path: Path) -> ArchivePolicy:
    """Read and validate `archive.toml`. Every refusal here precedes any provider call."""
    if not path.is_file():
        raise ConfigError(
            f"no archive policy at {path}. The tool ships one at {DEFAULT_POLICY_FILE}, which "
            "binds the offline stub"
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    binding = document.get("summarizer", {})
    settings: dict[str, Any] = {
        "provider_id": str(binding.get("provider", "")),
        "model": str(binding.get("model", "")),
    }
    for key, value in document.get("archive", {}).items():
        settings[key] = _money_field(path, key, value) if key == "cluster_floor" else value

    if settings["provider_id"] not in PRESETS and settings["provider_id"] != "stub":
        known = ", ".join(sorted({*PRESETS, "stub"}))
        raise ConfigError(
            f"{path} binds the summarizer to unknown provider "
            f"{settings['provider_id']!r}; known providers: {known}"
        )
    try:
        return ArchivePolicy.model_validate(settings)
    except ValueError as exc:
        raise ConfigError(f"{path} is not a usable archive policy: {exc}") from exc


def _money_field(path: Path, key: str, value: object) -> Decimal:
    """A money-shaped field arrives as text on purpose.

    `tomllib` renders `0.25` as a Python `float`, and a binary-rounded floor would make
    `archive_digest` — and so `corpus_id` — depend on a value nobody can type exactly. The rule
    `matrices.dumps` already applies one package over, enforced on the way in rather than out.

    `parse_money` raises `MoneyError` on a float and re-raises unreadable *text* as `ValueError`;
    both are caught here because at this boundary either one is the operator's TOML being wrong,
    and both must reach them as a `ConfigError` naming the key.
    """
    try:
        return parse_money(value)
    except Exception as exc:
        raise ConfigError(
            f"{path} gives {key} = {value!r}, which is not a decimal written as a string. "
            f'Quote it — {key} = "0.25" — because a TOML float is binary-rounded inside the '
            "digest that keys corpus_id"
        ) from exc
```

**Note for the implementer:** `parse_money`'s two failure modes are deliberately different in the
bot (`MoneyError` for a `float`, `ValueError` for unreadable text — see CLAUDE.md). `_money_field`
catches both because at *this* boundary both mean the same thing: the operator's TOML is wrong.
Do not narrow that `except` to `ValueError` without first checking `MoneyError`'s base class in
`tradebot/core/money.py`; the test `test_a_float_floor_is_refused_naming_the_path` is what tells
you.

- [ ] **Step 4: Write the shipped policy file**

```toml
# decision_lab/config/archive.toml
# The summarising pass's binding, and the three numbers a replay reads an archive by.
#
# Everything here rides in `archive_digest` (§6.6) and therefore in `corpus_id`: two archives
# differing in any one of these are two experiments. Editing this file after an archive is
# published does not change that archive — re-run `archive publish` to re-stamp it.
#
# The binding ships as the offline stub, so `pytest` and the demo cost nothing. Point it at a
# real provider to run the pass for real; the provider's key is read from the environment by
# name (`OPENROUTER_API_KEY` for openrouter) and is never written here.

[summarizer]
provider = "stub"
model = "stub-summary"

[archive]
# §6.7: `observed_at = published_at + lag`. Zero is refused — it grants the panel the headline at
# the instant of publication, which is optimistic in the one direction that inflates a score.
observed_at_lag_seconds = 720

# §6.8.1: 48 hours, matching the bot's own `NewsStore.DEFAULT_LOOKBACK`. A replay measuring a
# panel the bot does not run measures the wrong panel.
news_window_seconds = 172800

# §6.8.2.1, measured 2026-10-05 over 401 likely-duplicate pairs against 300 controls: normalised
# short text separates same-event pairs (0.308) from unrelated ones (0.177) by 1.74x, against
# full bodies' 1.08x. This is the best lexical operating point and it still false-merges 15% of
# unrelated pairs — which is why the cluster *key* is the extracted fields and this is only a
# guard. Quoted, because a TOML float would be binary-rounded inside the digest.
cluster_floor = "0.25"

# `DEFAULT_EXCERPT_CHARS`. A summary longer than the panel's slot is our refusal, visible and
# tallied, rather than the renderer's silent truncation.
max_summary_chars = 280
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_config.py -q`
Expected: PASS, 12 tests

- [ ] **Step 6: Commit**

```bash
git add decision_lab/archive/config.py decision_lab/config/archive.toml \
        decision_lab/tests/test_archive_config.py
git commit -m "feat(decision_lab): archive.toml - the summariser binding and the reading policy

Four values, all of which end up inside archive_digest and therefore inside
corpus_id: the model, the prompt digest, the observed_at lag, and the window
and floor the feed reads through. Every refusal precedes any provider call,
for the reason candidates.py validates a matrix before any spend.

The floor is 0.25 because it was measured (6.8.2.1, 2026-10-05), and it is
quoted because a TOML float would be binary-rounded inside the digest.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: The prompt, the response contract, and `parse_summary`

**Files:**
- Create: `decision_lab/archive/summarize.py` (prompt and parsing only; the pass itself is Task 4)
- Test: `decision_lab/tests/test_archive_summarize.py`

**Interfaces:**
- Consumes: `EventType`, `Modality` from Task 1 (`decision_lab/archive/fields.py`); `ArchivePolicy` from Task 2.
- Produces:
  - `SUMMARY_SYSTEM: Final[str]` — the system prompt, verbatim and versioned
  - `prompt_digest(system: str) -> str` — `blake2s` over the system prompt, 16 bytes hex
  - `summary_user(*, title: str, body: str) -> str` — the user message; **takes nothing else**
  - `SummaryResponse(DomainModel)`: `summary: str`, `event_type: EventType`, `entities: tuple[str, ...]`, `assets: tuple[str, ...]`, `magnitudes: tuple[str, ...]`, `modality: Modality`
  - `parse_summary(text: str, *, max_chars: int) -> SummaryResponse` — raises `SchemaViolationError`
  - `ALLOWED_ASSETS: Final = ("BTC", "ETH", "OTHER", "NONE")`

**Why the prompt builder's signature is the hindsight closure.** §6.5 closes the channel four ways
and only one is checkable, but the second — *"by what it is not given"* — is enforced by this
function taking two strings. There is no instrument parameter, no market-data parameter and no
sibling-article parameter to pass, so the §16.2 test that asserts on the *call arguments* is
asserting a property of the type rather than of a prompt string somebody might edit.

**Why `_extract_object` is imported from the bot's `seat.py`.** Tolerating a code fence or a
sentence of prose around a JSON object is provider-habit handling, not domain logic, and §2.4 says
nothing about how the bot reads a model's reply is restated here. A second extractor would drift:
the bot's is a greedy first-brace-to-last-brace match precisely so a nested object survives
surrounding prose, and a tidier re-implementation would quietly reject answers the bot accepts.
It is a private name, which is a deliberate, documented reach rather than an accident.

**Review Focus items 2 and 4 are owned here.** A summary over the panel's slot is refused with its
length named, and an invented `event_type` or `modality` is refused rather than coerced.

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_archive_summarize.py
"""§6.5's response contract: what a summary may carry, and what it is refused for.

The prompt is asserted structurally rather than by its wording wherever possible — a test that
greps the prompt text for "no outlook" passes on a prompt that says it and then asks for one
anyway. What *is* asserted on the text is the inverse: that the three things §6.5 forbids giving
the model have no way to reach it, which is a property of `summary_user`'s signature.
"""

from __future__ import annotations

import json

import pytest

from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.summarize import (
    SUMMARY_SYSTEM,
    SummaryResponse,
    parse_summary,
    prompt_digest,
    summary_user,
)
from tradebot.core.errors import SchemaViolationError

GOOD = {
    "summary": "Kraken received EMI authorisation from the FCA, covering e-money services in the UK.",
    "event_type": "REGULATORY",
    "entities": ["Kraken", "FCA"],
    "assets": ["NONE"],
    "magnitudes": [],
    "modality": "CONFIRMED",
}


def test_a_well_formed_reply_parses() -> None:
    parsed = parse_summary(json.dumps(GOOD), max_chars=280)
    assert parsed.event_type is EventType.REGULATORY
    assert parsed.modality is Modality.CONFIRMED
    assert parsed.entities == ("Kraken", "FCA")


def test_a_fenced_reply_parses_because_a_fence_is_a_provider_habit() -> None:
    """The bot tolerates this for a seat vote; an archive pass meets the same habit."""
    fenced = "Here is the summary:\n```json\n" + json.dumps(GOOD) + "\n```\n"
    assert parse_summary(fenced, max_chars=280).summary == GOOD["summary"]


def test_a_reply_with_no_json_at_all_is_a_schema_violation() -> None:
    with pytest.raises(SchemaViolationError):
        parse_summary("I cannot summarise this article.", max_chars=280)


def test_truncated_json_is_a_schema_violation_rather_than_a_partial_summary() -> None:
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(GOOD)[:60], max_chars=280)


def test_a_summary_longer_than_the_slot_is_refused_with_its_length(
) -> None:
    """Review Focus 2. `NewsItem.view` would carry an overrun into the panel's 280-character slot
    and the cut would be the renderer's, silently. Ours, named, and tallied instead."""
    long = dict(GOOD, summary="x" * 281)
    with pytest.raises(SchemaViolationError, match="281"):
        parse_summary(json.dumps(long), max_chars=280)


def test_a_summary_at_exactly_the_slot_is_accepted() -> None:
    exact = dict(GOOD, summary="x" * 280)
    assert len(parse_summary(json.dumps(exact), max_chars=280).summary) == 280


def test_an_empty_summary_is_refused() -> None:
    """A model that answered with an empty string answered nothing, and an empty excerpt in the
    archive reads to the panel as a headline with no content rather than as a missing row."""
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(dict(GOOD, summary="   ")), max_chars=280)


def test_an_invented_event_type_is_refused_rather_than_coerced() -> None:
    """Review Focus 4. `event_type` is a third of the §6.8.2.1 cluster key, so a free-text value
    splits one event across two slots and the agreement figures stop meaning anything."""
    with pytest.raises(SchemaViolationError, match="event_type"):
        parse_summary(json.dumps(dict(GOOD, event_type="regulatory-approval")), max_chars=280)


def test_an_invented_modality_is_refused_rather_than_coerced() -> None:
    """Review Focus 4, the half that bites hardest: §6.8.2's correction rule *dispatches* on
    modality, so an unrecognised value collapses a retraction behind the claim it retracts."""
    with pytest.raises(SchemaViolationError, match="modality"):
        parse_summary(json.dumps(dict(GOOD, modality="SPECULATIVE")), max_chars=280)


def test_an_unknown_asset_token_is_refused() -> None:
    """§6.5: the text either names an asset or it does not. Inferring one is the relevance
    filter's job and it is the bot's — a model writing "SOL" here would be answering a question
    the archive did not ask, and `KeywordRelevanceFilter` never reads this field."""
    with pytest.raises(SchemaViolationError, match="assets"):
        parse_summary(json.dumps(dict(GOOD, assets=["SOL"])), max_chars=280)


def test_an_extra_key_is_refused_so_sentiment_can_never_arrive() -> None:
    """§6.5's "what it never carries" list, enforced rather than requested. A model volunteering
    `sentiment` or `expected_impact` would become the fourteenth seat every candidate shares."""
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(dict(GOOD, sentiment="bullish")), max_chars=280)


def test_the_user_message_carries_only_the_title_and_the_body() -> None:
    """§6.5's "by what it is not given" closure, asserted on the signature's effect: there is no
    parameter through which an instrument, a price or a sibling article could arrive."""
    rendered = summary_user(title="Kraken licensed in the UK", body="The FCA granted Kraken...")
    assert "Kraken licensed in the UK" in rendered
    assert "The FCA granted Kraken" in rendered
    for leak in ("BTC/USDT", "ATR", "RSI", "position", "equity"):
        assert leak not in rendered


def test_the_system_prompt_names_the_schema_and_the_vocabularies() -> None:
    """The one thing worth asserting on the prompt's text: a model cannot answer in a closed
    vocabulary it was never shown, and a drifted prompt would refuse every row at parse time."""
    for member in Modality:
        assert member.value in SUMMARY_SYSTEM
    for member in EventType:
        assert member.value in SUMMARY_SYSTEM


def test_the_prompt_digest_moves_when_the_prompt_does() -> None:
    """It is a third of `archive_digest`, so an edited prompt is a different archive (§6.6)."""
    assert prompt_digest(SUMMARY_SYSTEM) != prompt_digest(SUMMARY_SYSTEM + " Be brief.")
    assert prompt_digest(SUMMARY_SYSTEM) == prompt_digest(SUMMARY_SYSTEM)


def test_the_response_model_is_the_only_validation() -> None:
    """Nothing restates a rule: `parse_summary` validates through the model, so adding a field
    constraint cannot be forgotten in a second place (the `forms.py` rule, one package over)."""
    with pytest.raises(ValueError):
        SummaryResponse.model_validate(dict(GOOD, event_type="NOPE"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_summarize.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.archive.summarize'`

- [ ] **Step 3: Write `summarize.py`'s prompt and parsing half**

```python
# decision_lab/archive/summarize.py
"""§6.5's summarising pass: a compressor, not an analyst.

The job is **de-editorialising and genuine compression**, not merely fitting 280 characters, and
that framing is measured rather than assumed (§6.5, revised 2026-09-22). Bitcoin.com's abstract is
the body's first sentence verbatim — a lede is whatever the writer opened with, not a summary.
CryptoSlate's is written and it *evaluates*: "Securing EMI authorization **empowers** Kraken to
introduce **innovative** crypto-focused financial products." That is the fourteenth seat this
section exists to prevent, arriving from the publisher instead of from our model, and it arrives
on one source only — so the two registers are not comparable and a seat could learn the publisher
rather than the news.

What a summary carries is facts stated in the article: the event as a classification, the named
entities, the assets the text names, magnitudes exactly as stated, and the modality. What it never
carries is sentiment polarity, price direction, market implication, or anything about what
happened after publication.

**The hindsight channel is closed four ways, and only one is checkable.** By prompt (restate only
what the text says). By what the model is *not given* — `summary_user` takes a title and a body,
and there is no parameter through which an instrument, the day's market data or a sibling article
could arrive, so that closure is a property of this signature rather than of a sentence somebody
could edit. By binding, so the stub serves it in tests. And by traceability, asserted on a sample
by n-gram overlap against the source body — the only one that produces a number.

`_extract_object` is imported from the bot's `seat.py` deliberately, private name and all.
Tolerating a code fence or a sentence of prose is provider-habit handling, not domain logic, and
§2.4 says nothing about how the bot reads a model's reply is restated here. The bot's extractor is
greedy from the first brace to the last precisely so a nested object survives surrounding prose; a
tidier second one would quietly refuse answers the bot accepts.

Failure semantics: every malformed reply is `SchemaViolationError` — the bot's own class for
exactly this — and the pass tallies it as a refusal rather than propagating. A refusal is a
*number on the report*, never a quiet substitution of the publisher's abstract: that abstract is
the baseline the pass is measured against, and using it as a fallback would make the measurement
circular.
"""

from __future__ import annotations

import hashlib
from typing import Final

from pydantic import field_validator

from decision_lab.archive.fields import EventType, Modality
from tradebot.core.errors import SchemaViolationError
from tradebot.core.schema import DomainModel
from tradebot.decision.seat import _extract_object

#: §6.5: the text either names an asset or it does not. Inferring one is the relevance filter's
#: job and that filter is the bot's, so the vocabulary here is deliberately tiny — a model writing
#: "SOL" is answering a question the archive did not ask, and nothing downstream reads this field
#: for relevance.
ALLOWED_ASSETS: Final = ("BTC", "ETH", "OTHER", "NONE")

SUMMARY_SYSTEM: Final = f"""\
You compress one news article into a short factual record. You are not an analyst.

Restate only what the article's own text says. You must not offer an outlook, a market
implication, a price direction, a sentiment, or any reference to events after this article was
published. If the article states something as expected, proposed or rumoured, record it that way
rather than as fact.

Reply with JSON only, matching this schema exactly. No prose, no code fences.

{{
  "summary": "<at most {{max_chars}} characters, plain factual prose, no evaluation>",
  "event_type": "<one of: {" | ".join(m.value for m in EventType)}>",
  "entities": ["<who acted, and on whom, as the article names them>"],
  "assets": ["<only those the text names, from: {" | ".join(ALLOWED_ASSETS)}>"],
  "magnitudes": ["<figures exactly as stated, e.g. \\"$2.2B inflow\\"; omit if none>"],
  "modality": "<one of: {" | ".join(m.value for m in Modality)}>"
}}

Rules you must follow:
- A magnitude is copied, never characterised. "$2.2B inflow" is in the article; "large inflow" is
  a judgement about it.
- CONFIRMED means the article reports the event as having happened. PROPOSED means announced or
  intended. RUMOURED means unconfirmed or sourced to speculation. DENIED means a party denies it.
  CORRECTED means this article corrects or walks back an earlier claim.
- Use "NONE" in assets when the text names no crypto asset. Do not infer one from context.
- Add no keys beyond the schema.
"""


def prompt_digest(system: str) -> str:
    """A third of `archive_digest` (§6.6), so an edited prompt is a different archive."""
    return hashlib.blake2s(system.encode("utf-8"), digest_size=16).hexdigest()


def summary_user(*, title: str, body: str) -> str:
    """The user message. Two strings, and that is §6.5's second closure made structural.

    There is no instrument parameter, no market-data parameter and no sibling-article parameter,
    so the model cannot tilt toward BTC because it does not know we trade BTC. §16.2 asserts this
    on the recorded call arguments.
    """
    return f"TITLE:\n{title.strip()}\n\nARTICLE:\n{body.strip()}\n"


class SummaryResponse(DomainModel):
    """One summarised article, validated. `DomainModel` forbids extras, which is what keeps
    §6.5's "what it never carries" list enforced rather than merely requested: a model
    volunteering `sentiment` or `expected_impact` is refused, not ignored."""

    summary: str
    event_type: EventType
    entities: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    magnitudes: tuple[str, ...] = ()
    modality: Modality

    @field_validator("summary")
    @classmethod
    def _summary_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "summary is empty. An empty excerpt reads to the panel as a headline with no "
                "content rather than as a missing row"
            )
        return value.strip()

    @field_validator("assets")
    @classmethod
    def _assets_are_named_not_inferred(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(ALLOWED_ASSETS))
        if unknown:
            raise ValueError(
                f"assets names {unknown}, which is outside {list(ALLOWED_ASSETS)}. The text "
                "either names an asset or it does not; inferring one is the relevance filter's "
                "job and that filter is the bot's"
            )
        return value


def parse_summary(text: str, *, max_chars: int) -> SummaryResponse:
    """Extract and validate one summariser reply.

    `max_chars` is checked here rather than on the model because it is *policy* — `archive.toml`
    sets it and it rides in the digest — while the model's own constraints are facts about the
    schema. The over-length refusal names the length: the cut has to be visible and tallied, not
    `NewsItem.view` quietly carrying an overrun into the panel's slot.
    """
    try:
        payload = _extract_object(text)
    except SchemaViolationError:
        raise
    try:
        parsed = SummaryResponse.model_validate(payload)
    except ValueError as exc:
        raise SchemaViolationError(f"reply does not match the summary schema: {exc}") from exc
    if len(parsed.summary) > max_chars:
        raise SchemaViolationError(
            f"summary is {len(parsed.summary)} characters, over the {max_chars} the panel's slot "
            "holds. Refused rather than truncated, so the cut is ours and counted"
        )
    return parsed
```

**Note for the implementer:** `SUMMARY_SYSTEM` uses `{{max_chars}}` as a literal placeholder
inside an f-string — the doubled braces render as single ones. The pass in Task 4 substitutes it
with `SUMMARY_SYSTEM.replace("{max_chars}", str(policy.max_summary_chars))`. Do **not** make it a
`.format()` call: the prompt body contains JSON braces, and `format` would raise on every one of
them. `prompt_digest` is taken over the **substituted** system prompt in Task 4, so changing
`max_summary_chars` mints a new archive identity, which is correct — it changes what the model was
asked for.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_summarize.py -q`
Expected: PASS, 15 tests

- [ ] **Step 5: Confirm the no-float guard still holds over the new modules**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_discipline.py -q`
Expected: PASS — `FLOAT_EXEMPT` is still empty. If this fails, something annotated a `float`;
fix the annotation rather than adding an exemption.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/archive/summarize.py decision_lab/tests/test_archive_summarize.py
git commit -m "feat(decision_lab): the summariser prompt and its response contract

The job is de-editorialising and genuine compression, not fitting 280
characters: one source's abstract is the body's first sentence and the
other's evaluates, so neither is a summary (6.5, revised 2026-09-22).

6.5's second hindsight closure is made structural here - summary_user takes
a title and a body, so there is no parameter through which an instrument,
the day's prices or a sibling article could arrive. Both vocabularies are
closed, because event_type is a third of the cluster key and 6.8.2's
correction rule dispatches on modality.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: The pass — `RawArticle`'s extracted fields and `summarize_store`

**Files:**
- Modify: `decision_lab/archive/store.py` — `RawArticle` gains five fields, all defaulted
- Modify: `decision_lab/archive/summarize.py` — add the pass below the prompt half
- Test: `decision_lab/tests/test_archive_summarize_pass.py`

**Interfaces:**
- Consumes: `SUMMARY_SYSTEM`, `summary_user`, `parse_summary`, `prompt_digest`, `SummaryResponse` (Task 3); `ArchivePolicy` (Task 2); `EventType`, `Modality` (Task 1); `RawStore`, `RawArticle`, `StoreLock` (existing).
- Produces:
  - `RawArticle` additionally carries `event_type: EventType | None = None`, `entities: tuple[str, ...] = ()`, `assets: tuple[str, ...] = ()`, `magnitudes: tuple[str, ...] = ()`, `modality: Modality | None = None`, and the property `is_summarised -> bool`
  - `SummaryReport(DomainModel)`: `rows`, `already`, `requested`, `summarised`, `refused`, `bodyless`, `cost_usd: Money`, `prompt_tokens`, `completion_tokens`; method `added(**counts: int) -> SummaryReport`
  - `summarize_store(provider: LLMProvider, store: RawStore, policy: ArchivePolicy, clock: Clock, *, limit: int | None = None, pause: timedelta = DEFAULT_PAUSE) -> SummaryReport`
  - `system_for(policy: ArchivePolicy) -> str` — `SUMMARY_SYSTEM` with `{max_chars}` substituted
  - `DEFAULT_SUMMARY_RETRIES: Final = 2`

**Why the fields are defaulted and nullable.** 14 484 rows are already on disk and were written
before these fields existed. `EventType | None = None` validates every one of them unchanged;
an enum defaulted to `""` would refuse the entire store on the first read. The two enum fields are
nullable rather than carrying a sentinel member, because `OTHER` means *classified as other* and
`None` means *never summarised* — one is a measurement and the other is its absence, and
`is_summarised` is the predicate that keeps them apart.

**Why a refusal never falls back to the abstract.** The publisher's abstract is the **baseline the
pass is measured against** (§6.5): it was written at publication and cannot contain hindsight. Using
it when the model fails would make the measurement circular — and on CryptoSlate it would import
exactly the evaluative register the pass exists to strip. A refused row keeps `summary = ""`,
stays out of the published archive, and is a number on the report.

**Review Focus items 1 and 5 are owned here.**

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_archive_summarize_pass.py
"""The pass over a store: resumable, idempotent, batched, and honest about what it refused.

Every test here drives `StubLLMProvider` with scripted replies, so the suite is offline and free.
`provider.calls` is what the §16.2 contamination tests later read; it is used here for the
cheaper question of *how many* calls a pass made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.archive.summarize import summarize_store, system_for
from tradebot.core.clock import ManualClock
from tradebot.decision.providers.stub import FAIL, StubLLMProvider

GOOD = json.dumps(
    {
        "summary": "Kraken received EMI authorisation from the FCA.",
        "event_type": "REGULATORY",
        "entities": ["Kraken", "FCA"],
        "assets": ["NONE"],
        "magnitudes": [],
        "modality": "CONFIRMED",
    }
)

POLICY = ArchivePolicy(provider_id="stub", model="stub-summary")


def row(n: int, *, body: str = "The FCA granted Kraken an EMI licence today.", summary: str = "") -> RawArticle:
    return RawArticle(
        source_id="cryptoslate",
        url=f"https://x.test/{n}",
        title=f"Story {n}",
        body=body,
        abstract="Securing EMI authorization empowers Kraken to innovate.",
        published_at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        fetched_at=datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
        summary=summary,
    )


def store_with(tmp_path: Path, rows: tuple[RawArticle, ...]) -> RawStore:
    store = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate")
    for article in rows:
        store.append(article)
    return store


def clock() -> ManualClock:
    return ManualClock(datetime(2026, 10, 5, tzinfo=UTC))


@pytest.mark.asyncio
async def test_a_pass_summarises_every_bodyless_row_and_writes_the_fields(tmp_path: Path) -> None:
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.requested, report.summarised, report.refused) == (2, 2, 0)
    back = store.read_all()
    assert all(a.is_summarised for a in back)
    assert back[0].event_type is EventType.REGULATORY
    assert back[0].modality is Modality.CONFIRMED
    assert back[0].entities == ("Kraken", "FCA")


@pytest.mark.asyncio
async def test_a_second_pass_asks_for_nothing(tmp_path: Path) -> None:
    """Resumable and idempotent, the property `archive build` has and for the same reason: a pass
    interrupted at row 9 000 must not re-pay for the first 9 000."""
    store = store_with(tmp_path, (row(1), row(2)))
    first = StubLLMProvider([GOOD])
    await summarize_store(first, store, POLICY, clock(), pause=timedelta(0))

    second = StubLLMProvider([GOOD])
    report = await summarize_store(second, store, POLICY, clock(), pause=timedelta(0))

    assert second.calls == []
    assert (report.already, report.requested) == (2, 0)


@pytest.mark.asyncio
async def test_limit_commits_a_batch_and_leaves_the_rest(tmp_path: Path) -> None:
    """`rewrite` replaces the store from rows read at the start of the pass, so a long pass risks
    everything it collected — the same reason `backfill` is run in `--limit` batches."""
    store = store_with(tmp_path, (row(1), row(2), row(3)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), limit=2, pause=timedelta(0))

    assert report.summarised == 2
    assert sum(1 for a in store.read_all() if a.is_summarised) == 2


@pytest.mark.asyncio
async def test_a_blank_body_is_never_sent_and_never_summarised(tmp_path: Path) -> None:
    """Review Focus 5. A model given nothing invents something, and the 418-character minimum in
    this archive was *measured* rather than guaranteed by the type."""
    store = store_with(tmp_path, (row(1, body="   \n "), row(2)))
    provider = StubLLMProvider([GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.bodyless, report.summarised) == (1, 1)
    assert len(provider.calls) == 1
    blank = next(a for a in store.read_all() if a.url.endswith("/1"))
    assert blank.summary == "" and not blank.is_summarised


@pytest.mark.asyncio
async def test_a_malformed_reply_gets_one_repair_attempt_then_is_refused(tmp_path: Path) -> None:
    """Review Focus 1. Prose, a fence, a truncation or a refusal — every real run meets one in
    the first hundred rows. The bot's own `REPAIR_INSTRUCTION` is reused, never restated."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["I cannot summarise this.", "Still cannot."])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.refused, report.summarised) == (1, 0)
    assert len(provider.calls) == 2
    assert "rejected" in provider.calls[1].user


@pytest.mark.asyncio
async def test_a_repair_that_succeeds_is_counted_as_summarised(tmp_path: Path) -> None:
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["not json at all", GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert (report.summarised, report.refused) == (1, 0)


@pytest.mark.asyncio
async def test_a_refused_row_never_falls_back_to_the_publishers_abstract(tmp_path: Path) -> None:
    """§6.5: the abstract is the *baseline the pass is measured against*. Using it on failure
    makes the measurement circular — and on CryptoSlate it imports the evaluative register the
    pass exists to strip ("empowers", "innovative")."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider(["no.", "no."])

    await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    stored = store.read_all()[0]
    assert stored.summary == ""
    assert "empowers" not in stored.summary


@pytest.mark.asyncio
async def test_a_provider_failure_is_retried_within_a_budget(tmp_path: Path) -> None:
    """`ProviderError` is a `RetryableError`, and the class *is* the handling instruction. The
    first full-year crawl died on a single timeout at its 1 430th row; this one does not."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider([FAIL, GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.summarised == 1


@pytest.mark.asyncio
async def test_a_provider_down_past_the_budget_refuses_the_row_without_ending_the_pass(
    tmp_path: Path,
) -> None:
    """One dead row must not end a 14 000-row pass. A refusal is a tally, not an exception —
    `collect`'s rule, and the store is rewritten with what did succeed."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([FAIL, FAIL, FAIL, GOOD])

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.refused == 1
    assert report.summarised == 1


@pytest.mark.asyncio
async def test_the_report_totals_what_the_pass_spent(tmp_path: Path) -> None:
    """The cost projection on the CLI is this number over a sample, and a pass that cannot say
    what it spent cannot inform the decision to run the next one."""
    store = store_with(tmp_path, (row(1), row(2)))
    provider = StubLLMProvider([GOOD], cost_usd=Decimal("0.0004"))

    report = await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert report.cost_usd == Decimal("0.0008")
    assert report.prompt_tokens > 0


@pytest.mark.asyncio
async def test_the_system_prompt_carries_the_policys_character_limit(tmp_path: Path) -> None:
    """`max_summary_chars` is policy and rides in the digest, so the model is told the number the
    parser will hold it to — otherwise every row is refused for an overrun nobody asked about."""
    store = store_with(tmp_path, (row(1),))
    provider = StubLLMProvider([GOOD])

    await summarize_store(provider, store, POLICY, clock(), pause=timedelta(0))

    assert "280" in provider.calls[0].system
    assert "{max_chars}" not in provider.calls[0].system


def test_system_for_substitutes_without_format() -> None:
    """The prompt body is full of JSON braces; `.format()` would raise on every one."""
    rendered = system_for(ArchivePolicy(provider_id="stub", model="m", max_summary_chars=200))
    assert "200" in rendered and "{max_chars}" not in rendered


def test_the_two_enum_fields_distinguish_never_summarised_from_classified_other() -> None:
    """`None` means the pass never ran on this row; `OTHER` means it ran and classified. One is
    the absence of a measurement and the other is a measurement, and `is_summarised` is what
    keeps them apart — a sentinel member would make a 14 484-row store indistinguishable from a
    summarised one."""
    assert not row(1).is_summarised
    summarised = row(1).model_copy(
        update={"summary": "x", "event_type": EventType.OTHER, "modality": Modality.CONFIRMED}
    )
    assert summarised.is_summarised


def test_every_already_collected_row_still_validates(tmp_path: Path) -> None:
    """The five fields are additive and defaulted. 14 484 rows are on disk, written before they
    existed, and a non-defaulted addition would refuse the whole store on the first read."""
    legacy = {
        "source_id": "cryptoslate",
        "url": "https://x.test/legacy",
        "title": "Story",
        "body": "Body text.",
        "published_at": "2025-03-04T09:02:00Z",
        "fetched_at": "2025-03-04T10:00:00Z",
    }
    article = RawArticle.model_validate(legacy)
    assert article.event_type is None and not article.is_summarised
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_summarize_pass.py -q`
Expected: FAIL — `ImportError: cannot import name 'summarize_store'`

- [ ] **Step 3: Extend `RawArticle`**

In `decision_lab/archive/store.py`, add the import and the five fields. Replace the `summary`
field line with the block below, and extend the class docstring:

```python
from decision_lab.archive.fields import EventType, Modality
```

```python
    summary: str = ""
    #: §6.5's extracted fields, written by the same pass that writes `summary`. They are not
    #: decoration: `event_type`, `entities` and `assets` are the §6.8.2.1 cluster key, and
    #: `modality` is what stops a correction being collapsed behind the claim it corrects.
    #:
    #: Defaulted and nullable because 14 484 rows are already on disk, written before these
    #: fields existed — a non-defaulted addition would refuse the whole store on its first read.
    #: Nullable rather than carrying a sentinel member, because `EventType.OTHER` means
    #: *classified as other* while `None` means *never summarised*: one is a measurement and the
    #: other is its absence. `is_summarised` is the predicate that keeps them apart.
    event_type: EventType | None = None
    entities: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    magnitudes: tuple[str, ...] = ()
    modality: Modality | None = None

    @property
    def is_summarised(self) -> bool:
        """True once §6.5's pass has written this row. Both halves, because a row carrying a
        summary but no classification cannot be clustered and must be re-asked."""
        return bool(self.summary) and self.event_type is not None and self.modality is not None
```

- [ ] **Step 4: Write the pass, appended to `summarize.py`**

```python
#: Attempts per row, including the repair. Two: one call, one repair, then the row is refused and
#: counted. A third would triple the spend on a model that is answering in prose today.
DEFAULT_SUMMARY_RETRIES: Final = 2


class SummaryReport(DomainModel):
    """What one pass did. `refused` against `summarised` is what the report prints.

    `already` is what makes a batched run legible: a third invocation reporting
    `already: 9000, requested: 1000` is resuming, not re-paying.
    """

    rows: int = 0
    already: int = 0
    requested: int = 0
    summarised: int = 0
    refused: int = 0
    bodyless: int = 0
    cost_usd: Money = ZERO
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def added(self, **counts: int) -> SummaryReport:
        return self.model_copy(update={k: getattr(self, k) + v for k, v in counts.items()})


def system_for(policy: ArchivePolicy) -> str:
    """`SUMMARY_SYSTEM` with the policy's character limit substituted.

    `str.replace`, never `.format()`: the prompt body is full of JSON braces and `format` would
    raise on every one of them. The *substituted* text is what `prompt_digest` is taken over, so
    changing `max_summary_chars` mints a new archive identity — correct, because it changes what
    the model was asked for.
    """
    return SUMMARY_SYSTEM.replace("{max_chars}", str(policy.max_summary_chars))


async def summarize_store(
    provider: LLMProvider,
    store: RawStore,
    policy: ArchivePolicy,
    clock: Clock,
    *,
    limit: int | None = None,
    pause: timedelta = DEFAULT_PAUSE,
) -> SummaryReport:
    """Summarise every unsummarised row with a body. Resumable, batched, and never silent.

    Keyed on `RawArticle.is_summarised`, so a second invocation asks the provider for nothing and
    a pass interrupted at row 9 000 does not re-pay for the first 9 000 — the resume property
    `archive build` has, for the same reason.

    `limit` commits a batch. `rewrite` replaces the store from rows read at the *start* of a pass,
    so a single long pass risks everything it collected to one failure near the end; a 14 000-row
    job is a sequence of `--limit` batches, each of which commits. `StoreLock` is what stops two
    of them running at once: the second would read the same pre-batch state and write it back over
    the first's gains, both reporting success.

    A row is refused — and counted — rather than ending the pass. `ProviderError` is a
    `RetryableError` and the class *is* the handling instruction, so a transient failure is
    absorbed within `DEFAULT_SUMMARY_RETRIES`; a `SchemaViolationError` gets one repair attempt
    through the bot's own `REPAIR_INSTRUCTION` and is then a tally. Nothing falls back to the
    publisher's abstract: that is the baseline this pass is measured against (§6.5), and on
    CryptoSlate it carries exactly the evaluative register the pass exists to strip.
    """
    system = system_for(policy)
    with StoreLock(store):
        rows = store.read_all()
        report = SummaryReport(rows=len(rows))
        pending = [a for a in rows if not a.is_summarised]
        report = report.added(already=len(rows) - len(pending))

        updated: dict[str, RawArticle] = {}
        asked = 0
        for article in pending:
            if limit is not None and asked >= limit:
                break
            if not article.body.strip():
                # Review Focus 5: a model given nothing invents something, and the measured
                # 418-character minimum in this archive is a measurement, not a guarantee.
                report = report.added(bodyless=1)
                continue
            asked += 1
            report = report.added(requested=1)
            outcome = await _summarise_one(provider, policy, system, article, clock, pause)
            if outcome is None:
                report = report.added(refused=1)
                continue
            parsed, spend = outcome
            updated[article.url] = article.model_copy(
                update={
                    "summary": parsed.summary,
                    "event_type": parsed.event_type,
                    "entities": parsed.entities,
                    "assets": parsed.assets,
                    "magnitudes": parsed.magnitudes,
                    "modality": parsed.modality,
                }
            )
            report = report.added(summarised=1)
            report = report.model_copy(
                update={"cost_usd": report.cost_usd + (spend.cost_usd or ZERO)}
            ).added(
                prompt_tokens=spend.prompt_tokens, completion_tokens=spend.completion_tokens
            )

        if updated:
            # Only the six §6.5 fields move. Every other field was accepted at collection, and
            # rewriting one would silently change the archive's record of what was published —
            # `backfill`'s rule, which this pass shares the store with.
            store.rewrite(tuple(updated.get(a.url, a) for a in rows))
    logger.info("archive summarised", extra=report.model_dump(mode="json"))
    return report


async def _summarise_one(
    provider: LLMProvider,
    policy: ArchivePolicy,
    system: str,
    article: RawArticle,
    clock: Clock,
    pause: timedelta,
) -> tuple[SummaryResponse, CompletionResult] | None:
    """One row, up to `DEFAULT_SUMMARY_RETRIES` attempts. `None` means refused and counted."""
    user = summary_user(title=article.title, body=article.body)
    message = user
    for attempt in range(DEFAULT_SUMMARY_RETRIES):
        if attempt:
            await clock.sleep(pause.total_seconds())
        try:
            result = await provider.complete(
                CompletionRequest(model=policy.model, system=system, user=message)
            )
        except ProviderError as exc:
            logger.warning(
                "summariser call failed", extra={"url": article.url, "error": str(exc)}
            )
            message = user
            continue
        try:
            return parse_summary(result.text, max_chars=policy.max_summary_chars), result
        except SchemaViolationError as exc:
            logger.warning(
                "summariser reply refused", extra={"url": article.url, "error": str(exc)}
            )
            message = f"{user}\n{REPAIR_INSTRUCTION.format(error=exc)}"
    return None
```

Imports to add at the top of `summarize.py`:

```python
from datetime import timedelta

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.collect import DEFAULT_PAUSE
from decision_lab.archive.store import RawArticle, RawStore, StoreLock
from tradebot.core.clock import Clock
from tradebot.core.errors import ProviderError, SchemaViolationError
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money
from tradebot.decision.seat import REPAIR_INSTRUCTION, _extract_object
from tradebot.interfaces.llm import CompletionRequest, CompletionResult, LLMProvider

logger = get_logger(__name__)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_summarize_pass.py -q`
Expected: PASS, 14 tests

- [ ] **Step 6: Run the existing archive suite — the store model changed under it**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_store.py decision_lab/tests/test_archive_backfill.py decision_lab/tests/test_archive_shards.py decision_lab/tests/test_archive_store_lock.py decision_lab/tests/test_archive_reshard.py -q`
Expected: PASS. `backfill` already promises `summary` survives a re-fetch; the five new fields
must survive it too. If a backfill test fails, `backfill` is replacing rows wholesale somewhere
it claims not to — fix `backfill`, not the test.

- [ ] **Step 7: Verify the real store still reads with the extended model**

Run: `.venv\Scripts\python.exe -c "from pathlib import Path; from decision_lab.archive.store import RawStore; s = RawStore.open(Path('data/history-2025/news-raw'), source_id='cryptoslate'); r = s.read_all(); print(len(r), sum(1 for a in r if a.is_summarised))"`
Expected: `4869 0` — every collected row validates against the extended model, and none is
summarised yet. **This is a read, and it writes nothing.**

- [ ] **Step 8: Commit**

```bash
git add decision_lab/archive/store.py decision_lab/archive/summarize.py \
        decision_lab/tests/test_archive_summarize_pass.py
git commit -m "feat(decision_lab): the summarising pass, and the fields it writes

RawArticle gains the five 6.5 fields, defaulted and nullable: 14 484 rows are
already on disk and a non-defaulted addition would refuse the store on its
first read. Nullable rather than sentinel, because EventType.OTHER means
classified as other while None means never summarised - one is a measurement
and the other its absence.

The pass is keyed on is_summarised, so a second invocation asks for nothing;
batched through --limit because rewrite replaces the store from a pre-pass
snapshot; and locked, because two passes would each write over the other's
gains reporting success from both. A refusal is a tally and never falls back
to the publisher's abstract - that abstract is the baseline being measured.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: `archive summarise` on the CLI, and the cost projection

**Files:**
- Modify: `decision_lab/cli.py` — the parser block and the `archive_summarise` handler
- Modify: `decision_lab/archive/summarize.py` — add `SummaryReport.per_row_cost` and `project(store, policy)`
- Test: `decision_lab/tests/test_cli_archive.py` (extend)

**Interfaces:**
- Consumes: `summarize_store`, `SummaryReport`, `system_for` (Task 4); `load_policy`, `DEFAULT_POLICY_FILE` (Task 2).
- Produces:
  - `SummaryReport.per_row_cost -> Money` — measured, `ZERO` when nothing was summarised
  - `Projection(DomainModel)`: `rows`, `pending`, `bodyless`, `body_chars`, `prompt_tokens`
  - `project(store: RawStore, policy: ArchivePolicy) -> Projection` — **zero provider calls**
  - CLI: `archive summarise --data <dir> --source <id> [--policy <toml>] [--limit N] [--pause-ms N] [--dry-run] [--plumbing-check] [--verbose]`

**A stub binding refuses unless `--plumbing-check` is passed, and that is not redundant with
§7.2's "the binding decides, never a flag".** The binding still decides *what kind of run this
is* — the flag decides nothing and changes no behaviour, it only confirms the operator meant a
run that measures nothing. Two reasons it is needed. The shipped `archive.toml` binds the stub, so
`archive summarise --data data\history-2025 --source cryptoslate` with no `--policy` is the
command an operator types first; and what it would do is spin through 4 869 rows, refuse every one,
write nothing, and leave `archive publish` to refuse afterwards. That is fail-closed **by
accident**: the stub answers an unscripted prompt with `DEFAULT_RESPONSE`
([stub.py:155-157](../../../tradebot/decision/providers/stub.py#L155-L157)), which is a *trading
decision* — `{"action": "BUY", …}` — and it fails `SummaryResponse` on every field only because
two unrelated schemas happen not to match. A guarantee that rests on that coincidence is not a
guarantee. The refusal names the file to point at, and reuses `EXIT_CANDIDATE` (4), the existing
code for configuration that will not produce a measurement.

`summarize_store` itself is unguarded and stays directly callable — that is how Task 4's tests
drive it, and the refusal belongs at the operator's boundary rather than in the pass.

**The projection makes no provider calls and quotes no dollars.** This tool has no price table —
every cost figure in it comes from a provider's own response (`CompletionResult.cost_usd`), which
is why `total_cost` de-duplicates by `call_id` rather than multiplying a rate. So `--dry-run`
answers the two questions it *can* answer exactly — how many rows are pending and how many prompt
tokens they are — and the dollar figure comes from a measured batch: run `--limit 50`, read
`per_row_cost`, multiply. A projection that invented a rate would be a number with no provenance
in a tool whose whole argument is provenance.

**Absent from `LOCKED`, like every other `archive` action.** It writes beside the dataset, not into
the workspace, and a multi-hour pass holding the workspace lock would block every sweep and report
for its duration. Its own `StoreLock` is the protection that matters, and it is scoped to the one
store it rewrites.

- [ ] **Step 1: Write the failing tests**

Append to `decision_lab/tests/test_cli_archive.py`:

```python
@pytest.mark.asyncio
async def test_summarise_runs_the_pass_and_exits_zero(tmp_path: Path, capsys) -> None:
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    code = await cli.main(
        [
            "archive", "summarise",
            "--data", str(data),
            "--source", "cryptoslate",
            "--policy", str(DEFAULT_POLICY_FILE),
            "--pause-ms", "0",
            "--plumbing-check",
        ]
    )

    assert code == cli.EXIT_OK
    # Nothing is summarised: the stub answers an unscripted prompt with a trading decision, so
    # every row is refused. The pass completing and writing nothing is the assertion.
    assert not store.read_all()[0].is_summarised


@pytest.mark.asyncio
async def test_dry_run_makes_no_provider_calls_and_reports_what_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The projection is a read. A `--dry-run` that called the provider to estimate a provider
    call would be the thing it exists to avoid."""
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))
    store.append(summarisable_row(2, body="   "))

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("a projection must not construct a provider")

    monkeypatch.setattr(cli, "build_providers", explode)

    code = await cli.main(
        ["archive", "summarise", "--data", str(data), "--source", "cryptoslate", "--dry-run"]
    )

    assert code == cli.EXIT_OK
    assert not store.read_all()[0].is_summarised


@pytest.mark.asyncio
async def test_summarise_refuses_a_missing_policy_file(tmp_path: Path) -> None:
    data = tmp_path / "history"
    RawStore.open(data / RAW_DIR, source_id="cryptoslate")

    with pytest.raises(ConfigError, match="archive.toml"):
        await cli.main(
            [
                "archive", "summarise",
                "--data", str(data),
                "--source", "cryptoslate",
                "--policy", str(tmp_path / "absent.toml"),
            ]
        )


@pytest.mark.asyncio
async def test_summarise_refuses_before_any_spend_when_the_key_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule every `calibrate` command already has, applied here: a pass that discovers its
    key is absent on row 9 000 has spent nothing but taken an hour to say so, and a provider left
    unwired by ADR 0023 would make every row a refusal for a reason only the log explains."""
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))
    policy = tmp_path / "real.toml"
    policy.write_text(
        '[summarizer]\nprovider = "openrouter"\nmodel = "x/y:free"\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        await cli.main(
            [
                "archive", "summarise",
                "--data", str(data),
                "--source", "cryptoslate",
                "--policy", str(policy),
            ]
        )


@pytest.mark.asyncio
async def test_a_stub_binding_refuses_without_the_acknowledgement(tmp_path: Path) -> None:
    """The shipped policy binds the stub, so this is the command an operator types first. What it
    would otherwise do is refuse 4 869 rows one at a time and write nothing — fail-closed only
    because `DEFAULT_RESPONSE` is a trading decision and happens not to validate as a summary."""
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    with pytest.raises(ConfigError, match="--plumbing-check"):
        await cli.main(
            ["archive", "summarise", "--data", str(data), "--source", "cryptoslate"]
        )

    assert not store.read_all()[0].is_summarised


@pytest.mark.asyncio
async def test_dry_run_is_not_blocked_by_the_stub_refusal(tmp_path: Path) -> None:
    """Ordering, and it is load-bearing: a projection is useful whatever the binding, and it is
    the *only* thing that is useful before a key exists. The `--dry-run` branch returns before
    the stub check, so the question "how big is this pass" is always answerable."""
    data = tmp_path / "history"
    store = RawStore.open(data / RAW_DIR, source_id="cryptoslate")
    store.append(summarisable_row(1))

    code = await cli.main(
        ["archive", "summarise", "--data", str(data), "--source", "cryptoslate", "--dry-run"]
    )

    assert code == cli.EXIT_OK


def test_summarise_is_absent_from_locked() -> None:
    """It writes beside the dataset, not into the workspace, and a multi-hour pass holding the
    workspace lock would block every sweep and report — `archive build`'s argument, unchanged."""
    assert ("archive", "summarise") not in cli.LOCKED
    assert ("archive", "publish") not in cli.LOCKED
```

Add the shared fixture helper near the top of the file:

```python
def summarisable_row(n: int, *, body: str = "The FCA granted Kraken an EMI licence today.") -> RawArticle:
    return RawArticle(
        source_id="cryptoslate",
        url=f"https://x.test/{n}",
        title=f"Story {n}",
        body=body,
        abstract="An abstract.",
        published_at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        fetched_at=datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_archive.py -q -k "summarise or dry_run or locked"`
Expected: FAIL — argparse exits 2 on the unknown action `summarise`

- [ ] **Step 3: Add `per_row_cost` and `project` to `summarize.py`**

```python
class Projection(DomainModel):
    """What a pass would ask for, measured without asking (§10.6's cost projection, one tool over).

    No dollar figure, deliberately. This tool has no price table — every cost in it comes from a
    provider's own response, which is why `total_cost` de-duplicates by `call_id` rather than
    multiplying a rate. The dollars come from a measured batch: run `--limit 50` and read
    `per_row_cost`. A projected rate would be a number with no provenance, in a tool whose whole
    argument is provenance.
    """

    rows: int = 0
    pending: int = 0
    bodyless: int = 0
    body_chars: int = 0
    prompt_tokens: int = 0


def project(store: RawStore, policy: ArchivePolicy) -> Projection:
    """What `summarize_store` would request. Reads the store and nothing else."""
    rows = store.read_all()
    pending = [a for a in rows if not a.is_summarised]
    sendable = [a for a in pending if a.body.strip()]
    system = system_for(policy)
    # The stub's own estimator, which is also what every provider's `prompt_tokens` approximates:
    # four characters to a token. Exact enough to tell a 3 000-call pass from a 30 000-call one,
    # which is the decision this number informs.
    chars = sum(len(system) + len(summary_user(title=a.title, body=a.body)) for a in sendable)
    return Projection(
        rows=len(rows),
        pending=len(sendable),
        bodyless=len(pending) - len(sendable),
        body_chars=sum(len(a.body) for a in sendable),
        prompt_tokens=chars // 4,
    )
```

And on `SummaryReport`:

```python
    @property
    def per_row_cost(self) -> Money:
        """Measured, never projected. `ZERO` when nothing was summarised — a per-row cost over
        zero rows is not a small number, it is no number."""
        if not self.summarised:
            return ZERO
        return divide(self.cost_usd, Decimal(self.summarised))
```

Import `divide` from `tradebot.core.money` and `Decimal` from `decimal`.

- [ ] **Step 4: Add the parser block to `cli.py`**

Beside the other `archive_*` parsers, after `archive_reshard_parser`:

```python
    archive_summarise_parser = archive_actions.add_parser(
        "summarise",
        help="fill the §6.5 summary and extracted fields from the stored bodies",
    )
    archive_summarise_parser.add_argument(
        "--data", type=Path, required=True, help="dataset directory"
    )
    archive_summarise_parser.add_argument(
        "--source", required=True, help="the source whose store to summarise"
    )
    archive_summarise_parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_FILE,
        help="archive.toml: the summariser binding and the reading policy (§6.6)",
    )
    archive_summarise_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="rows to request in this batch. A long pass risks everything it collected to one "
        "late failure, so a large job is a sequence of batches, each of which commits",
    )
    archive_summarise_parser.add_argument(
        "--pause-ms",
        type=int,
        default=0,
        help="delay between retries, in integer milliseconds (no float, test_discipline.py)",
    )
    archive_summarise_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be requested and how many prompt tokens it is. Makes no "
        "provider call and quotes no dollars: the dollar figure comes from a measured batch",
    )
    archive_summarise_parser.add_argument(
        "--plumbing-check",
        action="store_true",
        help="acknowledge that the bound provider is the offline stub, which measures nothing. "
        "Changes no behaviour — the binding decides what kind of run this is (§7.2); this only "
        "confirms you meant it, because the shipped policy binds the stub",
    )
    archive_summarise_parser.add_argument("--verbose", action="store_true")
```

- [ ] **Step 5: Add the handler to `cli.py`**

```python
async def archive_summarise(args: argparse.Namespace) -> int:
    """Fill §6.5's summary and extracted fields from the bodies a crawl stored.

    Deliberately absent from `LOCKED`, like every other `archive` action: it writes beside the
    dataset rather than into the workspace, and a multi-hour pass holding the workspace lock
    would block every sweep and report for its duration. `StoreLock` is the protection that
    matters here and it is scoped to the one store this rewrites.

    The reachability refusal comes *before* any spend, which is the rule every `calibrate`
    command already has. A provider whose key is absent is left unwired by ADR 0023, so without
    this check every row would be refused for a reason only the event log explains — after an
    hour of asking.
    """
    policy = load_policy(args.policy)
    store = RawStore.open(args.data / RAW_DIR, source_id=args.source)

    if args.dry_run:
        projection = project(store, policy)
        logger.info(
            "archive summarise projection",
            extra=projection.model_dump(mode="json")
            | {
                "source": args.source,
                "model": f"{policy.provider_id}:{policy.model}",
                "note": "no provider call was made; measure dollars with a --limit batch",
            },
        )
        return EXIT_OK

    if policy.is_stub and not args.plumbing_check:
        raise ConfigError(
            f"{args.policy} binds the summarizer to the offline stub, which measures nothing: "
            "it answers an unscripted prompt with a trading decision, so every row would be "
            "refused and nothing written. Point --policy at a real binding, or pass "
            "--plumbing-check if a plumbing run is what you meant. The shipped policy binds the "
            "stub deliberately, so the suite stays offline and free"
        )

    pool = build_providers(_summarizer_settings(policy), SystemClock())
    provider = pool.providers.get(policy.provider_id)
    if provider is None:
        raise ConfigError(
            f"{policy.provider_id} is unreachable: "
            f"{preset(policy.provider_id).secret_ref} is not set. Set it and re-run — a pass "
            "against an unwired provider refuses every row for a reason only the log explains"
        )

    report = await summarize_store(
        provider,
        store,
        policy,
        SystemClock(),
        limit=args.limit,
        pause=timedelta(milliseconds=args.pause_ms),
    )
    logger.info(
        "archive summarised",
        extra=report.model_dump(mode="json")
        | {
            "source": args.source,
            "model": f"{policy.provider_id}:{policy.model}",
            "per_row_cost": str(report.per_row_cost),
            "store": str(store.path),
        },
    )
    return EXIT_OK
```

**Note for the implementer:** `_summarizer_settings(policy)` builds the one-entry
`Mapping[str, ProviderSettings]` that `build_providers` takes — `{policy.provider_id:
preset(policy.provider_id)}` for a real provider, and for `"stub"` whatever `PRESETS` holds for
it. Check `registry.preset` and `ProviderPool`'s shape before writing it; the stub may not be in
`PRESETS` at all, in which case construct `StubLLMProvider()` directly for that one case and say
in a comment that a stub binding is a plumbing check by §7.2's argument. `build_providers` and
`preset` need importing into `cli.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_archive.py -q`
Expected: PASS, the whole file

- [ ] **Step 7: Commit**

```bash
git add decision_lab/cli.py decision_lab/archive/summarize.py \
        decision_lab/tests/test_cli_archive.py
git commit -m "feat(decision_lab): archive summarise, and a projection that quotes no rate

--dry-run answers the two questions it can answer exactly - pending rows and
prompt tokens - and makes no provider call. It quotes no dollars on purpose:
this tool has no price table, every cost in it comes from a provider's own
response, and a projected rate is a number with no provenance. The dollars
come from a measured --limit batch via per_row_cost.

Refuses before any spend when the key is absent, the rule every calibrate
command already has. Absent from LOCKED, like every other archive action.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: `archive publish` — raw rows into the travelling archive

**Files:**
- Create: `decision_lab/archive/publish.py`
- Modify: `decision_lab/cli.py` — parser block and handler
- Modify: `decision_lab/archive/__init__.py` — export the new surfaces
- Test: `decision_lab/tests/test_archive_publish.py`

**Interfaces:**
- Consumes: `ArchiveItem`, `ArchiveHeader`, `PublishedArchive`, `ARCHIVE_DIR`, `ARCHIVE_FILE` (Task 1); `ArchivePolicy` (Task 2); `prompt_digest`, `system_for` (Tasks 3–4); `RawStore`, `RawArticle` (existing).
- Produces:
  - `PublishReport(DomainModel)`: `rows`, `published`, `unsummarised`, `archive_digest: str`, `path: str`
  - `publish_archive(store: RawStore, policy: ArchivePolicy, clock: Clock, *, out_dir: Path, capture: str) -> PublishReport`
  - `archive_path(data_dir: Path, source_id: str) -> Path` — `<data>/news/<source>/news.jsonl`
  - CLI: `archive publish --data <dir> --source <id> [--policy <toml>] [--capture <mode>] [--verbose]`

**This is where §6.4's boundary is crossed, in the one direction allowed.** The staging store holds
bodies; the travelling archive has no field for one. `publish_archive` is the projection, and it is
the only function in the package that reads `RawArticle.body` — which it reads **only** to decide
the row is publishable, never to copy. `ArchiveItem`'s lack of a `body` field means a mistake here
is a refusal rather than a leak.

**An unsummarised row is skipped and counted, never published blank.** A row with no summary has
no excerpt for the panel and no cluster key, so publishing it would put a title-only item into a
slot and then fail to collapse it. The count is what tells an operator the summarising pass is not
finished.

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_archive_publish.py
"""Raw rows → the §6.6 archive: `observed_at` derived, the body dropped, the digest stamped.

The §6.4 boundary is crossed here and only here, and only in the direction that drops data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.publish import archive_path, publish_archive
from decision_lab.archive.published import PublishedArchive
from decision_lab.archive.store import RawArticle, RawStore
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.news.normalize import url_hash

POLICY = ArchivePolicy(provider_id="stub", model="stub-summary")


def summarised(n: int, **overrides: object) -> RawArticle:
    base: dict[str, object] = {
        "source_id": "cryptoslate",
        "url": f"https://x.test/{n}?utm_source=twitter",
        "title": f"Story {n}",
        "body": "The FCA granted Kraken an EMI licence today.",
        "abstract": "An abstract.",
        "published_at": datetime(2025, 3, 4, 9, n, tzinfo=UTC),
        "fetched_at": datetime(2025, 3, 4, 10, 0, tzinfo=UTC),
        "summary": f"Summary {n}.",
        "event_type": EventType.REGULATORY,
        "entities": ("Kraken",),
        "assets": ("NONE",),
        "modality": Modality.CONFIRMED,
    }
    return RawArticle.model_validate(base | overrides)


def store_with(tmp_path: Path, rows: tuple[RawArticle, ...]) -> RawStore:
    store = RawStore.open(tmp_path / "news-raw", source_id="cryptoslate")
    for article in rows:
        store.append(article)
    return store


def clock() -> ManualClock:
    return ManualClock(datetime(2026, 10, 5, tzinfo=UTC))


def test_publishing_derives_observed_at_from_the_policys_lag(tmp_path: Path) -> None:
    """§6.7: never `published_at` itself, which grants the panel the headline at the instant of
    publication — optimistic in the one direction that inflates a score."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    item = PublishedArchive.read(archive_path(out, "cryptoslate")).items[0]
    assert item.observed_at == item.published_at + timedelta(minutes=12)
    assert item.observed_at > item.published_at


def test_the_published_row_carries_no_body_and_the_summary_is_the_excerpt(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    raw_text = archive_path(out, "cryptoslate").read_text(encoding="utf-8")
    assert "The FCA granted Kraken an EMI licence today." not in raw_text
    assert "Summary 2." in raw_text


def test_the_url_is_canonicalised_and_hashed_with_the_bots_own_functions(tmp_path: Path) -> None:
    """§2.4: a second canonicalisation would key the same story two ways, so a cross-source
    duplicate would stop being recognisable as one."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    item = PublishedArchive.read(archive_path(out, "cryptoslate")).items[0]
    assert "utm_source" not in item.url
    assert item.url_hash == url_hash(item.url)


def test_an_unsummarised_row_is_skipped_and_counted(tmp_path: Path) -> None:
    """Publishing it blank would put a title-only item into one of eight slots and then fail to
    collapse it, having no cluster key. The count is how an operator learns the pass is unfinished."""
    store = store_with(tmp_path, (summarised(2), summarised(3, summary="", event_type=None, modality=None)))
    out = tmp_path / "out"

    report = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    assert (report.published, report.unsummarised) == (1, 1)


def test_publishing_a_store_with_nothing_summarised_is_refused(tmp_path: Path) -> None:
    """An archive of zero rows is a NEWS-BLIND corpus wearing a digest, which is worse than no
    archive: §6.9 is the honest way to run without news, and it says so on the report."""
    store = store_with(tmp_path, (summarised(3, summary="", event_type=None, modality=None),))

    with pytest.raises(ConfigError, match="summarise"):
        publish_archive(store, POLICY, clock(), out_dir=tmp_path / "out", capture="sitemap")


def test_the_header_records_the_binding_the_prompt_and_the_reading_policy(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    report = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    header = PublishedArchive.read(archive_path(out, "cryptoslate")).header

    assert header.summarizer_model == "stub-summary"
    assert header.observed_at_policy == "published_at+00:12:00"
    assert header.news_window_seconds == 172_800
    assert header.prompt_digest and header.archive_digest == report.archive_digest


def test_republishing_the_same_rows_yields_the_same_digest(tmp_path: Path) -> None:
    """It feeds `corpus_id`. A digest that moved on a re-publish would renumber a corpus whose
    evidence is byte-identical."""
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    first = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    second = publish_archive(store, POLICY, ManualClock(datetime(2026, 12, 1, tzinfo=UTC)), out_dir=out, capture="sitemap")

    assert first.archive_digest == second.archive_digest


def test_a_different_lag_is_a_different_archive(tmp_path: Path) -> None:
    store = store_with(tmp_path, (summarised(2),))
    out = tmp_path / "out"

    base = publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")
    other = publish_archive(
        store,
        POLICY.model_copy(update={"observed_at_lag_seconds": 1800}),
        clock(),
        out_dir=out,
        capture="sitemap",
    )

    assert base.archive_digest != other.archive_digest


def test_rows_are_published_in_observed_at_order(tmp_path: Path) -> None:
    """The feed binary-searches this file's order. Shard order is month order, and a month's rows
    are in crawl order — which is not time order, because a resumed crawl appends."""
    store = store_with(tmp_path, (summarised(5), summarised(2), summarised(9)))
    out = tmp_path / "out"

    publish_archive(store, POLICY, clock(), out_dir=out, capture="sitemap")

    items = PublishedArchive.read(archive_path(out, "cryptoslate")).items
    assert [i.observed_at for i in items] == sorted(i.observed_at for i in items)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_publish.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.archive.publish'`

- [ ] **Step 3: Write `publish.py`**

```python
# decision_lab/archive/publish.py
"""Staging rows → the §6.6 archive a replay reads. The one place §6.4's boundary is crossed.

It is crossed in the only direction that is allowed: data is dropped. This is the only function in
the package that reads `RawArticle.body`, and it reads it **solely** to decide a row is
publishable — never to copy. `ArchiveItem` has no `body` field, so a mistake here is a refusal at
validation rather than a leak onto disk.

`observed_at` is derived here rather than at collection, so the lag can be revised without
re-crawling (§6.7). It is never `published_at` itself: that grants the panel the headline at the
instant of publication, which is optimistic in the one direction that inflates a score.

An unsummarised row is skipped and counted. Publishing it blank would put a title-only item into
one of eight slots and then fail to collapse it, having no cluster key — and a store with nothing
summarised is refused outright, because an archive of zero rows is a news-blind corpus wearing a
digest. §6.9 is the honest way to run without news, and it says so on the report.

Rows are written in `observed_at` order. Shard order is month order and a month's rows are in
*crawl* order, which is not time order once a crawl has been resumed.

Failure semantics: `ConfigError` for a store that cannot be published, naming the command that
fixes it. The write is `PublishedArchive.write`, which replaces the file whole — a partial archive
is not a smaller archive, it is one whose digest describes rows it does not hold.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.published import (
    ARCHIVE_DIR,
    ARCHIVE_FILE,
    ArchiveHeader,
    ArchiveItem,
    PublishedArchive,
)
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.archive.summarize import prompt_digest, system_for
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel
from tradebot.news.normalize import canonical_url, url_hash

logger = get_logger(__name__)


class PublishReport(DomainModel):
    """What one publish produced. `unsummarised` is how an operator learns the pass is unfinished."""

    rows: int = 0
    published: int = 0
    unsummarised: int = 0
    archive_digest: str = ""
    path: str = ""


def archive_path(data_dir: Path, source_id: str) -> Path:
    """`<data>/news/<source>/news.jsonl`. Beside the dataset, never in the workspace."""
    return data_dir / ARCHIVE_DIR / source_id / ARCHIVE_FILE


def publish_archive(
    store: RawStore,
    policy: ArchivePolicy,
    clock: Clock,
    *,
    out_dir: Path,
    capture: str,
) -> PublishReport:
    """Project one summarised staging store into its travelling archive."""
    rows = store.read_all()
    ready = tuple(a for a in rows if a.is_summarised)
    if not ready:
        raise ConfigError(
            f"{store.path} holds {len(rows)} rows and none is summarised. Run `archive "
            "summarise --data <dir> --source <id>` first: an archive of zero rows is a "
            "news-blind corpus wearing a digest, and §6.9 is the honest way to run without news"
        )

    items = tuple(sorted((_item(a, policy) for a in ready), key=lambda i: (i.observed_at, i.url)))
    header = ArchiveHeader(
        source=store.path.name,
        capture=capture,
        captured_at=clock.now(),
        observed_at_policy=policy.observed_at_policy,
        summarizer_provider=policy.provider_id,
        summarizer_model=policy.model,
        prompt_digest=prompt_digest(system_for(policy)),
        news_window_seconds=policy.news_window_seconds,
        cluster_floor=policy.cluster_floor,
        rows=len(items),
    )
    path = out_dir / ARCHIVE_DIR / store.path.name / ARCHIVE_FILE
    PublishedArchive.write(path, header, items)
    published = PublishedArchive.read(path)

    report = PublishReport(
        rows=len(rows),
        published=len(items),
        unsummarised=len(rows) - len(ready),
        archive_digest=published.header.archive_digest,
        path=str(path),
    )
    logger.info("archive published", extra=report.model_dump(mode="json"))
    return report


def _item(article: RawArticle, policy: ArchivePolicy) -> ArchiveItem:
    """One staging row as the archive carries it. The body is read nowhere in this function.

    `canonical_url` and `url_hash` are the bot's own (§2.4). A second canonicalisation would key
    one story two ways, and a cross-source duplicate would stop being recognisable as one.
    """
    assert article.event_type is not None and article.modality is not None  # is_summarised
    url = canonical_url(article.url)
    return ArchiveItem(
        source_id=article.source_id,
        url=url,
        url_hash=url_hash(url),
        title=article.title,
        summary=article.summary,
        event_type=article.event_type,
        entities=article.entities,
        assets=article.assets,
        magnitudes=article.magnitudes,
        modality=article.modality,
        published_at=article.published_at,
        observed_at=article.published_at + policy.lag,
    )
```

- [ ] **Step 4: Add the CLI parser and handler**

Parser, beside the others:

```python
    archive_publish_parser = archive_actions.add_parser(
        "publish",
        help="project the summarised staging store into the §6.6 archive a replay reads",
    )
    archive_publish_parser.add_argument("--data", type=Path, required=True, help="dataset directory")
    archive_publish_parser.add_argument("--source", required=True, help="the source to publish")
    archive_publish_parser.add_argument(
        "--policy", type=Path, default=DEFAULT_POLICY_FILE, help="archive.toml (§6.6)"
    )
    archive_publish_parser.add_argument(
        "--capture",
        default="sitemap",
        help="how the rows were collected; recorded in the header and inside archive_digest",
    )
    archive_publish_parser.add_argument("--verbose", action="store_true")
```

Handler:

```python
async def archive_publish(args: argparse.Namespace) -> int:
    """Project a summarised staging store into its travelling archive (§6.4, §6.6).

    Absent from `LOCKED` for the same two reasons every other `archive` action is: it writes
    beside the dataset, not into the workspace.
    """
    policy = load_policy(args.policy)
    store = RawStore.open(args.data / RAW_DIR, source_id=args.source)
    report = publish_archive(
        store, policy, SystemClock(), out_dir=args.data, capture=args.capture
    )
    logger.info(
        "archive publish complete",
        extra=report.model_dump(mode="json") | {"source": args.source},
    )
    return EXIT_OK
```

- [ ] **Step 5: Export the new surfaces from `decision_lab/archive/__init__.py`**

Extend the module docstring's two-store paragraph to name `published.py` as the travelling half,
and re-export `ArchiveItem`, `ArchiveHeader`, `PublishedArchive`, `ArchivePolicy`, `load_policy`,
`publish_archive`, `summarize_store`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_archive_publish.py decision_lab/tests/test_cli_archive.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add decision_lab/archive/publish.py decision_lab/archive/__init__.py decision_lab/cli.py \
        decision_lab/tests/test_archive_publish.py
git commit -m "feat(decision_lab): archive publish - staging rows into the archive that travels

The one place 6.4's boundary is crossed, in the only direction allowed: this
is the only function that reads RawArticle.body, and it reads it solely to
decide a row is publishable. ArchiveItem has no body field, so a mistake is a
validation refusal rather than a leak.

observed_at is derived here, not at collection, so the lag can be revised
without re-crawling - and never equals published_at. An unsummarised row is
skipped and counted; a store with nothing summarised is refused, because an
archive of zero rows is a news-blind corpus wearing a digest.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: The `build_sim(news_feed=…)` seam and its guards — ONE COMMIT

**Files:**
- Modify: `tradebot/app.py:883-895` (`_assemble` signature), `tradebot/app.py:947-949` (the news wiring), `tradebot/app.py:1204-1216` (`build_sim` signature), `tradebot/app.py:1234-1245` (`build_sim`'s `_assemble` call)
- Test: `tests/unit/test_news_feed_seam.py` — **the bot's suite, not the tool's**

**Interfaces:**
- Consumes: nothing from earlier tasks. This task is independent of them and could be done first.
- Produces: `build_sim(..., news_feed: NewsFeed | None = None)`. Task 8's feed and Task 10's corpus wiring both pass through it.

> ### This task's two halves land in ONE commit. Never two.
>
> §2.3: an injected `NewsFeed` reaching live would be an archive of stale, model-summarised
> headlines feeding a panel whose orders reach a real venue — "the single worst outcome in this
> design". The seam without its guards is that outcome one careless refactor away, and a commit
> that adds a parameter to the money path while its tests wait for a follow-up is exactly the
> window this rule closes. `git show --stat` on this commit must list `tradebot/app.py` **and**
> `tests/unit/test_news_feed_seam.py`.

**Why the guards live in the bot's suite.** §2.3 is explicit: placed in `decision_lab/tests/`, the
bot could break its own seam and its own gate would not notice. They stay inside the separation
contract because they name only `tradebot` symbols plus a three-line local stub satisfying the
`NewsFeed` protocol — nothing imports `decision_lab`, so `test_separation.py` still passes. **The
word `decision_lab` must not appear in this file**, not even in a docstring: `test_separation.py`
walks `tradebot/` only, but the habit is what keeps the boundary legible.

**Why `_assemble`'s parameter defaults to `None`.** It keeps `build_paper` and `build_live`'s call
sites untouched, so this commit's diff is four edits rather than six. The structural closure is
that the parameter exists on `build_sim` only; the defensive refusal in `_assemble` is what
survives somebody later adding it to `build_paper` for convenience.

- [ ] **Step 1: Write the failing guards**

```python
# tests/unit/test_news_feed_seam.py
"""The one injection seam a replay needs, and the three doors it must not open.

`NewsHub.snapshot_news` calls `refresh()` on every invocation. Wired into a replay under a
`ManualClock` it would fetch *today's* RSS and store each item with `observed_at = <the replayed
instant>` — and since selection filters on `observed_at`, a 2026 headline would be served into a
2024 decision as though it had been known at the time. That is the look-ahead bug DESIGN [L12]
exists to prevent, and no amount of care outside this module can prevent it from outside.

So `build_sim` takes an optional `NewsFeed`. Paper and live are refused alongside each other: a
paper soak is the evidence `report promotion` reads (ADR 0016, ADR 0020), and a soak deciding on
archived news is not a soak of the system that would trade.

The last test here is a *characterisation* test. It records what `NewsHub` does today — the
mechanism that makes this seam necessary — so a future change to `observed_at` stamping surfaces
in this file rather than silently in a replay.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from tradebot.app import _assemble, build, build_live, build_paper, build_sim
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.snapshot import NewsCoverage, NewsItemView


class StubFeed:
    """Three lines, satisfying the `NewsFeed` protocol and nothing else."""

    async def snapshot_news(
        self, instruments: tuple[Instrument, ...], as_of: datetime, limit: int
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        return (), NewsCoverage(sources_ok=("stub",))


def test_build_paper_and_build_live_carry_no_news_feed_parameter() -> None:
    """The structural closure. Fails loudly if a future refactor adds one for convenience."""
    for builder in (build_paper, build_live):
        assert "news_feed" not in inspect.signature(builder).parameters, builder.__name__


def test_build_sim_does_carry_it() -> None:
    """The guard above must not pass by the parameter simply never having been added."""
    assert "news_feed" in inspect.signature(build_sim).parameters


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_assemble_refuses_a_substituted_feed_outside_sim(mode: Mode) -> None:
    """The defensive closure, which survives somebody adding the parameter to `build_paper`."""
    with pytest.raises(ConfigError, match=mode.value):
        await _assemble(
            ManualClock(datetime(2024, 3, 11, tzinfo=UTC)),
            mode=mode,
            db_path=None,
            broker_choice=_any_broker(),
            start_equity=_any_equity(),
            baskets=None,
            global_policy=None,
            feed=_empty_feed(),
            news_sources=(),
            panel_id="stub",
            news_feed=StubFeed(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_the_dispatch_door_is_shut_too(mode: Mode) -> None:
    """`build(mode, **kwargs)` must not quietly accept what the builder does not take."""
    with pytest.raises((ConfigError, TypeError)):
        await build(mode, news_feed=StubFeed())


@pytest.mark.asyncio
async def test_build_sim_wires_exactly_the_object_given_and_never_builds_a_hub(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The seam does what it says, asserted two ways: the object is the one handed in, and
    `build_news_hub` is not called at all — not called-and-discarded."""
    import tradebot.app as app_module

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("build_news_hub must not be called when a feed is injected")

    monkeypatch.setattr(app_module, "build_news_hub", explode)
    feed = StubFeed()

    application = await build_sim(
        clock=ManualClock(datetime(2024, 3, 11, tzinfo=UTC)), news_feed=feed
    )
    try:
        assert application.runners.news_feed is feed
    finally:
        await application.shutdown()


@pytest.mark.asyncio
async def test_build_sim_with_sources_and_no_feed_still_wires_a_hub() -> None:
    """The regression net. The default path is byte-for-byte the old behaviour, so the seam
    cannot silently change what a sim run with configured sources does."""
    application = await build_sim(
        clock=ManualClock(datetime(2024, 3, 11, tzinfo=UTC)), news_sources=("cointelegraph",)
    )
    try:
        assert application.runners.news_feed is not None
    finally:
        await application.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("builder", [build_paper, build_live])
async def test_paper_and_live_news_is_unchanged_by_this_design(builder) -> None:
    """Tests 5 and 6 of §2.3 together: live and paper still wire a real hub from sources."""
    application = await builder(
        clock=ManualClock(datetime(2024, 3, 11, tzinfo=UTC)), news_sources=("cointelegraph",)
    )
    try:
        assert application.runners.news_feed is not None
    finally:
        await application.shutdown()


@pytest.mark.asyncio
async def test_a_news_hub_on_a_past_clock_stamps_items_with_that_instant() -> None:
    """Characterisation, not aspiration. This is the mechanism the seam exists to avoid, and it
    is currently unasserted anywhere: a `NewsHub` built on a `ManualClock` set to a past instant
    stamps whatever it fetched *now* with *then*, and selection filters on that field. A future
    change to `observed_at` stamping should surface here rather than inside a replay."""
    ...
```

**Note for the implementer on the last test and the three `_any_*` helpers.** Fill them from the
existing scenario wiring: look at how `tests/scenario/` already builds a `_assemble` call and a
`NewsHub` over a `ManualClock`, and reuse those fixtures rather than inventing new ones. The last
test's body is the one piece of this file that cannot be written without reading
`tradebot/news/hub.py:_normalize` and whichever existing fixture already produces a `NewsSource`
— write it as "fetch one item through a hub on a 2024 clock, then assert the stored item's
`observed_at` is the 2024 instant and not the wall clock". If no such fixture exists, a
four-line local `NewsSource` stub returning one `RawNewsItem` is in keeping with `StubFeed`
above. **Do not delete this test if it is awkward** — §16.2 names it specifically, and it is the
only thing in either suite that records *why* the seam is needed.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_news_feed_seam.py -q`
Expected: FAIL — `TypeError: build_sim() got an unexpected keyword argument 'news_feed'` on the
positive tests, and the two structural tests on `build_sim` failing for the same reason.

- [ ] **Step 3: Add the parameter to `build_sim`**

In `tradebot/app.py`, `build_sim`'s signature gains one line after `broker`:

```python
    broker: BrokerChoice = BrokerChoice.SIM,
    #: A substituted news feed, for a replay. Simulation only — `_assemble` refuses it in any
    #: other mode, and `build_paper`/`build_live` do not accept it at all. A replay must decide
    #: on what was knowable at the replayed instant, and `NewsHub.snapshot_news` refreshes on
    #: every call: wired into one, it would stamp today's headlines with the replayed `observed_at`
    #: and serve them into a past decision (DESIGN [L12]).
    news_feed: NewsFeed | None = None,
) -> Application:
```

and its `_assemble` call gains `news_feed=news_feed,` after `panel_id=panel_id,`.

- [ ] **Step 4: Add the parameter and the refusal to `_assemble`**

Signature gains, after `panel_id: str`:

```python
    news_feed: NewsFeed | None = None,
```

It defaults to `None` so `build_paper` and `build_live`'s call sites stay untouched — the
structural closure is that the parameter exists on `build_sim` alone, and the refusal below is
what survives somebody adding it to `build_paper` later.

Then, as the **first statement in the body**, before `create_database`:

```python
    # Belt and braces on purpose (DESIGN §5, and the separation contract's §2.3). The structural
    # closure is that no other builder takes this parameter; this refusal survives a future
    # refactor that adds one. Paper is refused alongside live deliberately: a paper soak is the
    # evidence `report promotion` reads (ADR 0016, ADR 0020), and a soak deciding on archived news
    # is not a soak of the system that would trade. Placed before the database is created, so a
    # refused build leaves no file behind.
    if news_feed is not None and mode is not Mode.SIM:
        raise ConfigError(
            f"a substituted news feed is simulation-only; {mode.value} must read live sources. "
            "An archived feed decides on news that is not current, and in paper it would "
            "contaminate the evidence report promotion reads."
        )
```

- [ ] **Step 5: Use it instead of building a hub**

Replace `tradebot/app.py:947-949` with:

```python
    # A substituted feed replaces the fetch half entirely — `build_news_hub` is not called, so
    # nothing in this process can reach a publisher. There is nothing to close: the injected
    # object owns no transport, which is the property that makes it safe here at all.
    if news_feed is None:
        news_feed, news_closers = build_news_hub(
            engine, writer, clock, news_sources or _news_sources_of(configured)
        )
    else:
        news_closers = ()
```

`NewsFeed` is already imported in `app.py` (it is `build_news_hub`'s return type). Confirm before
adding an import.

- [ ] **Step 6: Run the guards, then the bot's news and wiring tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_news_feed_seam.py -q`
Expected: PASS, 11 tests

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_separation.py decision_lab/tests/test_separation.py -q`
Expected: PASS — the seam names nothing in the tool. (Only one of those paths exists; run the one
that does.)

Run: `.venv\Scripts\python.exe -m pytest tests/ -q -k "news or wiring or assemble or build_sim"`
Expected: PASS, nothing regressed.

- [ ] **Step 7: Verify the diff against `main` is exactly the licensed surface**

Run: `git diff --stat main -- tradebot/`
Expected: `tradebot/app.py` and nothing else. If any other file appears, revert it — this is the
slice's exit criterion and the one bot change the whole design licenses.

- [ ] **Step 8: Commit — both halves together**

```bash
git add tradebot/app.py tests/unit/test_news_feed_seam.py
git commit -m "feat(app): build_sim accepts a substituted news feed, simulation only

The one tradebot change the decision_lab design licenses (spec 2.2), and it
lands with its guards in the same commit because the seam without them is the
design's worst outcome one refactor away: an archive of stale, model-written
headlines feeding a panel whose orders reach a real venue.

NewsHub.snapshot_news refreshes on every call, so wired into a replay under a
ManualClock it would fetch today's RSS and stamp it with the replayed
observed_at - serving a 2026 headline into a 2024 decision as though it had
been known (DESIGN L12). Closed structurally (no other builder takes the
parameter) and defensively (_assemble refuses any mode but SIM, before the
database is created). Paper is refused beside live: a soak deciding on
archived news is not a soak of the system that would trade.

The guards live in the bot's suite, not the tool's: placed there the bot
could break its own seam and its own gate would not notice.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: `ArchiveNewsFeed` — point-in-time selection, and coverage that distinguishes silence from absence

**Files:**
- Create: `decision_lab/newsfeed.py`
- Test: `decision_lab/tests/test_newsfeed.py`

**Interfaces:**
- Consumes: `PublishedArchive`, `ArchiveItem` (Task 1); `ArchivePolicy` (Task 2); the seam (Task 7).
- Produces:
  - `ArchiveNewsFeed(archives: tuple[PublishedArchive, ...], relevance: RelevanceFilter, *, window: timedelta, min_relevance: Money = DEFAULT_MIN_RELEVANCE)` — satisfies `NewsFeed`
  - `async snapshot_news(instruments, as_of, limit=DEFAULT_SNAPSHOT_ITEMS) -> tuple[tuple[NewsItemView, ...], NewsCoverage]`
  - `in_window(as_of: datetime) -> tuple[ArchiveItem, ...]` — the point-in-time read, public for Task 10's contamination test
  - `covers(as_of: datetime) -> bool`
  - `load_feed(data_dir: Path, sources: tuple[str, ...], policy: ArchivePolicy) -> ArchiveNewsFeed`
  - `archive_digest_of(feed: ArchiveNewsFeed) -> str` — the combined digest Task 10 threads into `corpus_id`

**It holds no fetcher and no HTTP client, and that is the point.** §16.2's first contamination test
asserts it structurally. The archives are loaded once at construction and indexed in memory — a
replay's archive is immutable for the length of a pass, so re-reading per cycle would buy nothing
and the one thing it could buy is a path to a file handle.

**Only the fetch half is replaced.** Relevance, the minimum-relevance floor, the ordering and the
truncation are the bot's and are imported: `KeywordRelevanceFilter`, `DEFAULT_MIN_RELEVANCE`,
`DEFAULT_SNAPSHOT_ITEMS`, and `NewsHub.select`'s exact sort key `(score, observed_at, item_id)`
reversed. A replay that ranked news differently from production would be measuring a different
pipeline (§2.4, ADR 0020's argument one door along).

**Silence and absence are different, and the coverage line is where that is said.** Review Focus 3.
A cycle inside the archive's span with nothing in its 48-hour window was a genuinely quiet two days:
`sources_ok`. A cycle **outside** the span — a 2024 corpus pointed at the 2025 archive, or a cycle
before the archive's first row — was never measured, and that source goes in `sources_failed`, so
`NewsCoverage.summary` renders "no coverage from cryptoslate" into the prompt. The panel is never
left to read an unmeasured window as a calm market, which is §6.9's rule arriving through the feed
instead of through the wiring.

> ### A documented deviation from §6.8.3
>
> §6.8.3 says a seat shown 8 of 65 items must be told it is 8 of 65, and that *"the existing
> `NewsCoverage` line carries it"*. **It cannot.** `NewsCoverage` has exactly two fields,
> `sources_ok` and `sources_failed`, and `summary` counts sources rather than items — so the
> cut has nowhere to ride. Adding a field is a **second `tradebot` change**, and this slice is
> licensed for exactly one (§2.2, and the empty-diff exit criterion).
>
> What this task does instead: the cut is logged and is reconstructible from the archive for the
> drill-down, and Task 9's `reports=N` count — which §6.8.2 *does* mandate on the item — carries
> the clustering half of the same information into the prompt. What is **not** delivered is the
> "8 of 65" figure reaching the seat. It is recorded in the plan's closing section as an open
> item needing either a `NewsCoverage` field or a §6.8.3 correction, and it is the operator's
> call which. Do not quietly add the field.

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_newsfeed.py
"""`ArchiveNewsFeed`: what was knowable at `as_of`, ranked by the bot's own filter.

The look-ahead test here is the one §16.2 calls the most important in the table, and it is run
again in `test_contamination.py` through the real `ContextBuilder`. Both are wanted: this one
fails fast and names the field, that one proves the real path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.newsfeed import ArchiveNewsFeed
from tradebot.core.instrument import AssetClass, Instrument
from tradebot.news.relevance import KeywordRelevanceFilter

BTC = Instrument(
    venue="binance", symbol="BTC/USDT", base="BTC", quote="USDT", asset_class=AssetClass.CRYPTO
)


def item(*, at: datetime, title: str, summary: str, url: str | None = None) -> ArchiveItem:
    link = url or f"https://x.test/{abs(hash(title)) % 10_000}"
    return ArchiveItem(
        source_id="cryptoslate",
        url=link,
        url_hash=f"h{abs(hash(link)) % 10_000}",
        title=title,
        summary=summary,
        event_type=EventType.REGULATORY,
        entities=("Kraken",),
        assets=("BTC",),
        magnitudes=(),
        modality=Modality.CONFIRMED,
        published_at=at - timedelta(minutes=12),
        observed_at=at,
    )


def archive(*items: ArchiveItem) -> PublishedArchive:
    header = ArchiveHeader(
        source="cryptoslate",
        capture="sitemap",
        captured_at=datetime(2026, 10, 5, tzinfo=UTC),
        observed_at_policy="published_at+00:12:00",
        summarizer_provider="stub",
        summarizer_model="stub-summary",
        prompt_digest="abc",
        news_window_seconds=172_800,
        cluster_floor=Decimal("0.25"),
        rows=len(items),
    )
    return PublishedArchive(header=header, items=tuple(sorted(items, key=lambda i: i.observed_at)))


def feed(*items: ArchiveItem, window: timedelta = timedelta(hours=48)) -> ArchiveNewsFeed:
    return ArchiveNewsFeed((archive(*items),), KeywordRelevanceFilter(), window=window)


@pytest.mark.asyncio
async def test_an_item_observed_after_the_instant_is_not_served() -> None:
    """§2.2, and §16.2's third test. The exact look-ahead the seam exists to prevent, reached
    from inside the archive rather than from a live feed."""
    before = item(at=datetime(2025, 3, 4, 8, tzinfo=UTC), title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")
    after = item(at=datetime(2025, 3, 4, 12, tzinfo=UTC), title="Bitcoin crashes", summary="Bitcoin fell sharply.")

    views, _ = await feed(before, after).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)

    titles = [v.title for v in views]
    assert "Bitcoin ETF approved" in titles
    assert "Bitcoin crashes" not in titles


@pytest.mark.asyncio
async def test_an_item_at_exactly_the_instant_is_served() -> None:
    """`observed_at <= as_of`, the bot's own boundary. Exclusive would silently drop whatever
    landed on a cycle boundary, which on an 8h grid is a recurring class of item."""
    at = datetime(2025, 3, 4, 10, tzinfo=UTC)
    views, _ = await feed(item(at=at, title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")).snapshot_news((BTC,), at, 8)
    assert len(views) == 1


@pytest.mark.asyncio
async def test_an_item_older_than_the_window_is_not_served() -> None:
    """§6.8.1's 48 hours, matching `NewsStore.DEFAULT_LOOKBACK`. A replay measuring a panel the
    bot does not run measures the wrong panel."""
    old = item(at=datetime(2025, 3, 1, tzinfo=UTC), title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")
    views, _ = await feed(old).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert views == ()


@pytest.mark.asyncio
async def test_a_quiet_window_inside_the_archives_span_reports_the_source_as_ok() -> None:
    """Review Focus 3, the half that must *not* alarm. Two quiet days are a fact about the news,
    and reporting them as a coverage failure would teach the panel to discount real calm."""
    early = item(at=datetime(2025, 3, 1, tzinfo=UTC), title="Kraken licensed", summary="Kraken was licensed.")
    late = item(at=datetime(2025, 3, 20, tzinfo=UTC), title="Kraken expands", summary="Kraken expanded.")

    views, coverage = await feed(early, late).snapshot_news((BTC,), datetime(2025, 3, 10, tzinfo=UTC), 8)

    assert views == ()
    assert coverage.sources_ok == ("cryptoslate",)
    assert coverage.is_complete


@pytest.mark.asyncio
async def test_an_instant_outside_the_archives_span_reports_no_coverage() -> None:
    """Review Focus 3, the half that must alarm: a 2024 corpus pointed at the 2025 archive, or a
    cycle before the first row. Never measured is not the same as nothing happened, and a seat
    told nothing happened will reason as though nothing happened."""
    only = item(at=datetime(2025, 3, 4, tzinfo=UTC), title="Kraken licensed", summary="Kraken was licensed.")

    _, coverage = await feed(only).snapshot_news((BTC,), datetime(2024, 3, 4, tzinfo=UTC), 8)

    assert coverage.sources_failed == ("cryptoslate",)
    assert not coverage.is_complete
    assert "no coverage from cryptoslate" in coverage.summary


@pytest.mark.asyncio
async def test_an_irrelevant_item_is_filtered_by_the_bots_own_floor() -> None:
    """Relevance is the bot's and is imported, never restated. An item naming no instrument and
    no macro term scores below `DEFAULT_MIN_RELEVANCE` and spends no tokens diluting evidence."""
    noise = item(at=datetime(2025, 3, 4, 9, tzinfo=UTC), title="Local bakery wins award", summary="A bakery in Leeds won a prize.")
    signal = item(at=datetime(2025, 3, 4, 9, tzinfo=UTC), title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")

    views, _ = await feed(noise, signal).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)

    assert [v.title for v in views] == ["Bitcoin ETF approved"]


@pytest.mark.asyncio
async def test_the_limit_truncates_most_relevant_first() -> None:
    items = tuple(
        item(at=datetime(2025, 3, 4, 9, n, tzinfo=UTC), title=f"Bitcoin story {n}", summary=f"Bitcoin news {n}.")
        for n in range(12)
    )
    views, _ = await feed(*items).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert len(views) == 8


@pytest.mark.asyncio
async def test_the_view_carries_the_summary_as_the_panels_text() -> None:
    """§6.5's output lands in `NewsItem.excerpt`, which `NewsItem.view` renders as `summary`. No
    new field and no second rendering path."""
    one = item(at=datetime(2025, 3, 4, 9, tzinfo=UTC), title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")
    views, _ = await feed(one).snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)
    assert views[0].summary == "The SEC approved a bitcoin ETF."


@pytest.mark.asyncio
async def test_two_sources_both_appear_in_coverage() -> None:
    """One feed over N archives. Each source answers for itself, so one store ending early is
    visible as that source's gap rather than as a thinner day."""
    btc_item = item(at=datetime(2025, 3, 4, 9, tzinfo=UTC), title="Bitcoin ETF approved", summary="The SEC approved a bitcoin ETF.")
    other = btc_item.model_copy(update={"source_id": "bitcoincom", "url": "https://y.test/1", "url_hash": "hy1"})
    second = PublishedArchive(
        header=archive(other).header.model_copy(update={"source": "bitcoincom"}),
        items=(other,),
    )
    multi = ArchiveNewsFeed((archive(btc_item), second), KeywordRelevanceFilter(), window=timedelta(hours=48))

    _, coverage = await multi.snapshot_news((BTC,), datetime(2025, 3, 4, 10, tzinfo=UTC), 8)

    assert set(coverage.sources_ok) == {"cryptoslate", "bitcoincom"}


def test_the_feed_holds_no_fetcher_and_no_http_client() -> None:
    """§16.2's first contamination test, asserted structurally: it cannot reach the network even
    by accident, because it owns no object that could."""
    built = feed(item(at=datetime(2025, 3, 4, tzinfo=UTC), title="Bitcoin ETF approved", summary="Approved."))
    names = {type(v).__name__.lower() for v in vars(built).values()}
    assert not any("client" in n or "fetcher" in n or "transport" in n for n in names)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_newsfeed.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.newsfeed'`

- [ ] **Step 3: Write `newsfeed.py`**

```python
# decision_lab/newsfeed.py
"""`ArchiveNewsFeed` — a `NewsFeed` that never fetches (spec §6.8).

It holds no fetcher, no HTTP client and no transport, and §16.2 asserts that structurally: it
cannot reach the network even by accident, because it owns no object that could. The archives are
loaded once at construction and indexed in memory. A replay's archive is immutable for the length
of a pass, so re-reading per cycle would buy nothing — and the one thing it could buy is a file
handle on a path.

**Only the fetch half is replaced.** Relevance, the minimum-relevance floor, the ordering and the
truncation are the bot's and are imported rather than restated (§2.4): `KeywordRelevanceFilter`,
`DEFAULT_MIN_RELEVANCE`, `DEFAULT_SNAPSHOT_ITEMS`, and `NewsHub.select`'s exact sort key. A replay
that ranked news differently from production would be measuring a different pipeline — ADR 0020's
argument, one door along.

**Silence and absence are different, and the coverage line is where that is said.** A cycle inside
the archive's span whose window holds nothing was a genuinely quiet two days, and reporting that as
a failure would teach the panel to discount real calm. A cycle *outside* the span was never
measured, and that source is reported failed, so `NewsCoverage.summary` renders "no coverage from
<source>" into the prompt. `_render_news` already states the reason this matters: a seat told
nothing happened will reason as though nothing happened.

**A documented deviation from §6.8.3.** That section says the seat shown 8 of 65 items must be told
it is 8 of 65, and that the existing `NewsCoverage` line carries it. It cannot: `NewsCoverage` has
two fields and `summary` counts *sources*, not items. Adding a field is a second `tradebot` change
and this design licenses exactly one (§2.2). So the cut is logged and reconstructible here, and
§6.8.2's `reports=N` carries the clustering half into the prompt; the "8 of 65" figure reaching the
seat is an open item needing either a `NewsCoverage` field or a §6.8.3 correction.

Failure semantics: `snapshot_news` always returns — items and a coverage report — and raises
nothing. There is no source to be down. A window with no rows is a coverage fact, never an
exception, which is the behaviour `NewsHub` already has and the panel already handles.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.publish import archive_path
from decision_lab.archive.published import ArchiveItem, PublishedArchive
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.schema import Money
from tradebot.core.snapshot import NewsCoverage, NewsItemView
from tradebot.interfaces.news import NewsItem, RelevanceFilter
from tradebot.news.hub import DEFAULT_MIN_RELEVANCE, DEFAULT_SNAPSHOT_ITEMS

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Indexed:
    """One archive, with its rows in `observed_at` order and that order extracted for bisect."""

    source: str
    items: tuple[ArchiveItem, ...]
    stamps: tuple[datetime, ...]
    digest: str

    @classmethod
    def of(cls, archive: PublishedArchive) -> _Indexed:
        rows = tuple(sorted(archive.items, key=lambda i: (i.observed_at, i.url)))
        return cls(
            source=archive.header.source,
            items=rows,
            stamps=tuple(i.observed_at for i in rows),
            digest=archive.header.archive_digest,
        )

    def covers(self, as_of: datetime) -> bool:
        """Whether this archive was *measured* at `as_of`. Empty covers nothing."""
        return bool(self.stamps) and self.stamps[0] <= as_of <= self.stamps[-1]

    def window(self, as_of: datetime, lookback: timedelta) -> tuple[ArchiveItem, ...]:
        """`observed_at` in `(as_of - lookback, as_of]`, inclusive at the instant.

        Inclusive because that is the bot's own boundary (`NewsStore.select`). Exclusive would
        silently drop whatever landed exactly on a cycle boundary, which on an 8h grid is a
        recurring class of item rather than a curiosity.
        """
        end = bisect_right(self.stamps, as_of)
        start = bisect_right(self.stamps, as_of - lookback)
        return self.items[start:end]


class ArchiveNewsFeed:
    """The §6.8 feed. Satisfies `NewsFeed`; reaches nothing."""

    def __init__(
        self,
        archives: Sequence[PublishedArchive],
        relevance: RelevanceFilter,
        *,
        window: timedelta,
        min_relevance: Money = DEFAULT_MIN_RELEVANCE,
    ) -> None:
        self._archives = tuple(_Indexed.of(a) for a in archives)
        self._relevance = relevance
        self._window = window
        self._min_relevance = min_relevance

    @property
    def digest(self) -> str:
        """Every source's `archive_digest`, joined in source order. Task 10 threads this into
        `corpus_identity`, so adding a source is a different corpus rather than a quiet change of
        evidence under a stable id."""
        return "+".join(a.digest for a in sorted(self._archives, key=lambda a: a.source))

    def covers(self, as_of: datetime) -> bool:
        return any(a.covers(as_of) for a in self._archives)

    def in_window(self, as_of: datetime) -> tuple[ArchiveItem, ...]:
        """The point-in-time read, before relevance and before the limit. Public because §16.2
        asserts on it directly, and because the drill-down reconstructs a cycle's evidence from
        exactly this set."""
        return tuple(row for a in self._archives for row in a.window(as_of, self._window))

    async def snapshot_news(
        self,
        instruments: tuple[Instrument, ...],
        as_of: datetime,
        limit: int = DEFAULT_SNAPSHOT_ITEMS,
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        """What was knowable at `as_of`, ranked and cut by the bot's own rules."""
        candidates = self.in_window(as_of)
        scored = [(self._relevance.relevance(_as_news_item(row), instruments), row) for row in candidates]
        keep = [pair for pair in scored if pair[0] >= self._min_relevance]
        # `NewsHub.select`'s exact key, reversed. Imported behaviour would be better still; this
        # is the one line of it that is a literal rather than a call, and it is pinned by test.
        keep.sort(key=lambda pair: (pair[0], pair[1].observed_at, pair[1].url_hash), reverse=True)
        views = tuple(_as_news_item(row).view(score) for score, row in keep[:limit])

        coverage = NewsCoverage(
            sources_ok=tuple(a.source for a in self._archives if a.covers(as_of)),
            sources_failed=tuple(a.source for a in self._archives if not a.covers(as_of)),
        )
        logger.debug(
            "archive news selected",
            extra={
                "as_of": as_of.isoformat(),
                "in_window": len(candidates),
                "relevant": len(keep),
                "served": len(views),
            },
        )
        return views, coverage


def _as_news_item(row: ArchiveItem) -> NewsItem:
    """The archive row as the bot's own model, so the bot's own relevance filter scores it.

    `item_id` is the url hash, exactly as `NewsHub._normalize` sets it — an item's identity is
    recomputable from its URL alone, and a second identity scheme would make the drill-down's
    reconstruction a different set from the one the panel read.
    """
    return NewsItem(
        item_id=row.url_hash,
        source_id=row.source_id,
        url=row.url,
        url_hash=row.url_hash,
        title=row.title,
        excerpt=row.summary,
        published_at=row.published_at,
        observed_at=row.observed_at,
    )


def load_feed(
    data_dir: Path, sources: tuple[str, ...], policy: ArchivePolicy
) -> ArchiveNewsFeed:
    """Read each source's published archive and build one feed over all of them.

    The window comes from `policy`, not from each archive's header: a feed reading two archives
    must read them through one window, and a header's own value is the provenance of how it was
    published. Task 10 refuses a mismatch rather than picking one.
    """
    from tradebot.news.relevance import KeywordRelevanceFilter

    archives = tuple(PublishedArchive.read(archive_path(data_dir, source)) for source in sources)
    return ArchiveNewsFeed(archives, KeywordRelevanceFilter(), window=policy.window)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_newsfeed.py -q`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/newsfeed.py decision_lab/tests/test_newsfeed.py
git commit -m "feat(decision_lab): ArchiveNewsFeed - point-in-time selection, no fetcher

Holds no client, no fetcher and no transport, asserted structurally: it cannot
reach the network because it owns no object that could. Only the fetch half is
replaced - relevance, the floor, the ordering and the truncation are the bot's
and are imported, because a replay that ranked news differently would measure
a different pipeline.

Silence and absence are kept apart in the coverage line: a quiet window inside
the archive's span is a fact about the news, while an instant outside it was
never measured and reports no coverage. A seat told nothing happened reasons as
though nothing happened.

Deviates from 6.8.3 and says so in the module docstring: the 8-of-65 cut has
nowhere to ride, because NewsCoverage has two fields and summary counts
sources. A field would be a second tradebot change and the design licenses one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 9: Clustering — annotates, never deletes

**Files:**
- Create: `decision_lab/clustering.py`
- Modify: `decision_lab/newsfeed.py` — fold before scoring; accept the floor
- Test: `decision_lab/tests/test_clustering.py`

**Interfaces:**
- Consumes: `ArchiveItem`, `Modality` (Task 1); `ArchiveNewsFeed` (Task 8).
- Produces:
  - `Cluster(DomainModel)`: `elected: ArchiveItem`, `members: tuple[ArchiveItem, ...]`; property `reports -> int`
  - `cluster_key(item: ArchiveItem) -> tuple[str, tuple[str, ...], tuple[str, ...]]`
  - `fold(items: tuple[ArchiveItem, ...], *, floor: Money) -> tuple[Cluster, ...]`
  - `annotate(cluster: Cluster) -> ArchiveItem` — the elected member with `reports=N` appended to its summary when `N > 1`
  - `ArchiveNewsFeed.__init__` gains `floor: Money`, and `snapshot_news` folds before scoring

**Four rules, and the last two exist because the obvious implementation loses the one item that
mattered.** No row is ever dropped — a cluster elects one member to occupy a slot and the line
carries the count, while every other member stays in the archive and is reconstructible for the
drill-down. The count is information the panel cannot otherwise have: twelve outlets reporting one
event currently render as twelve lines, and repetition reads as corroboration. A cluster is
represented by its **most recent** member as of `as_of`, never its first, or a story's original
claim outranks its own correction. And a member whose `modality` qualifies — `DENIED` or
`CORRECTED` — is **never collapsed at all** and takes its own slot.

**The key is the fields; the floor is only a guard.** §6.8.2.1, measured twice. The bot's embedding
is a hashed bag of n-grams whose own docstring says it captures lexical overlap, not meaning: a
claim and its walk-back score 0.085, and one event under two publishers' wording scores 0.556
against a 0.85 threshold — so a lexical clusterer collapses near-verbatim syndication and nothing
else. Normalising to the summary nearly doubles the separation (1.74× against 1.08×) but *lowers*
the absolute numbers, so a threshold tuned on bodies is tuned against a 0.737 noise floor. Hence:
cluster only when the fields match **and** the summaries clear `floor`. Fail-closed, per §6.8.2.1 —
when in doubt do not cluster, because two slots spent on one event costs a slot while one slot
hiding two events costs the evidence.

**Forward-only, and never precomputed.** A count taken over the whole archive encodes how much
coverage an event *eventually* attracted, which is information from after the cycle — §6.7's
failure arriving through a new door, wearing a deterministic, model-free disguise. `fold` is called
on the window, inside `snapshot_news`, and never cached across cycles.

- [ ] **Step 1: Write the failing test**

```python
# decision_lab/tests/test_clustering.py
"""§6.8.2: a cluster annotates, it never deletes — and a correction is never collapsed.

The two tests that matter most are `test_a_claim_and_its_correction_take_two_slots` and
`test_counts_ignore_rows_observed_after_the_instant`. §16.2 says of the first that the
collapse-to-first implementation passes every other test in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.published import ArchiveItem
from decision_lab.clustering import annotate, cluster_key, fold
from decision_lab.newsfeed import ArchiveNewsFeed
from tradebot.core.instrument import AssetClass, Instrument
from tradebot.news.relevance import KeywordRelevanceFilter

FLOOR = Decimal("0.25")
BTC = Instrument(
    venue="binance", symbol="BTC/USDT", base="BTC", quote="USDT", asset_class=AssetClass.CRYPTO
)


def row(
    *,
    at: datetime,
    summary: str,
    entities: tuple[str, ...] = ("Kraken",),
    event: EventType = EventType.REGULATORY,
    modality: Modality = Modality.CONFIRMED,
    source: str = "cryptoslate",
) -> ArchiveItem:
    link = f"https://{source}.test/{at.isoformat()}"
    return ArchiveItem(
        source_id=source,
        url=link,
        url_hash=f"h{abs(hash(link)) % 100_000}",
        title=summary[:40],
        summary=summary,
        event_type=event,
        entities=entities,
        assets=("BTC",),
        magnitudes=(),
        modality=modality,
        published_at=at - timedelta(minutes=12),
        observed_at=at,
    )


def test_the_key_is_the_three_extracted_fields() -> None:
    """Not a similarity score. The fields are semantic by construction and computed once per
    article at archive-build time, so clustering is deterministic and costs nothing per cycle."""
    one = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained a licence in the UK.")
    assert cluster_key(one) == ("REGULATORY", ("Kraken",), ("BTC",))


def test_two_reports_of_one_event_become_one_slot_carrying_the_count() -> None:
    a = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.")
    b = row(at=datetime(2025, 3, 4, 10, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA today.", source="bitcoincom")

    clusters = fold((a, b), floor=FLOOR)

    assert len(clusters) == 1
    assert clusters[0].reports == 2
    assert "reports=2" in annotate(clusters[0]).summary


def test_a_single_report_carries_no_count() -> None:
    """`reports=1` on every line is noise that trains a reader to ignore the field."""
    one = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence.")
    assert "reports=" not in annotate(fold((one,), floor=FLOOR)[0]).summary


def test_the_most_recent_member_is_elected_never_the_first() -> None:
    """§6.8.2: otherwise a story's original claim outranks its own correction and the correction
    is invisible behind a count."""
    early = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.")
    late = row(at=datetime(2025, 3, 4, 11, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA, sources say.")

    elected = fold((early, late), floor=FLOOR)[0].elected

    assert elected.observed_at == late.observed_at


def test_no_member_is_ever_dropped_from_the_cluster() -> None:
    """"Annotates, never deletes", asserted on the reconstruction rather than on the renderer.
    The bot's own dedup drops at ingest with a `logger.debug`, which is unrecoverable and
    invisible; a replay whose evidence cannot be reconstructed is not evidence."""
    rows = tuple(
        row(at=datetime(2025, 3, 4, 9, n, tzinfo=UTC), summary=f"Kraken gained an EMI licence from the FCA. {n}")
        for n in range(5)
    )
    clusters = fold(rows, floor=FLOOR)
    recovered = {m.url for c in clusters for m in c.members}
    assert recovered == {r.url for r in rows}


def test_a_claim_and_its_correction_take_two_slots() -> None:
    """§6.8.2's worked 09:00/15:00 case, and §16.2 says the collapse-to-first implementation
    passes every other test here. Textually near-identical, and collapsed to either single member
    the panel reads a claim without its retraction or a retraction without its claim."""
    claim = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="A public figure said he is enthusiastic about bitcoin.")
    walkback = row(
        at=datetime(2025, 3, 4, 15, tzinfo=UTC),
        summary="A public figure said he is enthusiastic about bitcoin, his office now says.",
        modality=Modality.CORRECTED,
    )

    clusters = fold((claim, walkback), floor=FLOOR)

    assert len(clusters) == 2
    assert {c.elected.modality for c in clusters} == {Modality.CONFIRMED, Modality.CORRECTED}


def test_a_denial_also_takes_its_own_slot() -> None:
    claim = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.")
    denial = row(at=datetime(2025, 3, 4, 15, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA, it denies.", modality=Modality.DENIED)

    assert len(fold((claim, denial), floor=FLOOR)) == 2


def test_a_proposal_is_not_a_qualifier_and_does_collapse() -> None:
    """`PROPOSED` and `RUMOURED` state a weaker claim, not a retraction of one. Giving each its
    own slot would hand every speculative rewrite a slot and reintroduce the flooding."""
    confirmed = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.")
    proposed = row(at=datetime(2025, 3, 4, 10, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA, expected.", modality=Modality.PROPOSED)

    assert len(fold((confirmed, proposed), floor=FLOOR)) == 1


def test_matching_fields_below_the_floor_do_not_cluster() -> None:
    """§6.8.2.1's known weakness and its guard: "Kraken licensed in the UK" and "...in Germany"
    share a type and an entity. Fail-closed — two slots spent on one event costs a slot, while
    one slot hiding two events costs the evidence."""
    uk = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken obtained electronic money authorisation covering the United Kingdom.")
    de = row(at=datetime(2025, 3, 4, 10, tzinfo=UTC), summary="Separate German federal paperwork concluded; Frankfurt desk opens Monday.")

    assert cluster_key(uk) == cluster_key(de)
    assert len(fold((uk, de), floor=Decimal("0.95"))) == 2


def test_differing_fields_never_cluster_however_similar_the_text() -> None:
    """The key is the fields. A high similarity between two different events is exactly the 15%
    false-merge rate the measurement found, and the key is what keeps it out."""
    one = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.")
    two = row(at=datetime(2025, 3, 4, 10, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA.", event=EventType.LISTING)

    assert len(fold((one, two), floor=Decimal("0.01"))) == 2


@pytest.mark.asyncio
async def test_counts_ignore_rows_observed_after_the_instant() -> None:
    """§6.8.2's forward-only rule. A count over the whole archive encodes how much coverage an
    event *eventually* attracted — information from after the cycle, with no model in it."""
    from decision_lab.tests.test_newsfeed import archive

    at = datetime(2025, 3, 4, 10, tzinfo=UTC)
    early = row(at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Bitcoin ETF approved by the SEC today.")
    later = row(at=datetime(2025, 3, 4, 11, tzinfo=UTC), summary="Bitcoin ETF approved by the SEC today, reports say.", source="bitcoincom")

    feed = ArchiveNewsFeed(
        (archive(early, later),), KeywordRelevanceFilter(), window=timedelta(hours=48), floor=FLOOR
    )
    views, _ = await feed.snapshot_news((BTC,), at, 8)

    assert len(views) == 1
    assert "reports=2" not in views[0].summary


def test_an_empty_window_folds_to_nothing_without_raising() -> None:
    assert fold((), floor=FLOOR) == ()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_clustering.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.clustering'`

- [ ] **Step 3: Write `clustering.py`**

```python
# decision_lab/clustering.py
"""§6.8.2's fold: a cluster annotates, it never deletes.

The feed bypasses `NewsHub._ingest`, so the bot's embedding dedup does not run in a replay —
without this step a replay's eight slots can be filled by eight rewrites of one story where
production's would hold eight distinct events. Clustering is therefore required for *fidelity*,
not for tidiness.

**The key is §6.5's extracted fields, not a similarity score** (§6.8.2.1, measured twice). The
bot's embedding is a hashed bag of unigrams and bigrams whose own docstring says it captures
lexical overlap, not meaning, and the numbers say how little: a claim and its walk-back score
0.085, one event under two publishers' wording scores 0.556, and only near-verbatim syndication
reaches 0.85. Normalising to the summary nearly doubles the separation — 1.74× against bodies'
1.08× — but *lowers* the absolute numbers, so nothing crosses 0.85 after summarising and a
threshold tuned on bodies is tuned against a 0.737 noise floor.

So the fields are the key and the floor is only a guard, for the weakness §6.8.2.1 states rather
than discovers later: two genuinely distinct events sharing a type and an entity — "Kraken licensed
in the UK" and "Kraken licensed in Germany" — collide on that key. Fail-closed applies: **when in
doubt, do not cluster.** Two slots spent on one event costs a slot; one slot hiding two events
costs the evidence.

Four rules, and the last two exist because the obvious implementation loses the one item that
mattered:

* **No row is ever dropped.** The elected member occupies the slot and the line carries the count;
  every other member stays in the cluster and is reconstructible for the drill-down. The bot's own
  dedup drops at ingest with a `logger.debug`, which is unrecoverable and invisible, and a replay
  whose evidence cannot be reconstructed is not evidence.
* **The count is information the panel cannot otherwise have.** Twelve outlets reporting one event
  currently render as twelve lines, and repetition reads as corroboration.
* **The most recent member is elected, never the first**, or a story's original claim outranks its
  own correction and the correction is invisible behind a count.
* **A member whose modality qualifies is never collapsed at all** and takes its own slot. That is
  what `Modality.qualifies` is for, and `PROPOSED`/`RUMOURED` are deliberately excluded from it.

Forward-only by construction: `fold` is handed the window, from inside `snapshot_news`, and is
never cached across cycles. A count over the whole archive would encode how much coverage an event
*eventually* attracted — §6.7's failure through a new door, wearing a deterministic, model-free
disguise.

Failure semantics: pure and total. An empty window folds to nothing.
"""

from __future__ import annotations

from typing import Final

from decision_lab.archive.published import ArchiveItem
from tradebot.core.schema import DomainModel, Money
from tradebot.news.embedding import embed, similarity

#: Rendered onto the elected member's summary when a cluster holds more than one report. Only
#: then: `reports=1` on every line is noise that trains a reader to ignore the field.
COUNT_TEMPLATE: Final = " (reports={count})"

ClusterKey = tuple[str, tuple[str, ...], tuple[str, ...]]


class Cluster(DomainModel):
    """One event, and every report of it that was in the window."""

    elected: ArchiveItem
    members: tuple[ArchiveItem, ...]

    @property
    def reports(self) -> int:
        return len(self.members)


def cluster_key(item: ArchiveItem) -> ClusterKey:
    """§6.8.2.1's key: the event, who it involved, and which assets the text named.

    Sorted, so two summaries listing the same entities in a different order are one event. A
    model's ordering is not a fact about the news.
    """
    return (item.event_type.value, tuple(sorted(item.entities)), tuple(sorted(item.assets)))


def fold(items: tuple[ArchiveItem, ...], *, floor: Money) -> tuple[Cluster, ...]:
    """Collapse reports of one event into one slot, keeping every member.

    Order out is first-appearance order of each cluster, so the caller's own relevance sort is
    what decides the final ordering rather than a fold artefact.
    """
    clusters: list[Cluster] = []
    for item in sorted(items, key=lambda i: (i.observed_at, i.url)):
        if item.modality.qualifies:
            # Never collapsed, in either direction: it does not join a cluster and nothing later
            # joins it. §6.8.2's worked case — a 09:00 claim and a 15:00 walk-back are textually
            # near-identical, and collapsed either way the panel reads one without the other.
            clusters.append(Cluster(elected=item, members=(item,)))
            continue
        home = _home_for(item, clusters, floor)
        if home is None:
            clusters.append(Cluster(elected=item, members=(item,)))
            continue
        index, cluster = home
        # Elected by recency: the loop is in `observed_at` order, so the newest member seen so
        # far is always the right one and `item` is it.
        clusters[index] = cluster.model_copy(
            update={"elected": item, "members": (*cluster.members, item)}
        )
    return tuple(clusters)


def _home_for(
    item: ArchiveItem, clusters: list[Cluster], floor: Money
) -> tuple[int, Cluster] | None:
    """The cluster `item` belongs to: same key, and similar enough to its elected member.

    Compared against the *elected* member rather than any member, so a chain of near-misses
    cannot walk a cluster away from the event it started as.
    """
    key = cluster_key(item)
    for index, cluster in enumerate(clusters):
        if cluster.elected.modality.qualifies or cluster_key(cluster.elected) != key:
            continue
        if similarity(embed(item.summary), embed(cluster.elected.summary)) >= floor:
            return index, cluster
    return None


def annotate(cluster: Cluster) -> ArchiveItem:
    """The elected member as the panel sees it, with the count when there is one to state."""
    if cluster.reports < 2:
        return cluster.elected
    return cluster.elected.model_copy(
        update={
            "summary": cluster.elected.summary + COUNT_TEMPLATE.format(count=cluster.reports)
        }
    )
```

- [ ] **Step 4: Fold inside `snapshot_news`**

In `decision_lab/newsfeed.py`: add `floor: Money` to `__init__` (required, no default — the policy
always has one and a default here would be a second place the number lives), store it, and change
the first two lines of `snapshot_news`:

```python
        candidates = self.in_window(as_of)
        # Forward-only and never precomputed (§6.8.2): `candidates` is already cut at `as_of`, so
        # a count cannot encode coverage the event attracted afterwards.
        elected = tuple(annotate(c) for c in fold(candidates, floor=self._floor))
        scored = [(self._relevance.relevance(_as_news_item(row), instruments), row) for row in elected]
```

and pass `floor=policy.cluster_floor` in `load_feed`. Add `clusters=len(elected)` to the debug log
line, beside `in_window`.

- [ ] **Step 5: Run both files to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_clustering.py decision_lab/tests/test_newsfeed.py -q`
Expected: PASS. `test_newsfeed.py`'s `feed()` helper needs `floor=Decimal("0.25")` added.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/clustering.py decision_lab/newsfeed.py \
        decision_lab/tests/test_clustering.py decision_lab/tests/test_newsfeed.py
git commit -m "feat(decision_lab): clustering annotates, and never collapses a correction

Required for fidelity rather than tidiness: the feed bypasses NewsHub._ingest,
so without a fold a replay's eight slots can hold eight rewrites of one story
where production's would hold eight events.

The key is the extracted fields and the floor is only a guard, because the
bot's embedding scores a claim and its walk-back at 0.085 and one event under
two wordings at 0.556 against a 0.85 threshold - the wrong instrument rather
than a mistuned one. Fail-closed on the known weakness: when in doubt do not
cluster, because two slots on one event costs a slot while one slot hiding two
events costs the evidence.

No row is dropped, the newest member is elected so a correction is never
hidden behind a count, and a DENIED or CORRECTED member is never collapsed in
either direction.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 10: Wire the feed into `corpus build`, and the two §6.7 banners

**Files:**
- Modify: `decision_lab/corpus.py` — `build(..., news_feed=…)`, and the digest it threads
- Modify: `decision_lab/cli.py` — `corpus build --news-source` (repeatable) and `--policy`
- Modify: `decision_lab/render.py` — the two banners and their provenance block
- Modify: `decision_lab/analysis.py` — carry the provenance through the one read assembly
- Test: `decision_lab/tests/test_corpus.py` (extend), `decision_lab/tests/test_render.py` (extend)

**Interfaces:**
- Consumes: `load_feed`, `ArchiveNewsFeed.digest` (Tasks 8–9); `load_policy` (Task 2); the seam (Task 7).
- Produces:
  - `corpus.build(..., news_feed: ArchiveNewsFeed | None = None)` — passes it to `build_sim` and uses `news_feed.digest` as `archive_digest`
  - `NewsProvenance(DomainModel)`: `sources: tuple[str, ...]`, `summarizer_model: str`, `prompt_digest: str`, `observed_at_policy: str`, `window_seconds: int`, `cluster_floor: Money`
  - `render.summarized_news(provenance) -> str`, `render.reconstructed_news(provenance) -> str`
  - `LabReport.news_archive: NewsProvenance | None = None`
  - CLI: `corpus build … [--news-source <id> ...] [--policy <toml>]`

**`archive_digest` is already a parameter of `build` and already feeds `corpus_identity`.** This
task only supplies it from a real archive instead of `""`. `corpus_identity` stays exactly four
components — a fifth would recompute the id of every corpus ever built, including the complete
540-cycle `8ac130d8…`, and every existing corpus has `archive_digest = ""` so nothing is renumbered.

**A news-fed corpus and a news-blind one are different corpora, and that is the design working.**
`news_blind = not archive_digest` is already on `CorpusMeta`. An operator who builds both over the
same window gets two directories and two ids, which is how the news contribution is measured rather
than argued about.

**Both banners are unconditional on a news-fed report, verdict regardless.** §6.7: a run whose
evidence was partly derived must never be quotable as one whose evidence was recorded. They render
with the other banners, above the identity block, exactly as the contamination banner does — a
reader must meet them before they meet a number.

- [ ] **Step 1: Write the failing tests**

Append to `decision_lab/tests/test_corpus.py`:

```python
def test_a_news_fed_corpus_has_a_different_id_from_a_news_blind_one() -> None:
    """`archive_digest` feeds `corpus_identity`, so the two are two experiments. Nothing existing
    is renumbered: every corpus built so far has `archive_digest = ""`."""
    blind = corpus_identity(
        dataset_digest="d", reference_config_digest="c", cadence_seconds=28_800, archive_digest=""
    )
    fed = corpus_identity(
        dataset_digest="d", reference_config_digest="c", cadence_seconds=28_800, archive_digest="a1"
    )
    assert blind != fed


def test_corpus_identity_still_takes_exactly_four_components() -> None:
    """§5.4. A fifth would recompute the id of every corpus ever built, including the complete
    540-cycle 8ac130d8. The window rides in `archive_digest` for exactly this reason."""
    assert set(inspect.signature(corpus_identity).parameters) == {
        "dataset_digest",
        "reference_config_digest",
        "cadence_seconds",
        "archive_digest",
    }


@pytest.mark.asyncio
async def test_build_with_a_feed_records_the_digest_and_is_not_news_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow the existing `built_corpus` helper in `test_slice_b_end_to_end.py` for the dataset
    and workspace setup, including the `STUB_SEED` pin."""
    ...


@pytest.mark.asyncio
async def test_build_without_a_feed_stays_news_blind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ...
```

Append to `decision_lab/tests/test_render.py`:

```python
def test_a_news_fed_report_carries_both_provenance_banners() -> None:
    """§6.7: a run whose evidence was partly derived must never be quotable as one whose evidence
    was recorded. Unconditional on a news-fed report, whatever the verdict."""
    page = report_markdown(_report_with_news())

    assert "SUMMARIZED NEWS" in page
    assert "RECONSTRUCTED NEWS" in page
    assert "NEWS-BLIND RUN" not in page


def test_both_banners_name_what_a_reader_needs_to_distrust_the_number() -> None:
    page = report_markdown(_report_with_news())

    assert "stub-summary" in page          # which model wrote the excerpts
    assert "published_at+00:12:00" in page  # how observed_at was derived
    assert "0.25" in page                   # the floor the clustering used


def test_the_banners_precede_the_identity_block() -> None:
    """Rendered above the identity block, exactly as the contamination banner is: a reader must
    meet them before they meet a number."""
    page = report_markdown(_report_with_news())
    assert page.index("SUMMARIZED NEWS") < page.index("## Identity")


def test_a_news_blind_report_carries_neither() -> None:
    page = report_markdown(_report_without_news())
    assert "SUMMARIZED NEWS" not in page
    assert "NEWS-BLIND RUN" in page


def test_a_stub_summarised_archive_stamps_the_plumbing_check_banner() -> None:
    """§7.2's whole-run label, one layer out. The archive's header records the provider
    truthfully, but a header nobody reads is not a banner: published and reported, a
    stub-summarised archive carries a real-looking `archive_digest` over canned text and every
    table on the page describes a measurement that did not happen."""
    page = report_markdown(_report_with_news(is_stub=True))

    assert "PLUMBING CHECK" in page
    # And both provenance banners still render: the run is a plumbing check *and* its evidence
    # was model-written and synthetically timed. Three facts, none of which implies the others.
    assert "SUMMARIZED NEWS" in page
    assert "RECONSTRUCTED NEWS" in page


def test_a_real_summarised_archive_does_not_stamp_it() -> None:
    """The guard above must not pass by the banner being unconditional."""
    assert "PLUMBING CHECK" not in report_markdown(_report_with_news(is_stub=False))


def test_the_flag_is_derived_from_the_header_and_never_passed() -> None:
    """One value that must agree with the header is one that can disagree with it. Assert on the
    assembly in `analysis.py`, not on a hand-built `NewsProvenance`: build a provenance from an
    `ArchiveHeader` whose `summarizer_provider` is "stub" and check the flag followed."""
    ...
```

**The derivation lives in `analysis.py`**, the one read assembly, as
`is_stub=header.summarizer_provider == "stub"` — alongside the fields it already copies out of the
header. Not in `render`, which must stay a renderer, and not on `ArchivePolicy`, whose `is_stub`
answers a question about a *policy file* while this one answers it about a published archive. The
two agree whenever the archive was published under the policy that summarised it, and the point of
reading the header is that this is not guaranteed.

**Note for the implementer:** `_report_with_news()` / `_report_without_news()` are local helpers
built from whatever `LabReport` factory `test_render.py` already uses — extend that one rather
than building a second. Check the exact identity-block heading string before asserting on
`"## Identity"`; use what `_identity` actually emits.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render.py decision_lab/tests/test_corpus.py -q`
Expected: FAIL — `ImportError` on the two banner functions and on `NewsProvenance`.

- [ ] **Step 3: Add the banners to `render.py`**

```python
class NewsProvenance(DomainModel):
    """How a news-fed run's evidence was produced. Rendered as §6.7's two banners.

    It is a model rather than six parameters because `analysis.py` is the one read assembly and
    carries it through to the CLI, the dashboard and the notebook alike — a second shape would be
    §14's rejected second `report` command arriving through another door.
    """

    sources: tuple[str, ...]
    summarizer_model: str
    prompt_digest: str
    observed_at_policy: str
    window_seconds: int
    cluster_floor: Money
    #: §7.2's whole-run label, one layer out. The archive's header records the provider truthfully,
    #: but a *header* nobody reads is not a banner: a stub-summarised archive published and
    #: reported would carry a real-looking `archive_digest` over canned text, and every table on
    #: the page would describe a measurement that did not happen. Derived from the header, never
    #: passed — one value that must agree with the header is one that can disagree with it.
    is_stub: bool = False


def summarized_news(provenance: NewsProvenance) -> str:
    """§6.7. The excerpts the panel read were written by a model, not by a publisher.

    A pre-trained model's weights encode the outcomes it is being asked to summarise toward, and
    memorisation is non-uniform across entities — heavily-covered names leak far more than
    obscure ones. BTC and ETH over 2025 are about the most-covered assets there are, so this
    banner is not a formality.
    """
    return (
        f"**SUMMARIZED NEWS** — the excerpts in every snapshot are model-generated "
        f"(model=`{provenance.summarizer_model}`, prompt=`{provenance.prompt_digest}`), not the "
        "publishers' own text. §6.5 closes the hindsight channel by prompt, by what the model was "
        "not given, by binding and by a traceability check — but only the last of those produces "
        "a number, and none of them is a guarantee."
    )


def reconstructed_news(provenance: NewsProvenance) -> str:
    """§6.7. `observed_at` was derived, so "what was knowable" is a policy, not a record."""
    return (
        f"**RECONSTRUCTED NEWS** — `observed_at` is synthetic "
        f"(policy=`{provenance.observed_at_policy}`). No source here publishes the moment *we* "
        f"learned something, so it is derived from the publication instant. Read through a "
        f"{provenance.window_seconds // 3600}h window over "
        f"{', '.join(provenance.sources)}, clustering on the §6.5 fields above a "
        f"{provenance.cluster_floor} similarity floor."
    )
```

On `LabReport`, beside `news_blind`:

```python
    #: §6.7's provenance, present exactly when a news archive was wired. `news_blind` is its
    #: inverse and both are kept: a reader scanning for one banner must not have to infer the
    #: other's absence.
    news_archive: NewsProvenance | None = None
```

In `report_markdown`, replacing the `news_blind` branch:

```python
    if report.news_blind:
        sections += ["", NEWS_BLIND]
    elif report.news_archive is not None:
        sections += ["", summarized_news(report.news_archive), "", reconstructed_news(report.news_archive)]
```

and the existing `plumbing_check` branch above it gains the archive as a second trigger, so one
banner answers for both ways a run can measure canned text:

```python
    if report.plumbing_check or (report.news_archive is not None and report.news_archive.is_stub):
        sections += ["", PLUMBING_CHECK]
```

`PLUMBING_CHECK`'s own wording is about candidates binding the stub, which is now half the story.
Extend it with one sentence rather than adding a second banner — a reader meeting two
nearly-identical red blocks learns to skim both:

```python
PLUMBING_CHECK: Final = (
    "**PLUMBING CHECK — NOT AN EVALUATION.** At least one candidate in this run binds the "
    "offline stub, whose votes are drawn from a fixed catalogue, **or the news archive it read "
    "was summarised by that stub.** Every table below exercises the sweep, the scoring and this "
    "page; none of it measures any model's judgement. Re-run with a matrix bound to real "
    "providers, over an archive summarised by one, to learn something."
)
```

Mirror the same block in the `calibrate long` page at `render.py:667`, where `BANNER` is repeated —
§10.4's page states it keeps the same banner order, and a page that dropped these two would be the
one report quotable as recorded evidence.

- [ ] **Step 4: Thread the feed through `corpus.build`**

Signature gains, after `archive_digest`:

```python
    news_feed: ArchiveNewsFeed | None = None,
```

and `archive_digest`'s default becomes derived rather than passed when a feed is present. At the
top of the body:

```python
    # §6.6: the archive's digest *is* the news component of `corpus_id`, so it is read off the
    # feed rather than passed beside it — two arguments that must agree is one that can disagree.
    if news_feed is not None:
        if archive_digest:
            raise ConfigError(
                "pass either a news feed or an archive_digest, never both: the feed carries its "
                "own digest and two values that must agree is one that can disagree"
            )
        archive_digest = news_feed.digest
```

and the `build_sim` call's news block becomes:

```python
        # §6.9 when there is no feed: the snapshot records "no sources configured" rather than
        # leaving the panel to read an empty news list as a quiet market.
        news_sources=(),
        news_feed=news_feed,
```

- [ ] **Step 5: Add the CLI flags and refusals**

On `corpus_build_parser`:

```python
    corpus_build_parser.add_argument(
        "--news-source",
        action="append",
        default=[],
        dest="news_sources",
        metavar="SOURCE",
        help="a published archive to read, repeatable. Omitted, the pass is NEWS-BLIND (§6.9) "
        "and the report says so",
    )
    corpus_build_parser.add_argument(
        "--policy", type=Path, default=DEFAULT_POLICY_FILE, help="archive.toml (§6.6)"
    )
```

In the handler, before building:

```python
    # One feed reads N archives through *one* window, so a header disagreeing with the policy in
    # force is a refusal rather than a silent choice between them: the window is inside
    # `archive_digest`, and reading an archive through a window it was not published for would
    # produce a corpus id describing evidence nobody selected.
    feed = load_feed(args.data, tuple(args.news_sources), policy) if args.news_sources else None
```

and a check inside `load_feed` (add it there, not in the CLI, so the dashboard and the notebook
inherit it):

```python
    mismatched = [
        a.header.source
        for a in archives
        if a.header.news_window_seconds != policy.news_window_seconds
        or a.header.cluster_floor != policy.cluster_floor
    ]
    if mismatched:
        raise ConfigError(
            f"{mismatched} were published under a different window or floor than "
            f"{policy} declares. Re-publish them, or point --policy at the archive.toml they "
            "were published with: the window and the floor are inside archive_digest, so "
            "reading an archive through a policy it was not published for yields a corpus id "
            "describing evidence nobody selected"
        )
```

- [ ] **Step 6: Run the touched suites**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_corpus.py decision_lab/tests/test_render.py decision_lab/tests/test_cli_archive.py decision_lab/tests/test_analysis.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add decision_lab/corpus.py decision_lab/cli.py decision_lab/render.py \
        decision_lab/analysis.py decision_lab/newsfeed.py \
        decision_lab/tests/test_corpus.py decision_lab/tests/test_render.py
git commit -m "feat(decision_lab): a news-fed corpus, and the two provenance banners

archive_digest was already a parameter of corpus.build and already fed
corpus_identity; this only supplies it from a real archive. corpus_identity
stays four components, so no existing corpus is renumbered - every one built
so far has archive_digest = \"\".

Both 6.7 banners are unconditional on a news-fed report and render above the
identity block: a run whose evidence was partly derived must never be quotable
as one whose evidence was recorded. They name the model, the prompt digest, the
observed_at policy, the window and the floor - everything a reader needs in
order to distrust the number below them.

load_feed refuses an archive published under a different window or floor than
the policy in force, because both are inside archive_digest.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 11: The §16.2 contamination tests, the slice exit criterion, and the docs

**Files:**
- Create: `decision_lab/tests/test_contamination.py`
- Create: `decision_lab/tests/test_slice_e_end_to_end.py`
- Modify: `decision_lab/PROGRESS.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-08-23-decision-lab-design.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no new surface. This task is the proof and the record.

**The nine §16.2 rows, and the one that matters most.** The third — an archive holding one item
`observed_at` before the replay instant and one after it, with the snapshot carrying the first and
not the second, through the **real `ContextBuilder`** — is the exact scenario §2.2 describes, and it
fails if anyone ever reintroduces a fetch into the replay, including by wiring a live `NewsHub`
back in. Task 8 asserts the same property on the feed in isolation; both are wanted, because the
unit test names the field and this one proves the path.

- [ ] **Step 1: Write the contamination suite**

```python
# decision_lab/tests/test_contamination.py
"""§16.2: the point-in-time and hindsight guards, asserted against the real failure.

Nine rows in the spec's table. The third is the one that matters most and it runs through the
real `ContextBuilder` rather than against the feed in isolation — a test of the feed proves the
feed, and what is at stake is the snapshot.
"""

from __future__ import annotations

import pytest

# --- §16.2 row 1 ----------------------------------------------------------------------------
def test_the_feed_holds_no_fetcher_and_no_http_client() -> None:
    """Also asserted in `test_newsfeed.py`; repeated here because this file is where a reader
    looks for the contamination guarantees, and a guarantee split across two files is one
    somebody deletes half of."""
    ...


# --- §16.2 row 2 ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_corpus_cycle_completes_behind_a_transport_that_raises_on_any_request() -> None:
    """No network call happens on the real path, not merely in principle. Monkeypatch the
    `httpx` transport the bot's `FeedFetcher` and provider adapters build so that *any* request
    raises, then drive one `corpus build` through on the stub panel. A pass that completes proves
    nothing reached out; a pass that fails names which component did."""
    ...


# --- §16.2 row 3 — the one that matters most -------------------------------------------------
@pytest.mark.asyncio
async def test_the_snapshot_carries_the_earlier_item_and_not_the_later_one() -> None:
    """The exact scenario §2.2 describes, through the real `ContextBuilder` output. Build an
    archive with one item observed before the replay instant and one after it, drive one cycle,
    read `SNAPSHOT_FROZEN`'s news list out of the event log, and assert on titles.

    It fails if anyone reintroduces a fetch into the replay — including by wiring a live
    `NewsHub` back in, which is the failure mode no amount of care inside this package can
    prevent from outside."""
    ...


# --- §16.2 rows 4 and 5 ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_summarizer_is_never_called_during_a_corpus_sweep_or_calibration() -> None:
    """§6.5's reuse promise, and that a sweep's spend is the panel's alone. Pass a provider whose
    `complete` raises `AssertionError` as the *summariser* binding, publish an archive first with
    a working one, then run `corpus build`, `sweep` and `calibrate normal` over it."""
    ...


@pytest.mark.asyncio
async def test_the_summarizers_prompt_is_given_no_instrument_no_market_data_no_sibling() -> None:
    """Asserted on the recorded call arguments rather than on the prompt text, per §16.2. Run a
    pass over an archive of three rows with `StubLLMProvider`, then walk `provider.calls`: each
    `user` must contain that row's own title and body and **no other row's**, and neither `user`
    nor `system` may contain an instrument symbol, a price, or an indicator name."""
    ...


# --- §16.2 row 6 ----------------------------------------------------------------------------
def test_a_sampled_summarys_clauses_trace_to_ngrams_of_its_own_body() -> None:
    """§6.5's traceability check — the only one of the four closures that produces a number
    rather than an argument. A summary asserting something the article does not say fails, and it
    fails identically whether the claim came from hindsight, from priors, or from invention.

    Implement as: split the summary on clause boundaries, take 3-grams of each clause, and
    require a configurable share to appear in the body's 3-gram set. Assert the *mechanism* here
    on a handmade pair — a faithful summary passes, a summary carrying "analysts expect further
    upside" against a body that never says it fails. The number over the real archive is a
    measurement for the operator to run after the real pass, not a test fixture."""
    ...


# --- §16.2 rows 7, 8 and 9 ------------------------------------------------------------------
def test_a_claim_and_its_correction_yield_two_slots() -> None:
    """Covered in `test_clustering.py`; re-asserted here through `snapshot_news` so the guarantee
    holds at the seam the panel actually reads."""
    ...


@pytest.mark.asyncio
async def test_cluster_counts_for_a_cycle_ignore_rows_observed_after_it() -> None:
    """Forward-only. A count over the whole archive is look-ahead with no model in it."""
    ...


@pytest.mark.asyncio
async def test_no_archive_row_is_absent_from_the_reconstruction_of_a_collapsed_cycle() -> None:
    """"Annotates, never deletes", asserted on the reconstruction rather than on the renderer:
    for a cycle whose window collapsed twelve rows into one slot, `feed.in_window(as_of)` must
    still return all twelve."""
    ...
```

**Note for the implementer:** every body above is `...` on purpose — these are the nine
assertions, and each one's *setup* is substantial and must reuse existing fixtures rather than
invent parallel ones. Before writing any of them, read `test_slice_b_end_to_end.py`'s
`built_corpus` helper (dataset, workspace, and the `STUB_SEED = 2024` pin) and
`test_slice_d_end_to_end.py`'s sweep and calibration drivers. **Write them one at a time, running
each before starting the next**, and start with row 3 — it is the one the spec singles out, and
getting its setup right gives rows 2, 4 and 8 most of theirs.

- [ ] **Step 2: Run them one at a time as you write them**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_contamination.py -q`
Expected: each new test FAILs meaningfully before its implementation and PASSes after. A test
that passes the moment it is written is asserting nothing — delete it and reconsider.

- [ ] **Step 3: Write the slice exit criterion**

```python
# decision_lab/tests/test_slice_e_end_to_end.py
"""Slice E end to end, offline: crawl → summarise → publish → corpus → report.

One pass, on the stub provider and the stub panel, over a handmade dataset and a handmade raw
store. It asserts the loop closes and that the page says what it measured — not that any model's
judgement was measured, which is what the real pass is for.

Assert on the markup only the deriving branch emits, never on a bare value. That rule was learned
expensively in pass 2: three assertions on that branch were vacuous substring matches against a
whole rendered page, including its own exit criterion.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_the_loop_closes_from_a_raw_store_to_a_news_fed_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice's exit criterion.

    1. A handmade dataset, verified, with a pinned day set.
    2. A handmade raw store of ~12 rows across one month, two sources, including one pair that
       clusters and one claim/correction pair that must not.
    3. `summarize_store` driven by a **scripted** `StubLLMProvider` → every row summarised.
       Scripted, not default: an unscripted stub answers with a trading decision and refuses
       every row, which is the behaviour Task 5's `--plumbing-check` refusal exists to make
       loud. Scripting is the fault-injection path and a script outranks the model name, so a
       valid canned `SummaryResponse` is exactly what an offline end-to-end test should use.
       Drive the function, not the CLI — the CLI's refusal is Task 5's test, not this one's.
    4. `archive publish` → two `news.jsonl` files, no body in either.
    5. `corpus build --news-source cryptoslate --news-source bitcoincom` → a corpus whose
       `news_blind` is False and whose id differs from the same window built blind.
    6. `report --corpus <id>` → a page carrying both §6.7 banners, the `PLUMBING CHECK` banner
       (the archive was stub-summarised, and the page must say so), and no `NEWS-BLIND RUN`.
    7. At least one cycle's snapshot carries at least one news item, read out of the event log.

    Step 7 is the load-bearing one: steps 1-6 can all pass over an archive the panel never saw.
    """
    ...


def test_the_bot_diff_is_exactly_the_licensed_surface() -> None:
    """The slice's structural exit criterion. `git diff --stat main -- tradebot/` must name
    `app.py` and nothing else: slice E is licensed one bot change (§2.2), and the seam's guards
    live in `tests/`, which this check does not cover and which is why it is named explicitly.

    Implement with `subprocess.run(["git", "diff", "--stat", "main", "--", "tradebot/"])` and
    assert the changed-file set is `{"tradebot/app.py"}`. Skip when `main` is not resolvable, so
    a shallow CI checkout does not fail for the wrong reason."""
    ...
```

- [ ] **Step 4: Run it**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_slice_e_end_to_end.py -q`
Expected: PASS

- [ ] **Step 5: Run both authoritative gates, in the foreground, and record the numbers**

Run: `.\decision_lab\check.ps1`
Expected: PASS — format, lint, mypy over the package, and the whole tool suite.

Run: `.\check.ps1`
Expected: PASS — including every coverage gate. **The root gate exceeds the Bash tool's 600s cap,
so it is the controller's to run, never an implementer's.** Record the commit it ran against: a
number that does not name its commit is not evidence.

- [ ] **Step 6: Update the docs**

- `decision_lab/PROGRESS.md`: slice E's table row becomes ✅; tick the four checkboxes at lines
  214-218; replace *"What is still owed is the `tradebot` seam, `ArchiveNewsFeed`, and the
  summarising pass"* with what is now owed — the real summarising run, a 2025 corpus, and its
  calibration. Add the new commands to *What you can run today*. Move the §6.8.3 cut into
  *Open items inside what already shipped*, with its reasoning.
- `CLAUDE.md`: in the `decision_lab` tree, add `archive/` with its five new modules, `newsfeed.py`
  and `clustering.py`. In the rules list, add the four from this plan that are easiest to get
  backwards: the two closed vocabularies and why; the floor as a guard rather than a key; a
  refusal never falling back to the publisher's abstract; and silence versus absence in the
  coverage line. Update *Phase status* to note slice E's code is complete and its archive is not
  yet summarised.
- The spec: mark §6.8.3's cut as unimplemented with the reason, and record that §6.6's single-file
  format is kept deliberately against month shards. **Do not quietly edit §6.8.3's claim away** —
  a spec that silently agrees with the code is a spec that cannot catch the code.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/tests/test_contamination.py decision_lab/tests/test_slice_e_end_to_end.py \
        decision_lab/PROGRESS.md CLAUDE.md docs/superpowers/specs/2026-08-23-decision-lab-design.md
git commit -m "test(decision_lab): slice E's contamination guards and its exit criterion

The nine 16.2 rows, and the third is the one that matters: an archive holding
one item observed before the replay instant and one after it, driven through
the real ContextBuilder, with the snapshot carrying the first and not the
second. It fails if anyone reintroduces a fetch into a replay - including by
wiring a live NewsHub back in, which is the one failure no care inside this
package can prevent from outside.

The exit criterion runs the whole loop offline: raw store, summarise, publish,
news-fed corpus, report with both provenance banners. Its load-bearing step is
the last one - the other six can all pass over an archive the panel never saw.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

Run after the plan is written, before execution. Findings fixed inline.

**Spec coverage.** §2.2 → Task 7. §2.3's seven guards → Task 7. §2.4 → the import choices in
Tasks 3, 6 and 8. §6.4 → Tasks 1 and 6. §6.5 → Tasks 2, 3, 4. §6.6 → Tasks 1, 6, 10. §6.7 →
Tasks 6 and 10. §6.8 → Task 8. §6.8.1 → Tasks 2 and 8. §6.8.2 and §6.8.2.1 → Task 9. §6.9 →
Tasks 6 and 10. §16.2 → Task 11. **§6.8.3 is the one gap and it is deliberate, documented in
Task 8 and in the open items below.**

**Type consistency.** `ArchiveItem` is written by Task 6 and read by Tasks 8, 9 and 11;
`SummaryResponse`'s six fields map onto `RawArticle`'s five new ones plus `summary` in Task 4's
`model_copy`; `ArchivePolicy.cluster_floor` reaches `fold(floor=…)` through
`ArchiveNewsFeed.__init__`; `prompt_digest` is computed in Task 3 and consumed in Task 6's header
and Task 10's banner. `Money` is the money alias throughout — no `Decimal` annotation appears in a
signature.

**Three things an implementer must verify rather than trust in this plan:**

1. `MoneyError`'s base class is `FailClosedError`, **not** `ValueError` — so Task 2's `except
   Exception` is deliberate and must not be narrowed. Confirmed against
   `tradebot/core/errors.py:43`.
2. `_extract_object` is private in `tradebot/decision/seat.py:56`. Task 3 imports it on purpose
   and says why; if a reviewer objects, the answer is §2.4, not a second extractor.
3. `Application.runners.news_feed` is the attribute Task 7's test asserts on. Check
   `RunnerBuilder`'s field name before writing it — the plan reads it from `app.py:1004`'s
   keyword, which is `news_feed=news_feed` into `RunnerBuilder`, and the stored attribute may be
   `_news_feed`.

---

## When the real key arrives

Everything above ships and is green on the stub. Wiring a real model is then **a two-line edit to
one file and a sequence of batches** — no code change, which is the property `archive.toml` exists
to give.

1. **Verify the model id before spending anything.** The three `:free` slots in
   `decision/presets.py` are 2024-era and unchecked against openrouter.ai/models (R11). A retired
   id does not degrade quietly here — it raises `ProviderError` on every row, so the pass refuses
   14 484 times and writes nothing. Cheap to check, expensive to skip.

2. **Edit `decision_lab/config/archive.toml`'s binding only.** The three `[archive]` numbers must
   not move in the same edit: they are inside `archive_digest`, so changing a window or a floor
   alongside the model makes it impossible to attribute a later difference to the model rather
   than to the reading policy.

   ```toml
   [summarizer]
   provider = "openrouter"
   model = "<a verified id>"
   ```

   ```powershell
   $env:OPENROUTER_API_KEY = "..."
   ```

3. **Price it before running it** — zero calls, then one small batch:

   ```powershell
   .venv\Scripts\python.exe -m decision_lab archive summarise --data data\history-2025 `
       --source cryptoslate --dry-run                     # rows and prompt tokens, no calls
   .venv\Scripts\python.exe -m decision_lab archive summarise --data data\history-2025 `
       --source cryptoslate --limit 50                    # read per_row_cost off this
   ```

   `per_row_cost × pending` prices the rest. The `--plumbing-check` refusal is now *inactive*,
   because the binding is no longer the stub — the flag doing its job, and needing no removal.

4. **Then the batches**, `--limit 1000` at a time, each of which commits. `rewrite` replaces the
   store from rows read at the *start* of a pass, so one long pass risks everything it collected
   to a single late failure — the lesson `backfill` learned over seven batches. Resumable by
   construction: a re-run asks only for rows not yet summarised.

   ```powershell
   .venv\Scripts\python.exe -m decision_lab archive summarise --data data\history-2025 `
       --source cryptoslate --limit 1000                  # repeat until requested: 0
   .venv\Scripts\python.exe -m decision_lab archive summarise --data data\history-2025 `
       --source bitcoincom  --limit 1000
   ```

5. **Measure the traceability share over a sample before publishing.** It is the only one of
   §6.5's four hindsight closures that produces a number, and it is worth having *before* a corpus
   is built on the archive rather than after.

6. **Publish, then build** — and note what this does to identity, because it is the design working
   rather than a problem. A real binding changes the header's model and prompt digest, so
   `archive_digest` changes, so `corpus_id` changes. **Any corpus built over a stub-summarised
   archive is a different corpus and stays one.** Nothing is overwritten; delete its workspace
   directory if you want that id back.

   ```powershell
   .venv\Scripts\python.exe -m decision_lab archive publish --data data\history-2025 --source cryptoslate
   .venv\Scripts\python.exe -m decision_lab archive publish --data data\history-2025 --source bitcoincom
   .venv\Scripts\python.exe -m decision_lab corpus build --data data\history-2025 --every 8h `
       --reference-panel sim --news-source cryptoslate --news-source bitcoincom
   ```

   That last command is where the §5 race still bites — see the open items.

---

## Open items this plan deliberately leaves

- **§6.8.3's "8 of 65" cut does not reach the seat.** `NewsCoverage` has two fields and
  `summary` counts sources, so the figure has nowhere to ride, and a field is a second `tradebot`
  change against a one-change licence. Task 9's `reports=N` carries the clustering half. Closing
  it properly is either a `NewsCoverage` field — a decision about the bot, with its own guard
  tests — or a §6.8.3 correction. **The operator's call, not an implementer's.**
- **The real summarising run is not in this plan**, by the chosen scope. ~11 894 rows in 2025
  proper, 14 484 across both stores including the adjacent years a boundary sitemap chunk brought
  in. *When the real key arrives* above is the whole of it — no code changes, and the first two
  commands cost nothing.
- **No archive this plan produces is a measurement.** On the stub every row is refused, so a
  stub-built corpus is news-blind in substance even where it is news-fed in wiring. The guards
  added for that are two: `archive summarise` refuses a stub binding without `--plumbing-check`,
  and any report over a stub-summarised archive carries `PLUMBING CHECK`. Both exist so that the
  honest version of "we wired news" cannot be mistaken for "we measured news".
- **The traceability number is not measured here.** Task 11 asserts the mechanism on a handmade
  pair. The share over the real archive is a measurement to take after the real pass, and it is
  the only one of §6.5's four closures that produces a number.
- **The 2025 corpus still carries the KNOWN_GAPS §5 intra-cycle race, unseeded.** `corpus build`
  has no `--seed`, `BacktestHarness._replay` polls only after `worker.cycle()`, and both prior
  ~1 080-cycle passes died short. Nothing in this plan changes that, and a news-fed 2025 corpus is
  ~1 095 cycles. Weigh closing §5's intra-cycle half first; it is a money-path ordering change and
  a decision of its own.
- **CoinDesk 2024 is not summarised either.** It is a complete full-body store over the *price*
  dataset's window rather than the news year. Everything in this plan works on it unchanged —
  `archive summarise --data data\history --source coindesk` — but 2024 has the corpus and 2025 has
  the news, and reconciling that is the sequencing decision above.
- **`calibrate long` has no news wiring yet.** It was scoped out of slice E (D5); a six-month
  profit run is still news-blind, whatever corpus its sibling commands read.
- **Archive coverage is [first row, last row]**, because the header declares no captured-from/to
  span. A span in the header would let trailing silence read as a quiet market rather than as "no
  coverage".
- **Per-row summariser provenance was added to `RawArticle`** (`summary_provider`, `summary_model`,
  `summary_prompt_digest`), and `archive publish` refuses a store whose rows mix provenance
  (ruling B10), so one archive digest never vouches for two prompts' output.
