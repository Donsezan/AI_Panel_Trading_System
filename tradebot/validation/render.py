"""Rendering reports as Markdown — the form a human actually reads and signs.

Markdown rather than a dashboard page or JSON, because a promotion report is an artifact that
outlives the process that produced it: it gets attached to the decision it justified, read six
months later, and diffed against the next one. Plain text does that; a rendered view does not.

The promotion and backtest reports share their statistical sections, so those are written once
here and given an `Evidence` — the two differ in what they *conclude*, not in how they count. The
shadow A/B report is a different question entirely (two panels, one snapshot stream) and so has
sections of its own. Numbers are formatted from `Decimal` directly; no value passes through a
float on its way to being read (PLAN §2.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from tradebot.core.money import ZERO, divide
from tradebot.validation.backtest import BacktestReport
from tradebot.validation.comparison import Comparison, ComparisonReport
from tradebot.validation.evidence import Evidence
from tradebot.validation.promotion import PromotionReport

#: How many money-moving divergences a report lists before summarising the rest. A weeks-long
#: soak can produce hundreds, and a table nobody scrolls to the end of is not evidence.
MAX_LISTED_DIVERGENCES = 50

#: What a human confirms before live can be armed. Straight from PLAN §3.2, §9 and DESIGN §9
#: rung 6 — the checks nobody can automate, printed where the decision is made.
SIGN_OFF = (
    "reviewed the decision drill-down for a representative sample of cycles",
    "confirmed automated trading of these instruments is permitted in your jurisdiction "
    "and under each venue's terms",
    "venue keys are trade-only, withdrawals disabled at the venue, IP-allowlisted",
    "chosen the live notional cap, and it is small enough to lose",
    "alerting reaches a human who is awake (kill switch, halt, recon mismatch)",
)


def promotion_markdown(report: PromotionReport) -> str:
    """The promotion report: verdict, the numbers behind it, and the human's sign-off block."""
    verdict = "PASSED" if report.passed else "FAILED"
    sections = [
        f"# Promotion report — {report.mode.value}",
        _meta(report),
        "## Automatic gates",
        _table(
            ("gate", "verdict", "observed", "required", "detail"),
            (
                (
                    gate.name,
                    "pass" if gate.passed else "**FAIL**",
                    gate.observed,
                    gate.required,
                    gate.detail,
                )
                for gate in report.gates
            ),
        ),
        f"**Automatic gates: {verdict}.** "
        + (
            "Nothing here promotes anything: the last gate is a human's, below."
            if report.passed
            else "The failures above must be resolved and the soak continued."
        ),
        _valuation_basis(),
        _sign_off(),
        *_evidence_sections(report.evidence),
    ]
    return "\n\n".join(sections) + "\n"


def _valuation_basis() -> str:
    """The boundary the drawdown gate changed across (ADR 0027, PHASE_12 decision D4).

    Printed on every promotion report rather than only where it applies. Whether a given soak spans
    the change is a fact about *this database*, and the operator is the one who knows it; a report
    that silently omitted the question would let the decision pass as an oversight instead of a
    decision, which is the whole reason this section exists.
    """
    return (
        "## Valuation basis\n\n"
        "Portfolio equity became **mark-to-market** in ADR 0027. Before it, the drawdown kill "
        "switch measured the *cost basis* and could not see unrealized loss at all — a portfolio "
        "that had halved reported 0% drawdown.\n\n"
        "Cycles gathered before that change therefore ran under a drawdown limit that was not "
        "enforcing what it claims. Search this mode's log for the `valuation_basis` risk event to "
        "find where the change landed:\n\n"
        "```powershell\n"
        ".venv\\Scripts\\python.exe -m tradebot config history basket demo --mode paper\n"
        "```\n\n"
        "**Whether the earlier cycles still count is a decision, not a detail.** Record it on the "
        "sign-off below."
    )


def backtest_markdown(report: BacktestReport) -> str:
    """The backtest report: banner first, contamination second, numbers after."""
    sections = [
        "# Backtest report",
        f"> **{report.banner}**",
        _table(
            ("field", "value"),
            (
                ("window", f"{_stamp(report.window_start)} → {_stamp(report.window_end)}"),
                ("requested from", _stamp(report.requested_start)),
                ("indicator warm-up", str(report.warmup)),
                ("data source", report.data_source or "unstated"),
                ("instruments", ", ".join(report.instruments)),
                ("timeframes", ", ".join(report.timeframes) or "engine default"),
                ("cycles planned", str(report.planned_cycles)),
                ("cycles run", str(report.ran_cycles)),
                ("cycles not run (basket stopped)", str(report.skipped_cycles)),
            ),
        ),
        "## Look-ahead exposure",
        _contamination(report),
        *_evidence_sections(report.evidence),
    ]
    return "\n\n".join(sections) + "\n"


