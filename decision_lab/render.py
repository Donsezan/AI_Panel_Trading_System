"""The report, as Markdown (spec §14).

Markdown rather than a dashboard page or JSON, for the reason `validation/render.py` gives: a
result that justified a decision gets attached to that decision, read six months later, and
diffed against the next one. Plain text does that; a rendered view does not. Never printed.

Every report opens with its banners. The `BacktestHarness` contamination banner is verbatim and
unconditional — every model in `validation/cutoffs.py` was trained on this period, and a tool that
only warned when it thought it mattered would be a tool nobody could quote. Then `NEWS-BLIND RUN`
where it applies, then the tool's own line stating it is a comparison instrument and not evidence
of alpha.

Then the experiment's identity, in full, because a result whose provenance is not on the page is
not reproducible. Then, per regime — `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN` and one row per named
window — the tables. Never a pooled `SHOCK`: it averages "did the seats catch the move" with "did
the seats protect capital" and hides both.

Numbers are formatted from `Decimal` directly; no value passes through a float on its way to being
read, which `test_discipline.py` asserts structurally.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import Field

from decision_lab.compare import Agreement, Ranked
from decision_lab.sampling import Sample
from decision_lab.scoring import RegimeMetrics, ScoringParams
from decision_lab.seats import FINAL, ROUND_ZERO, SeatMetrics, rounds_are_identical
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.validation.backtest import BANNER

#: The tool's own standing disclaimer. §1.1: it compares configurations an operator wrote; it
#: does not search, does not optimise, and has no authority over anything.
DISCLAIMER = (
    "**This is a comparison instrument, not evidence of alpha.** It ranks configurations against "
    "one another on recorded history. It is not a promotion gate — `validation/promotion.py` "
    "remains the only thing that answers whether anything may be promoted, and it reads the "
    "production log."
)

NEWS_BLIND = (
    "**NEWS-BLIND RUN** — no news archive was wired, so every snapshot records "
    '"no sources configured". A shock block therefore measures the panel\'s reaction to a violent '
    "price move rather than to the reporting of an event."
)

#: §7.2. A run in which any candidate bound the offline stub measured canned JSON. Rendered
#: unconditionally and above the identity block, exactly as the contamination banner is — a
#: reader must meet it before they meet a number.
PLUMBING_CHECK: Final = (
    "**PLUMBING CHECK — NOT AN EVALUATION.** At least one candidate in this run binds the "
    "offline stub, whose votes are drawn from a fixed catalogue. Every table below exercises the "
    "sweep, the scoring and this page; none of it measures any model's judgement. Re-run with a "
    "matrix bound to real providers to learn something."
)

#: §10.6. `--skip-gate` was used, so nobody proved every seat answered on its own model before
#: this run. Rendered with the other banners, above the identity block: a result whose provenance
#: reads "nobody checked the seats first" should say so on its face.
GATE_SKIPPED: Final = (
    "**GATE SKIPPED.** This run was started with `--skip-gate`, so the §10.6 calibration gate "
    "was not consulted. Nothing here proves that every candidate materialised, that every seat "
    "answered on its own model rather than on a backup, or that the panel reached a decision at "
    "all. A seat that abstained silently would leave exactly these tables, and they would look "
    "no different."
)


class CandidateSeats(DomainModel):
    """§9.7's tables, one set per candidate — a seat is only comparable within its own panel."""

    candidate_id: str
    seats: tuple[SeatMetrics, ...] = ()


class NotMeasured(DomainModel):
    """A candidate that produced no measurement, and why (§9.6).

    The reason is carried rather than re-derived at render time because the three causes are not
    distinguishable from the report alone: a halt that never reached this candidate, rows that all
    failed or were all contaminated, and a clean replay that carried no decision to score.
    """

    candidate_id: str
    reason: str


class DayMetrics(DomainModel):
    """One candidate on one pinned day (§10.2).

    Reported per day *and* pooled: a candidate whose pooled accuracy comes entirely from one of
    three days is not a candidate the report should present as steady, and only the per-day rows
    can say so.
    """

    candidate_id: str
    day: date
    pool: str
    scored: int = 0
    correct: int = 0
    accuracy: Money = ZERO


class CostProjection(DomainModel):
    """§10.6's projection: what nine days say a long run would cost, per cadence.

    `projected` is cadence label → spend over the declared window. Printed beside `--budget` so
    §7.5's ceiling stops being a guess — and so an over-ceiling projection is learned for the
    price of nine days rather than at hour nine.
    """

    candidate_id: str
    cost_per_cycle: Money = ZERO
    cost_per_scored: Money = ZERO
    projected: dict[str, Money] = Field(default_factory=dict)


