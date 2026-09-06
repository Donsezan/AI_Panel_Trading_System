# decision_lab slice D, pass 1 — calibration scenarios and the §10.6 gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the three §10 calibration scenarios (`calibrate normal`, `calibrate shock`, `calibrate long`), the §10.6 gate that refuses an uncalibrated sweep or long run, and the cost projection that turns `--budget` from a guess into a measurement.

**Architecture:** Scenarios 1 and 2 are *the existing sweep pointed at a fixed entry set* — `sweep.run` already accepts a `Sample`, so calibration builds one from the pinned days and calls it unchanged, inheriting the cache, the budget ceiling, resume and the §7.7 contamination policy. Only scenario 3 is a genuinely different instrument (§10.1): a full `BacktestHarness` pass with its own ledger, in its own workspace database. A small `gate.py` owns the pass/fail artifact so `sweep` can consult it without importing the scenario machinery.

**Tech Stack:** Python 3.11, pydantic v2 (`DomainModel`), `Decimal` only, pytest, ruff, mypy. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §10 (the three scenarios and the gate), §11 (the registry), §13 (CLI), §14 (the report), §15 (failure semantics), §16 (testing).

## Global Constraints

- **No `float`, anywhere in `decision_lab`.** Enforced by `decision_lab/tests/test_discipline.py`. Money and every ratio is `Decimal`, via `tradebot.core.money` and `tradebot.core.schema.Money`.
- **Nothing under `tradebot/` may name `decision_lab`** — not an import, not an attribute, not a string. Enforced by `decision_lab/tests/test_separation.py`. **This pass touches no file under `tradebot/`.** `git diff --stat main -- tradebot/` must stay empty.
- **Nothing prints.** `T20` bans `print` repo-wide. A result is written to a file under `decision_lab/reports/`; progress goes to the logger; the verdict is the exit code.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never `datetime.now()` in library code.
- **Comments explain *why*, and cite the spec section they implement** (`§10.6`, `§7.4`), per CLAUDE.md.
- **Docstrings state failure semantics at module level.**
- **Both gates must pass:** `.\decision_lab\check.ps1` and the root `.\check.ps1`.
- Exit codes (already declared in `decision_lab/cli.py`): `0` ok, `2` misuse, `3` dataset/day-set, `4` candidate invalid, `5` budget ceiling, `6` gate unsatisfied.

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `decision_lab/gate.py` | The §10.6 artifact: `GateRecord`, its key, its persistence, and `require_satisfied`. Models and file I/O only — no scenario logic, so `sweep` imports this and nothing else. |
| `decision_lab/calibration.py` | Scenarios 1 and 2: pinned days → corpus entries → `sweep.run` → the four conditions → the cost projection. |
| `decision_lab/longrun.py` | Scenario 3: one candidate through `BacktestHarness`, its own ledger, the §10.4 profit block and veto breakdown. |
| `decision_lab/tests/test_gate.py` | Gate key, round-trip, and every refusal message. |
| `decision_lab/tests/test_calibration.py` | The four conditions and the cost projection, on handmade rows. |
| `decision_lab/tests/test_longrun.py` | `net_profit` arithmetic, `UNVALUABLE`, veto breakdown, window parsing. |
| `decision_lab/tests/test_cli_calibrate.py` | The three commands through `cli.main`. |
| `decision_lab/tests/test_slice_d_end_to_end.py` | The slice exit criterion. |

**Modify**

| File | Change |
|---|---|
| `decision_lab/params.py` | `WINDOW_DAYS` and `DEFAULT_LONG_WINDOW`. |
| `decision_lab/registry.py` | Six non-identity fields for §10's numbers; `gate_unsatisfied` as a new `status` value. |
| `decision_lab/render.py` | `LabReport` gains the calibration blocks; new `DayMetrics`, `CostProjection`, `LongRunReport`, and the `GATE SKIPPED` banner. |
| `decision_lab/cli.py` | The `calibrate` command family; `--skip-gate` on `sweep` and `calibrate long`; the gate consult in `sweep_command`. |
| `decision_lab/tests/conftest.py` | A `calibrated_corpus` fixture — a corpus whose dataset also has a pinned day set. |
| `decision_lab/tests/test_cli_sweep.py`, `test_slice_c_end_to_end.py` | `sweep` now consults the gate. |
| `decision_lab/PROGRESS.md`, `CLAUDE.md` | Slice D pass 1 status, the new commands, the rules that are easy to get backwards. |

**Dependency direction (no cycles):** `gate.py` → `params` only. `calibration.py` → `gate`, `render`, `sweep`, `scoring`, `seats`, `sampling`, `corpus`, `candidates`, `calibration_days`. `longrun.py` → `render`, `candidates`, `corpus`, `registry`, `tradebot.validation`. `render.py` imports neither `gate` nor `calibration` — it carries the gate's verdict as plain fields.

---

### Task 1: `gate.py` — the §10.6 artifact

**Files:**
- Create: `decision_lab/gate.py`
- Test: `decision_lab/tests/test_gate.py`

**Interfaces:**
- Consumes: `decision_lab.params.workspace_root`, `tradebot.core.schema.{DomainModel, Money, UtcDatetime}`, `tradebot.core.money.ZERO`, `tradebot.core.errors.ConfigError`.
- Produces:
  - `gate.GATES_DIR: Final[str]`, `gate.SCENARIOS: Final[tuple[str, str]]`
  - `gate.SeatEvidence(candidate_id: str, seat_id: str, primary: str, answered_on: tuple[str, ...])` with `.answered_on_primary -> bool`
  - `gate.CandidateEvidence(candidate_id: str, rows: int, decisions: int, undegraded: int, scored: int, accuracy: Money, cost_usd: Money, cost_per_cycle: Money, cost_per_scored: Money, seats: tuple[SeatEvidence, ...])`
  - `gate.ScenarioVerdict(scenario: str, ran_at: UtcDatetime, cadence_seconds: int, corpus_id: str, days: tuple[date, ...], candidates: tuple[CandidateEvidence, ...], report_path: str, failures: tuple[str, ...])` with `.passed -> bool`
  - `gate.GateRecord(gate_key: str, dataset_digest: str, matrix_digest: str, dayset_digest: str, evaluation: bool, normal: ScenarioVerdict | None, shock: ScenarioVerdict | None)` with `.satisfied -> bool`, `.failures -> tuple[str, ...]`
  - `gate.gate_key(*, dataset_digest: str, matrix_digest: str, dayset_digest: str) -> str`
  - `gate.gate_path(key: str, *, workspace: Path | None = None) -> Path`
  - `gate.read(key: str, *, workspace: Path | None = None) -> GateRecord | None`
  - `gate.write(record: GateRecord, *, workspace: Path | None = None) -> Path`
  - `gate.record_scenario(record: GateRecord, verdict: ScenarioVerdict) -> GateRecord`
  - `gate.require_satisfied(*, dataset_digest: str, matrix_digest: str, dayset_digest: str, workspace: Path | None = None) -> GateRecord`

- [ ] **Step 1: Write the failing tests**

Create `decision_lab/tests/test_gate.py`:

```python
"""The §10.6 gate artifact: its key, its persistence, and every way it refuses.

The gate is the one thing standing between an operator and a six-month run paid for with a seat
that never answered, so each of its four conditions is asserted by the message it produces —
"which of the four failed, and which seat" is the whole value of the refusal (§15).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import gate
from tradebot.core.errors import ConfigError

KEYS = {"dataset_digest": "d1", "matrix_digest": "m1", "dayset_digest": "s1"}


def verdict(scenario: str, *, failures: tuple[str, ...] = ()) -> gate.ScenarioVerdict:
    return gate.ScenarioVerdict(
        scenario=scenario,
        ran_at=datetime(2026, 1, 1, tzinfo=UTC),
        cadence_seconds=14_400,
        corpus_id="c1",
        days=(date(2026, 1, 1),),
        candidates=(gate.CandidateEvidence(candidate_id="baseline", rows=3, scored=3),),
        report_path="reports/x.md",
        failures=failures,
    )


def record() -> gate.GateRecord:
    return gate.GateRecord(gate_key=gate.gate_key(**KEYS), **KEYS)


def test_the_key_is_stable_and_covers_all_three_parts() -> None:
    first = gate.gate_key(**KEYS)
    assert first == gate.gate_key(**KEYS), "the same triple is the same gate"
    assert first != gate.gate_key(dataset_digest="d2", matrix_digest="m1", dayset_digest="s1")
    assert first != gate.gate_key(dataset_digest="d1", matrix_digest="m2", dayset_digest="s1")
    assert first != gate.gate_key(dataset_digest="d1", matrix_digest="m1", dayset_digest="s2")


def test_the_key_is_exactly_the_three_parts_and_never_the_cadence() -> None:
    """§10.6: all four conditions are properties of the candidates, the seats and the days.
    Keying on the corpus — or on the cadence — would force a re-calibration every time §10.4
    varied `--every`, which is the one axis the long run exists to vary. Asserted on the
    signature, so adding a fourth part to the key fails here rather than silently doubling
    everyone's calibration bill."""
    import inspect

    assert set(inspect.signature(gate.gate_key).parameters) == {
        "dataset_digest",
        "matrix_digest",
        "dayset_digest",
    }


def test_a_record_round_trips(tmp_path: Path) -> None:
    written = gate.record_scenario(record(), verdict("normal"))
    gate.write(written, workspace=tmp_path)
    assert gate.read(written.gate_key, workspace=tmp_path) == written


def test_an_absent_record_reads_as_none_never_as_an_error(tmp_path: Path) -> None:
    assert gate.read("nothing-here", workspace=tmp_path) is None


def test_both_halves_are_needed(tmp_path: Path) -> None:
    """§10.6 names scenarios 1 *and* 2. One half is not a pass."""
    gate.write(gate.record_scenario(record(), verdict("normal")), workspace=tmp_path)
    with pytest.raises(ConfigError, match="shock"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_failing_half_refuses_and_names_the_failure(tmp_path: Path) -> None:
    both = gate.record_scenario(record(), verdict("normal"))
    both = gate.record_scenario(
        both, verdict("shock", failures=("baseline / analyst: never answered on its primary",))
    )
    gate.write(both, workspace=tmp_path)
    with pytest.raises(ConfigError, match="never answered on its primary"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_missing_record_names_the_command_that_produces_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="calibrate normal"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_satisfied_gate_returns_its_record(tmp_path: Path) -> None:
    both = gate.record_scenario(record(), verdict("normal"))
    both = gate.record_scenario(both, verdict("shock"))
    gate.write(both, workspace=tmp_path)
    assert gate.require_satisfied(**KEYS, workspace=tmp_path).satisfied


def test_recording_a_scenario_replaces_that_half_and_leaves_the_other() -> None:
    both = gate.record_scenario(record(), verdict("normal", failures=("stale",)))
    both = gate.record_scenario(both, verdict("shock"))
    fixed = gate.record_scenario(both, verdict("normal"))
    assert fixed.normal is not None and fixed.normal.passed
    assert fixed.shock is not None and fixed.shock.passed
    assert fixed.satisfied


def test_a_seat_that_only_answered_on_a_fallback_is_not_on_its_primary() -> None:
    seat = gate.SeatEvidence(
        candidate_id="baseline",
        seat_id="analyst",
        primary="openrouter:model-a",
        answered_on=("openrouter:model-b",),
    )
    assert not seat.answered_on_primary


def test_evidence_defaults_are_zero_not_none() -> None:
    """A candidate nobody measured must read as zero decisions, never as a missing field."""
    evidence = gate.CandidateEvidence(candidate_id="x")
    assert evidence.rows == 0
    assert evidence.cost_per_cycle == Decimal(0)


def test_an_unknown_scenario_is_refused_rather_than_stored() -> None:
    with pytest.raises(ConfigError, match="not a calibration scenario"):
        gate.record_scenario(record(), verdict("sideways"))
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.gate'`

