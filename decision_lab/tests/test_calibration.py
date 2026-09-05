"""The §10.6 conditions, on handmade rows.

The gate's value is entirely in *which* condition failed and *which* seat, so these are asserted
against constructed evidence rather than only end to end: the end-to-end run on the stub panel
passes every condition by construction and would never exercise a refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import calibration as cal
from decision_lab import gate
from decision_lab import sweep as sw
from decision_lab.calibration_days import Pool
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


def row(*responses: SeatResponse, cost: str = "0.10") -> sw.SweepRow:
    return sw.SweepRow(cycle_id="c1", as_of=AT, responses=responses, cost_usd=Decimal(cost))


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


# --- the cost projection


def test_the_projection_scales_with_cadence() -> None:
    """§10.6: the nine days measure $/cycle, and the projection is printed beside `--budget` so
    §7.5's ceiling stops being a guess."""
    projected = cal.project_cost(
        evidence(cost_per_cycle=Decimal("0.02")), window="6m", instruments=1
    )
    # 182 days at 24h is 182 cycles; at 12h it is 364 — twice the spend.
    assert projected["24h"] == Decimal("3.64")
    assert projected["12h"] == projected["24h"] * 2
    assert set(projected) == set(cal.CADENCE_SECONDS)


def test_the_projection_is_zero_when_nothing_was_spent() -> None:
    """A stub calibration costs nothing, and a projection of zero is the honest answer."""
    assert cal.project_cost(evidence(), window="6m", instruments=1)["24h"] == Decimal(0)


def test_the_projection_does_not_multiply_by_the_instrument_count() -> None:
    """Cost is per *cycle* — one call answers for every instrument in basket mode — so the
    instrument count moves the decision count, never the spend."""
    one = cal.project_cost(evidence(cost_per_cycle=Decimal("0.02")), window="6m", instruments=1)
    four = cal.project_cost(evidence(cost_per_cycle=Decimal("0.02")), window="6m", instruments=4)
    assert one == four


def test_an_unknown_window_refuses_naming_the_ones_that_exist() -> None:
    import pytest

    from tradebot.core.errors import ConfigError

    with pytest.raises(ConfigError, match="6m"):
        cal.project_cost(evidence(), window="7m", instruments=1)


# --- day selection


def test_the_normal_scenario_takes_only_the_normal_pool() -> None:
    assert cal.POOLS["normal"] == (Pool.NORMAL,)


def test_the_shock_scenario_takes_both_directions() -> None:
    """One command, two regimes: they share a matrix and a gate half, and are reported apart."""
    assert cal.POOLS["shock"] == (Pool.SHOCK_UP, Pool.SHOCK_DOWN)


def test_an_unknown_scenario_refuses_rather_than_selecting_nothing() -> None:
    """Nine days with none selected would run zero entries and pass every condition vacuously."""
    import pytest

    from decision_lab.calibration_days import CalibrationDays, Thresholds
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
