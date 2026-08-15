"""The shared price cache the whole portfolio is valued against.

One property is load-bearing and is the entire defect this phase fixes: a mark that is absent or
stale is `None`, never a fallback. Valuing a position at a four-hour-old price is not more
conservative than valuing it at cost — it is differently wrong, in whichever direction the market
moved (PHASE_12 §1.4).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import MoneyError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Quote
from tradebot.ledger.marks import Marks

TOLERANCE = timedelta(minutes=5)


class TestObservation:
    def test_a_fresh_mark_is_returned(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) == Decimal(
            "50000"
        )

    def test_a_quote_is_observed_under_its_own_instrument_key(
        self, clock: ManualClock, quote: Quote
    ) -> None:
        marks = Marks()
        marks.observe_quote(quote)

        assert (
            marks.price_of(quote.instrument_key, now=clock.now(), tolerance=TOLERANCE) == quote.last
        )

    def test_a_later_observation_replaces_an_earlier_one(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())
        marks.observe("sim:BTC/USDT", Decimal("51000"), clock.now())

        assert marks.price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) == Decimal(
            "51000"
        )


class TestStaleness:
    def test_an_absent_mark_is_none_never_a_fallback(self, clock: ManualClock) -> None:
        assert Marks().price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) is None

    def test_a_mark_older_than_tolerance_is_none(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())
        later = clock.now() + TOLERANCE + timedelta(seconds=1)

        assert marks.price_of("sim:BTC/USDT", now=later, tolerance=TOLERANCE) is None

    def test_a_mark_exactly_at_tolerance_is_still_a_mark(self, clock: ManualClock) -> None:
        """The boundary is inclusive, so a sweep landing exactly on it does not freeze."""
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.price_of(
            "sim:BTC/USDT", now=clock.now() + TOLERANCE, tolerance=TOLERANCE
        ) == Decimal("50000")

    def test_age_is_reported_for_an_operator_to_read(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.age_of("sim:BTC/USDT", now=clock.now() + timedelta(minutes=2)) == timedelta(
            minutes=2
        )

    def test_age_of_an_absent_mark_is_none(self, clock: ManualClock) -> None:
        assert Marks().age_of("sim:BTC/USDT", now=clock.now()) is None


class TestKeyspace:
    def test_instrument_and_currency_marks_share_one_namespace_without_colliding(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Instrument keys are `venue:symbol` and always carry a colon; currencies never do."""
        marks = Marks()
        marks.observe(instrument.key, Decimal("50000"), clock.now())
        marks.observe("BTC", Decimal("50000"), clock.now())

        assert ":" in instrument.key
        assert marks.keys() == frozenset({instrument.key, "BTC"})

    def test_a_float_price_is_refused(self, clock: ManualClock) -> None:
        """`Marks` is on the money path, so the money discipline applies to it."""
        with pytest.raises(MoneyError):
            Marks().observe("sim:BTC/USDT", 50000.0, clock.now())  # type: ignore[arg-type]
