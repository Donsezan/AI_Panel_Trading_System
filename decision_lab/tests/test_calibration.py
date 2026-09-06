"""The §10.6 conditions, on handmade rows.

The gate's value is entirely in *which* condition failed and *which* seat, so most of these are
asserted against constructed evidence rather than only end to end: the end-to-end run on the stub
panel passes every condition by construction and would never exercise a refusal. The arithmetic
that builds that evidence in the first place (`candidate_evidence`, `sample_for`, a populated
`days_for`) is exercised separately, on real `ScoredDecision`s and a real `Corpus` built by
`decision_lab.tests.factories` — evidence built by hand would let a mutation of the arithmetic
itself pass unnoticed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from decision_lab import calibration as cal
from decision_lab import gate
from decision_lab import sweep as sw
from decision_lab.calibration_days import CalibrationDays, Pool, Thresholds
from decision_lab.params import CADENCE_SECONDS
from decision_lab.scoring import ScoredDecision, Truth, Verdict
from decision_lab.tests.factories import corpus_with_entries
from tradebot.core.config import PanelConfig, ProviderBinding, ProviderSettings, SeatConfig
from tradebot.core.decision import SeatResponse, SeatVote
from tradebot.core.enums import Action, SizeHint

AT = datetime(2026, 1, 1, tzinfo=UTC)


def seat(seat_id: str, model: str, *, fallbacks: tuple[tuple[str, str], ...] = ()) -> SeatConfig:
    return SeatConfig(
        seat_id=seat_id,
        role="analyst",
        provider_id="stub",
        model=model,
        fallbacks=tuple(ProviderBinding(provider_id=p, model=m) for p, m in fallbacks),
    )


def panel(*seats: SeatConfig) -> PanelConfig:
    # `required_votes` is not a field of `PanelConfig` — it is `consensus.required_votes(panel)`,
    # `ceil(qualified_majority × seat_count)` — and the default `qualified_majority` (0.5) already
    # gives 1 for the one- and two-seat panels these tests build, which is all `seat_evidence` and
    # `candidate_evidence` need: neither reads it directly.
    return PanelConfig(
        panel_id="panel",
        providers=(ProviderSettings(provider_id="stub", kind="stub"),),
        seats=seats,
    )


def response(seat_id: str, model: str, *, abstained: bool = False) -> SeatResponse:
    # `SeatVote` requires `size_hint`, and its `_check_coherence` validator refuses a tradable
    # action carrying `SizeHint.NONE` — a vote built without both fails validation, not the test.
    vote = (
        None
        if abstained
        else SeatVote(action=Action.BUY, conviction=3, size_hint=SizeHint.HALF, thesis="t")
    )
    return SeatResponse(
        seat_id=seat_id,
        role="analyst",
        provider_id="stub",
        model=model,
        round_index=0,
        instrument_key="binance:BTC/USDT",
        vote=vote,
        abstain_reason=None if vote else "no answer",
        responded_at=AT,
    )


def row(*responses: SeatResponse, cost: str = "0.10", error: str = "") -> sw.SweepRow:
    return sw.SweepRow(
        cycle_id="c1", as_of=AT, responses=responses, cost_usd=Decimal(cost), error=error
    )


class FakeCandidate:
    """Only `candidate_id` and `panel` are read by the evidence functions."""

    def __init__(self, panel_: PanelConfig, candidate_id: str = "baseline") -> None:
        self.candidate_id = candidate_id
        self.panel = panel_


# --- condition 2: every seat answered at least once on its primary binding


def test_a_seat_that_answered_on_its_primary_passes() -> None:
    candidate = FakeCandidate(panel(seat("analyst", "model-a")))
    found = cal.seat_evidence(candidate, {"c1": row(response("analyst", "model-a"))})  # type: ignore[arg-type]
    assert len(found) == 1
    assert found[0].answered_on_primary


def test_a_seat_that_only_ever_answered_on_a_fallback_fails() -> None:
    """§10.6: this is the whole point of checking seats on a short horizon."""
    candidate = FakeCandidate(panel(seat("analyst", "model-a", fallbacks=(("stub", "model-b"),))))
    found = cal.seat_evidence(candidate, {"c1": row(response("analyst", "model-b"))})  # type: ignore[arg-type]
    assert not found[0].answered_on_primary
    assert found[0].answered_on == ("stub:model-b",)


def test_an_abstention_is_not_an_answer() -> None:
    """A seat whose key is missing abstains quietly, and that is exactly what this catches."""
    candidate = FakeCandidate(panel(seat("analyst", "model-a")))
    found = cal.seat_evidence(
        candidate,  # type: ignore[arg-type]
        {"c1": row(response("analyst", "model-a", abstained=True))},
    )
    assert not found[0].answered_on_primary
    assert found[0].answered_on == ()


def test_a_seat_that_never_appeared_at_all_fails() -> None:
    candidate = FakeCandidate(panel(seat("analyst", "model-a"), seat("risk", "model-c")))
    found = {
        s.seat_id: s
        for s in cal.seat_evidence(candidate, {"c1": row(response("analyst", "model-a"))})  # type: ignore[arg-type]
    }
    assert found["analyst"].answered_on_primary
    assert not found["risk"].answered_on_primary


# --- the four conditions, as failure messages


def evidence(**overrides: object) -> gate.CandidateEvidence:
    base: dict[str, object] = {
        "candidate_id": "baseline",
        "rows": 3,
        "decisions": 3,
        "undegraded": 3,
        "scored": 3,
        "seats": (
            gate.SeatEvidence(
                candidate_id="baseline",
                seat_id="analyst",
                primary="stub:model-a",
                answered_on=("stub:model-a",),
            ),
        ),
    }
    return gate.CandidateEvidence(**{**base, **overrides})


def test_a_clean_calibration_has_no_failures() -> None:
    assert cal.failures_for((evidence(),), report_written=True) == ()


def test_condition_one_a_candidate_that_never_deliberated() -> None:
    assert any(
        "never deliberated" in r for r in cal.failures_for((evidence(rows=0),), report_written=True)
    )


def test_condition_two_names_the_seat_and_both_bindings() -> None:
    broken = gate.SeatEvidence(
        candidate_id="baseline",
        seat_id="analyst",
        primary="stub:model-a",
        answered_on=("stub:model-b",),
    )
    failures = cal.failures_for((evidence(seats=(broken,)),), report_written=True)
    assert any("analyst" in r and "stub:model-a" in r and "stub:model-b" in r for r in failures)


def test_condition_two_distinguishes_never_answered_from_answered_on_a_fallback() -> None:
    silent = gate.SeatEvidence(
        candidate_id="baseline", seat_id="analyst", primary="stub:model-a", answered_on=()
    )
    failures = cal.failures_for((evidence(seats=(silent,)),), report_written=True)
    assert any("never answered at all" in r for r in failures)
    assert not any("only on" in r for r in failures)


def test_condition_three_an_all_degraded_calibration_has_proved_nothing() -> None:
    assert any(
        "PANEL_DEGRADED" in r
        for r in cal.failures_for((evidence(undegraded=0),), report_written=True)
    )


def test_condition_four_no_scored_verdict() -> None:
    assert any(
        "no scored verdict" in r
        for r in cal.failures_for((evidence(scored=0),), report_written=True)
    )


def test_condition_four_an_unrendered_report() -> None:
    assert any("report" in r for r in cal.failures_for((evidence(),), report_written=False))


def test_no_candidates_at_all_is_a_failure_not_a_pass() -> None:
    """Fail closed: an empty evidence list must never read as "every candidate passed", which is
    what an `all()` over nothing would say."""
    assert cal.failures_for((), report_written=True) != ()


# --- candidate_evidence: the arithmetic that produces the evidence, not just its messages


def test_candidate_evidence_counts_correct_by_verdict_not_by_truth_presence() -> None:
    """§9's `_correct` must count `Verdict.CORRECT` and nothing else. An unscorable decision
    (a gap in the tape) still carries a truth label — deliberately, here — and must not be
    counted just because `truth is not None`, or the gate's accuracy would disagree with the
    number `scoring.summarise` puts on the report for the same rows."""
    candidate = FakeCandidate(panel(seat("analyst", "model-a")))
    correct_decision = ScoredDecision(
        cycle_id="c1",
        as_of=AT,
        instrument_key="binance:BTC/USDT",
        regime=Pool.NORMAL,
        action=Action.BUY,
        conviction=Decimal("3"),
        asked_for_an_order=True,
        holding=False,
        truth=Truth.BUY,
        verdict=Verdict.CORRECT,
    )
    unscorable_decision = ScoredDecision(
        cycle_id="c2",
        as_of=AT,
        instrument_key="binance:BTC/USDT",
        regime=Pool.NORMAL,
        action=Action.WAIT,
        conviction=Decimal("1"),
        asked_for_an_order=False,
        holding=False,
        truth=Truth.STAND_ASIDE,
        verdict=Verdict.UNSCORED_GAP,
    )
    rows = {
        "c1": row(response("analyst", "model-a"), cost="0.10"),
        "c2": row(response("analyst", "model-a"), cost="0.10"),
    }
    found = cal.candidate_evidence(
        candidate,  # type: ignore[arg-type]
        rows,
        records=(),
        scored=(correct_decision, unscorable_decision),
    )
    assert found.scored == 1
    assert found.accuracy == Decimal(1)
    assert found.cost_per_scored == found.cost_usd


def test_cost_per_cycle_excludes_errored_rows_from_the_denominator() -> None:
    """§10.6: an errored row costs nothing by construction — the deliberation that would have
    cost something never completed — so it must not pad the denominator and understate $/cycle,
    which is exactly the direction the projection exists to guard against."""
    candidate = FakeCandidate(panel(seat("analyst", "model-a")))
    rows = {
        "c1": row(response("analyst", "model-a"), cost="0.10"),
        "c2": row(error="boom", cost="0"),
    }
    found = cal.candidate_evidence(
        candidate,  # type: ignore[arg-type]
        rows,
        records=(),
        scored=(),
    )
    assert found.rows == 2  # condition 1 counts every recorded row, errored or not
    assert found.cost_per_cycle == Decimal("0.10")


# --- the cost projection


def test_the_projection_scales_with_cadence() -> None:
    """§10.6: the nine days measure $/cycle, and the projection is printed beside `--budget` so
    §7.5's ceiling stops being a guess."""
    projected = cal.project_cost(evidence(cost_per_cycle=Decimal("0.02")), window="6m")
    # 182 days at 24h is 182 cycles; at 12h it is 364 — twice the spend.
    assert projected["24h"] == Decimal("3.64")
    assert projected["12h"] == projected["24h"] * 2
    assert set(projected) == set(CADENCE_SECONDS)


