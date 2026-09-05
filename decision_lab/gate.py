"""The §10.6 calibration gate — the artifact, and nothing else.

The sweep and the long run both refuse to start unless scenarios 1 and 2 have passed for this
exact `(dataset_digest, matrix_digest, dayset_digest)`. Change one prompt, change the matrix
digest, calibrate again — cheap, because it is nine days.

The key is deliberately **not** `corpus_id`. All four conditions are properties of the candidates,
the seats and the days; none is cadence-dependent. Keying on the corpus would force a
re-calibration every time §10.4 varied `--every`, which is the one axis the long run exists to
vary. The cadence a scenario ran at is *recorded* on its verdict for provenance, never used to
scope the gate.

A stub matrix can never satisfy a real matrix's gate, and it needs no runtime check to say so:
the bindings feed `Candidate.panel_digest`, which feeds `Matrix.matrix_digest`, which is a third
of this key. `evaluation` is recorded anyway, because a reader of the record must be able to see
which kind of run cleared it without re-deriving a digest.

This module holds models and file I/O only. The machinery that *produces* a verdict is
`calibration.py`; `sweep` and `longrun` import this one alone, so consulting the gate costs
neither of them a dependency on the thing that fills it in.

Failure semantics: an absent record reads as `None`, never as an error — "nobody has calibrated"
is a fact, not a fault. `require_satisfied` is the only refusal, and it names which of the four
conditions failed and which seat, because those need four different fixes (§15).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Final

from decision_lab.params import workspace_root
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime

#: Where a gate record lives, under the workspace. Gitignored with everything else there.
GATES_DIR: Final = "gates"

#: The two halves §10.6 requires, in the order an operator runs them.
SCENARIOS: Final = ("normal", "shock")


class SeatEvidence(DomainModel):
    """One seat's answering record over a calibration — §10.6's second condition.

    `answered_on` holds the bindings that actually produced a *vote*, read off
    `SeatResponse.fingerprint` — the same field §7.7's contamination check reads. An abstention is
    deliberately not an answer: a seat whose key is missing abstains quietly, and over six months
    that is a panel you paid to run and never tested.
    """

    candidate_id: str
    seat_id: str
    #: `provider_id:model` of the seat's configured primary binding.
    primary: str
    answered_on: tuple[str, ...] = ()

    @property
    def answered_on_primary(self) -> bool:
        return self.primary in self.answered_on


class CandidateEvidence(DomainModel):
    """What one candidate produced over the nine days, and what it cost.

    `cost_per_cycle` is what §10.6's projection multiplies: a provider call in `basket` mode
    answers for every instrument (`total_cost` de-duplicates by `call_id`), so cost is a property
    of the cycle, not of the decision. `cost_per_scored` is the §9.5 figure, reported beside it
    rather than instead of it.
    """

    candidate_id: str
    rows: int = 0
    decisions: int = 0
    #: Decisions the panel actually reached — §10.6's third condition. An all-`PANEL_DEGRADED`
    #: calibration has proved nothing.
    undegraded: int = 0
    scored: int = 0
    accuracy: Money = ZERO
    cost_usd: Money = ZERO
    cost_per_cycle: Money = ZERO
    cost_per_scored: Money = ZERO
    seats: tuple[SeatEvidence, ...] = ()


class ScenarioVerdict(DomainModel):
    """One half of the gate: what `calibrate normal` or `calibrate shock` found."""

    scenario: str
    ran_at: UtcDatetime
    #: Provenance only. Never in `gate_key` — see the module docstring.
    cadence_seconds: int = 0
    corpus_id: str = ""
    days: tuple[date, ...] = ()
    candidates: tuple[CandidateEvidence, ...] = ()
    report_path: str = ""
    #: One line per failed condition, naming the candidate and, where it is the reason, the seat.
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


class GateRecord(DomainModel):
    """Both halves, under one key. Written by calibration, read by `sweep` and `calibrate long`."""

    gate_key: str
    dataset_digest: str
    matrix_digest: str
    dayset_digest: str
    #: False when any candidate bound the offline stub (§7.2). Unreachable as a way to satisfy a
    #: real matrix's gate — the bindings are in `matrix_digest` — but recorded so the record can
    #: be read without re-deriving one.
    evaluation: bool = True
    normal: ScenarioVerdict | None = None
    shock: ScenarioVerdict | None = None

    @property
    def satisfied(self) -> bool:
        return not self.failures

    @property
    def failures(self) -> tuple[str, ...]:
        """Every reason this gate is shut, in scenario order. Empty means open."""
        reasons: list[str] = []
        for name in SCENARIOS:
            verdict: ScenarioVerdict | None = getattr(self, name)
            if verdict is None:
                reasons.append(
                    f"scenario '{name}' has not been run for this dataset, matrix and day set"
                )
            else:
                reasons += [f"{name}: {reason}" for reason in verdict.failures]
        return tuple(reasons)


def gate_key(*, dataset_digest: str, matrix_digest: str, dayset_digest: str) -> str:
    """§10.6's identity. The three things a pass is a property of, and nothing else."""
    payload = f"{dataset_digest}|{matrix_digest}|{dayset_digest}"
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def gate_path(key: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / GATES_DIR / f"{key}.json"


def read(key: str, *, workspace: Path | None = None) -> GateRecord | None:
    """The record for this key, or `None`. Absent is a fact, not an error."""
    path = gate_path(key, workspace=workspace)
    if not path.is_file():
        return None
    return GateRecord.model_validate_json(path.read_text(encoding="utf-8"))


def write(record: GateRecord, *, workspace: Path | None = None) -> Path:
    path = gate_path(record.gate_key, workspace=workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def record_scenario(record: GateRecord, verdict: ScenarioVerdict) -> GateRecord:
    """Replace one half, leaving the other untouched.

    Overwritten rather than merged: a re-run of `calibrate normal` is the newer measurement of
    that half, and folding the two would leave a verdict that is partly the run before the fix.
    """
    if verdict.scenario not in SCENARIOS:
        raise ConfigError(
            f"{verdict.scenario!r} is not a calibration scenario; §10.6 has "
            f"{' and '.join(SCENARIOS)}"
        )
    return record.model_copy(update={verdict.scenario: verdict})


def require_satisfied(
    *,
    dataset_digest: str,
    matrix_digest: str,
    dayset_digest: str,
    workspace: Path | None = None,
) -> GateRecord:
    """The open gate for this triple, or a refusal naming every reason it is shut (§15)."""
    key = gate_key(
        dataset_digest=dataset_digest,
        matrix_digest=matrix_digest,
        dayset_digest=dayset_digest,
    )
    record = read(key, workspace=workspace)
    if record is None:
        raise ConfigError(
            f"the §10.6 calibration gate has never been run for this dataset, matrix and day set "
            f"(gate {key}). Run `python -m decision_lab calibrate normal` and `calibrate shock` "
            "against this corpus and matrix first — it is nine days, and it is what proves every "
            "seat answered on its own model before you pay for a long run. Pass --skip-gate to "
            "proceed anyway; the report and the registry row will say that nobody checked."
        )
    if not record.satisfied:
        raise ConfigError(
            f"the §10.6 calibration gate is unsatisfied for gate {key}: "
            f"{'; '.join(record.failures)}. Fix the cause and re-run the scenario it names, or "
            "pass --skip-gate to proceed with that stamped on the report and the registry row."
        )
    return record
