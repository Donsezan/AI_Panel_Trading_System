"""The pure halves of the news pipeline: normalization, embedding, relevance.

All three are deterministic by design, so these assert values rather than approximate them. That
is the property a downloaded embedding model would cost us, and the reason for not using one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.enums import AssetClass
from tradebot.core.instrument import Instrument
from tradebot.interfaces.news import NewsItem
from tradebot.news.embedding import (
    DEFAULT_DUPLICATE_THRESHOLD,
    DIMENSIONS,
    dumps,
    embed,
    loads,
    similarity,
)
from tradebot.news.normalize import (
    canonical_url,
    excerpt,
    strip_html,
    tokens,
    url_hash,
)
from tradebot.news.relevance import KeywordRelevanceFilter

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def item(title: str, body: str = "", *, source: str = "rss") -> NewsItem:
    return NewsItem(
        item_id="i",
        source_id=source,
        url="https://x/1",
        url_hash="h",
        title=title,
        excerpt=body,
        published_at=NOW,
        observed_at=NOW,
    )


def instrument(symbol: str, base: str, asset_class: AssetClass = AssetClass.CRYPTO) -> Instrument:
    return Instrument(
        symbol=symbol,
        venue="binance",
        asset_class=asset_class,
        base_currency=base,
        quote_currency="USDT",
        lot_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
    )


BTC = instrument("BTC/USDT", "BTC")
ETH = instrument("ETH/USDT", "ETH")
AAPL = instrument("AAPL", "AAPL", AssetClass.EQUITY)


class TestStripHtml:
    def test_tags_are_removed_and_entities_decoded(self) -> None:
        assert strip_html("<p>Bitcoin &amp; friends</p>") == "Bitcoin & friends"

    def test_whitespace_is_collapsed(self) -> None:
        assert strip_html("a\n\n  b\tc") == "a b c"

    def test_unclosed_tags_do_not_eat_the_sentence(self) -> None:
        """The reason this uses a parser and not a regex."""
        assert "market moved" in strip_html("<div><b>market moved<i> today")

    def test_empty_input_stays_empty(self) -> None:
        assert strip_html("") == ""


class TestExcerpt:
    def test_short_text_is_untouched(self) -> None:
        assert excerpt("short enough", 100) == "short enough"

    def test_long_text_is_cut_on_a_word_boundary(self) -> None:
        """Retention policy in code: we keep an excerpt, never the article (PLAN §3.3)."""
        text = " ".join(["word"] * 100)
        cut = excerpt(text, 40)
        assert len(cut) <= 41
        assert cut.endswith("…")
        assert not cut.rstrip("…").endswith("wor")

    def test_a_single_long_token_is_cut_hard(self) -> None:
        assert excerpt("x" * 100, 10) == "x" * 10 + "…"


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://Example.COM/a/", "https://example.com/a"),
            ("https://example.com/a#section", "https://example.com/a"),
            ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
            ("https://example.com/a?fbclid=x", "https://example.com/a"),
            ("https://example.com/", "https://example.com/"),
        ],
    )
    def test_tracking_and_case_are_normalized(self, raw: str, expected: str) -> None:
        assert canonical_url(raw) == expected

    def test_the_same_article_from_two_feeds_hashes_identically(self) -> None:
        """The cheap half of dedup only works if the canonicalization does."""
        first = "https://cointelegraph.com/news/story?utm_source=rss&utm_medium=feed"
        second = "https://Cointelegraph.com/news/story/#top"
        assert url_hash(first) == url_hash(second)

    def test_different_articles_hash_differently(self) -> None:
        assert url_hash("https://x.com/a") != url_hash("https://x.com/b")

    def test_the_hash_is_stable_across_processes(self) -> None:
        """`blake2s`, not `hash()`: a salted hash would make yesterday's rows uncomparable."""
        assert url_hash("https://x.com/a") == url_hash("https://x.com/a")
        assert len(url_hash("https://x.com/a")) == 32


class TestTokens:
    def test_words_are_lowercased_and_punctuation_dropped(self) -> None:
        assert tokens("Bitcoin's ETF: approved!") == frozenset({"bitcoin", "s", "etf", "approved"})


