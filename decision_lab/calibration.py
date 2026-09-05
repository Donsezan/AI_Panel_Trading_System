"""Scenarios 1 and 2 — the snapshot-scored calibration, and the §10.6 gate it fills in.

Both scenarios are **the sweep with a fixed entry set**. `sweep.run` already takes a `Sample`, so
this module builds one from the pinned days (§4.5) and calls it unchanged: the §7.4 cache, the
§7.5 budget ceiling, §7.6 resume and §7.7's substitute policy are inherited rather than
reimplemented. That is also what makes §10.1's "identical evidence" claim true by construction —
it is the same corpus the sweep reads, not a second pass over it.

`SHOCK_UP` and `SHOCK_DOWN` are drawn together by `calibrate shock` and never pooled in the result
(§8.3): they ask opposite questions of a long-only system, and a blended figure hides both.

Failure semantics: a missing or stale pinned day set refuses, naming `dataset days` (§15). Every
gate condition is fail-closed — an empty evidence list is a failure, not a vacuous pass — and each
failure names the candidate, and the seat where the seat is the reason, because the four
conditions need four different fixes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from decision_lab.calibration_days import CalibrationDays, Pool
from decision_lab.candidates import Candidate
from decision_lab.corpus import Corpus
from decision_lab.gate import CandidateEvidence, SeatEvidence
from decision_lab.params import CADENCE_SECONDS, WINDOW_DAYS
from decision_lab.records import CycleRecord
from decision_lab.sampling import Sample
from decision_lab.scoring import ScoredDecision, Verdict, ratio
from decision_lab.sweep import SweepRow
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO, multiply
from tradebot.core.schema import Money

#: Which pools each scenario draws. §10.3's two blocks are one *command* and two regimes: run
#: together because they share a matrix and a gate half, reported apart because they ask opposite
#: questions (§8.3). Keys must track `gate.SCENARIOS` — `test_calibration.py` pins the two sets
#: equal, because `days_for` refuses on this one and `gate.record_scenario` refuses on the other,
#: and a scenario known to only one would be recordable with no pool or drawable with no record.
POOLS: Final[dict[str, tuple[Pool, ...]]] = {
    "normal": (Pool.NORMAL,),
    "shock": (Pool.SHOCK_UP, Pool.SHOCK_DOWN),
}

#: For the §10.6 projection, so the arithmetic below reads as "days × cycles per day" rather than
#: as a magic constant.
SECONDS_PER_DAY: Final = 86_400


def days_for(pinned: CalibrationDays, scenario: str) -> tuple[date, ...]:
    """The pinned days this scenario runs over, sorted.

    Refuses an unknown scenario rather than returning an empty tuple: nine days with none of them
    selected would run zero entries and pass every gate condition vacuously.
    """
    if scenario not in POOLS:
        raise ConfigError(
            f"{scenario!r} is not a calibration scenario; §10 has {' and '.join(sorted(POOLS))}"
        )
    wanted = {pool.value for pool in POOLS[scenario]}
    return tuple(
        sorted({day for name, days in pinned.days.items() if name in wanted for day in days})
    )


def sample_for(corpus: Corpus, pinned: CalibrationDays, days: Sequence[date]) -> Sample:
    """Every corpus entry falling on these days — a `Sample`, so `sweep.run` needs no new path.

    `full=True` because nothing here is sampled: §10.2 runs *every* candidate over *every* entry
    of the pinned days, and a stratified draw over nine days would defeat the point of pinning
    them. `selected` and `available` are keyed by pool so a thin day is visible on the report,
    exactly as they are for a sweep.
    """
    entries = corpus.for_days(days)
    selected: dict[str, int] = {}
    for entry in entries:
        pool = pinned.pool_of(entry.day)
        if pool is not None:
            selected[pool.value] = selected.get(pool.value, 0) + 1
    return Sample(
        cycle_ids=tuple(entry.cycle_id for entry in entries),
        seed=pinned.seed,
        full=True,
        selected=selected,
        available=dict(selected),
    )


def seat_evidence(candidate: Candidate, rows: Mapping[str, SweepRow]) -> tuple[SeatEvidence, ...]:
    """Which bindings each declared seat actually voted on (§10.6, condition 2).

    Read off `SeatResponse.fingerprint`, the binding that answered *after* any fallback — the same
    field §7.7's contamination check reads, so "substituted" and "never answered on its primary"
    can never disagree.

    An abstention contributes nothing. That is the case this condition exists for: a seat whose
    key is missing abstains quietly, and over six months that is a panel you paid to run and never
    tested. A response naming a seat the panel does not declare is ignored here — §7.7 already
    treats it as contamination, which drops the whole row before it reaches this.
    """
    primary = {s.seat_id: s.primary.fingerprint for s in candidate.panel.seats}
    seen: dict[str, set[str]] = {seat_id: set() for seat_id in primary}
    for row in rows.values():
        for answer in row.responses:
            if answer.seat_id in seen and not answer.abstained:
                seen[answer.seat_id].add(answer.fingerprint)
    return tuple(
        SeatEvidence(
            candidate_id=candidate.candidate_id,
            seat_id=seat_id,
            primary=fingerprint,
            answered_on=tuple(sorted(seen[seat_id])),
        )
        for seat_id, fingerprint in sorted(primary.items())
    )


def candidate_evidence(
    candidate: Candidate,
    rows: Mapping[str, SweepRow],
    records: Sequence[CycleRecord],
    scored: Sequence[ScoredDecision],
) -> CandidateEvidence:
    """Everything the gate and the cost projection need about one candidate.

    `records` is `sweep.records_from_rows(corpus, rows)` — this candidate's own rows
    folded onto the corpus's frozen snapshots, never the corpus's reference-pass records:
    `Sequence[CycleRecord]` admits either, and passing the reference pass's would make
    `decisions` a corpus-wide constant identical for every candidate, regardless of what it
    actually decided.

    `cost_per_cycle` divides by the rows that produced an answer, not by every row `rows` holds.
    `sweep._evaluate` gives an errored row `cost_usd=ZERO` by construction — the deliberation that
    would have cost something never completed — so counting it in the denominator would divide
    real spend across more cycles than were ever paid for and *understate* the $/cycle a long run
    would face, exactly the direction §10.6's projection exists to guard against. A contaminated
    row stays in the denominator: a substitute answered, so a real call was made and real money
    was spent — it just cannot be scored (§7.7).
    """
    scored_count = sum(1 for row in scored if row.verdict.is_scored)
    correct = _correct(scored)
    priced = [row for row in rows.values() if not row.error]
    cost = sum((row.cost_usd for row in priced), start=ZERO)
    return CandidateEvidence(
        candidate_id=candidate.candidate_id,
        rows=len(rows),
        decisions=sum(len(record.decisions) for record in records),
        undegraded=sum(1 for row in scored if not row.degraded),
        scored=scored_count,
        accuracy=ratio(correct, scored_count),
        cost_usd=cost,
        cost_per_cycle=ratio(cost, len(priced)),
        cost_per_scored=ratio(cost, scored_count),
        seats=seat_evidence(candidate, rows),
    )


def _correct(scored: Sequence[ScoredDecision]) -> int:
    """How many verdicts were right, by the *same* predicate `scoring.summarise` uses.

    `Verdict.CORRECT` and nothing else. Not "`truth` is not None" — an unscorable decision carries
    a truth label too, and counting those would give the gate a number the report disagrees with.
    """
    return sum(1 for row in scored if row.verdict is Verdict.CORRECT)


def failures_for(evidence: Sequence[CandidateEvidence], *, report_written: bool) -> tuple[str, ...]:
    """§10.6's four conditions, as one message per failure. Empty means the half passed.

    Fail-closed on an empty list: a calibration that measured no candidate at all must never read
    as "every candidate passed", which is what an `all()` over nothing would say.
    """
    if not evidence:
        reasons = ["no candidate produced any evidence — nothing was measured"]
    else:
        reasons = [reason for found in evidence for reason in _failures_for_one(found)]
    if not report_written:
        reasons.append("the report was not rendered, so the path did not complete (condition 4)")
    return tuple(reasons)


def _failures_for_one(found: CandidateEvidence) -> list[str]:
    """One candidate against all four conditions, in the spec's own order."""
    name = found.candidate_id
    reasons: list[str] = []
    if found.rows == 0:
        # Condition 1. §10.6 says "actually exercised rather than asserted": `load_matrix`
        # returning a valid `Basket` proves the TOML, not that anything ever ran on it.
        reasons.append(f"{name}: never deliberated — no row was recorded (condition 1)")
    for seat in found.seats:
        if seat.answered_on_primary:
            continue
        if not seat.answered_on:
            reasons.append(
                f"{name} / {seat.seat_id}: never answered at all; its primary binding is "
                f"{seat.primary} (condition 2)"
            )
        else:
            reasons.append(
                f"{name} / {seat.seat_id}: never answered on its primary binding "
                f"{seat.primary}, only on {', '.join(seat.answered_on)} (condition 2)"
            )
    if found.undegraded == 0:
        reasons.append(
            f"{name}: reached no decision — every cycle resolved PANEL_DEGRADED, which proves "
            "nothing (condition 3)"
        )
    if found.scored == 0:
        reasons.append(f"{name}: produced no scored verdict (condition 4)")
    return reasons


def project_cost(evidence: CandidateEvidence, *, window: str) -> dict[str, Money]:
    """§10.6's cost projection: what this candidate would spend over `window`, per cadence.

    Multiplied by `cost_per_cycle`, never by `cost_per_scored`. In `basket` mode one provider call
    answers for every instrument (`total_cost` de-duplicates by `call_id`), so cost is a property
    of the cycle rather than of how many instruments it answered for — a basket of four costs
    exactly what a basket of one costs, which is why there is no instrument count here for the
    projection to multiply by.
    """
    if window not in WINDOW_DAYS:
        raise ConfigError(
            f"{window!r} is not a window this tool measures; it has "
            f"{', '.join(sorted(WINDOW_DAYS))}"
        )
    seconds = Decimal(WINDOW_DAYS[window] * SECONDS_PER_DAY)
    return {
        label: multiply(evidence.cost_per_cycle, seconds / Decimal(cadence))
        for label, cadence in CADENCE_SECONDS.items()
    }