def test_the_projection_is_zero_when_nothing_was_spent() -> None:
    """A stub calibration costs nothing, and a projection of zero is the honest answer."""
    assert cal.project_cost(evidence(), window="6m")["24h"] == Decimal(0)


def test_an_unknown_window_refuses_naming_the_ones_that_exist() -> None:
    import pytest

    from tradebot.core.errors import ConfigError

    with pytest.raises(ConfigError, match="6m"):
        cal.project_cost(evidence(), window="7m")


# --- sample_for: every entry on the pinned days, folded into a Sample


def test_sample_for_takes_every_day_and_reports_per_pool_counts() -> None:
    """`full=True` because §10.2 runs every entry of the pinned days, never a stratified draw —
    and `selected`/`available` are keyed by pool so a thin day is visible on the report."""
    corpus = corpus_with_entries(count=48, as_of=datetime(2024, 1, 1, tzinfo=UTC))
    normal_day = date(2024, 1, 1)
    shock_day = date(2024, 1, 2)
    pinned = CalibrationDays(
        selected_at=AT,
        seed=1,
        reference_instrument="binance:BTC/USDT",
        scoring_timeframe="1h",
        thresholds=Thresholds(),
        dataset_digest="d1",
        dayset_digest="s1",
        days={"NORMAL": (normal_day,), "SHOCK_UP": (shock_day,), "SHOCK_DOWN": ()},
    )
    sample = cal.sample_for(corpus, pinned, (normal_day, shock_day))
    assert sample.full is True
    assert len(sample.cycle_ids) == 48
    assert sample.selected == {"NORMAL": 24, "SHOCK_UP": 24}
    assert sample.available == sample.selected