class TestEmbedding:
    def test_embedding_is_deterministic(self) -> None:
        assert embed("Bitcoin rallies to a new high") == embed("Bitcoin rallies to a new high")

    def test_the_vector_is_normalised(self) -> None:
        """Self-similarity is 1 up to the residual of a 34-digit decimal square root."""
        vector = embed("Bitcoin rallies to a new high")
        assert abs(similarity(vector, vector) - Decimal(1)) < Decimal("1e-20")

    def test_dimensions_are_bounded(self) -> None:
        vector = embed(" ".join(f"word{index}" for index in range(2000)))
        assert all(0 <= bucket < DIMENSIONS for bucket in vector)
        assert len(vector) <= DIMENSIONS

    def test_empty_text_has_no_vector_and_matches_nothing(self) -> None:
        """An item with no usable text must never be mistaken for a duplicate."""
        assert embed("") == {}
        assert similarity(embed(""), embed("anything")) == Decimal(0)

    def test_bigrams_separate_opposite_headlines(self) -> None:
        """ "Bitcoin falls" and "Bitcoin rises" share every unigram."""
        falls = embed("Bitcoin falls sharply on ETF outflows")
        rises = embed("Bitcoin rises sharply on ETF inflows")
        assert similarity(falls, rises) < DEFAULT_DUPLICATE_THRESHOLD

    def test_a_syndicated_copy_scores_as_a_duplicate(self) -> None:
        original = "SEC approves spot Bitcoin ETF applications from eight issuers"
        syndicated = "SEC approves spot Bitcoin ETF applications from eight issuers."
        assert similarity(embed(original), embed(syndicated)) >= DEFAULT_DUPLICATE_THRESHOLD

    def test_unrelated_stories_score_low(self) -> None:
        crypto = embed("Bitcoin rallies past resistance on heavy volume")
        weather = embed("Storm warnings issued for coastal counties tonight")
        assert similarity(crypto, weather) < Decimal("0.2")

    def test_similarity_is_symmetric(self) -> None:
        left, right = embed("alpha beta gamma"), embed("beta gamma delta")
        assert similarity(left, right) == similarity(right, left)

    def test_similarity_never_exceeds_one(self) -> None:
        """Accumulated rounding above 1 would silently break every threshold comparison."""
        vector = embed("a b c d e f g h i j k l m n o p")
        assert Decimal(0) <= similarity(vector, vector) <= Decimal(1)

    def test_vectors_round_trip_through_storage_exactly(self) -> None:
        vector = embed("Bitcoin rallies to a new high")
        assert loads(dumps(vector)) == vector

    def test_an_empty_vector_round_trips(self) -> None:
        assert loads(dumps({})) == {}


class TestRelevance:
    @pytest.fixture
    def scorer(self) -> KeywordRelevanceFilter:
        return KeywordRelevanceFilter()

    def test_the_ticker_in_the_title_scores_highest(self, scorer: KeywordRelevanceFilter) -> None:
        assert scorer.relevance(item("BTC breaks out"), (BTC,)) == Decimal("1.00")

    def test_a_common_name_in_the_title_scores_just_below(
        self, scorer: KeywordRelevanceFilter
    ) -> None:
        """Headlines say "Bitcoin", not "BTC/USDT"."""
        assert scorer.relevance(item("Bitcoin breaks out"), (BTC,)) == Decimal("0.90")

    def test_a_body_mention_scores_below_a_title_mention(
        self, scorer: KeywordRelevanceFilter
    ) -> None:
        title_hit = scorer.relevance(item("BTC breaks out"), (BTC,))
        body_hit = scorer.relevance(item("Markets today", "BTC broke out"), (BTC,))
        assert body_hit < title_hit

    def test_asset_class_news_is_relevant_but_weaker(self, scorer: KeywordRelevanceFilter) -> None:
        assert scorer.relevance(item("Crypto market rallies"), (BTC,)) == Decimal("0.40")

    def test_macro_news_earns_a_floor(self, scorer: KeywordRelevanceFilter) -> None:
        """A rate decision moves everything, even with no instrument named."""
        assert scorer.relevance(item("Federal Reserve holds rates"), (BTC,)) == Decimal("0.20")

    def test_a_multi_word_term_matches_across_a_token_boundary(
        self, scorer: KeywordRelevanceFilter
    ) -> None:
        assert scorer.relevance(item("The federal reserve met"), (BTC,)) > Decimal(0)

    def test_a_substring_is_not_a_token_match(self, scorer: KeywordRelevanceFilter) -> None:
        """`ETH` must not match `together`, or every headline scores as relevant."""
        assert scorer.relevance(item("They worked together on it"), (ETH,)) == Decimal(0)

    def test_the_best_matching_instrument_wins(self, scorer: KeywordRelevanceFilter) -> None:
        score = scorer.relevance(item("Ethereum upgrade ships"), (BTC, ETH))
        assert score == Decimal("0.90")

    def test_an_irrelevant_item_scores_zero(self, scorer: KeywordRelevanceFilter) -> None:
        assert scorer.relevance(item("Local bakery wins award"), (BTC,)) == Decimal(0)

    def test_equities_use_their_own_class_terms(self, scorer: KeywordRelevanceFilter) -> None:
        assert scorer.relevance(item("Nasdaq closes higher"), (AAPL,)) == Decimal("0.40")
        assert scorer.relevance(item("AAPL beats estimates"), (AAPL,)) == Decimal("1.00")

    def test_aliases_can_be_extended_by_config(self) -> None:
        scorer = KeywordRelevanceFilter(aliases={"BTC": ("orange coin",)})
        assert scorer.relevance(item("Orange coin surges"), (BTC,)) == Decimal("0.90")
