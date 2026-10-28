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


def item(
    *, url: str = "https://x.test/a", summary: str = "Kraken gained an EMI licence."
) -> ArchiveItem:
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
    path.write_text(
        path.read_text(encoding="utf-8") + '{"kind": "item", "title"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=r"news\.jsonl:3"):
        PublishedArchive.read(path)


def test_a_file_whose_first_line_is_not_a_header_is_refused(tmp_path: Path) -> None:
    """The header is line 1 by §6.6's format. A file without one has no declared provenance, and
    an archive whose `observed_at` policy is unknown cannot be read point-in-time at all."""
    path = tmp_path / ARCHIVE_FILE
    path.write_text(item().model_dump_json() + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="header"):
        PublishedArchive.read(path)


def test_an_edited_row_is_refused_by_the_digest(tmp_path: Path) -> None:
    path = tmp_path / ARCHIVE_FILE
    PublishedArchive.write(path, header(), (item(),))
    text = path.read_text(encoding="utf-8").replace("Kraken gained", "Kraken lost")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="edited since it was published"):
        PublishedArchive.read(path)


def test_an_edited_digest_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ARCHIVE_FILE
    PublishedArchive.write(path, header(), (item(),))
    stamped = PublishedArchive.read(path).header.archive_digest
    path.write_text(
        path.read_text(encoding="utf-8").replace(stamped, "0" * len(stamped)), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="edited since it was published"):
        PublishedArchive.read(path)


def test_an_empty_digest_is_refused_by_name(tmp_path: Path) -> None:
    """An empty digest is the news-blind corpus identity, so it must not read as 'unchecked'."""
    import json

    path = tmp_path / ARCHIVE_FILE
    PublishedArchive.write(path, header(), (item(),))
    lines = path.read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0]) | {"archive_digest": ""}
    path.write_text("\n".join([json.dumps(head), *lines[1:]]) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"news\.jsonl carries no archive_digest"):
        PublishedArchive.read(path)