# --- day selection


def test_the_normal_scenario_takes_only_the_normal_pool() -> None:
    assert cal.POOLS["normal"] == (Pool.NORMAL,)


def test_the_shock_scenario_takes_both_directions() -> None:
    """One command, two regimes: they share a matrix and a gate half, and are reported apart."""
    assert cal.POOLS["shock"] == (Pool.SHOCK_UP, Pool.SHOCK_DOWN)


def test_pools_and_the_gate_scenarios_name_the_same_set() -> None:
    """`days_for` refuses on this declaration and `gate.record_scenario` refuses on the other —
    a scenario known to only one would be recordable with no pool, or drawable with no record."""
    assert set(cal.POOLS) == set(gate.SCENARIOS)


def test_days_for_a_populated_set_returns_exactly_its_pool() -> None:
    """The test a transposed `wanted` set (built from the wrong pools) would fail: `normal` must
    return only the NORMAL days, and `shock` the union of both shock directions — never a pool
    the scenario did not name."""
    normal_days = (date(2024, 1, 1), date(2024, 1, 2))
    up_days = (date(2024, 2, 1),)
    down_days = (date(2024, 3, 1),)
    pinned = CalibrationDays(
        selected_at=AT,
        seed=1,
        reference_instrument="binance:BTC/USDT",
        scoring_timeframe="1h",
        thresholds=Thresholds(),
        dataset_digest="d1",
        dayset_digest="s1",
        days={"NORMAL": normal_days, "SHOCK_UP": up_days, "SHOCK_DOWN": down_days},
    )
    assert cal.days_for(pinned, "normal") == normal_days
    assert cal.days_for(pinned, "shock") == tuple(sorted(up_days + down_days))


