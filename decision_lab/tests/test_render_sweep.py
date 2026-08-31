"""The report grows a comparison; it does not become a second report (spec §14).

One command, one set of tables, one rendering path. With no sweep the page is exactly what slice B
wrote. With one, the candidate sections appear above the reference pass's own — and if any
candidate bound the stub, the whole page says so at the top, before a reader has formed an opinion
about a number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import render as rd
from decision_lab.compare import Agreement, Ranked
from decision_lab.sampling import Sample
from decision_lab.scoring import ScoringParams

AT = datetime(2026, 8, 30, tzinfo=UTC)


def report(**overrides: object) -> rd.LabReport:
    base: dict[str, object] = {
        "generated_at": AT,
        "corpus_id": "c1",
        "dataset_directory": "data/history",
        "dataset_digest": "d1",
        "reference_instrument": "binance:BTC/USDT",
        "reference_panel_id": "sim",
        "reference_config_digest": "r1",
        "cadence_seconds": 28800,
        "scoring": ScoringParams(timeframe="1h"),
        "vol_window_bars": 30,
        "shock_percentile": Decimal("0.90"),
        "start_equity": Decimal(10000),
    }
    base.update(overrides)
    return rd.LabReport(**base)


def ranked(candidate_id: str, regime: str = "NORMAL", accuracy: str = "0.5") -> Ranked:
    return Ranked(regime=regime, candidate_id=candidate_id, scored=100, accuracy=Decimal(accuracy))


def test_a_report_with_no_sweep_is_unchanged() -> None:
    text = rd.report_markdown(report())

    assert "## Candidates, by regime" not in text
    assert "## Panel, by regime" in text
    assert rd.PLUMBING_CHECK not in text


def test_the_plumbing_banner_is_at_the_top_and_unconditional() -> None:
    text = rd.report_markdown(report(plumbing_check=True, ranking=(ranked("a"),)))

    assert rd.PLUMBING_CHECK in text
    assert text.index(rd.PLUMBING_CHECK) < text.index("## Experiment")


def test_an_evaluation_carries_no_plumbing_banner() -> None:
    text = rd.report_markdown(report(plumbing_check=False, ranking=(ranked("a"),)))

    assert rd.PLUMBING_CHECK not in text


def test_the_ranking_renders_one_row_per_candidate_per_regime() -> None:
    text = rd.report_markdown(
        report(ranking=(ranked("strong", accuracy="0.6"), ranked("weak", accuracy="0.4")))
    )

    assert "## Candidates, by regime" in text
    assert "| NORMAL | strong |" in text
    assert "| NORMAL | weak |" in text
    assert text.index("strong") < text.index("weak"), "ordered by accuracy"


def test_the_agreement_matrix_renders_when_two_candidates_ran() -> None:
    rows = (
        Agreement(
            regime="NORMAL",
            left="a",
            right="b",
            compared=100,
            agreed=98,
            rate=Decimal("0.98"),
            tradable_divergences=1,
        ),
    )
    text = rd.report_markdown(report(ranking=(ranked("a"), ranked("b")), agreement=rows))

    assert "### Agreement" in text
    assert "98.0%" in text
    assert "one experiment run twice" in text, "the reading, not only the number"


def test_a_halt_is_named_on_the_page() -> None:
    text = rd.report_markdown(
        report(
            ranking=(ranked("a"),),
            matrix_digest="m1",
            sweep_status="halted_fallback",
            halted_on="baseline at 2024-03-01: technical: openrouter:x -> gemini:y",
            on_fallback="halt",
        )
    )

    assert "halted_fallback" in text
    assert "gemini:y" in text


def test_the_sample_and_the_spend_are_on_the_identity_block() -> None:
    text = rd.report_markdown(
        report(
            ranking=(ranked("a"),),
            matrix_digest="m1",
            sample=Sample(
                cycle_ids=("c1",), seed=7, selected={"NORMAL": 1}, available={"NORMAL": 9}
            ),
            budget_usd=Decimal(40),
            spent_usd=Decimal("12.5"),
        )
    )

    assert "| sample seed | 7 |" in text
    assert "| spent | 12.5 |" in text
    assert "| matrix | m1 |" in text
    assert "NORMAL 1/9" in text


def test_contaminated_cycles_are_reported_beside_the_scored_count() -> None:
    text = rd.report_markdown(
        report(ranking=(ranked("a"),), matrix_digest="m1", contaminated=4, on_fallback="exclude")
    )

    assert "4 cycle" in text
    assert "substitute" in text.lower()


def test_one_candidate_says_so_rather_than_printing_an_empty_matrix() -> None:
    text = rd.report_markdown(report(ranking=(ranked("a"),), agreement=()))

    assert "nothing to compare it against" in text


def test_a_candidate_a_halt_never_reached_is_not_ranked_as_a_scored_zero() -> None:
    """finding 3: `by_regime(())`'s legitimate zero row must never stand in for "not measured" —
    that reads as measured and worst rather than not measured at all."""
    text = rd.report_markdown(
        report(ranking=(ranked("a", accuracy="0.6"),), not_measured_candidates=("b",))
    )

    assert "| NORMAL | b |" not in text, "an unreached candidate must never appear as a ranked row"
    assert "**Not measured:** b" in text


def test_the_candidates_section_appears_even_with_nothing_ranked() -> None:
    """Every candidate in the matrix halted before it ran: `ranking` is empty, but the reader
    still needs to be told the sweep produced nothing rather than the section vanishing."""
    text = rd.report_markdown(report(not_measured_candidates=("a", "b")))

    assert "## Candidates, by regime" in text
    assert "**Not measured:** a, b" in text
