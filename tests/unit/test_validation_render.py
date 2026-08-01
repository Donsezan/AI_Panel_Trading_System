"""Rendering a report a human signs.

The assertions are about what a reader must not be able to miss: the banner on a backtest, the
sign-off block on a promotion report, and money printed as the decimal it is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.enums import Action, CycleOutcome, Mode, ReconcileClass
from tradebot.validation.backtest import BANNER, BacktestReport
from tradebot.validation.comparison import Comparison, ComparisonReport, Pairing, ShadowFailure
from tradebot.validation.cutoffs import classify_all
from tradebot.validation.evidence import (
    CycleFacts,
    Evidence,
    Incident,
    IncidentKind,
    ReconcileFacts,
    RoundTripFacts,
)
from tradebot.validation.promotion import evaluate
from tradebot.validation.render import (
    MAX_LISTED_DIVERGENCES,
    SIGN_OFF,
    backtest_markdown,
    comparison_markdown,
    promotion_markdown,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)
BUY, WAIT, HOLD = Action.BUY, Action.WAIT, Action.HOLD
WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)


def evidence() -> Evidence:
    return Evidence(
        since=WINDOW_START,
        until=NOW,
        cycles=(
            CycleFacts(
                cycle_id="c1",
                basket_id="demo",
                venue="sim",
                started_at=NOW,
                completed_at=NOW,
                outcome=CycleOutcome.ORDERS_PLACED,
                cost_usd=Decimal("0.125"),
            ),
        ),
        reconciliations=(ReconcileFacts(at=NOW, venue="sim", classification=ReconcileClass.MATCH),),
        round_trips=(
            RoundTripFacts(
                at=NOW,
                basket_id="demo",
                instrument_key="sim:BTC/USDT",
                realized_pnl=Decimal("-12.3456789"),
            ),
        ),
        actions={"BUY": 1},
        fills=2,
    )


class TestPromotion:
    def test_the_verdict_and_every_gate_are_visible(self) -> None:
        markdown = promotion_markdown(evaluate(evidence(), mode=Mode.PAPER, generated_at=NOW))

        assert "# Promotion report — paper" in markdown
        assert "**Automatic gates: FAILED.**" in markdown
        assert "completed_cycles" in markdown and "reconciliation_clean" in markdown

    def test_the_human_gate_is_printed_where_the_decision_is_made(self) -> None:
        markdown = promotion_markdown(evaluate(evidence(), mode=Mode.PAPER, generated_at=NOW))

        assert "## Sign-off" in markdown
        for item in SIGN_OFF:
            assert item in markdown

    def test_money_is_rendered_exactly(self) -> None:
        """A realized PnL that has been through a float is a report nobody should trust."""
        markdown = promotion_markdown(evaluate(evidence(), mode=Mode.PAPER, generated_at=NOW))

        assert "-12.3456789" in markdown

    def test_incidents_are_listed_rather_than_counted(self) -> None:
        loud = Evidence(
            since=None,
            until=None,
            incidents=(
                Incident(
                    kind=IncidentKind.KILL_SWITCH,
                    at=NOW,
                    scope="watchdog",
                    detail="drawdown breached",
                ),
            ),
        )

        markdown = promotion_markdown(evaluate(loud, mode=Mode.PAPER, generated_at=NOW))
        assert "kill_switch_tripped" in markdown
        assert "drawdown breached" in markdown


class TestBacktestRendering:
    def report(self, models: tuple[str, ...]) -> BacktestReport:
        return BacktestReport(
            requested_start=WINDOW_START,
            warmup=timedelta(days=2),
            window_start=WINDOW_START + timedelta(days=2),
            window_end=NOW,
            finished_at=NOW,
            data_source="binance spot, public REST",
            instruments=("binance:BTC/USDT",),
            timeframes=("1h", "4h"),
            panel_models=models,
            contamination=classify_all(models, start=WINDOW_START, end=NOW),
            evidence=evidence(),
            planned_cycles=10,
        )

    def test_the_banner_is_the_first_thing_after_the_title(self) -> None:
        markdown = backtest_markdown(self.report(()))

        assert markdown.index(BANNER) < markdown.index("## Look-ahead exposure")
        assert "NOT ALPHA EVIDENCE" in markdown

    def test_the_warm_up_the_window_lost_is_stated(self) -> None:
        """A report whose window silently differs from the requested one is a different test."""
        markdown = backtest_markdown(self.report(()))

        assert "indicator warm-up" in markdown
        assert "2 days" in markdown

    def test_a_hosted_model_gets_a_cutoff_verdict_with_its_source(self) -> None:
        markdown = backtest_markdown(self.report(("meta-llama/llama-3.3-70b-instruct:free",)))

        assert "meta-llama/llama-3.3" in markdown
        assert "vendor-published" in markdown
        assert "clean" in markdown

    def test_an_unknown_model_is_never_presented_as_clean(self) -> None:
        markdown = backtest_markdown(self.report(("someone-elses/model",)))

        assert "unknown" in markdown
        assert "no cutoff on file" in markdown


def a_pairing(
    instrument_key: str,
    champion: Action,
    challenger: Action,
    *,
    champion_conviction: str = "0.7",
    challenger_conviction: str = "0.7",
) -> Pairing:
    return Pairing(
        cycle_id="cycle-abcdef123456",
        basket_id="demo",
        at=NOW,
        instrument_key=instrument_key,
        champion=champion,
        challenger=challenger,
        champion_conviction=Decimal(champion_conviction),
        challenger_conviction=Decimal(challenger_conviction),
    )


def comparison_of(*pairings: Pairing, **overrides: object) -> ComparisonReport:
    return ComparisonReport(
        mode=Mode.PAPER,
        generated_at=NOW,
        comparison=Comparison(
            since=WINDOW_START,
            until=NOW,
            challenger_panels=("challenger",),
            pairings=pairings,
            compared_cycles=1,
            champion_cost=Decimal("0.02"),
            challenger_cost=Decimal("0.10"),
            **overrides,  # type: ignore[arg-type]
        ),
    )


class TestComparisonReport:
    def test_an_empty_window_explains_itself_rather_than_reading_as_a_tie(self) -> None:
        report = ComparisonReport(
            mode=Mode.SIM,
            generated_at=NOW,
            comparison=Comparison(since=None, until=None),
        )

        markdown = comparison_markdown(report)
        assert "**No shadow evaluation ran in this window.**" in markdown

    def test_it_leads_with_the_fact_that_the_snapshot_was_shared(self) -> None:
        """Without that sentence a reader takes the difference for a difference in markets."""
        markdown = comparison_markdown(comparison_of(a_pairing("sim:BTC/USDT", BUY, BUY)))

        assert "same frozen snapshot" in markdown
        assert "The challenger never traded" in markdown

    def test_agreement_and_the_matrix_are_both_shown(self) -> None:
        markdown = comparison_markdown(
            comparison_of(
                a_pairing("sim:BTC/USDT", BUY, BUY),
                a_pairing("sim:ETH/USDT", BUY, WAIT),
            )
        )

        assert "| agreement rate | 50.0% |" in markdown
        assert "| BUY | WAIT | 1 |" in markdown

    def test_a_money_moving_divergence_gets_its_own_section(self) -> None:
        markdown = comparison_markdown(comparison_of(a_pairing("sim:ETH/USDT", BUY, WAIT)))

        assert "## Divergence that would have moved money" in markdown
        assert "sim:ETH/USDT" in markdown

    def test_agreement_on_doing_nothing_is_not_a_money_moving_divergence(self) -> None:
        markdown = comparison_markdown(comparison_of(a_pairing("sim:BTC/USDT", HOLD, WAIT)))

        assert "no disagreement in this window would have changed a position" in markdown

    def test_each_side_is_costed_separately(self) -> None:
        markdown = comparison_markdown(comparison_of(a_pairing("sim:BTC/USDT", BUY, BUY)))

        assert "| champion (traded) | $0.02 |" in markdown
        assert "| challenger (shadow) | $0.10 |" in markdown

    def test_two_challenger_panels_in_one_window_are_flagged_as_two_experiments(self) -> None:
        report = ComparisonReport(
            mode=Mode.PAPER,
            generated_at=NOW,
            comparison=Comparison(
                since=WINDOW_START,
                until=NOW,
                challenger_panels=("a", "b"),
                pairings=(a_pairing("sim:BTC/USDT", BUY, BUY),),
                compared_cycles=2,
            ),
        )

        assert "different experiments" in comparison_markdown(report)

    def test_a_challenger_failure_is_listed_rather_than_hidden(self) -> None:
        report = comparison_of(
            a_pairing("sim:BTC/USDT", BUY, BUY),
            failures=(ShadowFailure(cycle_id="broken-1234", at=NOW, error="ProviderError: down"),),
        )

        markdown = comparison_markdown(report)
        assert "ProviderError: down" in markdown

    def test_a_long_divergence_list_is_truncated_with_a_count(self) -> None:
        """A table nobody scrolls to the end of is not evidence."""
        many = tuple(
            a_pairing(f"sim:X{index}/USDT", BUY, WAIT)
            for index in range(MAX_LISTED_DIVERGENCES + 5)
        )

        markdown = comparison_markdown(comparison_of(*many))
        assert "5 further divergence(s) not listed." in markdown