class LabReport(DomainModel):
    """Everything one report says. One object, so the notebook and the renderer agree."""

    generated_at: UtcDatetime
    corpus_id: str
    dataset_directory: str
    dataset_digest: str
    dayset_digest: str = ""
    reference_instrument: str
    reference_panel_id: str
    reference_config_digest: str
    cadence_seconds: int
    scoring: ScoringParams
    vol_window_bars: int
    shock_percentile: Money
    named_windows: tuple[str, ...] = ()
    start_equity: Money
    news_blind: bool = True
    panel_models: tuple[str, ...] = ()
    cycles: int = 0
    regimes: tuple[RegimeMetrics, ...] = ()
    seats: tuple[SeatMetrics, ...] = ()

    # --- The sweep (§7, §9.6). All empty on a reference-pass report, which then renders exactly
    # as it did in slice B: one command, one rendering path (§14).
    plumbing_check: bool = False
    matrix_digest: str = ""
    matrix_source: str = ""
    on_fallback: str = ""
    sweep_status: str = ""
    halted_on: str = ""
    sample: Sample | None = None
    budget_usd: Money = ZERO
    spent_usd: Money = ZERO
    #: Cycles dropped because a substitute model answered (§7.7). Reported beside the scored
    #: count, never instead of it.
    contaminated: int = 0
    ranking: tuple[Ranked, ...] = ()
    agreement: tuple[Agreement, ...] = ()
    candidate_seats: tuple[CandidateSeats, ...] = ()
    #: Candidates that produced no scored decision — a halt never reached them, every row
    #: failed or was contaminated, or nothing they replayed carried a decision (finding 3).
    #: Kept out of `ranking`, `agreement` *and* `candidate_seats` rather than folded in at
    #: 0.0% accuracy, which would read as measured and worst rather than not measured at all.
    not_measured_candidates: tuple[NotMeasured, ...] = ()

    # --- Slice D (§10). All empty on a sweep or reference-pass report, which then renders
    # exactly as it did before: one command, one rendering path (§14).
    #: "normal" or "shock" when this page is a calibration, "" otherwise.
    scenario: str = ""
    calibration_days: tuple[date, ...] = ()
    per_day: tuple[DayMetrics, ...] = ()
    gate_passed: bool = False
    gate_failures: tuple[str, ...] = ()
    #: §10.6's `--skip-gate`, stamped here and on the §11 row.
    gate_skipped: bool = False
    cost_projection: tuple[CostProjection, ...] = ()


def report_markdown(report: LabReport) -> str:
    sections = [
        "# decision_lab — decision quality over recorded history",
        "",
        BANNER,
        "",
        DISCLAIMER,
    ]
    if report.plumbing_check:
        sections += ["", PLUMBING_CHECK]
    if report.news_blind:
        sections += ["", NEWS_BLIND]
    if report.gate_skipped:
        sections += ["", GATE_SKIPPED]
    sections += ["", _identity(report)]
    calibration_block = _calibration_block(report)
    if calibration_block:
        sections += ["", calibration_block]
    if report.ranking or report.not_measured_candidates:
        sections += [
            "",
            "## Candidates, by regime",
            "",
            _ranking_table(report.ranking) + _not_measured_note(report.not_measured_candidates),
            "",
            _agreement_table(report.agreement, report.ranking),
            "",
            _candidate_seat_tables(report.candidate_seats),
        ]
    sections += [
        "",
        "## Panel, by regime",
        "",
        _regime_table(report.regimes),
        "",
        _unscored(report.regimes),
        "",
        "## Seats, by regime",
        "",
        _seat_tables(report.seats),
    ]
    # §10.6: last on the page — the final `rstrip()` below absorbs the blank line an empty
    # projection would otherwise leave, so a report with none renders exactly as before.
    sections += ["", _cost_projection_table(report.cost_projection)]
    return "\n".join(sections).rstrip() + "\n"