def comparison_markdown(report: ComparisonReport) -> str:
    """The shadow A/B report: what the two panels made of one identical stream of snapshots."""
    comparison = report.comparison
    sections = [
        "# Shadow A/B comparison",
        _table(
            ("field", "value"),
            (
                ("generated", _stamp(report.generated_at)),
                ("mode", report.mode.value),
                ("window", f"{_stamp(comparison.since)} → {_stamp(comparison.until)}"),
                ("challenger panel(s)", ", ".join(comparison.challenger_panels)),
                ("cycles compared", str(comparison.compared_cycles)),
                ("decisions paired", str(len(comparison.pairings))),
                ("decisions unpaired", str(comparison.unpaired)),
                ("challenger failures", str(len(comparison.failures))),
            ),
        ),
        _comparison_preamble(comparison),
        "## Agreement",
        _agreement(comparison),
        "## Where they disagreed",
        _matrix(comparison),
        "## Divergence that would have moved money",
        _divergences(comparison),
        "## Conviction spread",
        _conviction(comparison),
        "## Cost per decision",
        _comparison_cost(comparison),
        "## Challenger failures",
        _failures(comparison),
    ]
    return "\n\n".join(sections) + "\n"


def _comparison_preamble(comparison: Comparison) -> str:
    if not comparison.ran:
        return (
            "**No shadow evaluation ran in this window.** A basket runs a challenger only when "
            "its `shadow_panel` is set; unset, the harness costs nothing and records nothing."
        )
    caveat = (
        "\n\n> More than one challenger panel appears in this window. Those are different "
        "experiments and the totals below mix them; narrow the window to one panel to compare "
        "like with like."
        if len(comparison.challenger_panels) > 1
        else ""
    )
    return (
        "Both panels were given the **same frozen snapshot** every cycle, so nothing below is a "
        "difference in market conditions. The challenger never traded: these are the orders it "
        "would have asked for." + caveat
    )


def _agreement(comparison: Comparison) -> str:
    if not comparison.pairings:
        return "_no decision was ruled on by both panels_"
    return _table(
        ("metric", "value"),
        (
            ("agreed", f"{comparison.agreements} of {len(comparison.pairings)}"),
            ("agreement rate", f"{comparison.agreement_pct:.1f}%"),
            ("disagreed", str(len(comparison.disagreements))),
        ),
    )


def _matrix(comparison: Comparison) -> str:
    """Champion action against challenger action. The diagonal is agreement."""
    matrix = comparison.matrix
    if not matrix:
        return "_none recorded_"
    return _table(
        ("champion", "challenger", "decisions"),
        (
            (champion, challenger, str(count))
            for (champion, challenger), count in sorted(
                matrix.items(), key=lambda item: (-item[1], item[0])
            )
        ),
    )


def _divergences(comparison: Comparison) -> str:
    diverged = comparison.tradable_divergences
    if not diverged:
        return (
            "None. Wherever the two differed, neither was asking for an order — so no "
            "disagreement in this window would have changed a position."
        )
    shown = diverged[:MAX_LISTED_DIVERGENCES]
    table = _table(
        ("when", "cycle", "instrument", "champion", "challenger"),
        (
            (
                _stamp(pairing.at),
                pairing.cycle_id[:8],
                pairing.instrument_key,
                f"{pairing.champion.value} ({pairing.champion_conviction})",
                f"{pairing.challenger.value} ({pairing.challenger_conviction})",
            )
            for pairing in shown
        ),
    )
    if len(diverged) == len(shown):
        return table
    return f"{table}\n\n_{len(diverged) - len(shown)} further divergence(s) not listed._"


def _conviction(comparison: Comparison) -> str:
    if not comparison.pairings:
        return "_no decision was ruled on by both panels_"
    return _table(
        ("metric", "value"),
        (
            ("mean gap (challenger − champion)", f"{comparison.conviction_gap_mean:.4f}"),
            ("mean absolute gap", f"{comparison.conviction_gap_abs_mean:.4f}"),
        ),
    )


def _comparison_cost(comparison: Comparison) -> str:
    return _table(
        ("panel", "total", "per decision"),
        (
            (
                "champion (traded)",
                f"${comparison.champion_cost}",
                f"${comparison.cost_per_decision(comparison.champion_cost):.6f}",
            ),
            (
                "challenger (shadow)",
                f"${comparison.challenger_cost}",
                f"${comparison.cost_per_decision(comparison.challenger_cost):.6f}",
            ),
        ),
    )


