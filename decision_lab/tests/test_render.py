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