def _identity(report: LabReport) -> str:
    rows: list[tuple[str, str]] = [
        ("generated", _stamp(report.generated_at)),
        ("corpus", report.corpus_id),
        ("dataset", f"{report.dataset_directory} (`{report.dataset_digest}`)"),
        ("day set", report.dayset_digest or "not pinned"),
        ("reference instrument", report.reference_instrument),
        ("reference panel", f"{report.reference_panel_id} (`{report.reference_config_digest}`)"),
        ("panel models", ", ".join(report.panel_models) or "none recorded"),
        ("cadence", f"{report.cadence_seconds}s"),
        ("cycles", str(report.cycles)),
        ("scoring timeframe", report.scoring.timeframe),
        ("band", f"{report.scoring.band_k} × ATR"),
        ("forward horizon", f"{report.scoring.horizon_bars} bars"),
        ("ATR lookback", f"{report.scoring.atr_lookback_bars} bars"),
        ("volatility window", f"{report.vol_window_bars} bars"),
        ("shock percentile", str(report.shock_percentile)),
        ("named windows", ", ".join(report.named_windows) or "none"),
        ("starting equity", str(report.start_equity)),
    ]
    # §7, §9.6: a sweep's own provenance, appended rather than interleaved — a reference-pass
    # report (matrix_digest unset) keeps the rows above exactly as slice B rendered them.
    if report.matrix_digest:
        rows += [
            ("matrix", report.matrix_digest),
            ("matrix source", report.matrix_source),
            ("on_fallback", report.on_fallback),
            ("sweep status", report.sweep_status or "ok"),
            ("budget", str(report.budget_usd)),
            ("spent", str(report.spent_usd)),
        ]
        if report.sample is not None:
            rows.append(("sample seed", str(report.sample.seed)))
            rows.append(
                (
                    "sample",
                    "every entry"
                    if report.sample.full
                    else ", ".join(
                        f"{name} {count}/{report.sample.available.get(name, count)}"
                        for name, count in sorted(report.sample.selected.items())
                    ),
                )
            )
        if report.contaminated:
            rows.append(
                (
                    "dropped",
                    f"{report.contaminated} cycle(s) — a substitute model answered, so they "
                    "measure a panel that was never configured (§7.7)",
                )
            )
        if report.halted_on:
            rows.append(("halted on", report.halted_on))
    return "## Experiment\n\n" + _table(("", ""), [[label, value] for label, value in rows])


def _regime_table(regimes: Sequence[RegimeMetrics]) -> str:
    headers = (
        "regime",
        "scored",
        "accuracy",
        "action rate",
        "precision on action",
        "conviction gap",
        "regret/decision",
        "degraded",
        "$/scored",
    )
    rows = [
        [
            metrics.regime,
            str(metrics.scored),
            _pct(metrics.accuracy),
            _pct(metrics.action_rate),
            _pct(metrics.precision_on_action),
            _num(metrics.mean_conviction_gap),
            _num(metrics.regret_per_decision),
            _pct(metrics.degradation_rate),
            _num(metrics.cost_per_scored),
        ]
        for metrics in regimes
    ]
    note = (
        "\n\n`regret/decision` is the oracle's capture minus the panel's, in band units. It is a "
        "**ranking aid and is unreachable by construction**: the oracle exits at the high of every "
        "window and no risk-managed system can match it.\n\n"
        "`SHOCK_UP` and `SHOCK_DOWN` are never pooled. An up-shock asks whether the seats caught "
        "the move; a down-shock asks whether they protected capital. **Read `SHOCK_DOWN` first** — "
        "a long-only system's worst outcome is not a missed rally."
    )
    return _table(headers, rows) + note


def _unscored(regimes: Sequence[RegimeMetrics]) -> str:
    rows = [
        [metrics.regime, reason, str(count)]
        for metrics in regimes
        for reason, count in sorted(metrics.unscored.items())
    ]
    if not rows:
        return "Every decision was scored."
    return (
        "### Unscored\n\nCounted with its reason, never dropped — a run that dropped them would "
        "report accuracy over a subset it chose after the fact.\n\n"
        + _table(("regime", "reason", "count"), rows)
    )