- [ ] **Step 3: Write `decision_lab/gate.py`**

```python
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
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_gate.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 5: Lint and type-check**

Run: `.venv\Scripts\python.exe -m ruff format decision_lab; .venv\Scripts\python.exe -m ruff check decision_lab; .venv\Scripts\python.exe -m mypy decision_lab`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/gate.py decision_lab/tests/test_gate.py
git commit -m "feat(decision_lab): the section 10.6 gate artifact"
```

---

### Task 2: registry fields and the window table

**Files:**
- Modify: `decision_lab/params.py`
- Modify: `decision_lab/registry.py`
- Test: `decision_lab/tests/test_registry.py`

**Interfaces:**
- Produces:
  - `params.WINDOW_DAYS: Final[dict[str, int]]` — `{"1m": 30, "3m": 91, "6m": 182, "12m": 365}`
  - `params.DEFAULT_LONG_WINDOW: Final[str]` — `"6m"`
  - `registry.RunRow` gains `gate_skipped: bool`, `total_profit: Money`, `realized_pnl: Money`, `unrealized_pnl: Money`, `net_profit: Money`, `unvaluable: bool` — all **non-identity**.
  - `registry.STATUS_GATE_UNSATISFIED: Final[str]` — `"gate_unsatisfied"`

- [ ] **Step 1: Write the failing tests**

Append to `decision_lab/tests/test_registry.py` (add `from datetime import UTC, datetime` and `from decimal import Decimal` at the top if they are not already imported):

```python
def test_slice_d_fields_are_recorded_but_never_identity() -> None:
    """§11: `run_id` is over the parameters of the experiment. What a run *produced* — profit,
    whether the gate was skipped — is recorded beside it and must never split one experiment
    into two rows."""
    base = registry.RunRow(
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        scenario="calibrate-long",
        dataset_digest="d1",
        matrix_digest="m1",
        dayset_digest="s1",
        candidate_id="baseline",
        cadence_seconds=14_400,
        start_equity=Decimal(1000),
        window="6m",
    )
    produced = base.model_copy(
        update={
            "gate_skipped": True,
            "total_profit": Decimal("80.00"),
            "realized_pnl": Decimal("50.00"),
            "unrealized_pnl": Decimal("30.00"),
            "net_profit": Decimal("-40.00"),
            "unvaluable": False,
        }
    )
    assert produced.identity == base.identity, "an outcome is not a parameter"


def test_window_and_start_equity_do_split_the_identity() -> None:
    """They are §10.4 parameters, and §11 put them in `run_id` from the start for this reason."""
    base = registry.RunRow(
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC), scenario="calibrate-long", window="6m"
    )
    assert base.identity != base.model_copy(update={"window": "3m"}).identity
    assert base.identity != base.model_copy(update={"start_equity": Decimal(500)}).identity


def test_net_profit_may_be_negative() -> None:
    """A panel that made $80 on $120 of tokens lost money, and the row must be able to say so."""
    row = registry.RunRow(
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC), net_profit=Decimal("-40.00")
    )
    assert row.net_profit < 0
    assert '"net_profit":"-40.00"' in row.model_dump_json()


def test_the_window_table_covers_the_long_run_default() -> None:
    from decision_lab.params import DEFAULT_LONG_WINDOW, WINDOW_DAYS

    assert DEFAULT_LONG_WINDOW in WINDOW_DAYS
    assert WINDOW_DAYS[DEFAULT_LONG_WINDOW] == 182
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_registry.py -q`
Expected: FAIL — `RunRow` has no `gate_skipped`; `decision_lab.params` has no `WINDOW_DAYS`.

- [ ] **Step 3: Add the constants to `params.py`**

Append after `SAMPLE_SIZES`:

```python
#: `--window` values `calibrate long` accepts, in days. A month is 30 and a year 365; six months
#: is 182 rather than 183 so `6m` and two `3m` runs cover the same span and stay comparable —
#: §10.5's "cadences, one candidate" row is the only fair profit comparison this table protects.
WINDOW_DAYS: Final = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}

#: §10.4's six-month long exposure run.
DEFAULT_LONG_WINDOW: Final = "6m"
```

- [ ] **Step 4: Add the fields to `registry.py`**

Add to `RunRow`, immediately after the existing `cost_usd` line:

```python
    #: §10.6. A run whose provenance reads "nobody checked the seats first" says so on its face.
    #: Not identity: skipping the gate changes nothing the run produces, only what is known
    #: about it beforehand, and a row that split on it would show one experiment as two.
    gate_skipped: bool = False

    # --- §10.4's profit block, filled by `calibrate long` alone and zero everywhere else.
    #: Mark-to-market: `aggregate(...).equity − start_equity`. **Not** `Evidence.realized_pnl`,
    #: which sums closed round trips only and would ignore an open position at the window's end —
    #: the same class of error as a drawdown gate measuring cost basis (ADR 0027).
    total_profit: Money = ZERO
    realized_pnl: Money = ZERO
    unrealized_pnl: Money = ZERO
    #: `total_profit − cost_usd`. The ask is *efficient*, not merely profitable.
    net_profit: Money = ZERO
    #: A frozen aggregate at the window's end. No figure at all is reported: freezing is
    #: ignorance, and a number produced in ignorance is worse than its absence (§10.4).
    unvaluable: bool = False
```

Add beside `REGISTRY_FILE`:

```python
#: §11's statuses, plus slice D's. A run that never produced a number is still a fact about the
#: experiment, and "the gate was shut" is a different fact from "no provider answered".
STATUS_GATE_UNSATISFIED: Final = "gate_unsatisfied"
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_registry.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/params.py decision_lab/registry.py decision_lab/tests/test_registry.py
git commit -m "feat(decision_lab): registry fields for the calibration scenarios"
```

---

### Task 3: `calibration.py` — the four conditions and the cost projection

**Files:**
- Create: `decision_lab/calibration.py`
- Test: `decision_lab/tests/test_calibration.py`

**Interfaces:**
- Consumes: `gate.{SeatEvidence, CandidateEvidence}`, `candidates.Candidate`, `sweep.SweepRow`, `sampling.Sample`, `corpus.Corpus`, `calibration_days.{CalibrationDays, Pool}`, `scoring.{ScoredDecision, ratio}`, `records.CycleRecord`, `params.{CADENCE_SECONDS, WINDOW_DAYS}`.
- Produces:
  - `calibration.POOLS: Final[dict[str, tuple[Pool, ...]]]`
  - `calibration.SECONDS_PER_DAY: Final[int]`
  - `calibration.days_for(pinned: CalibrationDays, scenario: str) -> tuple[date, ...]`
  - `calibration.sample_for(corpus: Corpus, pinned: CalibrationDays, days: Sequence[date]) -> Sample`
  - `calibration.seat_evidence(candidate: Candidate, rows: Mapping[str, SweepRow]) -> tuple[SeatEvidence, ...]`
  - `calibration.candidate_evidence(candidate: Candidate, rows: Mapping[str, SweepRow], records: Sequence[CycleRecord], scored: Sequence[ScoredDecision]) -> CandidateEvidence`
  - `calibration.failures_for(evidence: Sequence[CandidateEvidence], *, report_written: bool) -> tuple[str, ...]`
  - `calibration.project_cost(evidence: CandidateEvidence, *, window: str, instruments: int) -> dict[str, Money]`

`calibration.per_day` also belongs to this module but returns `render.DayMetrics`, which Task 4 defines. It lands in Task 4.

**Before writing any code, read two things:** `SeatConfig.fallbacks`' declared element type in `tradebot/core/config.py` (the test helper below builds them), and how `scoring.summarise` counts "correct" — `candidate_evidence` must use the *same* predicate, not an approximation.

- [ ] **Step 1: Write the failing tests**

Create `decision_lab/tests/test_calibration.py`:

```python
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
from tradebot.core.config import PanelConfig, ProviderBinding, ProviderConfig, SeatConfig
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
    return PanelConfig(
        providers=(ProviderConfig(provider_id="stub", kind="stub"),),
        seats=seats,
        required_votes=1,
    )


def response(seat_id: str, model: str, *, abstained: bool = False) -> SeatResponse:
    # `SeatVote` requires `size_hint`, and its `_check_coherence` validator refuses a tradable
    # action carrying `SizeHint.NONE` — a vote built without both fails validation, not the test.
    vote = (
        None
        if abstained
        else SeatVote(
            action=Action.BUY, conviction=3, size_hint=SizeHint.HALF, thesis="t"
        )
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
    found = cal.seat_evidence(  # type: ignore[arg-type]
        candidate, {"c1": row(response("analyst", "model-a", abstained=True))}
    )
    assert not found[0].answered_on_primary
    assert found[0].answered_on == ()


def test_a_seat_that_never_appeared_at_all_fails() -> None:
    candidate = FakeCandidate(panel(seat("analyst", "model-a"), seat("risk", "model-c")))
    found = {s.seat_id: s for s in cal.seat_evidence(candidate, {"c1": row(response("analyst", "model-a"))})}  # type: ignore[arg-type]
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
    return gate.CandidateEvidence(**{**base, **overrides})  # type: ignore[arg-type]


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
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_calibration.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.calibration'`

