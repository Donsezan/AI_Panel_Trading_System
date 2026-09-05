"""Every report opens with its banners and its identity (spec §14).

A tuning result is filed beside the decision it justified, exactly as `report promotion` and
`report shadow` are — so it is written to a file, never printed, and a result whose provenance is
not on the page is not reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import render as rd
from decision_lab import scoring as sc
from tradebot.validation.backtest import BANNER

AT = datetime(2026, 8, 23, tzinfo=UTC)


def report(**overrides: object) -> rd.LabReport:
    base: dict[str, object] = {
        "generated_at": AT,
        "corpus_id": "abc123",
        "dataset_directory": "data/history",
        "dataset_digest": "d0",
        "dayset_digest": "d1",
        "reference_instrument": "binance:BTC/USDT",
        "reference_panel_id": "sim",
        "reference_config_digest": "c0",
        "cadence_seconds": 14_400,
        "scoring": sc.ScoringParams(timeframe="1h"),
        "vol_window_bars": 30,
        "shock_percentile": Decimal("0.90"),
        "named_windows": ("spot ETF approval",),
        "start_equity": Decimal(10_000),
        "news_blind": True,
        "panel_models": ("varied-a", "varied-b"),
        "cycles": 120,
        "regimes": (sc.RegimeMetrics(regime="NORMAL", decisions=10, scored=8, correct=6),),
        "seats": (),
    }
    return rd.LabReport(**{**base, **overrides})


def _minimal_report() -> rd.LabReport:
    """Only `LabReport`'s required fields — the base every slice D test `model_copy`s from, so a
    field this test does not care about stays at its slice-D default (empty, meaning "no
    calibration on this page")."""
    return rd.LabReport(
        generated_at=AT,
        corpus_id="abc123",
        dataset_directory="data/history",
        dataset_digest="d0",
        reference_instrument="binance:BTC/USDT",
        reference_panel_id="sim",
        reference_config_digest="c0",
        cadence_seconds=14_400,
        scoring=sc.ScoringParams(timeframe="1h"),
        vol_window_bars=30,
        shock_percentile=Decimal("0.90"),
        start_equity=Decimal(10_000),
    )


def test_the_contamination_banner_is_unconditional() -> None:
    """§1.1: every model in `validation/cutoffs.py` was trained on this period."""
    assert BANNER in rd.report_markdown(report())


def test_the_tools_own_disclaimer_is_on_every_report() -> None:
    text = rd.report_markdown(report())
    assert "comparison instrument" in text
    assert "not evidence of alpha" in text


def test_a_news_blind_run_says_so() -> None:
    assert "NEWS-BLIND RUN" in rd.report_markdown(report(news_blind=True))


def test_the_identity_block_carries_every_parameter() -> None:
    """A result whose provenance is not on the page is not reproducible (§14)."""
    text = rd.report_markdown(report())
    for expected in ("abc123", "data/history", "binance:BTC/USDT", "sim", "1h", "0.90", "30"):
        assert expected in text


def test_every_regime_gets_a_row_even_when_empty() -> None:
    """§8.3: a missing SHOCK_DOWN row reads as 'not measured', which is the opposite of
    'never happened'."""
    text = rd.report_markdown(report(regimes=sc.by_regime([])))
    for regime in ("NORMAL", "SHOCK_UP", "SHOCK_DOWN"):
        assert regime in text


def test_no_pooled_shock_row_is_ever_rendered() -> None:
    text = rd.report_markdown(report(regimes=sc.by_regime([])))
    assert "| SHOCK |" not in text


def test_unscored_counts_appear_with_their_reasons() -> None:
    metrics = sc.RegimeMetrics(regime="NORMAL", decisions=3, unscored={"UNSCORED (gap)": 2})
    text = rd.report_markdown(report(regimes=(metrics,)))
    assert "UNSCORED (gap)" in text
    assert "2" in text


def test_the_regret_column_is_labelled_unreachable() -> None:
    """§9.5: reported as a ranking aid, explicitly labelled unreachable."""
    assert "unreachable" in rd.report_markdown(report()).lower()


def test_the_report_is_written_to_a_file(tmp_path: Path) -> None:
    path = rd.write_report(report(), tmp_path / "r.md")
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("#")


def test_identical_input_renders_identically() -> None:
    """Deterministic, so two reports diff cleanly — which is how a tuning result is compared."""
    assert rd.report_markdown(report()) == rd.report_markdown(report())


def test_a_calibration_report_carries_the_gate_verdict_and_the_spread() -> None:
    """§10.2: three days is not a distribution, but it is enough to see when one day carried a
    result — so the spread across the three is on the page beside the pooled figure."""
    from datetime import date

    from decision_lab import render as rd

    report = _minimal_report().model_copy(
        update={
            "scenario": "normal",
            "calibration_days": (date(2026, 1, 1), date(2026, 1, 2)),
            "per_day": (
                rd.DayMetrics(
                    candidate_id="baseline",
                    day=date(2026, 1, 1),
                    pool="NORMAL",
                    scored=10,
                    correct=9,
                    accuracy=Decimal("0.90"),
                ),
                rd.DayMetrics(
                    candidate_id="baseline",
                    day=date(2026, 1, 2),
                    pool="NORMAL",
                    scored=10,
                    correct=3,
                    accuracy=Decimal("0.30"),
                ),
            ),
            "gate_passed": True,
        }
    )

    text = rd.report_markdown(report)

    assert "## Calibration — normal" in text
    assert "2026-01-01" in text and "2026-01-02" in text
    assert "spread" in text.lower()
    assert "60.0" in text, "the spread between 90% and 30% is 60 points"
    assert "Gate: PASSED" in text


def test_a_failed_gate_lists_every_reason() -> None:
    from decision_lab import render as rd

    report = _minimal_report().model_copy(
        update={
            "scenario": "shock",
            "gate_passed": False,
            "gate_failures": ("shock: baseline / analyst: never answered at all",),
        }
    )

    text = rd.report_markdown(report)

    assert "Gate: FAILED" in text
    assert "never answered at all" in text


def test_the_skip_gate_banner_is_above_the_numbers() -> None:
    """§14: a reader must meet the banner before they meet a number."""
    from decision_lab import render as rd

    text = rd.report_markdown(_minimal_report().model_copy(update={"gate_skipped": True}))

    assert rd.GATE_SKIPPED in text
    assert text.index(rd.GATE_SKIPPED) < text.index("## ")


def test_a_report_with_no_scenario_renders_exactly_as_before() -> None:
    """§14: one command, one rendering path. A sweep's page must not grow an empty section."""
    from decision_lab import render as rd

    text = rd.report_markdown(_minimal_report())

    assert "## Calibration" not in text
    assert "Projected spend" not in text
    assert rd.GATE_SKIPPED not in text


def test_the_cost_projection_is_rendered_per_cadence() -> None:
    from decision_lab import render as rd

    report = _minimal_report().model_copy(
        update={
            "cost_projection": (
                rd.CostProjection(
                    candidate_id="baseline",
                    cost_per_cycle=Decimal("0.02"),
                    cost_per_scored=Decimal("0.01"),
                    projected={"24h": Decimal("3.64"), "4h": Decimal("21.84")},
                ),
            )
        }
    )

    text = rd.report_markdown(report)

    assert "Projected spend" in text
    assert "3.64" in text and "21.84" in text