def _seat_tables(seats: Sequence[SeatMetrics]) -> str:
    if not seats:
        return "No seat responses were recorded."
    identical = rounds_are_identical(seats)
    shown = [s for s in seats if s.round_label == FINAL] if identical else list(seats)
    headers = (
        "seat",
        "regime",
        "round",
        "votes",
        "accuracy",
        "precision on action",
        "abstained",
        "fell back",
        "swing rate",
        "marginal",
        "$/vote",
        "ms/vote",
    )
    rows = [
        [
            metrics.seat_id,
            metrics.regime,
            metrics.round_label,
            str(metrics.scored),
            _pct(metrics.accuracy),
            _pct(metrics.precision_on_action),
            _pct(metrics.abstention_rate),
            _pct(metrics.fallback_rate),
            _pct(metrics.swing_rate) if metrics.round_label == FINAL else "—",
            str(metrics.marginal_contribution) if metrics.round_label == FINAL else "—",
            _num(metrics.cost_per_vote),
            str(metrics.latency_ms_per_vote),
        ]
        for metrics in shown
    ]
    note = (
        "\n\nUnder `blind_then_debate` a seat's later votes are contaminated by its peers **by "
        f"design** — that is what the debate is for. `{ROUND_ZERO}` is the seat's own independent "
        f"opinion; `{FINAL}` is the seat after persuasion. *Which seat reasons well* and *which "
        "seat is easily talked round* are different questions.\n\n"
        "`swing rate` is how often removing this seat would have changed the panel's decision — "
        "what separates a seat carrying weight from one padding a majority. `marginal` is right "
        "dissents against a wrong panel minus wrong dissents against a right one."
    )
    if identical:
        note = (
            "\n\nThis panel ran `single_round`, so round 0 **is** the final vote; one table is "
            "shown rather than the same numbers twice." + note
        )
    return _table(headers, rows) + note


def _ranking_table(rows: Sequence[Ranked]) -> str:
    headers = (
        "regime",
        "candidate",
        "scored",
        "accuracy",
        "action rate",
        "precision on action",
        "conviction gap",
        "regret/decision",
        "degraded",
        "$/scored",
    )
    body = [
        [
            row.regime,
            row.candidate_id,
            str(row.scored),
            _pct(row.accuracy),
            _pct(row.action_rate),
            _pct(row.precision_on_action),
            _num(row.mean_conviction_gap),
            _num(row.regret_per_decision),
            _pct(row.degradation_rate),
            _num(row.cost_per_scored),
        ]
        for row in rows
    ]
    return _table(headers, body) + (
        "\n\nOrdered by accuracy within each regime. **Read `SHOCK_DOWN` first**: a long-only "
        "system's worst outcome is not a missed rally, and a candidate that ranks first in "
        "`NORMAL` and last in `SHOCK_DOWN` is not the safer panel."
    )


def _not_measured_note(rows: Sequence[NotMeasured]) -> str:
    """A candidate that produced no scored decision (finding 3) is absent from the tables above
    rather than shown at 0.0% accuracy, which would read as measured and worst — not measured at
    all. Each is named with its own reason, because the three causes need different fixes."""
    if not rows:
        return ""
    listed = "".join(
        f"\n* `{row.candidate_id}` — {row.reason}"
        for row in sorted(rows, key=lambda row: row.candidate_id)
    )
    one = len(rows) == 1
    return (
        "\n\n**Not measured.** "
        + ("This candidate" if one else "These candidates")
        + " produced no scored decision, so "
        + ("it is" if one else "they are")
        + " left out of the ranking, the agreement matrix and the seat tables rather than "
        "reported at 0%.\n" + listed
    )


def _agreement_table(rows: Sequence[Agreement], ranking: Sequence[Ranked]) -> str:
    """Finding 6: an empty matrix has two causes, and they are opposite facts about the run.

    `compare.agreement` returns `()` both when one candidate ran and when *none* was measured, so
    the number of candidates that actually reached the ranking is what tells them apart. Asserting
    "only one candidate ran" over a ranking table listing none of them states a fact about the
    experiment that is simply untrue.
    """
    if not rows:
        if len({row.candidate_id for row in ranking}) == 1:
            return "Only one candidate ran, so there is nothing to compare it against."
        return (
            "No candidate produced a scored decision, so there is nothing to compare. See "
            "**Not measured** above for why."
        )
    body = [
        [
            row.regime,
            row.left,
            row.right,
            str(row.compared),
            _pct(row.rate),
            str(row.tradable_divergences),
        ]
        for row in rows
    ]
    return (
        "### Agreement\n\nPairwise, per regime. **Two candidates agreeing 98% of the time are "
        "one experiment run twice** — and paying for both buys one answer. `tradable divergence` "
        "is the disagreement that moves money: the cycles where exactly one of them asked for an "
        "order.\n\n"
        + _table(("regime", "left", "right", "compared", "agreement", "tradable divergence"), body)
    )