- [ ] **Step 3: Write `decision_lab/calibration.py`**

```python
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
#: questions (§8.3).
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

    `cost_per_cycle` divides by the rows actually bought, not by the entries requested: a resumed
    calibration served half its answers from the §7.4 cache, and dividing by the request would
    report a $/cycle that halves every time somebody re-runs it.
    """
    scored_count = sum(1 for row in scored if row.verdict.is_scored)
    correct = _correct(scored)
    cost = sum((row.cost_usd for row in rows.values()), start=ZERO)
    return CandidateEvidence(
        candidate_id=candidate.candidate_id,
        rows=len(rows),
        decisions=sum(len(record.decisions) for record in records),
        undegraded=sum(1 for row in scored if not row.degraded),
        scored=scored_count,
        accuracy=ratio(correct, scored_count),
        cost_usd=cost,
        cost_per_cycle=ratio(cost, len(rows)),
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


def project_cost(evidence: CandidateEvidence, *, window: str, instruments: int) -> dict[str, Money]:
    """§10.6's cost projection: what this candidate would spend over `window`, per cadence.

    Multiplied by `cost_per_cycle`, never by `cost_per_scored`. In `basket` mode one provider call
    answers for every instrument (`total_cost` de-duplicates by `call_id`), so a basket of four
    costs what a basket of one costs and only the *decision* count moves — which is why
    `instruments` is taken and deliberately not used as a multiplier. It stays in the signature so
    a future per-asset cost model has one place to land rather than a second call site to find.
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
```

- [ ] **Step 4: Confirm `_correct` still matches `summarise`**

Open `decision_lab/scoring.py`'s `summarise` and check that its correct-verdict expression is still `d.verdict is Verdict.CORRECT`. If it has changed, change `_correct` to match it — the gate and the report must count the same thing, or the gate passes on a number the page it renders disagrees with.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_calibration.py -q`
Expected: PASS. `ratio` already returns `ZERO` on a zero denominator — an empty regime is a row of zeroes, not a refusal — which is what the two zero-cost tests expect.

- [ ] **Step 6: Lint and type-check**

Run: `.venv\Scripts\python.exe -m ruff format decision_lab; .venv\Scripts\python.exe -m ruff check decision_lab; .venv\Scripts\python.exe -m mypy decision_lab`

- [ ] **Step 7: Commit**

```bash
git add decision_lab/calibration.py decision_lab/tests/test_calibration.py
git commit -m "feat(decision_lab): the four gate conditions and the cost projection"
```

---

### Task 4: `render.py` — the calibration blocks and the `GATE SKIPPED` banner

**Files:**
- Modify: `decision_lab/render.py`
- Modify: `decision_lab/calibration.py` (add `per_day`)
- Test: `decision_lab/tests/test_render.py`, `decision_lab/tests/test_calibration.py`

**Interfaces:**
- Produces:
  - `render.GATE_SKIPPED: Final[str]`
  - `render.DayMetrics(candidate_id: str, day: date, pool: str, scored: int, correct: int, accuracy: Money)`
  - `render.CostProjection(candidate_id: str, cost_per_cycle: Money, cost_per_scored: Money, projected: dict[str, Money])`
  - `render.LabReport` gains `scenario: str = ""`, `calibration_days: tuple[date, ...] = ()`, `per_day: tuple[DayMetrics, ...] = ()`, `gate_passed: bool = False`, `gate_failures: tuple[str, ...] = ()`, `gate_skipped: bool = False`, `cost_projection: tuple[CostProjection, ...] = ()`
  - `calibration.per_day(candidate_id: str, scored: Sequence[ScoredDecision], pinned: CalibrationDays) -> tuple[DayMetrics, ...]`

- [ ] **Step 1: Write the failing tests**

Append to `decision_lab/tests/test_render.py`. If the file has no minimal-report helper, add one first — build a `LabReport` with only its required fields (`generated_at`, `corpus_id`, `dataset_directory`, `dataset_digest`, `reference_instrument`, `reference_panel_id`, `reference_config_digest`, `cadence_seconds`, `scoring`, `vol_window_bars`, `shock_percentile`, `start_equity`), copying the construction from the tests already in the file.

```python
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
```

Append to `decision_lab/tests/test_calibration.py`:

```python
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
```