def _failures(comparison: Comparison) -> str:
    if not comparison.failures:
        return "None. Every cycle's challenger produced a verdict."
    return _table(
        ("when", "cycle", "error"),
        (
            (_stamp(failure.at), failure.cycle_id[:8], failure.error)
            for failure in comparison.failures
        ),
    )


def _contamination(report: BacktestReport) -> str:
    if not report.panel_models:
        return (
            "No hosted model was contacted: this run used the offline scripted panel, so there "
            "is no training window to compare against. It validates plumbing only."
        )
    return "\n\n".join(
        (
            _table(
                ("model", "cutoff", "source", "window after cutoff", "verdict"),
                (
                    (
                        entry.model,
                        entry.cutoff.isoformat() if entry.cutoff else "—",
                        entry.source,
                        f"{entry.post_cutoff_pct:.1f}%",
                        entry.verdict.value,
                    )
                    for entry in report.contamination
                ),
            ),
            "A clean window removes one known contaminant; it does not make a backtest evidence "
            "of alpha. Cutoff dates are rarely published — check the source column before "
            "quoting any of this.",
        )
    )


def _meta(report: PromotionReport) -> str:
    evidence = report.evidence
    return _table(
        ("field", "value"),
        (
            ("generated", _stamp(report.generated_at)),
            ("window", f"{_stamp(evidence.since)} → {_stamp(evidence.until)}"),
            ("evidence venues", ", ".join(sorted(report.criteria.evidence_venues))),
            ("minimum cycles", str(report.criteria.min_cycles)),
        ),
    )


def _sign_off() -> str:
    checklist = "\n".join(f"- [ ] {item}" for item in SIGN_OFF)
    return (
        "## Sign-off\n\n"
        "This report cannot sign itself off. Promotion is a human act, and these are the "
        "checks no gate above can make:\n\n"
        f"{checklist}\n\n"
        "Signed: ______________________   Date: ____________"
    )


def _evidence_sections(evidence: Evidence) -> tuple[str, ...]:
    return (
        "## Cycles",
        _counts(("venue", "completed cycles"), evidence.cycles_by_venue),
        _counts(("outcome", "cycles"), evidence.outcomes),
        "## Panel decisions",
        _counts(("action", "decisions"), evidence.actions),
        "## Orders and fills",
        _counts(
            ("final order state", "orders"),
            {state.value: count for state, count in evidence.order_states.items()},
        ),
        f"Fills booked: **{evidence.fills}**.",
        "## Round trips",
        _round_trips(evidence),
        "## Reconciliation",
        _reconciliation(evidence),
        "## Risk activity",
        _counts(("rule / action", "times"), evidence.risk_events),
        "## Incidents",
        _incidents(evidence),
        "## Deliberation cost",
        _cost(evidence),
    )


def _round_trips(evidence: Evidence) -> str:
    trips = evidence.round_trips
    if not trips:
        return "No position was closed in this window."
    return _table(
        ("metric", "value"),
        (
            ("closed round trips", str(len(trips))),
            ("realized PnL", f"{evidence.realized_pnl}"),
            ("losing trips", f"{evidence.losing_trips} of {len(trips)}"),
        ),
    )


def _reconciliation(evidence: Evidence) -> str:
    if not evidence.reconciliations:
        return (
            "No reconciliation was recorded. That is not the same as a clean one: the ledger has "
            "not been shown to agree with the venue even once."
        )
    counts: dict[str, int] = {}
    for pass_ in evidence.reconciliations:
        counts[pass_.classification.value] = counts.get(pass_.classification.value, 0) + 1
    return _counts(("classification", "passes"), counts)


def _incidents(evidence: Evidence) -> str:
    if not evidence.incidents:
        return "None. Nothing in this window needed a human."
    return _table(
        ("when", "kind", "scope", "detail"),
        (
            (_stamp(incident.at), incident.kind.value, incident.scope, incident.detail)
            for incident in evidence.incidents
        ),
    )


def _cost(evidence: Evidence) -> str:
    cycles = len(evidence.cycles)
    per_cycle = divide(evidence.cost_usd, Decimal(cycles)) if cycles else ZERO
    return _table(
        ("metric", "value"),
        (
            ("total", f"${evidence.cost_usd}"),
            ("per cycle", f"${per_cycle:.6f}"),
        ),
    )


def _counts(headers: tuple[str, str], counts: Mapping[str, int]) -> str:
    if not counts:
        return "_none recorded_"
    rows = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return _table(headers, ((name, str(count)) for name, count in rows))


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(cell or "—" for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _stamp(moment: datetime | None) -> str:
    """An absent bound reads as the whole log, which is what `None` means to a window."""
    return moment.isoformat() if moment is not None else "—"