def test_an_unknown_scenario_refuses_rather_than_selecting_nothing() -> None:
    """Nine days with none selected would run zero entries and pass every condition vacuously."""
    import pytest

    from tradebot.core.errors import ConfigError

    pinned = CalibrationDays(
        selected_at=AT,
        seed=1,
        reference_instrument="binance:BTC/USDT",
        scoring_timeframe="1h",
        thresholds=Thresholds(),
        dataset_digest="d1",
        dayset_digest="s1",
        days={"NORMAL": (), "SHOCK_UP": (), "SHOCK_DOWN": ()},
    )
    with pytest.raises(ConfigError, match="not a calibration scenario"):
        cal.days_for(pinned, "sideways")


# --- per_day: the report's per-day rows, labelled with the pinned pool


def _scored(day: date, *, correct: bool) -> ScoredDecision:
    """A minimal scored decision on `day`, using the only two verdicts `is_scored` is `True`
    for — the same pair `scoring.summarise` and `cal._correct` split on."""
    return ScoredDecision(
        cycle_id="c1",
        as_of=datetime(day.year, day.month, day.day, tzinfo=UTC),
        instrument_key="binance:BTC/USDT",
        regime=Pool.NORMAL,
        action=Action.BUY,
        conviction=Decimal("3"),
        asked_for_an_order=True,
        holding=False,
        verdict=Verdict.CORRECT if correct else Verdict.WRONG,
    )


def test_per_day_groups_by_pinned_day_and_labels_its_pool() -> None:
    from datetime import date

    from decision_lab import render as rd
    from decision_lab.calibration_days import CalibrationDays, Thresholds

    pinned = CalibrationDays(
        selected_at=AT,
        seed=1,
        reference_instrument="binance:BTC/USDT",
        scoring_timeframe="1h",
        thresholds=Thresholds(),
        dataset_digest="d1",
        dayset_digest="s1",
        days={"NORMAL": (date(2026, 1, 1),), "SHOCK_UP": (), "SHOCK_DOWN": ()},
    )
    scored = (_scored(date(2026, 1, 1), correct=True), _scored(date(2026, 1, 1), correct=False))

    rows = cal.per_day("baseline", scored, pinned)

    assert len(rows) == 1
    assert isinstance(rows[0], rd.DayMetrics)
    assert rows[0].pool == "NORMAL"
    assert rows[0].scored == 2
    assert rows[0].correct == 1


def test_per_day_skips_a_day_the_set_never_pinned() -> None:
    """A corpus can hold entries either side of the nine days, and a row with a blank pool would
    read as a day whose regime nobody could determine."""
    from datetime import date

    from decision_lab.calibration_days import CalibrationDays, Thresholds

    pinned = CalibrationDays(
        selected_at=AT,
        seed=1,
        reference_instrument="binance:BTC/USDT",
        scoring_timeframe="1h",
        thresholds=Thresholds(),
        dataset_digest="d1",
        dayset_digest="s1",
        days={"NORMAL": (date(2026, 1, 1),), "SHOCK_UP": (), "SHOCK_DOWN": ()},
    )

    assert cal.per_day("baseline", (_scored(date(2026, 2, 2), correct=True),), pinned) == ()