Write a `_scored(day: date, *, correct: bool) -> ScoredDecision` helper in that file, using `Verdict.CORRECT` when `correct` and `Verdict.WRONG` otherwise — the only two members whose `is_scored` is `True`, and the same pair `summarise` and `_correct` split on. Build the rest of the `ScoredDecision` from its required fields (`cycle_id`, `as_of`, `instrument_key`, `regime`, `action`, `conviction`, `asked_for_an_order`, `holding`, `verdict`); read the model before writing the helper.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render.py decision_lab/tests/test_calibration.py -q`
Expected: FAIL — `render` has no attribute `DayMetrics`.

- [ ] **Step 3: Add the banner and the models to `render.py`**

Beside `PLUMBING_CHECK`:

```python
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
```

After `NotMeasured`:

```python
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
```

Add `date` to the `datetime` import and `Field` from `pydantic` if they are not already imported.

Add to `LabReport`, after `not_measured_candidates`:

```python
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
```

- [ ] **Step 4: Render the new blocks**

Add `GATE_SKIPPED` to the banner list in `report_markdown` when `report.gate_skipped`, and add these functions, wiring both into the section list after the per-regime block:

```python
def _calibration_block(report: LabReport) -> str:
    """§10.2's per-day table and its spread, and the gate verdict this page produced."""
    if not report.scenario:
        return ""
    parts = [
        f"## Calibration — {report.scenario}",
        "",
        "Days: " + ", ".join(day.isoformat() for day in report.calibration_days),
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
    return "\n".join([*parts, _gate_verdict(report), ""])


def _spread_note(rows: Sequence[DayMetrics]) -> str:
    """The spread across the days, per candidate. §10.2: three days is not a distribution, but it
    is enough to see when one day carried a result — and a candidate whose pooled accuracy comes
    entirely from one of the three is not one this page should present as steady."""
    lines = []
    for candidate_id in dict.fromkeys(row.candidate_id for row in rows):
        accuracies = [row.accuracy for row in rows if row.candidate_id == candidate_id]
        lines.append(
            f"- **{candidate_id}** — accuracy spread across its days: "
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
    listed = "\n".join(f"- {reason}" for reason in report.gate_failures)
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
```

- [ ] **Step 5: Add `per_day` to `calibration.py`**

Import `DayMetrics` from `decision_lab.render` at the top, then:

```python
def per_day(
    candidate_id: str, scored: Sequence[ScoredDecision], pinned: CalibrationDays
) -> tuple[DayMetrics, ...]:
    """§10.2's per-day rows, labelled with the pool each day was pinned into.

    A decision falling on a day this set never pinned is skipped rather than given a blank pool: a
    corpus can hold entries either side of the nine days, and a row labelled "" would read as a
    day whose regime nobody could determine.
    """
    by_day: dict[date, list[ScoredDecision]] = {}
    for row in scored:
        by_day.setdefault(row.as_of.date(), []).append(row)
    rows = []
    for day in sorted(by_day):
        pool = pinned.pool_of(day)
        if pool is None:
            continue
        here = [row for row in by_day[day] if row.verdict.is_scored]
        correct = _correct(here)
        rows.append(
            DayMetrics(
                candidate_id=candidate_id,
                day=day,
                pool=pool.value,
                scored=len(here),
                correct=correct,
                accuracy=ratio(correct, len(here)),
            )
        )
    return tuple(rows)
```

- [ ] **Step 6: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render.py decision_lab/tests/test_calibration.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/render.py decision_lab/calibration.py decision_lab/tests/
git commit -m "feat(decision_lab): calibration blocks and the gate-skipped banner"
```

---

### Task 5: `calibrate normal` and `calibrate shock` on the CLI

**Files:**
- Modify: `decision_lab/cli.py`
- Modify: `decision_lab/tests/conftest.py`
- Create: `decision_lab/tests/test_cli_calibrate.py`

**Interfaces:**
- Produces: `cli.calibrate_snapshot(args) -> int` (serves both `normal` and `shock`); `COMMANDS` gains `("calibrate", "normal")` and `("calibrate", "shock")`; conftest gains `calibrated_corpus -> tuple[str, Path]`.

**`--configs` is `required=True`, deliberately unlike `sweep`'s.** The gate is keyed on `matrix_digest`, so a defaulted matrix would let an operator calibrate one matrix and sweep another and never be told the gate they opened was not the gate they needed.

- [ ] **Step 1: Add the fixture**

In `decision_lab/tests/conftest.py`:

```python
@pytest.fixture
def calibrated_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path]]:
    """A corpus whose dataset also carries a pinned day set — what §10 requires and a sweep does
    not. Three shocks in each direction, because `DAYS_PER_POOL` is 3 and a thinner dataset makes
    `dataset days` refuse the pool rather than the test."""
    from decision_lab import cli
    from decision_lab import gate as gate_module

    monkeypatch.setattr(registry, "workspace_root", lambda: tmp_path / "workspace")
    monkeypatch.setattr(gate_module, "workspace_root", lambda: tmp_path / "workspace")
    corpus_id = built_corpus(tmp_path, monkeypatch, shock_up=(5, 12, 19), shock_down=(8, 15, 22))
    data = tmp_path / "history"
    assert cli.main(["dataset", "days", "--data", str(data)]) == cli.EXIT_OK
    yield corpus_id, data
```

`built_corpus` and `registry` are already imported at the top of that file. The two `monkeypatch.setattr` calls rebind each module's own reference to `workspace_root`, which is how `built_corpus` already redirects `corpus`'s.

- [ ] **Step 2: Write the failing tests**

Create `decision_lab/tests/test_cli_calibrate.py`:

```python
"""`calibrate normal` and `calibrate shock` through `cli.main`, as an operator runs them.

Offline on the stub matrix, so the whole file is free and deterministic — which also means every
gate condition passes by construction here, and the refusals are asserted in `test_calibration.py`
against handmade evidence instead.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import gate
from decision_lab import registry


def run(scenario: str, corpus_id: str, out: Path) -> int:
    return cli.main(
        [
            "calibrate",
            scenario,
            "--corpus",
            corpus_id,
            "--configs",
            str(cd.STUB_MATRIX),
            "--budget",
            "1",
            "--out",
            str(out),
        ]
    )


def test_calibrate_normal_writes_a_report_and_half_a_gate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, data = calibrated_corpus
    out = tmp_path / "normal.md"

    assert run("normal", corpus_id, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "## Calibration — normal" in text
    assert "Gate: PASSED" in text

    record = _record(corpus_id, data, tmp_path)
    assert record is not None
    assert record.normal is not None and record.normal.passed
    assert record.shock is None, "one command fills one half"


def test_both_halves_open_the_gate(tmp_path: Path, calibrated_corpus: tuple[str, Path]) -> None:
    corpus_id, data = calibrated_corpus

    assert run("normal", corpus_id, tmp_path / "n.md") == cli.EXIT_OK
    assert run("shock", corpus_id, tmp_path / "s.md") == cli.EXIT_OK

    record = _record(corpus_id, data, tmp_path)
    assert record is not None and record.satisfied


def test_the_shock_report_never_pools_the_two_directions(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§8.3: SHOCK_UP and SHOCK_DOWN ask opposite questions of a long-only system."""
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "s.md"

    assert run("shock", corpus_id, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "| SHOCK |" not in text


def test_a_calibration_files_a_registry_row_per_candidate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus

    assert run("normal", corpus_id, tmp_path / "n.md") == cli.EXIT_OK

    rows = [
        row
        for row in registry.read_all(workspace=tmp_path / "workspace")
        if row.scenario == "calibrate-normal"
    ]
    assert rows, "§11: every calibration run appends a row"
    assert all(row.dayset_digest for row in rows), "the day set is part of the identity"


def test_a_dataset_with_no_pinned_day_set_refuses(tmp_path: Path, built_corpus_id: str) -> None:
    """§15: a missing pinned day set refuses a calibration, naming `dataset days`."""
    assert run("normal", built_corpus_id, tmp_path / "n.md") == cli.EXIT_DATASET


def test_the_report_carries_the_cost_projection(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "n.md"

    assert run("normal", corpus_id, out) == cli.EXIT_OK

    assert "Projected spend" in out.read_text(encoding="utf-8")


def _record(corpus_id: str, data: Path, tmp_path: Path) -> gate.GateRecord | None:
    meta = cp.load(corpus_id, workspace=tmp_path / "workspace").meta
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=meta.reference_basket)
    key = gate.gate_key(
        dataset_digest=ds.require_verified(data).dataset_digest,
        matrix_digest=matrix.matrix_digest,
        dayset_digest=cday.require_pinned(data).dayset_digest,
    )
    return gate.read(key, workspace=tmp_path / "workspace")
```

- [ ] **Step 3: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_calibrate.py -q`
Expected: FAIL — `argument command: invalid choice: 'calibrate'`

- [ ] **Step 4: Add the parser**

In `parse_args`, after the `sweep_` block:

```python
    calibrate = commands.add_parser(
        "calibrate", help="the §10 scenarios: the short seat check, and the long profit run"
    )
    calibrate_actions = calibrate.add_subparsers(dest="action", required=True)
    for scenario, blurb in (
        ("normal", "every candidate over the three pinned NORMAL days"),
        ("shock", "the three SHOCK_UP and three SHOCK_DOWN days, never pooled"),
    ):
        snapshot = calibrate_actions.add_parser(scenario, help=blurb)
        snapshot.add_argument("--corpus", required=True, help="corpus id from `corpus build`")
        snapshot.add_argument("--configs", type=Path, required=True, help="the candidate matrix")
        snapshot.add_argument("--budget", type=_decimal_arg, default=Decimal(5))
        snapshot.add_argument("--data", type=Path, default=None)
        snapshot.add_argument("--regimes", type=Path, default=None)
        snapshot.add_argument("--scoring-timeframe", default="")
        snapshot.add_argument("--band-k", type=_decimal_arg, default=None)
        snapshot.add_argument("--horizon", type=int, default=None)
        snapshot.add_argument(
            "--window",
            default=DEFAULT_LONG_WINDOW,
            choices=sorted(WINDOW_DAYS),
            help="the long-run window the cost projection is for",
        )
        snapshot.add_argument("--out", type=Path, default=None)
        snapshot.add_argument("--verbose", action="store_true")
```

Add the imports: `from decision_lab import calibration as cal`, `from decision_lab import gate`, and `DEFAULT_LONG_WINDOW`, `WINDOW_DAYS` from `decision_lab.params`.

- [ ] **Step 5: Write the handler**

```python
async def calibrate_snapshot(args: argparse.Namespace) -> int:
    """§10.2 and §10.3 — the snapshot-scored scenarios, and the gate half each one fills in.

    The sweep, pointed at the pinned days instead of a stratified sample. Everything downstream of
    `sweep.run` — cache, budget, resume, the §7.7 substitute policy — is inherited rather than
    repeated, which is also what makes §10.1's "identical evidence" claim true by construction.
    """
    clock = SystemClock()
    scenario = args.action
    corpus = cp.load(args.corpus)
    data_dir = args.data or Path(corpus.meta.dataset_directory)
    audit = ds.require_verified(data_dir)
    dataset = ReplayDataset.load(data_dir, clock)
    # §15: a missing or stale pinned day set refuses a *calibration*, naming the command that
    # produces one. `require_pinned` raises `ConfigError`, which `main` maps to EXIT_DATASET.
    pinned = cday.require_pinned(data_dir)

    try:
        matrix = cd.load_matrix(args.configs, reference=corpus.meta.reference_basket)
        cd.require_reachable(matrix)
    except ConfigError as error:
        logger.error("calibration refused; nothing was spent", extra={"reason": str(error)})
        return EXIT_CANDIDATE

    days = cal.days_for(pinned, scenario)
    sample = cal.sample_for(corpus, pinned, days)
    result = await sw.run(corpus, matrix, sample=sample, clock=clock, budget_usd=args.budget)
    sw.write_meta(result)

    params = sc.ScoringParams(
        timeframe=args.scoring_timeframe or dataset.timeframes[0],
        **({"band_k": args.band_k} if args.band_k is not None else {}),
        **({"horizon_bars": args.horizon} if args.horizon is not None else {}),
    )
    index = await sc.build_price_index(dataset, audit, params)
    regime_index = (await rg.index_dataset(dataset, params.timeframe)).with_windows(
        rg.load_windows(args.regimes or rg.DEFAULT_REGIMES_TOML)
    )

    evidence: list[gate.CandidateEvidence] = []
    by_candidate: dict[str, tuple[sc.ScoredDecision, ...]] = {}
    day_rows: list[rd.DayMetrics] = []
    seat_blocks: list[rd.CandidateSeats] = []
    not_measured: list[rd.NotMeasured] = []
    for candidate in matrix.candidates:
        rows = sw.read_rows(
            sw.rows_path(corpus.meta.corpus_id, matrix.matrix_digest, candidate.candidate_id)
        )
        records = sw.records_from_rows(corpus, rows)
        scored = sc.score_records(records, index=index, regimes=regime_index, params=params)
        evidence.append(cal.candidate_evidence(candidate, rows, records, scored))
        if not scored:
            not_measured.append(
                rd.NotMeasured(
                    candidate_id=candidate.candidate_id,
                    reason=_not_measured_reason(rows, records),
                )
            )
            continue
        by_candidate[candidate.candidate_id] = scored
        day_rows += cal.per_day(candidate.candidate_id, scored, pinned)
        seat_blocks.append(
            rd.CandidateSeats(
                candidate_id=candidate.candidate_id,
                seats=st.score_seats(records, scored, panel=candidate.panel),
            )
        )

    failures = cal.failures_for(evidence, report_written=True)
    key = gate.gate_key(
        dataset_digest=audit.dataset_digest,
        matrix_digest=matrix.matrix_digest,
        dayset_digest=pinned.dayset_digest,
    )
    out = args.out or reports_dir() / f"decision-lab-calibration-{scenario}-{key}.md"
    pooled = tuple(row for rows_ in by_candidate.values() for row in rows_)
    built = rd.LabReport(
        generated_at=clock.now(),
        corpus_id=corpus.meta.corpus_id,
        dataset_directory=str(data_dir),
        dataset_digest=corpus.meta.dataset_digest,
        dayset_digest=pinned.dayset_digest,
        reference_instrument=pinned.reference_instrument,
        reference_panel_id=corpus.meta.reference_panel_id,
        reference_config_digest=corpus.meta.reference_config_digest,
        cadence_seconds=corpus.meta.cadence_seconds,
        scoring=params,
        vol_window_bars=regime_index.window_bars,
        shock_percentile=regime_index.shock_percentile,
        named_windows=tuple(w.name for w in regime_index.windows),
        start_equity=corpus.meta.start_equity,
        news_blind=corpus.meta.news_blind,
        panel_models=tuple(
            dict.fromkeys(
                f"{s.provider_id}:{s.model}"
                for candidate in matrix.candidates
                for s in candidate.panel.seats
            )
        ),
        cycles=len(sample.cycle_ids),
        regimes=sc.by_regime(pooled),
        seats=(),
        plumbing_check=not matrix.is_evaluation,
        matrix_digest=matrix.matrix_digest,
        matrix_source=str(matrix.source),
        on_fallback=matrix.on_fallback.value,
        sweep_status=result.status.value,
        halted_on=result.halted_on,
        sample=sample,
        budget_usd=result.budget_usd,
        spent_usd=result.spent_usd,
        contaminated=result.contaminated,
        ranking=cmp.ranking(by_candidate),
        agreement=cmp.agreement(by_candidate),
        candidate_seats=tuple(seat_blocks),
        not_measured_candidates=tuple(not_measured),
        scenario=scenario,
        calibration_days=days,
        per_day=tuple(day_rows),
        gate_passed=not failures,
        gate_failures=failures,
        cost_projection=tuple(
            rd.CostProjection(
                candidate_id=found.candidate_id,
                cost_per_cycle=found.cost_per_cycle,
                cost_per_scored=found.cost_per_scored,
                projected=cal.project_cost(found, window=args.window),
            )
            for found in evidence
        ),
    )
    rd.write_report(built, out)

    record = gate.read(key) or gate.GateRecord(
        gate_key=key,
        dataset_digest=audit.dataset_digest,
        matrix_digest=matrix.matrix_digest,
        dayset_digest=pinned.dayset_digest,
    )
    gate.write(
        gate.record_scenario(
            record.model_copy(update={"evaluation": matrix.is_evaluation}),
            gate.ScenarioVerdict(
                scenario=scenario,
                ran_at=clock.now(),
                cadence_seconds=corpus.meta.cadence_seconds,
                corpus_id=corpus.meta.corpus_id,
                days=days,
                candidates=tuple(evidence),
                report_path=str(out),
                failures=failures,
            ),
        )
    )

    for found in evidence:
        registry.record(
            _registry_row(corpus, matrix, clock, seed=sample.seed).model_copy(
                update={
                    "scenario": f"calibrate-{scenario}",
                    "candidate_id": found.candidate_id,
                    "status": result.status.value,
                    "scored": found.scored,
                    "accuracy": found.accuracy,
                    "cost_usd": found.cost_usd,
                }
            )
        )

    logger.info(
        "calibration complete",
        extra={
            "scenario": scenario,
            "gate": "passed" if not failures else "failed",
            "failures": list(failures),
            "days": [day.isoformat() for day in days],
            "spent": str(result.spent_usd),
            "report": str(out),
        },
    )
    if failures:
        return EXIT_GATE
    if result.status in (sw.SweepStatus.HALTED_BUDGET, sw.SweepStatus.HALTED_FALLBACK):
        return EXIT_BUDGET
    return EXIT_OK
```

Register both actions in `COMMANDS`:

```python
    ("calibrate", "normal"): calibrate_snapshot,
    ("calibrate", "shock"): calibrate_snapshot,
```

- [ ] **Step 6: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_calibrate.py -q`
Expected: PASS, 6 tests.

- [ ] **Step 7: Run the tool's whole gate**

Run: `.\decision_lab\check.ps1`
Expected: all four steps green.

- [ ] **Step 8: Commit**

```bash
git add decision_lab/cli.py decision_lab/tests/
git commit -m "feat(decision_lab): calibrate normal and calibrate shock"
```

---

### Task 6: the gate refuses an uncalibrated sweep

**Files:**
- Modify: `decision_lab/cli.py`
- Modify: `decision_lab/tests/test_cli_sweep.py`
- Modify: `decision_lab/tests/test_slice_c_end_to_end.py`

**Interfaces:**
- Produces: `sweep` accepts `--skip-gate`; `sweep_command` refuses with `EXIT_GATE` and records a `gate_unsatisfied` registry row.

- [ ] **Step 1: Write the failing tests**

Add to `decision_lab/tests/test_cli_sweep.py` (import `registry` if it is not already imported):

```python
def test_an_uncalibrated_sweep_refuses(tmp_path: Path, calibrated_corpus: tuple[str, Path]) -> None:
    """§10.6: the sweep refuses to start unless scenarios 1 and 2 have passed for this exact
    dataset, matrix and day set."""
    corpus_id, _ = calibrated_corpus
    assert _sweep(corpus_id) == cli.EXIT_GATE


def test_a_refused_sweep_files_a_row_saying_why(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§11: a run that never produced a number is still a fact about the experiment."""
    corpus_id, _ = calibrated_corpus
    _sweep(corpus_id)

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert any(row.status == registry.STATUS_GATE_UNSATISFIED for row in rows)


def test_skip_gate_proceeds_and_stamps_the_row(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus

    assert _sweep(corpus_id, skip_gate=True) == cli.EXIT_OK

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert any(row.gate_skipped for row in rows)


def test_a_calibrated_sweep_runs(tmp_path: Path, calibrated_corpus: tuple[str, Path]) -> None:
    corpus_id, _ = calibrated_corpus
    for scenario in ("normal", "shock"):
        assert (
            cli.main(
                [
                    "calibrate",
                    scenario,
                    "--corpus",
                    corpus_id,
                    "--configs",
                    str(cd.STUB_MATRIX),
                    "--budget",
                    "1",
                    "--out",
                    str(tmp_path / f"{scenario}.md"),
                ]
            )
            == cli.EXIT_OK
        )

    assert _sweep(corpus_id) == cli.EXIT_OK


def _sweep(corpus_id: str, *, skip_gate: bool = False) -> int:
    argv = ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    return cli.main([*argv, "--skip-gate"] if skip_gate else argv)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_sweep.py -q`
Expected: the four new tests FAIL (the sweep still returns 0 and knows no `--skip-gate`); every pre-existing test in the file still passes.

- [ ] **Step 3: Add `--skip-gate` and the consult**

Add to the `sweep_` parser:

```python
    sweep_.add_argument(
        "--skip-gate",
        action="store_true",
        help="run without the §10.6 calibration gate; stamped on the report and the registry row",
    )
```

In `sweep_command`, replace the existing `row = _registry_row(...)` line with this block, placed **after** `load_matrix` and **before** `require_reachable`:

```python
    pinned = _pinned_calibration(data_dir)
    row = _registry_row(corpus, matrix, clock, seed=args.seed).model_copy(
        update={"gate_skipped": args.skip_gate}
    )
    if not args.skip_gate:
        # §10.6, and it comes before the reachability check on purpose: an operator who has not
        # calibrated must fix that first, and `calibrate` performs the same reachability refusal
        # itself. Reporting a missing key here would send them to fix the second problem.
        if pinned is None:
            logger.error(
                "sweep refused; the §10.6 gate needs a pinned day set — run "
                "`python -m decision_lab dataset days` first",
                extra={"data": str(data_dir)},
            )
            return EXIT_DATASET
        try:
            gate.require_satisfied(
                dataset_digest=corpus.meta.dataset_digest,
                matrix_digest=matrix.matrix_digest,
                dayset_digest=pinned.dayset_digest,
            )
        except ConfigError as error:
            registry.record(
                row.model_copy(
                    update={
                        "status": registry.STATUS_GATE_UNSATISFIED,
                        "dayset_digest": pinned.dayset_digest,
                        "note": str(error),
                    }
                )
            )
            logger.error("sweep refused; nothing was spent", extra={"reason": str(error)})
            return EXIT_GATE
```

The later `pinned = _pinned_calibration(data_dir)` line inside the sampling block must be **deleted** — `pinned` is now bound above and the function must not read the day set twice.

- [ ] **Step 4: Update the slice C end-to-end test**

Slice C's exit criterion is the sweep, not §10.6's gate. Add `--skip-gate` to its `cli_sweep` helper with the reason:

```python
def cli_sweep(corpus_id: str) -> int:
    from decision_lab import cli

    # Slice C's exit criterion is the sweep itself, not §10.6's gate — which slice D asserts in
    # `test_cli_sweep.py` and `test_slice_d_end_to_end.py`. Skipping it keeps this test about
    # what it was written to prove.
    return cli.main(
        [
            "sweep",
            "--corpus",
            corpus_id,
            "--configs",
            str(cd.STUB_MATRIX),
            "--budget",
            "1",
            "--skip-gate",
        ]
    )
```

- [ ] **Step 5: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS. Any other test that assumed `sweep` needs no gate takes `--skip-gate` with the same comment.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/cli.py decision_lab/tests/
git commit -m "feat(decision_lab): the gate refuses an uncalibrated sweep"
```

---

### Task 7: `longrun.py` — the profit arithmetic

**Files:**
- Create: `decision_lab/longrun.py`
- Create: `decision_lab/tests/test_longrun.py`

**Interfaces:**
- Produces:
  - `longrun.ProfitEvidence` (Protocol over `realized_pnl`, `cost_usd`, `risk_events`)
  - `longrun.Profit(unvaluable, freeze_reason, equity, start_equity, total, realized, unrealized, cost_usd, net)`
  - `longrun.VetoRow(rule: str, action_taken: str, count: int)`
  - `longrun.profit_of(aggregate: PortfolioAggregate, evidence: ProfitEvidence, *, start_equity: Decimal) -> Profit`
  - `longrun.veto_breakdown(evidence: ProfitEvidence) -> tuple[VetoRow, ...]`
  - `longrun.window_bounds(dataset_end: datetime, window: str) -> tuple[datetime, datetime]`

This task is the pure arithmetic only. The harness run lands in Task 8.

**Before writing the test helpers, check** `PortfolioAggregate`'s required fields in `tradebot/risk/aggregate.py` and supply zeroes for any the helpers below omit.

- [ ] **Step 1: Write the failing tests**

Create `decision_lab/tests/test_longrun.py`:

```python
"""§10.4's profit arithmetic, in isolation.

"Total profit" is the number a reader will trust without checking, and it is the one this design
says is easiest to get wrong: `Evidence.realized_pnl` sums *closed* round trips only, so a run
ending with an open position would report a profit that ignores it — the same class of error as a
drawdown gate measuring cost basis (ADR 0027).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from decision_lab import longrun
from tradebot.core.errors import ConfigError
from tradebot.risk.aggregate import PortfolioAggregate

AT = datetime(2026, 1, 1, tzinfo=UTC)


class FakeEvidence:
    """Only the three properties `profit_of` and `veto_breakdown` read."""

    def __init__(
        self,
        realized: str = "0",
        cost: str = "0",
        risk_events: Mapping[str, int] | None = None,
    ) -> None:
        self.realized_pnl = Decimal(realized)
        self.cost_usd = Decimal(cost)
        self.risk_events = risk_events or {}


def valued(equity: str) -> PortfolioAggregate:
    return PortfolioAggregate(equity=Decimal(equity), gross_exposure=Decimal(0), as_of=AT)


def frozen(reason: str) -> PortfolioAggregate:
    return PortfolioAggregate(
        equity=Decimal(0), gross_exposure=Decimal(0), as_of=AT, frozen_reason=reason
    )


def test_total_profit_is_mark_to_market_not_realized_pnl() -> None:
    """A run ending with an open position: realized is 50, the position is worth 30 more."""
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="50"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(80)
    assert profit.realized == Decimal(50)
    assert profit.unrealized == Decimal(30)


def test_both_halves_are_always_present_never_just_the_total() -> None:
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="50"), start_equity=Decimal(1000)
    )
    assert profit.realized + profit.unrealized == profit.total


def test_net_profit_subtracts_what_the_deliberation_cost() -> None:
    """A panel that made $80 on $120 of tokens lost money. Nothing else in this design says so."""
    profit = longrun.profit_of(
        valued("1080"), FakeEvidence(realized="80", cost="120"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(80)
    assert profit.net == Decimal(-40)


def test_a_loss_is_reported_as_a_loss() -> None:
    profit = longrun.profit_of(
        valued("900"), FakeEvidence(realized="-100"), start_equity=Decimal(1000)
    )
    assert profit.total == Decimal(-100)
    assert profit.unrealized == Decimal(0)


def test_a_frozen_aggregate_reports_unvaluable_and_no_figure() -> None:
    """§10.4: freezing is ignorance, and a number produced in ignorance is worse than its
    absence — including a partial one."""
    profit = longrun.profit_of(
        frozen("no mark for binance:BTC/USDT"),
        FakeEvidence(realized="50", cost="10"),
        start_equity=Decimal(1000),
    )
    assert profit.unvaluable
    assert profit.freeze_reason == "no mark for binance:BTC/USDT"
    assert profit.total == Decimal(0)
    assert profit.realized == Decimal(0)
    assert profit.net == Decimal(0)


def test_the_veto_breakdown_splits_the_rule_from_the_action() -> None:
    """`Evidence.risk_events` is already keyed `rule/action_taken` — the breakdown reads it
    rather than re-folding the log."""
    rows = longrun.veto_breakdown(
        FakeEvidence(risk_events={"cooldown/veto": 4, "price_collar/veto": 1, "drawdown/warn": 2})
    )
    assert rows[0] == longrun.VetoRow(rule="cooldown", action_taken="veto", count=4)
    assert [row.count for row in rows] == [4, 2, 1], "most frequent first"


def test_a_malformed_risk_event_key_is_kept_not_dropped() -> None:
    """A row nobody can parse is still a refusal that happened; dropping it would understate the
    breakdown."""
    assert longrun.veto_breakdown(FakeEvidence(risk_events={"weird": 3})) == (
        longrun.VetoRow(rule="weird", action_taken="", count=3),
    )


def test_the_window_ends_at_the_dataset_and_runs_backwards() -> None:
    start, end = longrun.window_bounds(AT, "6m")
    assert end == AT
    assert end - start == timedelta(days=182)


def test_an_unknown_window_refuses_naming_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="6m"):
        longrun.window_bounds(AT, "7m")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_longrun.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.longrun'`

- [ ] **Step 3: Write the arithmetic half of `decision_lab/longrun.py`**

```python
"""Scenario 3 — six months of long exposure, with its own ledger (spec §10.4).

A different *kind* of instrument from scenarios 1 and 2, and §10.1 keeps them apart for a reason:
positions compound, so a candidate's cycle 400 happens in a market its own cycle 12 helped create.
Its numbers are not on the same scale as a snapshot-scored accuracy and must never be ranked
against one — §10.5 prints both rankings and names every position where they disagree.

`BacktestHarness` runs unchanged, in a workspace database, from a declared starting balance. No
`tradebot` seam is needed: `build_sim(start_equity=…)` already threads to `SimBroker.balances`.

Failure semantics: a frozen aggregate at the window's end reports `UNVALUABLE` and no figure at
all — freezing is ignorance, and a number produced in ignorance is worse than its absence. An
unknown `--window` refuses, naming the ones that exist. Nothing here writes to a bot database or
constructs a venue broker; its orders reach `SimBroker` only, in a workspace database.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from decision_lab.params import WINDOW_DAYS
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money
from tradebot.risk.aggregate import PortfolioAggregate


class ProfitEvidence(Protocol):
    """What `profit_of` and `veto_breakdown` need of `validation.Evidence`.

    A Protocol rather than the class itself, so the arithmetic can be driven by a stand-in without
    building a whole event log — and so this module states exactly which three of `Evidence`'s
    properties it depends on.
    """

    @property
    def realized_pnl(self) -> Decimal: ...

    @property
    def cost_usd(self) -> Decimal: ...

    @property
    def risk_events(self) -> Mapping[str, int]: ...


class Profit(DomainModel):
    """§10.4's headline, with both halves and never just the total."""

    #: A frozen aggregate. Every figure below is zero and none of them means anything.
    unvaluable: bool = False
    freeze_reason: str = ""
    equity: Money = ZERO
    start_equity: Money = ZERO
    #: `equity − start_equity`. Mark-to-market, so an open position at the window's end counts.
    total: Money = ZERO
    realized: Money = ZERO
    unrealized: Money = ZERO
    cost_usd: Money = ZERO
    #: `total − cost_usd`. The ask is efficient, not merely profitable.
    net: Money = ZERO


class VetoRow(DomainModel):
    """One rule's refusals over the run.

    §10.4: a panel that is right often but only in ways the price collar, the cooldown or the
    daily cap refuse is not an improvement — and a corpus sweep can never discover that.
    """

    rule: str
    action_taken: str = ""
    count: int = 0


def profit_of(
    aggregate: PortfolioAggregate, evidence: ProfitEvidence, *, start_equity: Decimal
) -> Profit:
    """What the run made, decomposed. `UNVALUABLE` when the portfolio cannot be valued.

    The total is **not** `Evidence.realized_pnl`: that property sums closed round trips only, so a
    run ending with an open position would report a profit that ignores it (§10.4). Realized is
    reported beside the mark-to-market total and unrealized is the difference — both halves always
    printed, because "made $80" reads very differently when $80 of it is still at risk.
    """
    if aggregate.frozen:
        # Every figure stays zero. Quoting realized alone would be the cost-basis fallback ADR
        # 0027 forbids, arriving as a partial answer instead of a wrong one.
        return Profit(unvaluable=True, freeze_reason=aggregate.frozen_reason)
    total = aggregate.equity - start_equity
    realized = evidence.realized_pnl
    return Profit(
        equity=aggregate.equity,
        start_equity=start_equity,
        total=total,
        realized=realized,
        unrealized=total - realized,
        cost_usd=evidence.cost_usd,
        net=total - evidence.cost_usd,
    )


def veto_breakdown(evidence: ProfitEvidence) -> tuple[VetoRow, ...]:
    """Which rule refused what, most frequent first.

    `Evidence.risk_events` is already keyed `rule/action_taken`, so this splits rather than
    re-folds the log. A key with no separator is kept under an empty action: a refusal nobody can
    parse is still a refusal that happened, and dropping it would understate the breakdown.
    """
    rows = []
    for key, count in evidence.risk_events.items():
        rule, _, action = key.partition("/")
        rows.append(VetoRow(rule=rule, action_taken=action, count=count))
    return tuple(sorted(rows, key=lambda row: (-row.count, row.rule, row.action_taken)))


def window_bounds(dataset_end: datetime, window: str) -> tuple[datetime, datetime]:
    """The window to replay, ending at the dataset's last covered instant.

    Backwards from the end rather than forwards from the start: the most recent history is the
    most relevant, and it is also the half most likely to be complete after a §4.3 repair.
    """
    if window not in WINDOW_DAYS:
        raise ConfigError(
            f"{window!r} is not a window this tool measures; it has "
            f"{', '.join(sorted(WINDOW_DAYS))}"
        )
    return dataset_end - timedelta(days=WINDOW_DAYS[window]), dataset_end
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_longrun.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/longrun.py decision_lab/tests/test_longrun.py
git commit -m "feat(decision_lab): the long-run profit arithmetic"
```

---

### Task 8: `calibrate long` — the six-month run and its report

**Files:**
- Modify: `decision_lab/longrun.py`, `decision_lab/render.py`, `decision_lab/cli.py`
- Modify: `decision_lab/tests/test_cli_calibrate.py`

**Interfaces:**
- Produces:
  - `longrun.LONG_DB: Final = "long.db"`, `longrun.LONG_META: Final = "long.json"`
  - `longrun.LongRunMeta(...)` as written below
  - `longrun.long_dir(run_id: str, *, workspace: Path | None = None) -> Path`
  - `longrun.run(*, data_dir: Path, candidate: Candidate, cadence_seconds: int, window: str, start_equity: Decimal, wall_clock: Clock, workspace: Path | None = None) -> LongRunMeta`
  - `render.LongRunReport`, `render.long_run_markdown(report) -> str`, `render.write_long_run(report, path) -> Path`
  - `cli.calibrate_long(args) -> int`; `COMMANDS` gains `("calibrate", "long")`

**Before writing `run`, check** `ReplayDataset.window`'s return shape against how `corpus.build` unpacks it.

- [ ] **Step 1: Write the failing tests**

Add to `decision_lab/tests/test_cli_calibrate.py`:

```python
def _long(data: Path, out: Path, *, skip_gate: bool = False) -> int:
    argv = [
        "calibrate",
        "long",
        "--data",
        str(data),
        "--configs",
        str(cd.STUB_MATRIX),
        "--candidate",
        _first_candidate(data),
        "--start-equity",
        "1000",
        "--every",
        "24h",
        "--window",
        "1m",
        "--out",
        str(out),
    ]
    return cli.main([*argv, "--skip-gate"] if skip_gate else argv)


def _first_candidate(data: Path) -> str:
    """The stub matrix's first candidate id, whatever it is called."""
    from tradebot.app import dataset_basket, select_panel
    from tradebot.core.clock import SystemClock
    from tradebot.marketdata.recorder import ReplayDataset

    dataset = ReplayDataset.load(data, SystemClock())
    reference = dataset_basket(dataset, select_panel("sim"), basket_id="reference")
    return cd.load_matrix(cd.STUB_MATRIX, reference=reference).candidates[0].candidate_id


def test_calibrate_long_refuses_without_the_gate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§10.6: the sweep and the long run both refuse to start unless 1 and 2 have passed."""
    _, data = calibrated_corpus
    assert _long(data, tmp_path / "long.md") == cli.EXIT_GATE


def test_calibrate_long_runs_and_reports_both_halves_of_the_profit(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, data = calibrated_corpus
    for scenario in ("normal", "shock"):
        assert run(scenario, corpus_id, tmp_path / f"{scenario}.md") == cli.EXIT_OK

    out = tmp_path / "long.md"
    assert _long(data, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8").lower()
    assert "realized" in text and "unrealized" in text
    assert "net profit" in text
    assert "veto" in text


def test_a_skipped_gate_stamps_the_long_report(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    from decision_lab import render as rd

    _, data = calibrated_corpus
    out = tmp_path / "long.md"

    assert _long(data, out, skip_gate=True) == cli.EXIT_OK

    assert rd.GATE_SKIPPED in out.read_text(encoding="utf-8")


def test_a_long_run_is_reused_at_its_identity(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§5.5's rule one level over: identical parameters are one experiment, and a second
    invocation returns the first pass rather than appending a second into its log."""
    from decision_lab import longrun

    _, data = calibrated_corpus
    assert _long(data, tmp_path / "a.md", skip_gate=True) == cli.EXIT_OK
    directories = sorted((tmp_path / "workspace").glob("long-*"))
    assert len(directories) == 1
    first = (directories[0] / longrun.LONG_META).read_text(encoding="utf-8")

    assert _long(data, tmp_path / "b.md", skip_gate=True) == cli.EXIT_OK

    assert sorted((tmp_path / "workspace").glob("long-*")) == directories
    assert (directories[0] / longrun.LONG_META).read_text(encoding="utf-8") == first


def test_a_long_run_files_a_registry_row_carrying_its_window_and_equity(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    _, data = calibrated_corpus
    assert _long(data, tmp_path / "long.md", skip_gate=True) == cli.EXIT_OK

    rows = [
        row
        for row in registry.read_all(workspace=tmp_path / "workspace")
        if row.scenario == "calibrate-long"
    ]
    assert len(rows) == 1
    assert rows[0].window == "1m"
    assert rows[0].start_equity == 1000
    assert rows[0].gate_skipped
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_calibrate.py -q`
Expected: the six new tests FAIL — `argument action: invalid choice: 'long'`

- [ ] **Step 3: Add the harness half of `longrun.py`**

```python
LONG_DB: Final = "long.db"
LONG_META: Final = "long.json"

logger = get_logger("decision_lab.longrun")


class LongRunMeta(DomainModel):
    """One long run: its identity, its window, and what it produced."""

    run_id: str
    #: Derived through `corpus.corpus_identity` so a long run has "its own corpus_id" exactly as
    #: §5.5 defines one — provenance for the §11 row, never a shared directory. The two have
    #: different windows by construction, and putting them in one place would turn §5.4's
    #: window-mismatch refusal into a refusal of an unrelated command.
    corpus_id: str
    built_at: UtcDatetime
    dataset_directory: str
    dataset_digest: str
    candidate_id: str
    panel_digest: str
    cadence_seconds: int
    window: str
    start_equity: Money
    requested_start: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    warmup_seconds: int = 0
    planned_cycles: int = 0
    ran_cycles: int = 0
    profit: Profit = Profit()
    vetoes: tuple[VetoRow, ...] = ()
    incidents: int = 0
    decisions: int = 0
    fills: int = 0


def long_dir(run_id: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / f"long-{run_id}"


async def run(
    *,
    data_dir: Path,
    candidate: Candidate,
    cadence_seconds: int,
    window: str,
    start_equity: Decimal,
    wall_clock: Clock,
    workspace: Path | None = None,
) -> LongRunMeta:
    """One candidate's own path through `BacktestHarness`, from a declared starting balance.

    Mirrors `corpus.build`'s shape — identity, reuse-if-built, refuse-if-interrupted, run — for
    the same reasons: a second pass appended into one log doubles every entry, and the database a
    failed pass left behind is the only record of why it failed.
    """
    audit = require_verified(data_dir)
    probe = ReplayDataset.load(data_dir, ManualClock(audit.audited_at))
    _, dataset_end = probe.window(None, None)
    start, end = window_bounds(dataset_end, window)

    clock = ManualClock(start)
    dataset = ReplayDataset.load(data_dir, clock)
    basket = candidate.basket.model_copy(
        update={
            "basket_id": "longrun",
            "instruments": dataset.instruments,
            "timeframes": dataset.timeframes,
            "schedule": Schedule(every_seconds=cadence_seconds),
        }
    )
    corpus_id = corpus_identity(
        dataset_digest=audit.dataset_digest,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest="",
    )
    identity = registry.run_id(
        scenario="calibrate-long",
        dataset_digest=audit.dataset_digest,
        corpus_id=corpus_id,
        matrix_digest="",
        dayset_digest="",
        candidate_id=candidate.candidate_id,
        cadence=str(cadence_seconds),
        start_equity=str(start_equity),
        window=window,
        sample_seed="0",
    )
    directory = long_dir(identity, workspace=workspace)
    meta_path = directory / LONG_META
    if meta_path.is_file():
        # §5.4's rule one level over: identical parameters are one experiment. Re-running returns
        # the pass that already happened rather than appending a second into its log.
        return LongRunMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / LONG_DB
    if database.is_file():
        raise ConfigError(
            f"{database} already exists but its run has no {LONG_META}, so a previous pass was "
            "interrupted. Its event log is the record of why that pass failed — read it before "
            "you discard it. Remove the directory once you are done with it to run again"
        )

    application = await build_sim(
        clock=clock,
        db_path=database,
        baskets=(basket,),
        start_equity=start_equity,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        news_sources=(),
    )
    try:
        report = await BacktestHarness(
            application, clock, start=start, end=end, data_source=str(data_dir)
        ).run()
        # Taken *before* shutdown, and through `Application.valuation` rather than a price map
        # built here: six call sites each building their own out of `avg_entry` is how the
        # drawdown kill switch came to report 0% on a portfolio that had halved (ADR 0027).
        valuation = application.valuation()
    finally:
        await application.shutdown()

    evidence = report.evidence
    meta = LongRunMeta(
        run_id=identity,
        corpus_id=corpus_id,
        built_at=wall_clock.now(),
        dataset_directory=str(data_dir),
        dataset_digest=audit.dataset_digest,
        candidate_id=candidate.candidate_id,
        panel_digest=candidate.panel_digest,
        cadence_seconds=cadence_seconds,
        window=window,
        start_equity=start_equity,
        requested_start=report.requested_start,
        window_start=report.window_start,
        window_end=report.window_end,
        warmup_seconds=int(report.warmup // timedelta(seconds=1)),
        planned_cycles=report.planned_cycles,
        ran_cycles=report.ran_cycles,
        profit=profit_of(valuation, evidence, start_equity=start_equity),
        vetoes=veto_breakdown(evidence),
        incidents=len(evidence.incidents),
        decisions=sum(evidence.actions.values()),
        fills=evidence.fills,
    )
    meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "long run complete",
        extra={
            "run_id": identity,
            "cycles": report.ran_cycles,
            "net": str(meta.profit.net),
            "unvaluable": meta.profit.unvaluable,
        },
    )
    return meta
```

Add the imports this needs: `from pathlib import Path`, `from typing import Final`, `from decision_lab import registry`, `from decision_lab.candidates import Candidate`, `from decision_lab.corpus import config_digest, corpus_identity`, `from decision_lab.dataset import require_verified`, `from decision_lab.params import workspace_root`, `from tradebot.app import build_sim, dataset_catalogue`, `from tradebot.core.clock import Clock, ManualClock`, `from tradebot.core.config import Schedule`, `from tradebot.core.logging import get_logger`, `from tradebot.core.schema import UtcDatetime`, `from tradebot.marketdata.recorder import ReplayDataset`, `from tradebot.validation.backtest import BacktestHarness`.

- [ ] **Step 4: Add `LongRunReport` to `render.py`**

```python
class LongRunReport(DomainModel):
    """§10.4's page. A different kind of number from a calibration's, never on one scale."""

    generated_at: UtcDatetime
    run_id: str
    corpus_id: str
    dataset_directory: str
    dataset_digest: str
    candidate_id: str
    panel_digest: str
    panel_models: tuple[str, ...] = ()
    cadence_seconds: int
    window: str
    requested_start: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    warmup_seconds: int = 0
    planned_cycles: int = 0
    ran_cycles: int = 0
    news_blind: bool = True
    plumbing_check: bool = False
    gate_skipped: bool = False
    #: `longrun.Profit`, carried as its own fields so `render` need not import `longrun`.
    unvaluable: bool = False
    freeze_reason: str = ""
    equity: Money = ZERO
    start_equity: Money = ZERO
    total_profit: Money = ZERO
    realized: Money = ZERO
    unrealized: Money = ZERO
    cost_usd: Money = ZERO
    net_profit: Money = ZERO
    #: rule, action, count — §10.4's veto breakdown.
    vetoes: tuple[tuple[str, str, int], ...] = ()
    incidents: int = 0
    decisions: int = 0
    fills: int = 0
    #: §10.5. This candidate's snapshot-scored accuracy from the gate record, so the two rankings
    #: sit side by side. Empty under `--skip-gate`, which the page then says rather than leaving
    #: a blank column.
    snapshot_accuracy: Money | None = None
```

Then `long_run_markdown(report: LongRunReport) -> str`, using the same banner order as `report_markdown` (`BANNER`, `DISCLAIMER`, `NEWS_BLIND` when `news_blind`, `PLUMBING_CHECK` when `plumbing_check`, `GATE_SKIPPED` when `gate_skipped`), then an identity block, then:

- **Profit** — when `report.unvaluable`, render exactly `**UNVALUABLE** — <freeze_reason>` and no table at all; otherwise a two-column table with rows `equity`, `start equity`, `total profit`, `realized`, `unrealized`, `deliberation cost`, `net profit`.
- **Vetoes** — `_table(("rule", "action", "count"), …)` over `report.vetoes`, with a line saying no rule refused anything when the tuple is empty.
- **Rankings (§10.5)** — when `snapshot_accuracy` is not `None`, print it beside `net_profit` and state that profit over one path is a weak comparison (§10.5's third row: one lucky early fill compounds for six months); when it is `None`, say the gate was skipped so there is no snapshot ranking to set beside it.

And `write_long_run(report: LongRunReport, path: Path) -> Path`, mirroring `write_report`.

- [ ] **Step 5: Add the CLI handler**

Parser, inside the `calibrate` block:

```python
    long_ = calibrate_actions.add_parser("long", help="six months of long exposure, own ledger")
    long_.add_argument("--data", type=Path, required=True)
    long_.add_argument("--configs", type=Path, required=True)
    long_.add_argument("--candidate", required=True, help="which candidate to run")
    long_.add_argument("--start-equity", type=_decimal_arg, default=Decimal(1000))
    long_.add_argument("--every", default="4h", choices=sorted(CADENCE_SECONDS))
    long_.add_argument("--window", default=DEFAULT_LONG_WINDOW, choices=sorted(WINDOW_DAYS))
    long_.add_argument("--skip-gate", action="store_true")
    long_.add_argument("--out", type=Path, default=None)
    long_.add_argument("--verbose", action="store_true")
```

Handler `calibrate_long`, in this order:

1. `clock = SystemClock()`; `audit = ds.require_verified(args.data)`; `dataset = ReplayDataset.load(args.data, clock)`.
2. Build the reference basket the way `corpus.build` does — `dataset_basket(dataset, select_panel("sim"), basket_id="reference", every_seconds=CADENCE_SECONDS[args.every])` — then `matrix = cd.load_matrix(args.configs, reference=reference)` and `cd.require_reachable(matrix)`, both inside one `try/except ConfigError` returning `EXIT_CANDIDATE`.
3. Find `args.candidate` among `matrix.candidates`. Absent → log the ids that do exist and return `EXIT_MISUSE`.
4. Unless `args.skip_gate`: `pinned = cday.require_pinned(args.data)`, then `record = gate.require_satisfied(dataset_digest=audit.dataset_digest, matrix_digest=matrix.matrix_digest, dayset_digest=pinned.dayset_digest)` inside `try/except ConfigError` → log and return `EXIT_GATE`. Keep `record` for step 6.
5. `meta = await longrun.run(data_dir=args.data, candidate=candidate, cadence_seconds=CADENCE_SECONDS[args.every], window=args.window, start_equity=args.start_equity, wall_clock=clock)`.
6. `snapshot_accuracy`: when the gate was consulted, read this candidate's `accuracy` out of `record.normal.candidates` (matching on `candidate_id`); `None` otherwise.
7. Build the `LongRunReport` from `meta` and write it to `args.out or reports_dir() / f"decision-lab-long-{meta.run_id}.md"`.
8. `registry.record(registry.RunRow(recorded_at=clock.now(), scenario="calibrate-long", dataset_digest=audit.dataset_digest, corpus_id=meta.corpus_id, matrix_digest=matrix.matrix_digest, dayset_digest=… , candidate_id=candidate.candidate_id, cadence_seconds=meta.cadence_seconds, start_equity=args.start_equity, window=args.window, gate_skipped=args.skip_gate, cost_usd=meta.profit.cost_usd, total_profit=…, realized_pnl=…, unrealized_pnl=…, net_profit=…, unvaluable=…))` — `dayset_digest` is `pinned.dayset_digest` when the gate was consulted and `""` otherwise.
9. `logger.info("long run reported", …)` and return `EXIT_OK`.

Register `("calibrate", "long"): calibrate_long` in `COMMANDS`.

- [ ] **Step 6: Run the tests and verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_calibrate.py -q`
Expected: PASS. `--window 1m` at `--every 24h` keeps the run to about thirty cycles on the 40-day fixture; if the fixture is too short for `1m` plus the indicators' warm-up, widen `shocked_walk(days=…)` in `test_slice_b_end_to_end.built_corpus` rather than shortening the window.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/longrun.py decision_lab/render.py decision_lab/cli.py decision_lab/tests/
git commit -m "feat(decision_lab): calibrate long, its profit block and its veto breakdown"
```

---

### Task 9: the slice exit criterion and the docs

**Files:**
- Create: `decision_lab/tests/test_slice_d_end_to_end.py`
- Modify: `decision_lab/PROGRESS.md`, `CLAUDE.md`

- [ ] **Step 1: Write the end-to-end test**

```python
"""Slice D pass 1 end to end: calibrate → gate → sweep, offline and free (§16).

The slice's exit criterion, driven through `cli.main` exactly as an operator would — the same
shape slices A, B and C take, and for the same reason: a handler called directly proves the
handler, while the operator's failure is usually in the wiring between them.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import registry


def test_the_gate_stands_between_an_operator_and_a_sweep(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus

    # Nothing calibrated: the expensive command refuses, and nothing is spent.
    assert _sweep(corpus_id) == cli.EXIT_GATE

    # Half calibrated is not calibrated.
    assert _calibrate("normal", corpus_id, tmp_path) == cli.EXIT_OK
    assert _sweep(corpus_id) == cli.EXIT_GATE

    # Both halves open it.
    assert _calibrate("shock", corpus_id, tmp_path) == cli.EXIT_OK
    assert _sweep(corpus_id) == cli.EXIT_OK

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert {"calibrate-normal", "calibrate-shock", "sweep"} <= {row.scenario for row in rows}
    assert all(row.run_id for row in rows)
    assert len({row.run_id for row in rows}) == len(rows), "§11: identical parameters update"


def test_a_calibration_report_names_the_days_the_gate_and_the_projection(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "shock.md"

    assert _calibrate("shock", corpus_id, tmp_path, out=out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "PLUMBING CHECK — NOT AN EVALUATION" in text, "the stub matrix is never an evaluation"
    assert "## Calibration — shock" in text
    assert "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "Projected spend" in text
    assert "Gate: PASSED" in text


def _sweep(corpus_id: str) -> int:
    return cli.main(
        ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    )


def _calibrate(scenario: str, corpus_id: str, tmp_path: Path, *, out: Path | None = None) -> int:
    return cli.main(
        [
            "calibrate",
            scenario,
            "--corpus",
            corpus_id,
            "--configs",
            str(cd.STUB_MATRIX),
            "--budget",
            "1",
            "--out",
            str(out or tmp_path / f"{scenario}.md"),
        ]
    )
```

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_slice_d_end_to_end.py -q`
Expected: PASS.

- [ ] **Step 3: Update `decision_lab/PROGRESS.md`**

- The at-a-glance row for **D** becomes `🟡 pass 1 shipped — calibration + gate; pass 2 (dashboard + notebook) not started`, and the summary paragraph under the table says four of five slices are partly or wholly in.
- Tick the first four boxes under "Slice D"; leave the dashboard box unticked and note it is pass 2.
- Add to "What you can run today":

```powershell
.venv\Scripts\python.exe -m decision_lab calibrate normal --corpus <id> --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab calibrate shock  --corpus <id> --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab calibrate long --data data\history --configs decision_lab\config\sweep.toml `
    --candidate baseline --start-equity 1000 --every 4h --window 6m
```

with a line saying `sweep` and `calibrate long` now refuse (exit 6) until both halves have passed for that dataset, matrix and day set, and that `--skip-gate` proceeds with that stamped on the report and the row.

- Add to "Open items": the gate is keyed on `(dataset, matrix, day set)`, so one edited prompt means calibrating again — nine days; and the dashboard and `notebooks/tuning.ipynb` are pass 2.

- [ ] **Step 4: Update `CLAUDE.md`**

In the `decision_lab` section: change the slice line to **A, B, C and D pass 1 have shipped** (D pass 2 — the dashboard and the notebook — and E are not built), add the three commands to the PowerShell block, and add these to "Rules that are easy to get backwards":

- **The gate is keyed on the dataset, the matrix and the day set — never on the corpus.** All four §10.6 conditions are properties of the candidates, the seats and the days. Keying on `corpus_id` would force a re-calibration every time the long run varied `--every`, which is the one axis it exists to vary. The cadence is recorded on the verdict for provenance and never scopes the gate.
- **A seat that only ever answered on its fallback fails the gate, and an abstention is not an answer.** A seat whose key is missing abstains quietly, and over six months that is a panel you paid to run and never tested. Read off `SeatResponse.fingerprint`, the same field §7.7's contamination check reads, so "substituted" and "never answered on its primary" can never disagree.
- **"Total profit" is mark-to-market, and it is not `Evidence.realized_pnl`.** That property sums closed round trips only, so a run ending with an open position would report a profit that ignores it. Realized and unrealized are both always printed, and `net_profit` subtracts what deliberation cost — a panel that made $80 on $120 of tokens lost money, and nothing else in the design says so. A frozen aggregate reports `UNVALUABLE` and **no figure at all**, not even the realized half: freezing is ignorance, and a partial number produced in ignorance is the cost-basis fallback ADR 0027 forbids, arriving through the other door.
- **A stub matrix cannot satisfy a real matrix's gate**, and needs no runtime check to say so: the bindings feed `panel_digest` → `matrix_digest`, which is a third of the gate key.
- **A long run lives in `workspace/long-<run_id>/`, never in the corpus directory**, though it derives its own `corpus_id` for §11 provenance. The two have different windows by construction, and sharing the directory would turn §5.4's window-mismatch refusal into a refusal of an unrelated command.

- [ ] **Step 5: Run both gates**

Run: `.\decision_lab\check.ps1`
Then: `.\check.ps1`
Then: `git diff --stat main -- tradebot/`
Expected: both gates green, and the diff prints nothing.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/tests/test_slice_d_end_to_end.py decision_lab/PROGRESS.md CLAUDE.md
git commit -m "feat(decision_lab): slice D pass 1 end to end, and the rules it adds"
```

---

## Self-review

**Spec coverage.** §10.2 → Tasks 3–5 (per-day rows and the spread). §10.3 → Task 5, with the two directions never pooled. §10.4 → Tasks 7–8 (mark-to-market profit, both halves, net of spend, `UNVALUABLE`, veto breakdown). §10.5 → Task 8's `snapshot_accuracy` block. §10.6 → Tasks 1, 3, 5, 6, 8 (the four conditions, the key, `--skip-gate`, the cost projection). §11 → Task 2 and every `registry.record` call. §13's CLI table → Tasks 5, 6, 8. §14's banners → Task 4. §15's failure semantics → the refusals in Tasks 1, 3, 5, 6, 7, 8. §16's rungs → Tasks 1, 3, 7 (unit), 9 (scenario).

**Deliberately out of scope for this pass:** §12 the dashboard, and `notebooks/tuning.ipynb`. Both are pass 2, recorded as such in Task 9's PROGRESS.md edit.

**Type consistency.** `gate.CandidateEvidence` is produced by `calibration.candidate_evidence` and consumed by `calibration.failures_for` and `calibration.project_cost` under that name throughout. `render.DayMetrics` is produced by `calibration.per_day` (Task 4) and consumed by `render._calibration_block`. `longrun.Profit`'s fields are flattened onto `render.LongRunReport` deliberately — named `total_profit`/`net_profit` there to match the registry row, and `total`/`net` on `Profit` itself; Task 8 step 5 maps between them.

**Soft spots the implementer must read the source for, not guess.** `SeatConfig.fallbacks`' declared element type (Task 3). `scoring.summarise`'s "correct" predicate — left as an explicit `NotImplementedError` in Task 3 step 3 and filled in at step 4, because a guess here is a gate that disagrees with the report it renders. `Verdict`'s member names (Task 4). The `sweep_` parser's `--configs` default (Task 5). `ReplayDataset.window`'s return shape (Task 8). `PortfolioAggregate`'s required fields (Task 7).
