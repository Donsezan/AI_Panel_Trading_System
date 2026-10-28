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
from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.news.relevance import KeywordRelevanceFilter

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
    a = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA."
    )
    b = row(
        at=datetime(2025, 3, 4, 10, tzinfo=UTC),
        summary="Kraken gained an EMI licence from the FCA today.",
        source="bitcoincom",
    )

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
    early = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA."
    )
    late = row(
        at=datetime(2025, 3, 4, 11, tzinfo=UTC),
        summary="Kraken gained an EMI licence from the FCA, sources say.",
    )

    elected = fold((early, late), floor=FLOOR)[0].elected

    assert elected.observed_at == late.observed_at


def test_no_member_is_ever_dropped_from_the_cluster() -> None:
    """ "Annotates, never deletes", asserted on the reconstruction rather than on the renderer.
    The bot's own dedup drops at ingest with a `logger.debug`, which is unrecoverable and
    invisible; a replay whose evidence cannot be reconstructed is not evidence."""
    rows = tuple(
        row(
            at=datetime(2025, 3, 4, 9, n, tzinfo=UTC),
            summary=f"Kraken gained an EMI licence from the FCA. {n}",
        )
        for n in range(5)
    )
    clusters = fold(rows, floor=FLOOR)
    recovered = {m.url for c in clusters for m in c.members}
    assert recovered == {r.url for r in rows}


def test_a_claim_and_its_correction_take_two_slots() -> None:
    """§6.8.2's worked 09:00/15:00 case, and §16.2 says the collapse-to-first implementation
    passes every other test here. Textually near-identical, and collapsed to either single member
    the panel reads a claim without its retraction or a retraction without its claim."""
    claim = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        summary="A public figure said he is enthusiastic about bitcoin.",
    )
    walkback = row(
        at=datetime(2025, 3, 4, 15, tzinfo=UTC),
        summary="A public figure said he is enthusiastic about bitcoin, his office now says.",
        modality=Modality.CORRECTED,
    )

    clusters = fold((claim, walkback), floor=FLOOR)

    assert len(clusters) == 2
    assert {c.elected.modality for c in clusters} == {Modality.CONFIRMED, Modality.CORRECTED}


def test_a_denial_also_takes_its_own_slot() -> None:
    claim = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA."
    )
    denial = row(
        at=datetime(2025, 3, 4, 15, tzinfo=UTC),
        summary="Kraken gained an EMI licence from the FCA, it denies.",
        modality=Modality.DENIED,
    )

    assert len(fold((claim, denial), floor=FLOOR)) == 2


def test_a_proposal_is_not_a_qualifier_and_does_collapse() -> None:
    """`PROPOSED` and `RUMOURED` state a weaker claim, not a retraction of one. Giving each its
    own slot would hand every speculative rewrite a slot and reintroduce the flooding."""
    confirmed = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA."
    )
    proposed = row(
        at=datetime(2025, 3, 4, 10, tzinfo=UTC),
        summary="Kraken gained an EMI licence from the FCA, expected.",
        modality=Modality.PROPOSED,
    )

    assert len(fold((confirmed, proposed), floor=FLOOR)) == 1


def test_matching_fields_below_the_floor_do_not_cluster() -> None:
    """§6.8.2.1's known weakness and its guard: "Kraken licensed in the UK" and "...in Germany"
    share a type and an entity. Fail-closed — two slots spent on one event costs a slot, while
    one slot hiding two events costs the evidence."""
    uk = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC),
        summary="Kraken obtained electronic money authorisation covering the United Kingdom.",
    )
    de = row(
        at=datetime(2025, 3, 4, 10, tzinfo=UTC),
        summary="Separate German federal paperwork concluded; Frankfurt desk opens Monday.",
    )

    assert cluster_key(uk) == cluster_key(de)
    assert len(fold((uk, de), floor=Decimal("0.95"))) == 2


def test_differing_fields_never_cluster_however_similar_the_text() -> None:
    """The key is the fields. A high similarity between two different events is exactly the 15%
    false-merge rate the measurement found, and the key is what keeps it out."""
    one = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Kraken gained an EMI licence from the FCA."
    )
    two = row(
        at=datetime(2025, 3, 4, 10, tzinfo=UTC),
        summary="Kraken gained an EMI licence from the FCA.",
        event=EventType.LISTING,
    )

    assert len(fold((one, two), floor=Decimal("0.01"))) == 2


@pytest.mark.asyncio
async def test_counts_ignore_rows_observed_after_the_instant() -> None:
    """§6.8.2's forward-only rule. A count over the whole archive encodes how much coverage an
    event *eventually* attracted — information from after the cycle, with no model in it."""
    from decision_lab.tests.test_newsfeed import archive

    at = datetime(2025, 3, 4, 10, tzinfo=UTC)
    early = row(
        at=datetime(2025, 3, 4, 9, tzinfo=UTC), summary="Bitcoin ETF approved by the SEC today."
    )
    later = row(
        at=datetime(2025, 3, 4, 11, tzinfo=UTC),
        summary="Bitcoin ETF approved by the SEC today, reports say.",
        source="bitcoincom",
    )

    feed = ArchiveNewsFeed(
        (archive(early, later),), KeywordRelevanceFilter(), window=timedelta(hours=48), floor=FLOOR
    )
    views, _ = await feed.snapshot_news((BTC,), at, 8)

    assert len(views) == 1
    assert "reports=2" not in views[0].summary


def test_an_empty_window_folds_to_nothing_without_raising() -> None:
    assert fold((), floor=FLOOR) == ()
