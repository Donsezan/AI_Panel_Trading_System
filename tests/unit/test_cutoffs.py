"""Knowledge-cutoff classification (DESIGN §2.6, [L12]).

The point of these is the direction the unknowns resolve in: an unrecognised model is read as
contaminated, never as clean, because a backtest report is a claim someone will quote.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from tradebot.validation.cutoffs import (
    Contaminated,
    ModelCutoff,
    classify,
    classify_all,
    cutoff_for,
    normalize,
)

TABLE = (
    ModelCutoff("vendor/model-1", date(2025, 1, 1), "vendor-published"),
    ModelCutoff("vendor/model-1.5-turbo", date(2026, 1, 1), "vendor-published"),
)


def moment(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


class TestLookup:
    def test_a_routing_suffix_is_not_a_different_model(self) -> None:
        """OpenRouter's `:free` names a billing lane, not another set of weights."""
        assert normalize("Vendor/Model-1:free") == "vendor/model-1"

    def test_a_point_release_resolves_through_its_family(self) -> None:
        entry = cutoff_for("vendor/model-1-instruct-2026", TABLE)

        assert entry is not None and entry.cutoff == date(2025, 1, 1)

    def test_the_longest_matching_family_wins(self) -> None:
        entry = cutoff_for("vendor/model-1.5-turbo-instruct", TABLE)

        assert entry is not None and entry.cutoff == date(2026, 1, 1)

    def test_an_unknown_model_resolves_to_nothing(self) -> None:
        assert cutoff_for("someone-elses/model", TABLE) is None


class TestVerdicts:
    def test_a_window_entirely_after_the_cutoff_is_clean(self) -> None:
        verdict = classify(
            "vendor/model-1", start=moment(2025, 6), end=moment(2025, 12), table=TABLE
        )

        assert verdict.verdict is Contaminated.CLEAN
        assert verdict.post_cutoff_fraction == Decimal(1)

    def test_a_window_entirely_before_it_is_contaminated(self) -> None:
        verdict = classify(
            "vendor/model-1", start=moment(2024, 1), end=moment(2024, 6), table=TABLE
        )

        assert verdict.verdict is Contaminated.CONTAMINATED
        assert verdict.post_cutoff_fraction == 0

    def test_a_straddling_window_reports_the_share_that_is_out_of_window(self) -> None:
        verdict = classify(
            "vendor/model-1", start=moment(2024, 7), end=moment(2025, 7), table=TABLE
        )

        assert verdict.verdict is Contaminated.PARTIAL
        assert Decimal("40") < verdict.post_cutoff_pct < Decimal("60")

    def test_an_unknown_model_is_never_reported_as_clean(self) -> None:
        verdict = classify("someone-elses/model", start=moment(2030), end=moment(2031), table=TABLE)

        assert verdict.verdict is Contaminated.UNKNOWN
        assert not verdict.verdict.is_clean
        assert verdict.cutoff is None

    def test_the_seeded_panel_models_all_have_a_cutoff_on_file(self) -> None:
        """A seeded panel whose models were unknown would report as wholly contaminated."""
        from tradebot.decision.presets import FREE_PANEL

        models = tuple(b.model for seat in FREE_PANEL.seats for b in seat.bindings)
        verdicts = classify_all(models, start=moment(2020), end=moment(2021))

        assert all(entry.verdict is not Contaminated.UNKNOWN for entry in verdicts)

    def test_classification_is_deduplicated_and_ordered(self) -> None:
        verdicts = classify_all(
            ("vendor/model-1:free", "vendor/model-1", "vendor/model-1.5-turbo"),
            start=moment(2025),
            end=moment(2026),
            table=TABLE,
        )

        assert [entry.model for entry in verdicts] == ["vendor/model-1", "vendor/model-1.5-turbo"]