def _candidate_seat_tables(blocks: Sequence[CandidateSeats]) -> str:
    if not blocks:
        return ""
    return "\n\n".join(
        f"### Seats — {block.candidate_id}\n\n{_seat_tables(block.seats)}" for block in blocks
    )


def _calibration_block(report: LabReport) -> str:
    """§10.2's per-day table and its spread, and the gate verdict this page produced."""
    if not report.scenario:
        return ""
    parts = [
        f"## Calibration — {report.scenario}",
        "",
        "Days: " + (", ".join(day.isoformat() for day in report.calibration_days) or "none"),
        "",
    ]
    if report.per_day:
        parts += [
            _table(
                ("candidate", "day", "pool", "scored", "correct", "accuracy"),
                (
                    (
                        row.candidate_id,
                        row.day.isoformat(),
                        row.pool,
                        str(row.scored),
                        str(row.correct),
                        _pct(row.accuracy),
                    )
                    for row in report.per_day
                ),
            ),
            "",
            _spread_note(report.per_day),
            "",
        ]
    # No trailing "" here, unlike `_cost_projection_table`: that one is the last section on the
    # page, so the document's final `.rstrip()` absorbs it. This block sits in the middle — the
    # caller already supplies one "" separator before it and the next heading supplies its own —
    # so a trailing blank line here would triple up into "\n\n\n" instead of the document's one.
    return "\n".join([*parts, _gate_verdict(report)])


def _spread_note(rows: Sequence[DayMetrics]) -> str:
    """The spread across the days, per candidate *and per pool* — never pooled across regimes
    (§8.3). `calibration.POOLS["shock"]` runs SHOCK_UP and SHOCK_DOWN together, so a shock
    scenario's rows legitimately mix both, and they ask opposite questions of a long-only system:
    did the seats catch the move, did they protect capital. Blending them into one spread would
    let a candidate that is excellent on every up day and dangerous on every down day read as
    merely "inconsistent" — the one reading this page must never produce.

    Three days is not a distribution, but it is enough to see when one day carried a result — and
    a candidate whose pooled accuracy comes entirely from one of the three is not one this page
    should present as steady."""
    lines = []
    for candidate_id, pool in dict.fromkeys((row.candidate_id, row.pool) for row in rows):
        accuracies = [
            row.accuracy for row in rows if row.candidate_id == candidate_id and row.pool == pool
        ]
        lines.append(
            f"- **{candidate_id}** / {pool} — accuracy spread across its days: "
            f"{_pct(max(accuracies) - min(accuracies))} "
            f"({_pct(min(accuracies))} to {_pct(max(accuracies))})"
        )
    return "\n".join(lines)


def _gate_verdict(report: LabReport) -> str:
    """§10.6's four conditions, as a verdict a reader can act on."""
    if report.gate_passed:
        return (
            "**Gate: PASSED.** Every candidate materialised and deliberated, every seat answered "
            "at least once on its primary binding, the panel reached decisions, and the path "
            "completed."
        )
    # `calibration.failures_for` guarantees at least one reason today, but this function must not
    # depend on a caller's invariant to avoid rendering a sentence that stops mid-thought.
    listed = "\n".join(f"- {reason}" for reason in report.gate_failures) or "- no reason recorded"
    return (
        "**Gate: FAILED.** The §10.6 gate is shut for this dataset, matrix and day set:\n\n"
        f"{listed}"
    )


def _cost_projection_table(rows: Sequence[CostProjection]) -> str:
    """§10.6's projection, so §7.5's ceiling stops being a guess."""
    if not rows:
        return ""
    cadences = sorted({label for row in rows for label in row.projected})
    return "\n".join(
        [
            "## Projected spend",
            "",
            "Measured over the pinned days, projected across the declared window.",
            "",
            _table(
                ("candidate", "$/cycle", "$/scored", *(f"{label} run" for label in cadences)),
                (
                    (
                        row.candidate_id,
                        _num(row.cost_per_cycle),
                        _num(row.cost_per_scored),
                        *(_num(row.projected.get(label)) for label in cadences),
                    )
                    for row in rows
                ),
            ),
            "",
        ]
    )


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(value: Decimal) -> str:
    return f"{(value * Decimal(100)).quantize(Decimal('0.1'))}%"


def _num(value: Decimal | None) -> str:
    return "—" if value is None else str(value.quantize(Decimal("0.0001")))


def _stamp(moment: datetime) -> str:
    return moment.isoformat()


def write_report(report: LabReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_markdown(report), encoding="utf-8")
    return path
