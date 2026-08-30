# decision_lab Slice C — the sweep, the comparison, and the registry

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the question `decision_lab` was built for and cannot yet answer — *is a **different** panel right more often?* — by running N candidate panel configurations over one identical corpus of frozen decision contexts, ranking them per regime, and keeping a registry so two sweeps are compared rather than remembered.

**Architecture:** Five new modules over slice B. `candidates.py` reads a TOML matrix, expands its cross product, materialises each candidate as a full `Basket` (the corpus's reference basket with its panel swapped) and refuses an invalid or unreachable one before a single provider call. `sampling.py` draws a seeded stratified sample of corpus entries. `sweep.py` runs each candidate over each sampled entry through the bot's own `DecisionEngine.deliberate`, caching by (snapshot, panel), appending results as they are produced, and enforcing the budget and the no-substitute-model rule. `compare.py` folds the per-candidate scores into §9.6's ranking and agreement tables. `registry.py` records every run. Scoring is **not** touched: a sweep row folds into the same `CycleRecord` slice B already scores, so `score_records` and `score_seats` are reused unchanged.

**Tech Stack:** Python 3.11, pydantic v2, stdlib `tomllib`, pytest, ruff, mypy. No new dependency.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §7 in full, §9.6, §11, the `sweep` row of §13, the matching rows of §15, and the matrix/cache/budget/registry rows of §16.

**Depends on:** Slices A and B, both merged. Every number here is derived from a corpus (A) and scored by the truth table (B).

**Deliberately out of scope, and why:**

- **§10 calibration scenarios and the §10.6 gate** are slice D. `registry.py` reserves their fields (`scenario`, `start_equity`, `window`) inside `run_id` from the start so slice D lands without renumbering rows already written, but nothing here populates them.
- **§12 the dashboard** is slice D. §7.6 appends results as they are produced, which is what will let it tail a running sweep; nothing here reads that.
- **News** is slice E. Every corpus is news-blind, so every report still carries `NEWS-BLIND RUN`.
- **Per-seat round-0 caching** is a spec non-goal (§17), deferred until sweep cost proves it necessary.

## Global Constraints

- **Nothing under `tradebot/` may name `decision_lab`,** and this slice modifies **no** file under `tradebot/`. `git diff --stat main -- tradebot/` must stay empty; `test_separation.py` enforces the import direction.
- **No `float` anywhere in `decision_lab/`,** enforced by `test_discipline.py`. Slice C adds **exactly one** exemption — `candidates.py`, for `SeatConfig.temperature`, which `tomllib` parses as a `float` and which is a model hyper-parameter, not money. Slice A's `test_discipline.py` docstring already predicts this entry. Add it there with that reason and nowhere else.
- **Reuse, never reimplement** (§2.4). `DecisionEngine.deliberate`, `build_providers`, `reach_of`, `total_cost`, `Action.is_tradable`, `Basket.model_validate`, and slice B's `score_records` / `score_seats` / `by_regime` are all imported. A second consensus rule, a second scorer or a second cost total in this package would make the measurement a measurement of the copy.
- **A contaminated decision is never scored** (§7.7). One substitute answer poisons the whole cycle — the panel row and every seat row for it — under either `on_fallback` setting.
- **An evaluation refuses before spend** (§7.2) on an invalid candidate (exit 4), on any unreachable provider (exit 4), and halts without overspending on the budget ceiling (exit 5). It never discards completed work.
- **The report is written to a file, never printed** (§14). Nothing in this package calls `print`; `T20` bans it repo-wide.
- **Every table carries the regime split.** `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN` and one row per named window, never pooled (§8.3).
- **Line length 100**, `ruff format`, `from __future__ import annotations`, full annotations, module docstrings stating failure semantics.
- Verification: `.\decision_lab\check.ps1` **and** the root `.\check.ps1` must both pass.

---

### Task 1: The matrix — parse and expand

**Files:**
- Create: `decision_lab/candidates.py`
- Test: `decision_lab/tests/test_matrix.py`

**Interfaces:**
- Consumes: `decision_lab.params.workspace_root`, `tradebot.core.errors.ConfigError`.
- Produces:
  - `DEFAULT_MATRIX_LIMIT: Final = 24`, `CONFIG_DIR`, `DEFAULT_MATRIX`, `STUB_MATRIX`
  - `class SweepPolicy(StrEnum)` with `HALT = "halt"`, `EXCLUDE = "exclude"`
  - `candidates.read_document(path: Path) -> dict[str, Any]`
  - `candidates.expand(document: Mapping[str, Any]) -> tuple[dict[str, Any], ...]`
  - `candidates.matrix_limit(document: Mapping[str, Any]) -> int`
  - `candidates.policy_of(document: Mapping[str, Any]) -> SweepPolicy`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_matrix.py`:

```python
"""The candidate matrix: expansion, its cap, and the run policy (spec §7.1, §7.7).

Expansion is a cross product over `[expand]`, applied to every declared candidate. The ids it
produces must be deterministic and readable, because they are what a ranking table is read by and
what §11 keys a row on — an id that moved between two runs would silently make one experiment look
like two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from tradebot.core.errors import ConfigError

MATRIX = """
[sweep]
on_fallback = "exclude"

[prompts.cautious]
text = "Favour standing aside."

[prompts.momentum]
text = "Weight recent trend continuation."

[[candidates]]
id = "baseline"
protocol = "blind_then_debate"
max_rounds = 3

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"
  prompt = "momentum"

  [[candidates.seats]]
  seat_id = "risk"
  role = "risk officer"
  provider_id = "stub"
  model = "varied-skeptic"
  prompt = "cautious"

[expand]
max_rounds = [1, 3]
limit = 8
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_expand_takes_the_cross_product_and_names_each_axis(tmp_path: Path) -> None:
    document = cd.read_document(write(tmp_path, MATRIX))
    expanded = cd.expand(document)

    assert [c["id"] for c in expanded] == ["baseline~max_rounds=1", "baseline~max_rounds=3"]
    assert [c["max_rounds"] for c in expanded] == [1, 3]


def test_expand_resolves_a_seats_prompt_from_the_library(tmp_path: Path) -> None:
    document = cd.read_document(write(tmp_path, MATRIX))
    seats = cd.expand(document)[0]["seats"]

    assert seats[0]["instruction"] == "Weight recent trend continuation."
    assert seats[1]["instruction"] == "Favour standing aside."
    assert "prompt" not in seats[0], "the library key is resolved away, not passed through"


def test_a_prompt_axis_varies_one_seat_and_names_it(tmp_path: Path) -> None:
    text = MATRIX.replace("max_rounds = [1, 3]", 'prompts.risk = ["cautious", "momentum"]')
    expanded = cd.expand(cd.read_document(write(tmp_path, text)))

    assert [c["id"] for c in expanded] == ["baseline~risk=cautious", "baseline~risk=momentum"]
    assert expanded[1]["seats"][1]["instruction"] == "Weight recent trend continuation."


def test_a_document_with_no_expand_block_is_its_own_candidates(tmp_path: Path) -> None:
    text = MATRIX.split("[expand]")[0]
    expanded = cd.expand(cd.read_document(write(tmp_path, text)))

    assert [c["id"] for c in expanded] == ["baseline"]


def test_an_unknown_prompt_name_refuses_by_name(tmp_path: Path) -> None:
    text = MATRIX.replace('prompt = "momentum"', 'prompt = "nonexistent"')
    with pytest.raises(ConfigError, match="nonexistent"):
        cd.expand(cd.read_document(write(tmp_path, text)))


def test_an_oversized_matrix_refuses_rather_than_starting(tmp_path: Path) -> None:
    text = MATRIX.replace("max_rounds = [1, 3]", "max_rounds = [1, 2, 3]").replace(
        "limit = 8", "limit = 2"
    )
    document = cd.read_document(write(tmp_path, text))
    with pytest.raises(ConfigError, match="3 candidates"):
        cd.expand(document)


def test_the_policy_defaults_to_halt(tmp_path: Path) -> None:
    text = MATRIX.replace('on_fallback = "exclude"', "")
    assert cd.policy_of(cd.read_document(write(tmp_path, text))) is cd.SweepPolicy.HALT
    assert cd.policy_of(cd.read_document(write(tmp_path, MATRIX))) is cd.SweepPolicy.EXCLUDE


def test_an_unknown_policy_refuses_rather_than_defaulting(tmp_path: Path) -> None:
    text = MATRIX.replace('on_fallback = "exclude"', 'on_fallback = "carry on"')
    with pytest.raises(ConfigError, match="carry on"):
        cd.policy_of(cd.read_document(write(tmp_path, text)))


def test_a_missing_matrix_file_refuses_by_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="sweep.toml"):
        cd.read_document(tmp_path / "sweep.toml")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_matrix.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.candidates'`

- [ ] **Step 3: Write minimal implementation**

Create `decision_lab/candidates.py`:

```python
"""The candidate matrix: TOML in, validated `Basket`s out (spec §7.1, §7.2).

A candidate is the corpus's *reference basket with its panel swapped* — never a basket of its own.
Every candidate is therefore judged on the same instruments, the same venue rules and the same
starting positions, which is §3's load-bearing decision: a difference in score is a difference in
reasoning rather than a difference in luck.

Matrices are TOML files in `decision_lab/config/`, never `ConfigStore` documents (§2.1). Read with
stdlib `tomllib`, so the separation costs no dependency.

Failure semantics: everything here refuses *before* a provider call. An unparseable file, an
unknown prompt name, an oversized cross product, a candidate the bot's own `Basket` model would
reject, or an endpoint whose key is absent — each is a `ConfigError` naming what to fix. Nothing
here performs I/O beyond reading the matrix file, and nothing writes.
"""

from __future__ import annotations

import hashlib
import itertools
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from tradebot.core.config import Basket, PanelConfig
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import ConfigError
from tradebot.core.schema import canonical_json
from tradebot.decision.providers.registry import preset, reach_of

#: Expanded candidates a matrix may hold unless it says otherwise. In the spirit of
#: `DEFAULT_MAX_CYCLES`: a 400-candidate cross product is not a sweep anybody meant to start.
DEFAULT_MATRIX_LIMIT: Final = 24

#: Where a matrix lives when nobody said otherwise.
CONFIG_DIR: Final = Path(__file__).parent / "config"
DEFAULT_MATRIX: Final = CONFIG_DIR / "sweep.toml"
STUB_MATRIX: Final = CONFIG_DIR / "sweep-stub.toml"


class SweepPolicy(StrEnum):
    """What a substitute model answering does to the run (§7.7).

    Neither setting scores a contaminated decision. The choice is only whether the run stops.
    """

    HALT = "halt"
    EXCLUDE = "exclude"


def read_document(path: Path) -> dict[str, Any]:
    """Parse a matrix file, refusing a missing or malformed one by name."""
    if not path.is_file():
        raise ConfigError(
            f"no candidate matrix at {path}. The tool ships two: "
            f"{DEFAULT_MATRIX.name} (an evaluation) and {STUB_MATRIX.name} (a plumbing check)"
        )
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error


def policy_of(document: Mapping[str, Any]) -> SweepPolicy:
    """§7.7's setting, declared in the matrix so it is recorded rather than typed."""
    raw = str(document.get("sweep", {}).get("on_fallback", SweepPolicy.HALT.value))
    try:
        return SweepPolicy(raw)
    except ValueError:
        known = ", ".join(policy.value for policy in SweepPolicy)
        raise ConfigError(f"unknown on_fallback {raw!r}; known policies: {known}") from None


def matrix_limit(document: Mapping[str, Any]) -> int:
    return int(document.get("expand", {}).get("limit", DEFAULT_MATRIX_LIMIT))


def expand(document: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The declared candidates, crossed with `[expand]`, prompts resolved from the library.

    The id of an expanded candidate names every axis that varies it, so a ranking table is read
    without consulting the matrix, and §11's `run_id` is stable across re-runs.
    """
    library = {name: block["text"] for name, block in document.get("prompts", {}).items()}
    declared = document.get("candidates", ())
    if not declared:
        raise ConfigError("a matrix declares at least one [[candidates]] entry")

    axes = _axes(document)
    expanded: list[dict[str, Any]] = []
    for base in declared:
        for combination in itertools.product(*(values for _, values in axes)):
            expanded.append(_apply(base, tuple(zip(axes, combination, strict=True)), library))

    limit = matrix_limit(document)
    if len(expanded) > limit:
        raise ConfigError(
            f"the matrix expands to {len(expanded)} candidates, above its limit of {limit}. "
            "Raise `limit` in [expand] deliberately, or narrow an axis"
        )
    return tuple(expanded)


def _axes(document: Mapping[str, Any]) -> tuple[tuple[tuple[str, str], Sequence[Any]], ...]:
    """`[expand]` as ordered (kind, name) -> values. `prompts.<seat_id>` varies one seat."""
    axes = []
    for key, values in sorted(document.get("expand", {}).items()):
        if key == "limit":
            continue
        if not isinstance(values, list):
            raise ConfigError(f"[expand] {key} must be a list of values, got {values!r}")
        kind, _, name = key.partition(".")
        axes.append((("prompt", name) if kind == "prompts" else ("field", key), values))
    return tuple(axes)


def _apply(
    base: Mapping[str, Any],
    chosen: Sequence[tuple[tuple[tuple[str, str], Sequence[Any]], Any]],
    library: Mapping[str, str],
) -> dict[str, Any]:
    candidate = {key: value for key, value in base.items() if key != "seats"}
    candidate["seats"] = [dict(seat) for seat in base.get("seats", ())]

    suffix = []
    for ((kind, name), _), value in chosen:
        if kind == "prompt":
            for seat in candidate["seats"]:
                if seat.get("seat_id") == name:
                    seat["prompt"] = value
        else:
            candidate[name] = value
        suffix.append(f"{name}={value}")

    for seat in candidate["seats"]:
        chosen_prompt = seat.pop("prompt", None)
        if chosen_prompt is None:
            continue
        if chosen_prompt not in library:
            known = ", ".join(sorted(library)) or "none declared"
            raise ConfigError(
                f"seat {seat.get('seat_id')!r} names prompt {chosen_prompt!r}, which the matrix "
                f"does not declare; known prompts: {known}"
            )
        seat["instruction"] = library[chosen_prompt]

    candidate["id"] = "~".join((str(base.get("id", "candidate")), *suffix))
    return candidate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_matrix.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/candidates.py decision_lab/tests/test_matrix.py
git commit -m "feat(decision_lab): the candidate matrix, its cross product and its cap"
```

---

### Task 2: Materialise, validate, and refuse before spend

**Files:**
- Modify: `decision_lab/candidates.py`
- Test: `decision_lab/tests/test_candidates.py`

**Interfaces:**
- Consumes: Task 1's `expand`, `read_document`, `policy_of`; `tradebot.core.config.Basket`, `tradebot.core.enums.ProviderKind`, `tradebot.decision.providers.registry.{preset, reach_of}`, `tradebot.core.schema.canonical_json`.
- Produces:
  - `class Candidate` (frozen dataclass): `candidate_id: str`, `basket: Basket`; properties `panel -> PanelConfig`, `panel_digest -> str`, `stub_bindings -> tuple[str, ...]`
  - `class Matrix` (frozen dataclass): `candidates: tuple[Candidate, ...]`, `on_fallback: SweepPolicy`, `source: Path`; properties `matrix_digest -> str`, `is_evaluation -> bool`, `stub_bindings -> tuple[str, ...]`
  - `candidates.load_matrix(path: Path, *, reference: Basket) -> Matrix`
  - `candidates.unreachable(matrix: Matrix, environ: Mapping[str, str] | None = None) -> tuple[str, ...]`
  - `candidates.require_reachable(matrix: Matrix, environ: Mapping[str, str] | None = None) -> None`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_candidates.py`:

```python
"""Candidates are validated, and a run's *kind* is decided by what it binds (spec §7.2).

Two refusals, both before a provider call, and one classification:

* a candidate the bot's own `Basket` would reject never reaches a model — the sweep cannot test a
  panel the bot itself would refuse;
* an evaluation whose declared endpoint has no key refuses naming the variable, deliberately
  stricter than ADR 0023, which is right for a trading system and wrong for a measuring one;
* a matrix binding the offline stub anywhere is a *plumbing check*, not an evaluation, and the
  binding decides that — never a flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab.tests import factories as f
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.errors import ConfigError

STUB_MATRIX = """
[[candidates]]
id = "baseline"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"
"""

REAL_MATRIX = """
[[candidates]]
id = "baseline"
providers = ["openrouter"]

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "openrouter"
  model = "deepseek/deepseek-chat-v3-0324:free"
"""


def reference() -> Basket:
    return Basket(
        basket_id="reference",
        name="reference",
        instruments=(f.instrument(),),
        panel=PanelConfig(
            panel_id="reference",
            seats=(SeatConfig(seat_id="a", role="a", provider_id="stub", model="stub-technical"),),
        ),
    )


def write(tmp_path: Path, text: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_candidate_is_the_reference_basket_with_its_panel_swapped(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())
    candidate = matrix.candidates[0]

    assert candidate.candidate_id == "baseline"
    assert candidate.basket.instruments == reference().instruments
    assert candidate.basket.basket_id == reference().basket_id
    assert [s.seat_id for s in candidate.panel.seats] == ["trend"]


def test_an_invalid_candidate_refuses_the_whole_matrix(tmp_path: Path) -> None:
    text = STUB_MATRIX.replace('id = "baseline"', 'id = "baseline"\nqualified_majority = "1.5"')
    with pytest.raises(ConfigError, match="baseline"):
        cd.load_matrix(write(tmp_path, text), reference=reference())


def test_a_stub_binding_makes_the_run_a_plumbing_check(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())

    assert matrix.is_evaluation is False
    assert matrix.stub_bindings == ("baseline: trend->stub:varied-technical",)


def test_a_real_binding_makes_the_run_an_evaluation(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    assert matrix.is_evaluation is True
    assert matrix.stub_bindings == ()


def test_a_stub_fallback_is_as_disqualifying_as_a_primary_one(tmp_path: Path) -> None:
    text = REAL_MATRIX.replace('["openrouter"]', '["openrouter", "stub"]') + """
    [[candidates.seats.fallbacks]]
    provider_id = "stub"
    model = "varied-news"
"""
    matrix = cd.load_matrix(write(tmp_path, text), reference=reference())

    assert matrix.is_evaluation is False


def test_a_missing_key_refuses_an_evaluation_and_names_the_variable(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        cd.require_reachable(matrix, environ={})


def test_a_present_key_passes(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    cd.require_reachable(matrix, environ={"OPENROUTER_API_KEY": "sk-test"})


def test_a_plumbing_matrix_needs_no_key_at_all(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())

    cd.require_reachable(matrix, environ={})


def test_the_policy_is_not_part_of_the_matrix_identity(tmp_path: Path) -> None:
    """§7.7: it changes when a run stops, never what a run produces."""
    base = cd.load_matrix(write(tmp_path / "a", STUB_MATRIX), reference=reference())
    other = cd.load_matrix(
        write(tmp_path / "b", '[sweep]\non_fallback = "exclude"\n' + STUB_MATRIX),
        reference=reference(),
    )

    assert other.matrix_digest == base.matrix_digest
    assert other.on_fallback is cd.SweepPolicy.EXCLUDE


def test_a_reworded_prompt_is_a_different_matrix(tmp_path: Path) -> None:
    """§7.1: changing one prompt is a new matrix and needs a new calibration."""
    base = cd.load_matrix(write(tmp_path / "a", STUB_MATRIX), reference=reference())
    reworded = cd.load_matrix(
        write(
            tmp_path / "b",
            '[prompts.p]\ntext = "Stand aside."\n'
            + STUB_MATRIX.replace('model = "varied-technical"', 'model = "varied-technical"\n  prompt = "p"'),
        ),
        reference=reference(),
    )

    assert reworded.matrix_digest != base.matrix_digest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_candidates.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.candidates' has no attribute 'load_matrix'`

- [ ] **Step 3: Write minimal implementation**

Append to `decision_lab/candidates.py`:

```python
@dataclass(frozen=True, slots=True)
class Candidate:
    """One configuration under test: the reference basket, with this panel."""

    candidate_id: str
    basket: Basket

    @property
    def panel(self) -> PanelConfig:
        return self.basket.panel

    @property
    def panel_digest(self) -> str:
        """Identity of what is being measured. The §7.4 cache key's second half."""
        return hashlib.blake2s(
            canonical_json(self.panel).encode("utf-8"), digest_size=16
        ).hexdigest()

    @property
    def stub_bindings(self) -> tuple[str, ...]:
        """Bindings served by the offline stub, anywhere in any seat's chain.

        States the rule `control.readiness._scripted_bindings` states for live, one level over.
        That function is private, and the separation contract (§2.1) forbids this package editing
        `tradebot` to make it public — so the *rule* is repeated here rather than the code, and it
        is the rule that matters: a fallback to a stub is as disqualifying as a primary one,
        because a run is then one outage away from measuring canned JSON.
        """
        kinds = {provider.provider_id: provider.kind for provider in self.panel.providers}
        return tuple(
            f"{self.candidate_id}: {seat.seat_id}->{binding.fingerprint}"
            for seat in self.panel.seats
            for binding in seat.bindings
            if kinds.get(binding.provider_id) is ProviderKind.STUB
        )


@dataclass(frozen=True, slots=True)
class Matrix:
    """An expanded, validated candidate set, and what kind of run it is."""

    candidates: tuple[Candidate, ...]
    on_fallback: SweepPolicy
    source: Path

    @property
    def matrix_digest(self) -> str:
        """§7.1: identity of the fully expanded set, so changing one prompt is a new matrix.

        `on_fallback` is deliberately not in it — it changes when a run stops, never what a run
        produces (§7.7), and a digest that split on it would show one experiment as two.
        """
        payload = "|".join(f"{c.candidate_id}:{c.panel_digest}" for c in self.candidates)
        return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()

    @property
    def stub_bindings(self) -> tuple[str, ...]:
        return tuple(b for candidate in self.candidates for b in candidate.stub_bindings)

    @property
    def is_evaluation(self) -> bool:
        """False when anything binds the stub: that run measures canned JSON (§7.2)."""
        return not self.stub_bindings


def load_matrix(path: Path, *, reference: Basket) -> Matrix:
    """Read, expand and validate a matrix. Every refusal here happens before any spend."""
    document = read_document(path)
    built = tuple(
        Candidate(candidate_id=str(entry["id"]), basket=_basket_for(entry, reference))
        for entry in expand(document)
    )
    seen = [candidate.candidate_id for candidate in built]
    if len(set(seen)) != len(seen):
        raise ConfigError(f"{path} expands to duplicate candidate ids: {sorted(set(seen))}")
    return Matrix(candidates=built, on_fallback=policy_of(document), source=path)


def _basket_for(entry: Mapping[str, Any], reference: Basket) -> Basket:
    """The reference basket with this candidate's panel, through the bot's own validation.

    `model_validate` rather than a constructor, so a candidate is refused by exactly the rules the
    bot would refuse it by: unresolvable bindings, a repeated fallback, a majority above 1, an
    over-long instruction (§7.2).
    """
    document = reference.model_dump(mode="json")
    document["panel"] = _panel_for(entry)
    if "decision_mode" in entry:
        document["decision_mode"] = entry["decision_mode"]
    # A challenger belongs to the bot's shadow comparison (ADR 0018), not to a sweep: every
    # candidate here is judged in its own right, so a challenger inherited from the reference
    # basket would deliberate a second panel nothing reads and bill it to this run.
    document.pop("shadow_panel", None)
    try:
        return Basket.model_validate(document)
    except ValueError as error:
        raise ConfigError(
            f"candidate {entry.get('id')!r} is not a valid basket: {error}"
        ) from error


def _panel_for(entry: Mapping[str, Any]) -> dict[str, Any]:
    declared = entry.get("providers", ["stub"])
    panel: dict[str, Any] = {
        "panel_id": str(entry["id"]),
        "providers": [preset(provider_id).model_dump(mode="json") for provider_id in declared],
        "seats": [dict(seat) for seat in entry.get("seats", ())],
    }
    for field in (
        "protocol",
        "max_rounds",
        "qualified_majority",
        "max_abstain_fraction",
        "max_cost_usd_per_cycle",
    ):
        if field in entry:
            panel[field] = entry[field]
    return panel


def unreachable(matrix: Matrix, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Endpoints a candidate declares whose key is absent, as printable findings.

    `reach_of` is the bot's single rule for the question and is imported rather than restated
    (§2.4). What differs here is the *threshold*: any missing key at all, not merely a silenced
    seat, because a partly-reachable seat is one that will answer on its backup — and §7.7 says
    that is not a measurement.
    """
    findings: list[str] = []
    for candidate in matrix.candidates:
        reach = reach_of(candidate.panel, environ)
        findings += [f"{candidate.candidate_id}: {missing}" for missing in reach.missing]
    return tuple(findings)


def require_reachable(matrix: Matrix, environ: Mapping[str, str] | None = None) -> None:
    """Refuse an evaluation whose providers cannot be reached (§7.2).

    A plumbing check is exempt: the stub has no endpoint and no key, so there is nothing to miss.
    """
    if not matrix.is_evaluation:
        return
    missing = unreachable(matrix, environ)
    if missing:
        raise ConfigError(
            "this matrix cannot be evaluated — an endpoint it declares has no key, so a seat "
            "would answer on its backup and measure a panel that was never configured: "
            + "; ".join(missing)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_candidates.py -q`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/candidates.py decision_lab/tests/test_candidates.py
git commit -m "feat(decision_lab): validate every candidate, and refuse an unreachable evaluation"
```

---

### Task 3: The two shipped matrices, and the one float exemption

**Files:**
- Create: `decision_lab/config/sweep.toml`
- Create: `decision_lab/config/sweep-stub.toml`
- Modify: `decision_lab/tests/test_discipline.py` (the `FLOAT_EXEMPT` set and the docstring)
- Test: `decision_lab/tests/test_shipped_matrices.py`

**Interfaces:**
- Consumes: Task 2's `load_matrix`, `DEFAULT_MATRIX`, `STUB_MATRIX`.
- Produces: no new symbols. Two files the CLI defaults to, and a test asserting both still parse and classify.

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_shipped_matrices.py`:

```python
"""The two matrices the repo ships, and what each one is (spec §7.2).

`sweep.toml` is an evaluation: real endpoints, real model ids, and it refuses without keys.
`sweep-stub.toml` is the plumbing check: stubs, so it runs anywhere and is stamped as not being a
measurement. Both are asserted here because a matrix that stopped parsing would be found by an
operator mid-run rather than by CI — and the free model ids rot (R11).
"""

from __future__ import annotations

import pytest

from decision_lab import candidates as cd
from decision_lab.tests.test_candidates import reference
from tradebot.core.errors import ConfigError
from tradebot.decision import presets


def test_the_shipped_evaluation_matrix_parses_and_is_an_evaluation() -> None:
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())

    assert matrix.is_evaluation is True
    assert matrix.on_fallback is cd.SweepPolicy.HALT, "strict by default (§7.7)"
    assert len(matrix.candidates) >= 2, "a sweep of one candidate compares nothing"


def test_the_shipped_evaluation_matrix_refuses_without_keys() -> None:
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        cd.require_reachable(matrix, environ={})


def test_the_shipped_matrix_uses_the_model_ids_presets_declares() -> None:
    """One place to fix when a free slot disappears. R11 is why they carry fallbacks at all."""
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())
    bound = {
        binding.model
        for candidate in matrix.candidates
        for seat in candidate.panel.seats
        for binding in seat.bindings
    }
    known = {
        presets.TECHNICAL_MODEL,
        presets.NEWS_MODEL,
        presets.SKEPTIC_MODEL,
        presets.LOCAL_TECHNICAL_MODEL,
        presets.LOCAL_SKEPTIC_MODEL,
        presets.GEMINI_MODEL,
    }
    assert bound <= known, f"model ids not declared in decision/presets.py: {sorted(bound - known)}"


def test_the_stub_matrix_is_a_plumbing_check_and_needs_nothing() -> None:
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=reference())

    assert matrix.is_evaluation is False
    assert len(matrix.candidates) >= 2
    cd.require_reachable(matrix, environ={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_shipped_matrices.py -q`
Expected: FAIL — `ConfigError: no candidate matrix at .../config/sweep.toml`

- [ ] **Step 3: Write the evaluation matrix**

Create `decision_lab/config/sweep.toml`:

```toml
# The evaluation matrix (spec §7.1).
#
# Real endpoints and real model ids, so running this is a *measurement*. It refuses before spend
# if OPENROUTER_API_KEY is absent (§7.2): a seat answering on its backup would be a panel nobody
# configured, recorded under the name of one somebody did.
#
# Model ids are the ones decision/presets.py declares, which is the single place to fix them when
# a free OpenRouter slot disappears — that churn is R11, and it is why every seat carries a
# cross-family fallback chain rather than a second model from the same vendor.
#
# To exercise the machinery without spending anything, use sweep-stub.toml. That is not a flag on
# this file: the binding decides what kind of run it is, so the answer is recorded on the report.

[sweep]
# halt    — the first substitute answer stops the sweep, completed rows kept (§7.7). The default.
# exclude — the run continues; every cycle a seat fell back in is dropped from the score.
on_fallback = "halt"

[prompts.cautious]
text = "Favour standing aside. State the single strongest argument against your own vote."

[prompts.momentum]
text = "Weight recent trend continuation over mean reversion. Name the invalidation level."

[[candidates]]
id = "baseline"
protocol = "blind_then_debate"
max_rounds = 3
qualified_majority = "0.5"
providers = ["openrouter", "gemini", "lmstudio"]

  [[candidates.seats]]
  seat_id = "technical"
  role = "Technical Analyst"
  provider_id = "openrouter"
  model = "deepseek/deepseek-chat-v3-0324:free"
  evidence = ["indicators", "position"]
  prompt = "momentum"

    [[candidates.seats.fallbacks]]
    provider_id = "lmstudio"
    model = "qwen2.5-7b-instruct"

  [[candidates.seats]]
  seat_id = "news"
  role = "News/Sentiment Analyst"
  provider_id = "openrouter"
  model = "meta-llama/llama-3.3-70b-instruct:free"
  evidence = ["news", "position"]
  prompt = "cautious"

    [[candidates.seats.fallbacks]]
    provider_id = "gemini"
    model = "gemini-2.0-flash"

  [[candidates.seats]]
  seat_id = "skeptic"
  role = "Macro/Risk Skeptic"
  provider_id = "openrouter"
  model = "qwen/qwen-2.5-72b-instruct:free"
  evidence = ["indicators", "news", "position"]
  devils_advocate = true
  prompt = "cautious"

    [[candidates.seats.fallbacks]]
    provider_id = "lmstudio"
    model = "mistral-7b-instruct"

# One blind round against three. "Does debating change the answer, and for the better" is the
# cheapest question this tool can ask, and it is the one an operator asks first.
[expand]
max_rounds = [1, 3]
limit = 8
```

- [ ] **Step 4: Write the plumbing matrix**

Create `decision_lab/config/sweep-stub.toml`:

```toml
# The plumbing check (spec §7.2).
#
# Every binding is the offline stub, so this matrix costs nothing, needs no key and reaches no
# network — and every report and registry row it produces is stamped
# PLUMBING CHECK — NOT AN EVALUATION. The stub draws from a fifteen-entry catalogue: it exercises
# the sweep, the cache, the budget, the tables and the report, and it measures nothing about any
# model's judgement.
#
# Use it to prove a change works. Use sweep.toml to learn something.

[sweep]
on_fallback = "halt"

[prompts.cautious]
text = "Favour standing aside. State the single strongest argument against your own vote."

[prompts.momentum]
text = "Weight recent trend continuation over mean reversion. Name the invalidation level."

[[candidates]]
id = "varied-three"
protocol = "blind_then_debate"
max_rounds = 3
max_cost_usd_per_cycle = "0"
providers = ["stub"]

  [[candidates.seats]]
  seat_id = "technical"
  role = "Technical Analyst"
  provider_id = "stub"
  model = "varied-technical"
  evidence = ["indicators", "position"]
  prompt = "momentum"

  [[candidates.seats]]
  seat_id = "news"
  role = "News/Sentiment Analyst"
  provider_id = "stub"
  model = "varied-news"
  evidence = ["news", "position"]
  prompt = "cautious"

  [[candidates.seats]]
  seat_id = "skeptic"
  role = "Macro/Risk Skeptic"
  provider_id = "stub"
  model = "varied-skeptic"
  evidence = ["indicators", "news", "position"]
  devils_advocate = true
  prompt = "cautious"

[expand]
max_rounds = [1, 3]
limit = 4
```

- [ ] **Step 5: Add the one float exemption**

In `decision_lab/tests/test_discipline.py`, replace the `FLOAT_EXEMPT` line:

```python
#: Modules permitted to name `float`. `candidates.py` is the only one, and only for
#: `SeatConfig.temperature`: `tomllib` parses a TOML float as a `float`, and a sampling temperature
#: is a model hyper-parameter, not money. Nothing else may be added here.
FLOAT_EXEMPT: frozenset[str] = frozenset({"candidates.py"})
```

And change the module docstring's last paragraph from predicting the entry to recording it:

```python
The exemption set holds exactly one entry: `candidates.py`, for `SeatConfig.temperature`, which
`tomllib` parses as a `float` and which is a model hyper-parameter rather than money. Nothing else
belongs there.
```

- [ ] **Step 6: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_shipped_matrices.py decision_lab/tests/test_discipline.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add decision_lab/config decision_lab/tests/test_shipped_matrices.py decision_lab/tests/test_discipline.py
git commit -m "feat(decision_lab): the evaluation matrix and the plumbing matrix, as two files"
```

---

### Task 4: The stratified, seeded sample

**Files:**
- Create: `decision_lab/sampling.py`
- Modify: `decision_lab/params.py`
- Modify: `decision_lab/tests/factories.py`
- Test: `decision_lab/tests/test_sampling.py`

**Interfaces:**
- Consumes: `decision_lab.corpus.{Corpus, CorpusEntry}`, `decision_lab.regimes.RegimeIndex`, `decision_lab.params.{DEFAULT_SEED, SAMPLE_SIZES}`.
- Produces:
  - `params.SAMPLE_SIZES: Final = {"NORMAL": 60, "SHOCK_UP": 30, "SHOCK_DOWN": 30}`
  - `sampling.PINNED: Final = "pinned"`
  - `class Sample(DomainModel)`: `cycle_ids: tuple[str, ...]`, `seed: int`, `full: bool`, `selected: dict[str, int]`, `available: dict[str, int]`
  - `sampling.stratified(corpus, *, regimes, reference_instrument, pinned=(), seed=DEFAULT_SEED, full=False, sizes=SAMPLE_SIZES) -> Sample`
  - `factories.corpus_with_entries(*, count: int, as_of: datetime, labels: Sequence[Pool] | None = None) -> Corpus`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_sampling.py`:

```python
"""The sample is stratified, seeded, and never crowds one shock direction out (spec §7.3).

Two properties matter more than the sizes. A re-run with the same seed draws the same entries, or
two sweeps are not comparable and the whole design collapses. And the rare strata — named windows
and the pinned days — are taken whole, because they are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from decision_lab import sampling
from decision_lab.calibration_days import Pool
from decision_lab.tests.factories import corpus_with_entries

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class FakeRegimes:
    """Labels by instant, so a test states its own distribution."""

    def __init__(self, labels: dict[datetime, Pool], windows: dict[datetime, str]) -> None:
        self._labels = labels
        self._windows = windows

    def label_at(self, instrument_key: str, as_of: datetime) -> Pool:
        return self._labels[as_of]

    def window_at(self, as_of: datetime) -> object | None:
        name = self._windows.get(as_of)
        return type("W", (), {"name": name})() if name else None


def fixture(labels: list[Pool], windows: dict[int, str] | None = None):  # type: ignore[no-untyped-def]
    corpus = corpus_with_entries(count=len(labels), as_of=EPOCH)
    by_time = {EPOCH + timedelta(hours=i): label for i, label in enumerate(labels)}
    named = {EPOCH + timedelta(hours=i): name for i, name in (windows or {}).items()}
    return corpus, FakeRegimes(by_time, named)


def test_the_same_seed_draws_the_same_entries() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 50)

    first = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=7, sizes={"NORMAL": 10}
    )
    second = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=7, sizes={"NORMAL": 10}
    )

    assert first.cycle_ids == second.cycle_ids
    assert len(first.cycle_ids) == 10


def test_a_different_seed_draws_a_different_sample() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 50)

    first = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=1, sizes={"NORMAL": 10}
    )
    second = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=2, sizes={"NORMAL": 10}
    )

    assert first.cycle_ids != second.cycle_ids


def test_neither_shock_direction_can_crowd_the_other_out() -> None:
    corpus, regimes = fixture([Pool.SHOCK_UP] * 40 + [Pool.SHOCK_DOWN] * 4 + [Pool.NORMAL] * 10)

    sample = sampling.stratified(
        corpus,
        regimes=regimes,
        reference_instrument="k",
        sizes={"NORMAL": 5, "SHOCK_UP": 6, "SHOCK_DOWN": 6},
    )

    assert sample.selected["SHOCK_UP"] == 6
    assert sample.selected["SHOCK_DOWN"] == 4, "a short stratum contributes all it has"
    assert sample.available["SHOCK_DOWN"] == 4


def test_a_named_window_is_taken_whole_however_small_the_quota() -> None:
    corpus, regimes = fixture(
        [Pool.NORMAL] * 20, windows={i: "spot ETF approval" for i in range(12, 20)}
    )

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", sizes={"NORMAL": 1}
    )

    assert sample.selected["spot ETF approval"] == 8


def test_a_pinned_day_is_taken_whole() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 30)

    sample = sampling.stratified(
        corpus,
        regimes=regimes,
        reference_instrument="k",
        pinned=(EPOCH.date(),),
        sizes={"NORMAL": 1},
    )

    assert sample.selected["pinned"] == 24, "every entry on a pinned day, not a quota of them"


def test_full_disables_sampling_entirely() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 30)

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", full=True, sizes={"NORMAL": 2}
    )

    assert len(sample.cycle_ids) == 30
    assert sample.full is True


def test_entries_come_back_in_corpus_order() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 40)

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", sizes={"NORMAL": 12}
    )

    order = [entry.cycle_id for entry in corpus.entries if entry.cycle_id in set(sample.cycle_ids)]
    assert list(sample.cycle_ids) == order, "a sweep walks history forwards, whatever it drew in"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sampling.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.sampling'`

- [ ] **Step 3: Add the corpus factory**

Append to `decision_lab/tests/factories.py`:

```python
def snapshot_at(as_of: datetime, *, price: str = "100") -> ContextSnapshot:
    """A minimal but real snapshot: one instrument, one quote, one ATR reading.

    Real rather than a stub because scoring reads `context.indicator("ATR", …)` off it, and the
    band is derived from exactly the evidence the panel had (§9.2).
    """
    inst = instrument()
    return ContextSnapshot(
        as_of=as_of,
        instruments=(
            InstrumentContext(
                instrument=inst,
                quote=Quote(
                    instrument_key=inst.key,
                    price=Decimal(price),
                    observed_at=as_of,
                    venue=inst.venue,
                ),
                indicators=(
                    IndicatorReading(
                        name="ATR", timeframe="1h", value=Decimal("1.0"), computed_at=as_of
                    ),
                ),
            ),
        ),
    )


def corpus_with_entries(
    *, count: int, as_of: datetime, corpus_id: str = "corpus-test", timeframe: str = "1h"
) -> Corpus:
    """A `Corpus` of `count` entries on the venue's bar grid, with a real snapshot on each."""
    interval = timeframe_interval(timeframe)
    entries = tuple(
        CorpusEntry(
            seq=index,
            cycle_id=f"c{index}",
            basket_id="reference",
            as_of=as_of + interval * index,
            snapshot=snapshot_at(as_of + interval * index),
        )
        for index in range(count)
    )
    meta = CorpusMeta(
        corpus_id=corpus_id,
        built_at=as_of,
        dataset_directory="data/history",
        dataset_digest="d1",
        reference_panel_id="stub",
        reference_basket=_reference_basket(),
        reference_config_digest="r1",
        cadence_seconds=int(interval.total_seconds()),
        start_equity=Decimal(10_000),
        requested_start=as_of,
        window_start=as_of,
        window_end=as_of + interval * count,
        warmup_seconds=0,
        planned_cycles=count,
        ran_cycles=count,
    )
    return Corpus(meta=meta, entries=entries)


def _reference_basket() -> Basket:
    return Basket(
        basket_id="reference",
        name="reference",
        instruments=(instrument(),),
        panel=PanelConfig(
            panel_id="reference",
            seats=(SeatConfig(seat_id="a", role="a", provider_id="stub", model="stub-technical"),),
        ),
    )
```

Add its imports at the top of `factories.py`:

```python
from decision_lab.corpus import Corpus, CorpusEntry, CorpusMeta
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.indicators import IndicatorReading
from tradebot.core.market import Quote
from tradebot.core.snapshot import ContextSnapshot, InstrumentContext
```

> **Note for the implementer:** verify `IndicatorReading`, `Quote` and `InstrumentContext` field names against `tradebot/core/` before writing — construct them the way `tests/unit` already does rather than guessing. If a required field is missing the models will say so, and the fix belongs here, not in a looser assertion.

- [ ] **Step 4: Add the sizes to `params.py`**

Append to `decision_lab/params.py`:

```python
#: Corpus entries drawn per stratum by `sweep` when nobody said otherwise (§7.3). Named windows
#: and the pinned days are taken *whole* and are deliberately not in this table: they are rare and
#: they are the point of the exercise, so a quota over them would defeat it.
SAMPLE_SIZES: Final = {"NORMAL": 60, "SHOCK_UP": 30, "SHOCK_DOWN": 30}
```

- [ ] **Step 5: Write the implementation**

Create `decision_lab/sampling.py`:

```python
"""Which corpus entries a sweep pays for (spec §7.3).

Evaluating every candidate on every entry is affordable only at coarse cadence, so the default is
a stratified, seeded draw. Two properties carry the design:

* **Seeded.** A re-run draws the same entries, so two sweeps are comparable. The seed is recorded
  on the report and on the §11 row; an unseeded sample would make every comparison a comparison of
  two different subsets of history.
* **Stratified by the reference instrument.** A cycle covers every instrument in the basket, and
  two of them can sit in two different regimes at one instant — so a cycle has no single regime of
  its own. The stratum is the regime of the instrument §4.5 already draws the day set from and
  every report already names, rather than a label a cycle cannot have one of.

Named windows and the pinned days are taken whole, never sampled: they are rare, and they are what
the shock questions are asked over.

Failure semantics: a stratum with fewer entries than its quota contributes all of them and says so
on `available`, rather than refusing — a corpus short of down-shocks is a fact about the window,
not a broken run. Nothing here performs I/O.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

from decision_lab.corpus import Corpus, CorpusEntry
from decision_lab.params import DEFAULT_SEED, SAMPLE_SIZES
from decision_lab.regimes import RegimeIndex
from tradebot.core.schema import DomainModel

#: The stratum taken whole because §4.5 pinned it.
PINNED: Final = "pinned"


class Sample(DomainModel):
    """The entries one sweep will pay for, and how they were chosen."""

    cycle_ids: tuple[str, ...] = ()
    seed: int = DEFAULT_SEED
    full: bool = False
    #: Stratum -> entries drawn. Printed on the report, so a thin stratum is visible.
    selected: dict[str, int] = {}
    #: Stratum -> entries the corpus held. Beside `selected`, this is what says "all there was".
    available: dict[str, int] = {}


def stratified(
    corpus: Corpus,
    *,
    regimes: RegimeIndex,
    reference_instrument: str,
    pinned: Sequence[date] = (),
    seed: int = DEFAULT_SEED,
    full: bool = False,
    sizes: Mapping[str, int] = SAMPLE_SIZES,
) -> Sample:
    """Draw the sample. `full` disables it and returns every entry (§7.3)."""
    if full:
        return Sample(
            cycle_ids=tuple(entry.cycle_id for entry in corpus.entries),
            seed=seed,
            full=True,
            selected={"all": len(corpus.entries)},
            available={"all": len(corpus.entries)},
        )

    strata: dict[str, list[CorpusEntry]] = {}
    pinned_days = set(pinned)
    for entry in corpus.entries:
        name = _stratum(entry, regimes, reference_instrument, pinned_days)
        strata.setdefault(name, []).append(entry)

    rng = random.Random(seed)
    chosen: set[str] = set()
    selected: dict[str, int] = {}
    for name in sorted(strata):
        pool = strata[name]
        quota = len(pool) if name == PINNED or name not in sizes else min(sizes[name], len(pool))
        drawn = pool if quota >= len(pool) else rng.sample(pool, quota)
        chosen.update(entry.cycle_id for entry in drawn)
        selected[name] = len(drawn)

    return Sample(
        cycle_ids=tuple(e.cycle_id for e in corpus.entries if e.cycle_id in chosen),
        seed=seed,
        full=False,
        selected=selected,
        available={name: len(pool) for name, pool in sorted(strata.items())},
    )


def _stratum(
    entry: CorpusEntry, regimes: RegimeIndex, instrument_key: str, pinned: set[date]
) -> str:
    """A named window outranks a pinned day outranks the automatic label.

    Windows first for the reason §8.2 gives — a named window is an operator's assertion about a
    period and overrides the labeller — and pinned days next, so a calibration day is never
    thinned by a `NORMAL` quota.
    """
    window = regimes.window_at(entry.as_of)
    if window is not None:
        return str(window.name)
    if entry.day in pinned:
        return PINNED
    return str(regimes.label_at(instrument_key, entry.as_of).value)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sampling.py -q`
Expected: PASS, 7 tests

- [ ] **Step 7: Commit**

```bash
git add decision_lab/sampling.py decision_lab/params.py decision_lab/tests/factories.py decision_lab/tests/test_sampling.py
git commit -m "feat(decision_lab): the seeded stratified sample, drawn on the reference instrument"
```

---

### Task 5: Sweep rows — the record, the cache, and resume

**Files:**
- Create: `decision_lab/sweep.py`
- Test: `decision_lab/tests/test_sweep_storage.py`

**Interfaces:**
- Consumes: `decision_lab.corpus.{Corpus, CorpusEntry, corpus_dir}`, `decision_lab.records.CycleRecord`, `tradebot.core.decision.{Decision, SeatResponse}`, `tradebot.core.config.PanelConfig`.
- Produces:
  - `CACHE_DIR: Final = "cache"`, `SWEEP_META: Final = "sweep.json"`
  - `class SweepRow(DomainModel)`: `cycle_id`, `as_of`, `decisions`, `responses`, `cost_usd`, `substitutes: tuple[str, ...]`, `error: str`; property `contaminated -> bool`
  - `sweep.sweep_dir(corpus_id, matrix_digest, *, workspace=None) -> Path`
  - `sweep.cache_dir(corpus_id, *, workspace=None) -> Path`
  - `sweep.rows_path(corpus_id, matrix_digest, candidate_id, *, workspace=None) -> Path`
  - `sweep.read_rows(path) -> dict[str, SweepRow]`
  - `sweep.append_row(path, row) -> None`
  - `sweep.cache_key(snapshot_digest: str, panel_digest: str) -> str`
  - `sweep.cache_read(corpus_id, key, *, workspace=None) -> SweepRow | None`
  - `sweep.cache_write(corpus_id, key, row, *, workspace=None) -> None`
  - `sweep.substitutes_in(responses, panel) -> tuple[str, ...]`
  - `sweep.records_from_rows(corpus, rows) -> tuple[CycleRecord, ...]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_sweep_storage.py`:

```python
"""Rows, the cache, and what folds back into a `CycleRecord` (spec §7.4, §7.6, §7.7).

Three things this file pins, each of which a later change could break silently:

* a cache key names the evidence and the panel and *nothing else*, so it is shared across
  matrices — scoping it by matrix would make adding one candidate re-pay for every other;
* a row survives a round trip, so resume picks up what a killed process had already bought;
* a substitute binding is detected from `fingerprint`, and it contaminates the whole cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import sweep
from decision_lab.tests.factories import corpus_with_entries
from tradebot.core.config import PanelConfig, ProviderBinding, SeatConfig
from tradebot.core.decision import Decision, SeatResponse, SeatVote
from tradebot.core.enums import Action

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)


def panel() -> PanelConfig:
    return PanelConfig(
        panel_id="p",
        seats=(
            SeatConfig(
                seat_id="technical",
                role="t",
                provider_id="openrouter",
                model="primary-model",
                fallbacks=(ProviderBinding(provider_id="gemini", model="backup-model"),),
            ),
        ),
    )


def response(provider_id: str, model: str, *, seat_id: str = "technical") -> SeatResponse:
    return SeatResponse(
        seat_id=seat_id,
        role="t",
        provider_id=provider_id,
        model=model,
        round_index=0,
        instrument_key="binance:BTC/USDT",
        vote=SeatVote(action=Action.BUY, conviction=3, thesis="t"),
        responded_at=AS_OF,
        cost_usd=Decimal("0.01"),
    )


def row(**overrides: object) -> sweep.SweepRow:
    base: dict[str, object] = dict(
        cycle_id="c0",
        as_of=AS_OF,
        decisions=(Decision(instrument_key="binance:BTC/USDT", action=Action.BUY),),
        responses=(response("openrouter", "primary-model"),),
        cost_usd=Decimal("0.01"),
    )
    base.update(overrides)
    return sweep.SweepRow(**base)  # type: ignore[arg-type]


def test_the_cache_key_names_the_evidence_and_the_panel_only() -> None:
    key = sweep.cache_key("snap-digest", "panel-digest")

    assert key == sweep.cache_key("snap-digest", "panel-digest")
    assert key != sweep.cache_key("snap-digest", "other-panel")
    assert key != sweep.cache_key("other-snap", "panel-digest")


def test_a_cached_row_is_returned_without_a_second_call(tmp_path: Path) -> None:
    sweep.cache_write("corpus-1", "key-1", row(), workspace=tmp_path)

    found = sweep.cache_read("corpus-1", "key-1", workspace=tmp_path)

    assert found is not None
    assert found.cycle_id == "c0"
    assert found.cost_usd == Decimal("0.01")
    assert sweep.cache_read("corpus-1", "absent", workspace=tmp_path) is None


def test_the_cache_is_shared_across_matrices(tmp_path: Path) -> None:
    """§7.4: adding one candidate must not re-pay for every candidate already answered."""
    directory = sweep.cache_dir("corpus-1", workspace=tmp_path)

    assert directory.name == "cache"
    assert "sweep-" not in str(directory)


def test_rows_append_and_read_back_keyed_by_cycle(tmp_path: Path) -> None:
    path = sweep.rows_path("corpus-1", "matrix-a", "baseline", workspace=tmp_path)
    sweep.append_row(path, row())
    sweep.append_row(path, row(cycle_id="c1"))

    found = sweep.read_rows(path)

    assert sorted(found) == ["c0", "c1"]
    assert found["c1"].as_of == AS_OF


def test_reading_rows_from_a_path_that_does_not_exist_is_empty(tmp_path: Path) -> None:
    assert sweep.read_rows(tmp_path / "nothing.jsonl") == {}


def test_two_matrices_do_not_resume_into_each_others_files(tmp_path: Path) -> None:
    left = sweep.rows_path("corpus-1", "matrix-a", "baseline", workspace=tmp_path)
    right = sweep.rows_path("corpus-1", "matrix-b", "baseline", workspace=tmp_path)

    assert left != right
    sweep.append_row(left, row())
    assert sweep.read_rows(right) == {}


def test_a_primary_binding_is_not_a_substitute() -> None:
    assert sweep.substitutes_in((response("openrouter", "primary-model"),), panel()) == ()


def test_a_fallback_binding_is_a_substitute_and_names_both_ends() -> None:
    found = sweep.substitutes_in((response("gemini", "backup-model"),), panel())

    assert found == ("technical: openrouter:primary-model -> gemini:backup-model",)


def test_a_row_with_any_substitute_is_contaminated() -> None:
    assert row().contaminated is False
    assert row(substitutes=("technical: a -> b",)).contaminated is True


def test_rows_fold_into_the_cycle_records_slice_b_already_scores() -> None:
    """The load-bearing reuse: slice C adds no scoring code at all."""
    corpus = corpus_with_entries(count=1, as_of=AS_OF)

    records = sweep.records_from_rows(corpus, {"c0": row()})

    assert len(records) == 1
    assert records[0].cycle_id == "c0"
    assert records[0].snapshot == corpus.entries[0].snapshot
    assert records[0].decisions[0].action is Action.BUY
    assert records[0].cost_usd == Decimal("0.01")


def test_a_contaminated_or_failed_row_never_becomes_a_scorable_record() -> None:
    """§7.7: a substitute answered, so no part of that cycle measures the configured panel."""
    corpus = corpus_with_entries(count=2, as_of=AS_OF)

    records = sweep.records_from_rows(
        corpus,
        {"c0": row(substitutes=("technical: a -> b",)), "c1": row(cycle_id="c1", error="boom")},
    )

    assert records == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sweep_storage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.sweep'`

- [ ] **Step 3: Write minimal implementation**

Create `decision_lab/sweep.py` — the storage half only; Task 6 appends `run`:

```python
"""N candidates over one corpus (spec §7).

The design's load-bearing decision is that a candidate never runs its own loop (§3): it
deliberates on the corpus's *already-frozen* snapshots, so every candidate is judged on the same
evidence and the same positions, and a difference in score is a difference in reasoning rather
than a difference in luck. That is ADR 0018's principle generalised from one challenger to N.

A result folds back into slice B's `CycleRecord`, which is why this package gains no scoring code:
`score_records` and `score_seats` read a sweep exactly as they read the reference pass.

Two storage locations, deliberately different in scope:

* `workspace/<corpus_id>/cache/` — content-addressed by (snapshot, panel) and **shared across
  matrices**, because that key already names everything that determines the answer. Scoping it by
  matrix would defeat §7.4: adding one candidate would re-pay for every candidate already answered.
* `workspace/<corpus_id>/sweep-<matrix_digest>/<candidate_id>.jsonl` — the experiment's record,
  appended as results are produced so an interrupted sweep resumes (§7.6) and so §12 can tail a
  running one.

Failure semantics: every refusal happens before spend (`candidates.py`); a budget breach halts and
keeps what it bought (§7.5); a substitute model answering is contamination and never scores (§7.7).
Nothing here writes to a bot database or constructs a venue broker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from decision_lab.corpus import Corpus, corpus_dir
from decision_lab.records import CycleRecord
from tradebot.core.config import PanelConfig
from tradebot.core.decision import Decision, SeatResponse
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime

CACHE_DIR: Final = "cache"
SWEEP_META: Final = "sweep.json"


class SweepRow(DomainModel):
    """One candidate's answer for one corpus entry."""

    cycle_id: str
    as_of: UtcDatetime
    decisions: tuple[Decision, ...] = ()
    responses: tuple[SeatResponse, ...] = ()
    cost_usd: Money = ZERO
    #: Seats whose answer came from a binding other than their primary (§7.7). Non-empty means the
    #: whole cycle is contaminated — the peers read this seat's arguments and its vote reached
    #: `reach_consensus`, so no part of the decision measures the configured panel.
    substitutes: tuple[str, ...] = ()
    #: A deliberation that raised. Recorded and counted, exactly as `ShadowEvaluator` does: a
    #: candidate that silently stopped being evaluated would leave a comparison built on fewer
    #: cycles than it claims.
    error: str = ""

    @property
    def contaminated(self) -> bool:
        return bool(self.substitutes)


def sweep_dir(corpus_id: str, matrix_digest: str, *, workspace: Path | None = None) -> Path:
    return corpus_dir(corpus_id, workspace=workspace) / f"sweep-{matrix_digest}"


def cache_dir(corpus_id: str, *, workspace: Path | None = None) -> Path:
    return corpus_dir(corpus_id, workspace=workspace) / CACHE_DIR


def rows_path(
    corpus_id: str, matrix_digest: str, candidate_id: str, *, workspace: Path | None = None
) -> Path:
    safe = candidate_id.replace("/", "_").replace("\\", "_")
    return sweep_dir(corpus_id, matrix_digest, workspace=workspace) / f"{safe}.jsonl"


def read_rows(path: Path) -> dict[str, SweepRow]:
    """Every row already bought, keyed by cycle. An absent file is an empty sweep, not an error."""
    if not path.is_file():
        return {}
    rows: dict[str, SweepRow] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = SweepRow.model_validate_json(line)
            rows[row.cycle_id] = row
    return rows


def append_row(path: Path, row: SweepRow) -> None:
    """Append one result. Written as it is produced, so a killed process keeps what it paid for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(row.model_dump_json() + "\n")


def cache_key(snapshot_digest: str, panel_digest: str) -> str:
    """§7.4. The evidence and the panel — the two things that determine the answer, and nothing
    else, which is what makes the cache shareable across matrices."""
    return hashlib.blake2s(f"{snapshot_digest}|{panel_digest}".encode(), digest_size=16).hexdigest()


def cache_read(corpus_id: str, key: str, *, workspace: Path | None = None) -> SweepRow | None:
    path = cache_dir(corpus_id, workspace=workspace) / f"{key}.json"
    if not path.is_file():
        return None
    return SweepRow.model_validate_json(path.read_text(encoding="utf-8"))


def cache_write(corpus_id: str, key: str, row: SweepRow, *, workspace: Path | None = None) -> None:
    directory = cache_dir(corpus_id, workspace=workspace)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.json").write_text(row.model_dump_json(), encoding="utf-8")


def substitutes_in(responses: Sequence[SeatResponse], panel: PanelConfig) -> tuple[str, ...]:
    """Seats that answered on something other than their primary binding (§7.7).

    Reads `SeatResponse.fingerprint`, which is the binding that actually answered after any
    fallback — the same field §9.7's fallback rate is computed from.
    """
    primary = {seat.seat_id: seat.primary.fingerprint for seat in panel.seats}
    found = {
        f"{r.seat_id}: {primary[r.seat_id]} -> {r.fingerprint}"
        for r in responses
        if r.seat_id in primary and r.fingerprint != primary[r.seat_id]
    }
    return tuple(sorted(found))


def records_from_rows(corpus: Corpus, rows: Mapping[str, SweepRow]) -> tuple[CycleRecord, ...]:
    """Fold a candidate's rows onto the corpus's frozen snapshots (§3).

    This is the whole reason slice C adds no scoring code: what comes out is exactly the
    `CycleRecord` slice B's `score_records` and `score_seats` already read.

    A contaminated or failed row yields no record at all — never a record with an empty decision
    list, which would score as a cycle the panel answered by saying nothing (§7.7).
    """
    by_cycle = {entry.cycle_id: entry for entry in corpus.entries}
    return tuple(
        CycleRecord(
            cycle_id=row.cycle_id,
            basket_id=by_cycle[row.cycle_id].basket_id,
            as_of=row.as_of,
            snapshot=by_cycle[row.cycle_id].snapshot,
            decisions=row.decisions,
            responses=row.responses,
            cost_usd=row.cost_usd,
        )
        for row in (rows[e.cycle_id] for e in corpus.entries if e.cycle_id in rows)
        if not row.contaminated and not row.error
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sweep_storage.py -q`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/sweep.py decision_lab/tests/test_sweep_storage.py
git commit -m "feat(decision_lab): sweep rows, the shared cache, and the fold back into CycleRecord"
```

---

### Task 6: The run loop — deliberate, budget, and the no-substitute rule

**Files:**
- Modify: `decision_lab/sweep.py`
- Test: `decision_lab/tests/test_sweep_run.py`

**Interfaces:**
- Consumes: Task 2's `Candidate`, `Matrix`, `SweepPolicy`; Task 4's `Sample`; Task 5's storage functions; `tradebot.decision.engine.DecisionEngine`, `tradebot.decision.seat.SeatRunner`, `tradebot.decision.providers.registry.build_providers`, `tradebot.core.clock.Clock`.
- Produces:
  - `class SweepStatus(StrEnum)`: `OK`, `PROVIDER_UNAVAILABLE`, `HALTED_FALLBACK`, `HALTED_BUDGET`
  - `class DeliberatingEngine(Protocol)` with `async def deliberate(snapshot, basket) -> PanelOutcome`
  - `EngineFor = Callable[[Candidate], DeliberatingEngine]`
  - `class SweepResult(DomainModel)`: `corpus_id`, `matrix_digest`, `status`, `evaluation`, `on_fallback`, `spent_usd`, `budget_usd`, `evaluated`, `cached`, `contaminated`, `failed`, `halted_on`, `sample`, `candidate_ids`
  - `sweep.engine_from_pool(clock: Clock) -> EngineFor`
  - `sweep.run(corpus, matrix, *, sample, clock, budget_usd, workspace=None, engine_for=None) -> SweepResult` (awaitable)
  - `sweep.write_meta(result, *, workspace=None) -> Path`
  - `sweep.read_meta(corpus_id, matrix_digest, *, workspace=None) -> SweepResult | None`
  - `sweep.latest_meta(corpus_id, *, workspace=None) -> SweepResult | None` — the single sweep under a corpus, or `None` when there are none or several; `report --matrix` is how a reader picks between two

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_sweep_run.py`:

```python
"""The run loop: cache, budget, and the rule that a substitute model stops the measurement.

§7.7 is the file's centre. A seat answering on its backup produced a *different panel's* answer in
a row labelled with the configured seat's name, so it can never be scored — and under the default
policy it stops the sweep, because the alternative is an operator reading a ranking built on a
panel that was never configured.

The engine is injected, so nothing here reaches a network: `engine_for` hands the loop a scripted
stand-in, exactly as `BacktestHarness` takes its dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab import sweep
from decision_lab.sampling import Sample
from decision_lab.tests.factories import corpus_with_entries
from decision_lab.tests.test_candidates import reference, write
from decision_lab.tests.test_sweep_storage import response
from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision, Deliberation, PanelOutcome
from tradebot.core.enums import Action

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)

MATRIX = """
[[candidates]]
id = "baseline"
providers = ["stub"]

  [[candidates.seats]]
  seat_id = "technical"
  role = "t"
  provider_id = "stub"
  model = "varied-technical"
"""


class ScriptedEngine:
    """A `DecisionEngine` stand-in. Counts calls and answers from a list."""

    def __init__(self, outcomes: list[PanelOutcome | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def deliberate(self, snapshot: object, basket: object) -> PanelOutcome:
        self.calls += 1
        answer = self._outcomes.pop(0) if self._outcomes else _plain()
        if isinstance(answer, Exception):
            raise answer
        return answer


def _plain(provider_id: str = "stub", model: str = "varied-technical") -> PanelOutcome:
    seat = response(provider_id, model, seat_id="technical")
    return PanelOutcome(
        decisions=(Decision(instrument_key="binance:BTC/USDT", action=Action.BUY),),
        deliberations=(
            Deliberation(
                instrument_keys=("binance:BTC/USDT",),
                protocol_id="single_round",
                rounds=1,
                responses=(seat,),
            ),
        ),
    )


def matrix_of(tmp_path: Path, text: str = MATRIX) -> cd.Matrix:
    return cd.load_matrix(write(tmp_path / "m", text), reference=reference())


def sample_of(corpus: object) -> Sample:
    return Sample(cycle_ids=tuple(e.cycle_id for e in corpus.entries))  # type: ignore[attr-defined]


async def _run(corpus, matrix, tmp_path, engine, budget="1"):  # type: ignore[no-untyped-def]
    return await sweep.run(
        corpus,
        matrix,
        sample=sample_of(corpus),
        clock=ManualClock(AS_OF),
        budget_usd=Decimal(budget),
        workspace=tmp_path,
        engine_for=lambda _: engine,
    )


def _rows(corpus, matrix, tmp_path):  # type: ignore[no-untyped-def]
    return sweep.read_rows(
        sweep.rows_path(
            corpus.meta.corpus_id, matrix.matrix_digest, "baseline", workspace=tmp_path
        )
    )


@pytest.mark.asyncio
async def test_a_cached_answer_costs_no_provider_call(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=3, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    first = await _run(corpus, matrix, tmp_path, engine)
    second = await _run(corpus, matrix, tmp_path, engine)

    assert first.evaluated == 3
    assert engine.calls == 3, "the second run answered entirely from what was already bought"
    assert second.evaluated == 0
    assert second.status is sweep.SweepStatus.OK


@pytest.mark.asyncio
async def test_a_substitute_binding_halts_the_sweep_by_default(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=4, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news"), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)

    assert result.status is sweep.SweepStatus.HALTED_FALLBACK
    assert "varied-news" in result.halted_on
    assert engine.calls == 2, "it stopped rather than buying the rest"


@pytest.mark.asyncio
async def test_completed_work_survives_a_halt(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=4, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news")])

    await _run(corpus, matrix, tmp_path, engine)
    kept = _rows(corpus, matrix, tmp_path)

    assert len(kept) == 2, "§7.5/§7.6: a halt never discards what it already bought"
    assert kept["c1"].contaminated is True


@pytest.mark.asyncio
async def test_exclude_carries_on_and_the_contaminated_cycle_never_scores(tmp_path: Path) -> None:
    corpus = corpus_with_entries(count=4, as_of=AS_OF)
    matrix = matrix_of(tmp_path, '[sweep]\non_fallback = "exclude"\n' + MATRIX)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news"), _plain(), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)
    rows = _rows(corpus, matrix, tmp_path)

    assert result.status is sweep.SweepStatus.OK
    assert result.contaminated == 1
    assert len(rows) == 4
    assert len(sweep.records_from_rows(corpus, rows)) == 3, "the contaminated cycle is not scored"


@pytest.mark.asyncio
async def test_the_budget_halts_without_overspending_or_discarding(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=10, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    result = await _run(corpus, matrix, tmp_path, engine, budget="0.025")

    assert result.status is sweep.SweepStatus.HALTED_BUDGET
    assert result.spent_usd <= Decimal("0.03")
    assert result.evaluated >= 2
    assert len(_rows(corpus, matrix, tmp_path)) == result.evaluated


@pytest.mark.asyncio
async def test_a_candidate_that_raises_is_recorded_and_counted(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=3, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), RuntimeError("provider exploded"), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)
    rows = _rows(corpus, matrix, tmp_path)

    assert result.failed == 1
    assert result.status is sweep.SweepStatus.OK, "one bad deliberation is not a failed sweep"
    assert any("provider exploded" in row.error for row in rows.values())
    assert len(sweep.records_from_rows(corpus, rows)) == 2


@pytest.mark.asyncio
async def test_only_the_sampled_entries_are_paid_for(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=10, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    result = await sweep.run(
        corpus,
        matrix,
        sample=Sample(cycle_ids=("c0", "c4")),
        clock=ManualClock(AS_OF),
        budget_usd=Decimal(1),
        workspace=tmp_path,
        engine_for=lambda _: engine,
    )

    assert result.evaluated == 2
    assert engine.calls == 2


@pytest.mark.asyncio
async def test_the_meta_round_trips_and_records_the_run_kind(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=2, as_of=AS_OF), matrix_of(tmp_path)

    result = await _run(corpus, matrix, tmp_path, ScriptedEngine([]))
    sweep.write_meta(result, workspace=tmp_path)

    reread = sweep.read_meta(corpus.meta.corpus_id, matrix.matrix_digest, workspace=tmp_path)
    assert reread is not None
    assert reread.matrix_digest == result.matrix_digest
    assert reread.evaluation is False, "a stub matrix is a plumbing check (§7.2)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sweep_run.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.sweep' has no attribute 'run'`

- [ ] **Step 3: Write the implementation**

Add these imports to the top of `decision_lab/sweep.py`:

```python
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from decision_lab.candidates import Candidate, Matrix, SweepPolicy
from decision_lab.corpus import CorpusEntry
from decision_lab.sampling import Sample
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.decision import PanelOutcome
from tradebot.core.logging import get_logger
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.providers.registry import build_providers
from tradebot.decision.seat import SeatRunner

logger = get_logger("decision_lab.sweep")
```

Append to `decision_lab/sweep.py`:

```python
class SweepStatus(StrEnum):
    """How a sweep ended. Recorded on the §11 row, because a run that produced no number is
    still a fact about the experiment."""

    OK = "ok"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    HALTED_FALLBACK = "halted_fallback"
    HALTED_BUDGET = "halted_budget"


class SweepResult(DomainModel):
    """What one sweep did, and what stopped it."""

    corpus_id: str
    matrix_digest: str
    status: SweepStatus = SweepStatus.OK
    #: False when any candidate binds the stub (§7.2). Carried into every banner and every row.
    evaluation: bool = True
    on_fallback: str = ""
    spent_usd: Money = ZERO
    budget_usd: Money = ZERO
    evaluated: int = 0
    cached: int = 0
    contaminated: int = 0
    failed: int = 0
    #: What the run stopped on — candidate, entry, seat and both bindings, or the ceiling.
    halted_on: str = ""
    sample: Sample = Sample()
    candidate_ids: tuple[str, ...] = ()
    matrix_source: str = ""


class DeliberatingEngine(Protocol):
    """What the loop needs of an engine. A Protocol, so a test drives the real loop offline."""

    async def deliberate(self, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome: ...


#: How the loop obtains an engine for one candidate.
EngineFor = Callable[[Candidate], DeliberatingEngine]


def engine_from_pool(clock: Clock) -> EngineFor:
    """The real engine: one provider pool per candidate, since panels declare their own."""

    def build(candidate: Candidate) -> DeliberatingEngine:
        pool = build_providers(candidate.panel.providers, clock)
        return DecisionEngine(SeatRunner(pool.providers, clock))

    return build


async def run(
    corpus: Corpus,
    matrix: Matrix,
    *,
    sample: Sample,
    clock: Clock,
    budget_usd: Decimal,
    workspace: Path | None = None,
    engine_for: EngineFor | None = None,
) -> SweepResult:
    """Every candidate over every sampled entry, cached, budgeted and resumable (§7.4–§7.7)."""
    build = engine_for or engine_from_pool(clock)
    wanted = [entry for entry in corpus.entries if entry.cycle_id in set(sample.cycle_ids)]
    result = SweepResult(
        corpus_id=corpus.meta.corpus_id,
        matrix_digest=matrix.matrix_digest,
        evaluation=matrix.is_evaluation,
        on_fallback=matrix.on_fallback.value,
        budget_usd=budget_usd,
        sample=sample,
        candidate_ids=tuple(c.candidate_id for c in matrix.candidates),
        matrix_source=str(matrix.source),
    )

    for candidate in matrix.candidates:
        engine = build(candidate)
        path = rows_path(
            corpus.meta.corpus_id,
            matrix.matrix_digest,
            candidate.candidate_id,
            workspace=workspace,
        )
        done = read_rows(path)
        for entry in wanted:
            if entry.cycle_id in done:
                continue

            key = cache_key(entry.snapshot.digest, candidate.panel_digest)
            hit = cache_read(corpus.meta.corpus_id, key, workspace=workspace)
            if hit is not None:
                append_row(path, hit.model_copy(update={"cycle_id": entry.cycle_id}))
                result = result.model_copy(update={"cached": result.cached + 1})
                continue

            if result.spent_usd >= budget_usd:
                return _halt(
                    result,
                    SweepStatus.HALTED_BUDGET,
                    f"the ${budget_usd} ceiling was reached at {candidate.candidate_id} "
                    f"/ {entry.as_of.isoformat()}",
                )

            row = await _evaluate(engine, candidate, entry)
            append_row(path, row)
            cache_write(corpus.meta.corpus_id, key, row, workspace=workspace)
            result = result.model_copy(
                update={
                    "evaluated": result.evaluated + 1,
                    "spent_usd": result.spent_usd + row.cost_usd,
                    "failed": result.failed + int(bool(row.error)),
                    "contaminated": result.contaminated + int(row.contaminated),
                }
            )
            if row.contaminated and matrix.on_fallback is SweepPolicy.HALT:
                return _halt(
                    result,
                    SweepStatus.HALTED_FALLBACK,
                    f"{candidate.candidate_id} at {entry.as_of.isoformat()}: "
                    + "; ".join(row.substitutes),
                )
    return result


def _halt(result: SweepResult, status: SweepStatus, reason: str) -> SweepResult:
    """Stop, keeping everything already appended. §7.5: never overspend, never discard."""
    logger.warning("sweep halted", extra={"status": status.value, "reason": reason})
    return result.model_copy(update={"status": status, "halted_on": reason})


async def _evaluate(
    engine: DeliberatingEngine, candidate: Candidate, entry: CorpusEntry
) -> SweepRow:
    """One candidate on one frozen snapshot.

    Never raises, exactly as `ShadowEvaluator` never does: a candidate that silently stopped being
    evaluated would leave a comparison built on fewer cycles than it claims (§15).
    """
    try:
        outcome = await engine.deliberate(entry.snapshot, candidate.basket)
    # Deliberately broad, and for the reason above: every failure is written down and counted.
    except Exception as exc:  # noqa: BLE001
        return SweepRow(
            cycle_id=entry.cycle_id, as_of=entry.as_of, error=f"{type(exc).__name__}: {exc}"
        )
    return SweepRow(
        cycle_id=entry.cycle_id,
        as_of=entry.as_of,
        decisions=outcome.decisions,
        responses=outcome.responses,
        cost_usd=outcome.cost_usd,
        substitutes=substitutes_in(outcome.responses, candidate.panel),
    )


def write_meta(result: SweepResult, *, workspace: Path | None = None) -> Path:
    directory = sweep_dir(result.corpus_id, result.matrix_digest, workspace=workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SWEEP_META
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_meta(
    corpus_id: str, matrix_digest: str, *, workspace: Path | None = None
) -> SweepResult | None:
    path = sweep_dir(corpus_id, matrix_digest, workspace=workspace) / SWEEP_META
    if not path.is_file():
        return None
    return SweepResult.model_validate_json(path.read_text(encoding="utf-8"))


def latest_meta(corpus_id: str, *, workspace: Path | None = None) -> SweepResult | None:
    """The one sweep under this corpus, or `None`. Refuses to guess between two (§14).

    `report --matrix` is how a reader picks when more than one has run; choosing for them would
    silently rank one experiment's candidates on another's page.
    """
    directory = corpus_dir(corpus_id, workspace=workspace)
    found = sorted(directory.glob(f"sweep-*/{SWEEP_META}")) if directory.is_dir() else []
    if len(found) != 1:
        return None
    return SweepResult.model_validate_json(found[0].read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_sweep_run.py -q`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/sweep.py decision_lab/tests/test_sweep_run.py
git commit -m "feat(decision_lab): the sweep loop — cache, budget, and a substitute model stops it"
```

---

### Task 7: The results registry

**Files:**
- Create: `decision_lab/registry.py`
- Test: `decision_lab/tests/test_registry.py`

**Interfaces:**
- Consumes: `decision_lab.params.workspace_root`, `tradebot.core.schema.{DomainModel, Money, UtcDatetime}`.
- Produces:
  - `REGISTRY_FILE: Final = "registry.jsonl"`
  - `class RunRow(DomainModel)` with the fields listed in the implementation; property `identity -> str`
  - `registry.run_id(**parts: str) -> str`
  - `registry.registry_path(*, workspace=None) -> Path`
  - `registry.record(row: RunRow, *, workspace=None) -> None`
  - `registry.read_all(*, workspace=None) -> tuple[RunRow, ...]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_registry.py`:

```python
"""Every run leaves a row, including the ones that produced no number (spec §11).

Identity is the whole design. Identical parameters *update* the row, so a re-run never duplicates;
any changed parameter creates a new one, so a changed prompt never silently overwrites the result
it should be compared against. Two rows on screen are always two genuinely different experiments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import registry

AT = datetime(2026, 8, 30, tzinfo=UTC)


def row(**overrides: object) -> registry.RunRow:
    base: dict[str, object] = dict(
        recorded_at=AT,
        scenario="sweep",
        status="ok",
        evaluation=True,
        on_fallback="halt",
        dataset_digest="d1",
        corpus_id="c1",
        matrix_digest="m1",
        dayset_digest="p1",
        candidate_id="baseline",
        cadence_seconds=28800,
        sample_seed=20260823,
    )
    base.update(overrides)
    return registry.RunRow(**base)  # type: ignore[arg-type]


def test_identical_parameters_update_rather_than_duplicate(tmp_path: Path) -> None:
    registry.record(row(scored=100), workspace=tmp_path)
    registry.record(row(scored=140, accuracy=Decimal("0.42")), workspace=tmp_path)

    rows = registry.read_all(workspace=tmp_path)

    assert len(rows) == 1
    assert rows[0].scored == 140
    assert rows[0].accuracy == Decimal("0.42")


def test_a_changed_parameter_is_a_new_row(tmp_path: Path) -> None:
    registry.record(row(), workspace=tmp_path)
    registry.record(row(matrix_digest="m2"), workspace=tmp_path)

    assert len(registry.read_all(workspace=tmp_path)) == 2


def test_the_policy_and_the_run_kind_do_not_change_identity(tmp_path: Path) -> None:
    """§7.7: `on_fallback` changes when a run stops, never what it produces."""
    registry.record(row(), workspace=tmp_path)
    registry.record(row(on_fallback="exclude", evaluation=False), workspace=tmp_path)

    rows = registry.read_all(workspace=tmp_path)
    assert len(rows) == 1
    assert rows[0].on_fallback == "exclude"


def test_slice_d_fields_are_in_the_identity_from_the_start(tmp_path: Path) -> None:
    """Empty for a sweep, so slice D lands without renumbering rows already written."""
    registry.record(row(), workspace=tmp_path)
    registry.record(
        row(scenario="long", start_equity=Decimal(1000), window="6m"), workspace=tmp_path
    )

    assert len(registry.read_all(workspace=tmp_path)) == 2


def test_a_refused_run_is_recorded_with_its_reason(tmp_path: Path) -> None:
    registry.record(
        row(status="provider_unavailable", note="baseline: OPENROUTER_API_KEY is not set"),
        workspace=tmp_path,
    )

    stored = registry.read_all(workspace=tmp_path)[0]
    assert stored.status == "provider_unavailable"
    assert stored.scored == 0
    assert "OPENROUTER_API_KEY" in stored.note


def test_reading_an_absent_registry_is_empty(tmp_path: Path) -> None:
    assert registry.read_all(workspace=tmp_path) == ()


def test_rows_keep_the_order_they_were_first_written_in(tmp_path: Path) -> None:
    registry.record(row(candidate_id="a"), workspace=tmp_path)
    registry.record(row(candidate_id="b"), workspace=tmp_path)
    registry.record(row(candidate_id="a", scored=9), workspace=tmp_path)

    assert [r.candidate_id for r in registry.read_all(workspace=tmp_path)] == ["a", "b"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.registry'`

- [ ] **Step 3: Write the implementation**

Create `decision_lab/registry.py`:

```python
"""Every run, kept (spec §11).

The answer to "compare and find the most efficient setup": append-only, a flat file a notebook can
read, and §12 renders it. Identity is what makes it work — identical parameters update the row, so
a re-run never duplicates; any changed parameter creates a new one, so a changed prompt never
silently overwrites the result it should be compared against.

`run_id` is computed over the **full** field set from the start, with §10's `scenario`,
`start_equity` and `window` empty for a sweep, so slice D lands without renumbering rows already
written. `on_fallback` and `evaluation` are deliberately *not* in it: neither changes what a run
produces (§7.7, §7.2), and a row that split on them would show one experiment as two.

Rows are never deleted by the tool. `--prune` is an operator act naming what it removes, in the
spirit of the bot's own rule that deletion is the one irreversible step (ADR 0028).

Failure semantics: an absent registry reads as no runs, never as an error. Recording rewrites the
whole file, which is safe because a sweep is a single process and nothing else holds it open.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from decision_lab.params import workspace_root
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime

REGISTRY_FILE: Final = "registry.jsonl"


class RunRow(DomainModel):
    """One experiment: every parameter that identifies it, and what it produced."""

    run_id: str = ""
    recorded_at: UtcDatetime

    # --- Identity (§11). Everything here feeds `run_id`.
    scenario: str = "sweep"
    dataset_digest: str = ""
    corpus_id: str = ""
    matrix_digest: str = ""
    dayset_digest: str = ""
    candidate_id: str = ""
    cadence_seconds: int = 0
    #: Slice D (§10). Empty for a sweep, and in the identity from the start so slice D's rows do
    #: not renumber these.
    start_equity: Money = ZERO
    window: str = ""
    sample_seed: int = 0

    # --- Recorded, but not identity.
    status: str = "ok"
    #: False when any candidate bound the stub — that run measured canned JSON (§7.2).
    evaluation: bool = True
    on_fallback: str = ""
    note: str = ""

    # --- Headline metrics, filled by `report` once the run is scored.
    scored: int = 0
    accuracy: Money = ZERO
    precision_on_action: Money = ZERO
    contaminated: int = 0
    cost_usd: Money = ZERO

    @property
    def identity(self) -> str:
        return run_id(
            scenario=self.scenario,
            dataset_digest=self.dataset_digest,
            corpus_id=self.corpus_id,
            matrix_digest=self.matrix_digest,
            dayset_digest=self.dayset_digest,
            candidate_id=self.candidate_id,
            cadence=str(self.cadence_seconds),
            start_equity=str(self.start_equity),
            window=self.window,
            sample_seed=str(self.sample_seed),
        )


def run_id(**parts: str) -> str:
    """§11's identity. Sorted by key, so the caller's argument order cannot change it."""
    payload = "|".join(f"{key}={parts[key]}" for key in sorted(parts))
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def registry_path(*, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / REGISTRY_FILE


def read_all(*, workspace: Path | None = None) -> tuple[RunRow, ...]:
    path = registry_path(workspace=workspace)
    if not path.is_file():
        return ()
    return tuple(
        RunRow.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def record(row: RunRow, *, workspace: Path | None = None) -> None:
    """Append, or replace the row with this identity in place (§11)."""
    stamped = row.model_copy(update={"run_id": row.identity})
    existing = list(read_all(workspace=workspace))
    for index, present in enumerate(existing):
        if present.run_id == stamped.run_id:
            existing[index] = stamped
            break
    else:
        existing.append(stamped)

    path = registry_path(workspace=workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(entry.model_dump_json() for entry in existing) + "\n", encoding="utf-8"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_registry.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/registry.py decision_lab/tests/test_registry.py
git commit -m "feat(decision_lab): the results registry, keyed so a re-run updates and a change appends"
```

---

### Task 8: Cross-candidate — the ranking and the agreement matrix

**Files:**
- Create: `decision_lab/compare.py`
- Test: `decision_lab/tests/test_compare.py`

**Interfaces:**
- Consumes: `decision_lab.scoring.{ScoredDecision, RegimeMetrics, by_regime, ratio}`, `decision_lab.calibration_days.Pool`.
- Produces:
  - `class Ranked(DomainModel)`: `regime`, `candidate_id`, `scored`, `accuracy`, `action_rate`, `precision_on_action`, `mean_conviction_gap`, `regret_per_decision`, `degradation_rate`, `cost_usd`, `cost_per_scored`
  - `class Agreement(DomainModel)`: `regime`, `left`, `right`, `compared`, `agreed`, `rate`, `tradable_divergences`
  - `compare.metrics_by_candidate(by_candidate) -> dict[str, tuple[RegimeMetrics, ...]]`
  - `compare.ranking(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Ranked, ...]`
  - `compare.agreement(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Agreement, ...]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_compare.py`:

```python
"""§9.6: two candidates agreeing 98% of the time are one experiment run twice.

The agreement matrix is pairwise *per regime*, because two panels can agree completely in quiet
markets and diverge entirely in a crash — which is exactly the case an operator is choosing
between them for. Tradable divergence is the disagreement that moves money: where exactly one of
them asked for an order. It reuses `asked_for_an_order`, which `scoring.py` sets from
`Action.is_tradable` — the same enum property `validation/comparison.py` pairs on — rather than a
second definition of "would have traded".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import compare
from decision_lab.calibration_days import Pool
from decision_lab.scoring import ScoredDecision, Verdict
from tradebot.core.enums import Action

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)


def decision(
    cycle: str, action: Action, *, regime: Pool = Pool.NORMAL, verdict: Verdict = Verdict.CORRECT
) -> ScoredDecision:
    return ScoredDecision(
        cycle_id=cycle,
        as_of=AS_OF,
        instrument_key="binance:BTC/USDT",
        regime=regime,
        action=action,
        conviction=Decimal("0.6"),
        asked_for_an_order=action.is_tradable,
        holding=False,
        verdict=verdict,
    )


def test_two_identical_candidates_agree_completely() -> None:
    rows = [decision("c1", Action.BUY), decision("c2", Action.WAIT)]

    normal = [r for r in compare.agreement({"a": rows, "b": list(rows)}) if r.regime == "NORMAL"]

    assert len(normal) == 1
    assert normal[0].rate == Decimal(1)
    assert normal[0].tradable_divergences == 0


def test_a_disagreement_that_moves_money_is_counted_apart() -> None:
    left = [decision("c1", Action.BUY), decision("c2", Action.HOLD)]
    right = [decision("c1", Action.WAIT), decision("c2", Action.SELL)]

    normal = [r for r in compare.agreement({"a": left, "b": right}) if r.regime == "NORMAL"][0]

    assert normal.agreed == 0
    assert normal.tradable_divergences == 1, "c1 only: BUY vs WAIT. c2 is SELL vs HOLD — both act"


def test_agreement_is_reported_per_regime_and_never_pooled() -> None:
    left = [decision("c1", Action.BUY), decision("c2", Action.BUY, regime=Pool.SHOCK_DOWN)]
    right = [decision("c1", Action.BUY), decision("c2", Action.WAIT, regime=Pool.SHOCK_DOWN)]

    found = {row.regime: row for row in compare.agreement({"a": left, "b": right})}

    assert found["NORMAL"].rate == Decimal(1)
    assert found["SHOCK_DOWN"].rate == Decimal(0)


def test_only_cycles_both_candidates_answered_are_compared() -> None:
    left = [decision("c1", Action.BUY), decision("c2", Action.BUY)]
    right = [decision("c1", Action.BUY)]

    normal = [r for r in compare.agreement({"a": left, "b": right}) if r.regime == "NORMAL"][0]

    assert normal.compared == 1, "a cycle one candidate never answered is not a disagreement"


def test_each_pair_is_reported_once() -> None:
    rows = [decision("c1", Action.BUY)]

    pairs = {
        (row.left, row.right)
        for row in compare.agreement({"a": rows, "b": list(rows), "c": list(rows)})
    }

    assert pairs == {("a", "b"), ("a", "c"), ("b", "c")}


def test_the_ranking_orders_by_accuracy_within_a_regime() -> None:
    good = [decision(f"c{i}", Action.BUY) for i in range(4)]
    bad = [decision(f"c{i}", Action.BUY, verdict=Verdict.WRONG) for i in range(4)]

    ranked = [r for r in compare.ranking({"weak": bad, "strong": good}) if r.regime == "NORMAL"]

    assert [row.candidate_id for row in ranked] == ["strong", "weak"]
    assert ranked[0].accuracy == Decimal(1)
    assert ranked[1].accuracy == Decimal(0)


def test_the_ranking_always_renders_every_regime_even_when_empty() -> None:
    rows = [decision("c1", Action.BUY)]

    regimes = {row.regime for row in compare.ranking({"a": rows})}

    assert {"NORMAL", "SHOCK_UP", "SHOCK_DOWN"} <= regimes, (
        "an absent SHOCK_DOWN row reads as *not measured*, the opposite of *never happened*"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_compare.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.compare'`

- [ ] **Step 3: Write the implementation**

Create `decision_lab/compare.py`:

```python
"""Candidate against candidate (spec §9.6).

§9.5 answers "how good is this panel". This answers the question a sweep exists for: *are these
two panels actually different, and which is better where?*

Two tables:

* **the ranking** — every candidate's §9.5 metrics, per regime, ordered by accuracy. Every regime
  is always rendered and `SHOCK_UP`/`SHOCK_DOWN` are never pooled (§8.3): an absent `SHOCK_DOWN`
  row reads as *not measured*, which is the opposite of *never happened*.
* **the agreement matrix** — pairwise, per regime. Two candidates agreeing 98% of the time are one
  experiment run twice, and an operator paying for both should be told so. Beside it, **tradable
  divergence**: the cycles where exactly one asked for an order, which is the disagreement that
  moves money.

Nothing here re-scores anything. `by_regime` is slice B's own fold, called once per candidate, and
`asked_for_an_order` is what `scoring.py` already stored from `Action.is_tradable` — the same enum
property the bot's `validation/comparison.py` pairs on (§2.4).

Failure semantics: only cycles *both* candidates answered are compared — a cycle one of them never
reached is not a disagreement. Nothing here performs I/O.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence

from decision_lab.scoring import RegimeMetrics, ScoredDecision, by_regime, ratio
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money


class Ranked(DomainModel):
    """One candidate's standing in one regime."""

    regime: str
    candidate_id: str
    scored: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_gap: Money = ZERO
    regret_per_decision: Money = ZERO
    degradation_rate: Money = ZERO
    cost_usd: Money = ZERO
    cost_per_scored: Money = ZERO


class Agreement(DomainModel):
    """How often two candidates said the same thing, and how often it mattered."""

    regime: str
    left: str
    right: str
    compared: int = 0
    agreed: int = 0
    rate: Money = ZERO
    #: Cycles where exactly one asked for an order — the disagreement that moves money (§9.6).
    tradable_divergences: int = 0


def metrics_by_candidate(
    by_candidate: Mapping[str, Sequence[ScoredDecision]],
) -> dict[str, tuple[RegimeMetrics, ...]]:
    """Slice B's own per-regime fold, once per candidate. No second scorer (§2.4)."""
    return {name: by_regime(rows) for name, rows in by_candidate.items()}


def ranking(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Ranked, ...]:
    """Every candidate, every regime, ordered by accuracy within each regime."""
    folded = metrics_by_candidate(by_candidate)
    regimes: list[str] = []
    for rows in folded.values():
        regimes += [row.regime for row in rows if row.regime not in regimes]

    ranked: list[Ranked] = []
    for regime in regimes:
        rows = [
            Ranked(
                regime=regime,
                candidate_id=name,
                scored=metrics.scored,
                accuracy=metrics.accuracy,
                action_rate=metrics.action_rate,
                precision_on_action=metrics.precision_on_action,
                mean_conviction_gap=metrics.mean_conviction_gap,
                regret_per_decision=metrics.regret_per_decision,
                degradation_rate=metrics.degradation_rate,
                cost_usd=metrics.cost_usd,
                cost_per_scored=metrics.cost_per_scored,
            )
            for name, metrics_rows in folded.items()
            for metrics in metrics_rows
            if metrics.regime == regime
        ]
        ranked += sorted(rows, key=lambda row: (-row.accuracy, row.candidate_id))
    return tuple(ranked)


def agreement(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Agreement, ...]:
    """Pairwise agreement per regime, each pair reported once."""
    indexed = {
        name: {(row.cycle_id, row.instrument_key): row for row in rows}
        for name, rows in by_candidate.items()
    }
    regimes: list[str] = []
    for rows in by_candidate.values():
        for row in rows:
            if row.regime.value not in regimes:
                regimes.append(row.regime.value)
            if row.window_name and row.window_name not in regimes:
                regimes.append(row.window_name)

    found: list[Agreement] = []
    for regime in regimes:
        for left, right in itertools.combinations(sorted(indexed), 2):
            shared = [
                (indexed[left][key], indexed[right][key])
                for key in indexed[left]
                if key in indexed[right] and _in_regime(indexed[left][key], regime)
            ]
            agreed = sum(1 for a, b in shared if a.action is b.action)
            divergent = sum(
                1 for a, b in shared if a.asked_for_an_order is not b.asked_for_an_order
            )
            found.append(
                Agreement(
                    regime=regime,
                    left=left,
                    right=right,
                    compared=len(shared),
                    agreed=agreed,
                    rate=ratio(agreed, len(shared)),
                    tradable_divergences=divergent,
                )
            )
    return tuple(found)


def _in_regime(row: ScoredDecision, regime: str) -> bool:
    """A named window is its own row *and* keeps its automatic label's row (§8.2, §8.3)."""
    return row.regime.value == regime or row.window_name == regime
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_compare.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add decision_lab/compare.py decision_lab/tests/test_compare.py
git commit -m "feat(decision_lab): the candidate ranking and the pairwise agreement matrix"
```

---

### Task 9: The report grows its cross-candidate sections

**Files:**
- Modify: `decision_lab/render.py`
- Test: `decision_lab/tests/test_render_sweep.py`

**Interfaces:**
- Consumes: Task 8's `Ranked`, `Agreement`; Task 4's `Sample`; `decision_lab.seats.SeatMetrics`.
- Produces:
  - `PLUMBING_CHECK: Final` — the banner
  - `class CandidateSeats(DomainModel)`: `candidate_id: str`, `seats: tuple[SeatMetrics, ...]`
  - `LabReport` gains: `plumbing_check: bool = False`, `matrix_digest: str = ""`, `matrix_source: str = ""`, `on_fallback: str = ""`, `sweep_status: str = ""`, `halted_on: str = ""`, `sample: Sample | None = None`, `budget_usd: Money = ZERO`, `spent_usd: Money = ZERO`, `contaminated: int = 0`, `ranking: tuple[Ranked, ...] = ()`, `agreement: tuple[Agreement, ...] = ()`, `candidate_seats: tuple[CandidateSeats, ...] = ()`
  - `report_markdown` renders `## Candidates, by regime`, `### Agreement` and one `### Seats — <candidate_id>` block per candidate, **only** when `ranking` is non-empty

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_render_sweep.py`:

```python
"""The report grows a comparison; it does not become a second report (spec §14).

One command, one set of tables, one rendering path. With no sweep the page is exactly what slice B
wrote. With one, the candidate sections appear above the reference pass's own — and if any
candidate bound the stub, the whole page says so at the top, before a reader has formed an opinion
about a number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import render as rd
from decision_lab.compare import Agreement, Ranked
from decision_lab.sampling import Sample
from decision_lab.scoring import ScoringParams

AT = datetime(2026, 8, 30, tzinfo=UTC)


def report(**overrides: object) -> rd.LabReport:
    base: dict[str, object] = dict(
        generated_at=AT,
        corpus_id="c1",
        dataset_directory="data/history",
        dataset_digest="d1",
        reference_instrument="binance:BTC/USDT",
        reference_panel_id="sim",
        reference_config_digest="r1",
        cadence_seconds=28800,
        scoring=ScoringParams(timeframe="1h"),
        vol_window_bars=30,
        shock_percentile=Decimal("0.90"),
        start_equity=Decimal(10000),
    )
    base.update(overrides)
    return rd.LabReport(**base)  # type: ignore[arg-type]


def ranked(candidate_id: str, regime: str = "NORMAL", accuracy: str = "0.5") -> Ranked:
    return Ranked(regime=regime, candidate_id=candidate_id, scored=100, accuracy=Decimal(accuracy))


def test_a_report_with_no_sweep_is_unchanged() -> None:
    text = rd.report_markdown(report())

    assert "## Candidates, by regime" not in text
    assert "## Panel, by regime" in text
    assert rd.PLUMBING_CHECK not in text


def test_the_plumbing_banner_is_at_the_top_and_unconditional() -> None:
    text = rd.report_markdown(report(plumbing_check=True, ranking=(ranked("a"),)))

    assert rd.PLUMBING_CHECK in text
    assert text.index(rd.PLUMBING_CHECK) < text.index("## Experiment")


def test_an_evaluation_carries_no_plumbing_banner() -> None:
    text = rd.report_markdown(report(plumbing_check=False, ranking=(ranked("a"),)))

    assert rd.PLUMBING_CHECK not in text


def test_the_ranking_renders_one_row_per_candidate_per_regime() -> None:
    text = rd.report_markdown(
        report(ranking=(ranked("strong", accuracy="0.6"), ranked("weak", accuracy="0.4")))
    )

    assert "## Candidates, by regime" in text
    assert "| NORMAL | strong |" in text
    assert "| NORMAL | weak |" in text
    assert text.index("strong") < text.index("weak"), "ordered by accuracy"


def test_the_agreement_matrix_renders_when_two_candidates_ran() -> None:
    rows = (
        Agreement(
            regime="NORMAL",
            left="a",
            right="b",
            compared=100,
            agreed=98,
            rate=Decimal("0.98"),
            tradable_divergences=1,
        ),
    )
    text = rd.report_markdown(report(ranking=(ranked("a"), ranked("b")), agreement=rows))

    assert "### Agreement" in text
    assert "98.0%" in text
    assert "one experiment run twice" in text, "the reading, not only the number"


def test_a_halt_is_named_on_the_page() -> None:
    text = rd.report_markdown(
        report(
            ranking=(ranked("a"),),
            matrix_digest="m1",
            sweep_status="halted_fallback",
            halted_on="baseline at 2024-03-01: technical: openrouter:x -> gemini:y",
            on_fallback="halt",
        )
    )

    assert "halted_fallback" in text
    assert "gemini:y" in text


def test_the_sample_and_the_spend_are_on_the_identity_block() -> None:
    text = rd.report_markdown(
        report(
            ranking=(ranked("a"),),
            matrix_digest="m1",
            sample=Sample(
                cycle_ids=("c1",), seed=7, selected={"NORMAL": 1}, available={"NORMAL": 9}
            ),
            budget_usd=Decimal(40),
            spent_usd=Decimal("12.5"),
        )
    )

    assert "| sample seed | 7 |" in text
    assert "| spent | 12.5 |" in text
    assert "| matrix | m1 |" in text
    assert "NORMAL 1/9" in text


def test_contaminated_cycles_are_reported_beside_the_scored_count() -> None:
    text = rd.report_markdown(
        report(ranking=(ranked("a"),), matrix_digest="m1", contaminated=4, on_fallback="exclude")
    )

    assert "4 cycle" in text
    assert "substitute" in text.lower()


def test_one_candidate_says_so_rather_than_printing_an_empty_matrix() -> None:
    text = rd.report_markdown(report(ranking=(ranked("a"),), agreement=()))

    assert "nothing to compare it against" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render_sweep.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.render' has no attribute 'PLUMBING_CHECK'`

- [ ] **Step 3: Add the banner**

In `decision_lab/render.py`, beside the existing banners:

```python
#: §7.2. A run in which any candidate bound the offline stub measured canned JSON. Rendered
#: unconditionally and above the identity block, exactly as the contamination banner is — a
#: reader must meet it before they meet a number.
PLUMBING_CHECK: Final = (
    "**PLUMBING CHECK — NOT AN EVALUATION.** At least one candidate in this run binds the "
    "offline stub, whose votes are drawn from a fixed catalogue. Every table below exercises the "
    "sweep, the scoring and this page; none of it measures any model's judgement. Re-run with a "
    "matrix bound to real providers to learn something."
)
```

- [ ] **Step 4: Add the model and the fields**

Add above `LabReport`:

```python
class CandidateSeats(DomainModel):
    """§9.7's tables, one set per candidate — a seat is only comparable within its own panel."""

    candidate_id: str
    seats: tuple[SeatMetrics, ...] = ()
```

And on `LabReport`, after `seats`:

```python
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
```

- [ ] **Step 5: Render the sections**

In `report_markdown`, replace the banner-and-identity block with:

```python
    if report.plumbing_check:
        sections += ["", PLUMBING_CHECK]
    if report.news_blind:
        sections += ["", NEWS_BLIND]
    sections += ["", _identity(report)]
    if report.ranking:
        sections += [
            "",
            "## Candidates, by regime",
            "",
            _ranking_table(report.ranking),
            "",
            _agreement_table(report.agreement),
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
    return "\n".join(sections).rstrip() + "\n"
```

Add the three renderers beside the existing ones:

```python
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


def _agreement_table(rows: Sequence[Agreement]) -> str:
    if not rows:
        return "Only one candidate ran, so there is nothing to compare it against."
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
```

- [ ] **Step 6: Extend the identity block**

In `_identity`, append these rows when `report.matrix_digest` is set:

```python
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
```

> **Note for the implementer:** `_identity` currently builds its rows inline. Refactor it to accumulate into a `rows: list[tuple[str, str]]` first if it does not already, then render once through `_table`. Keep the existing rows and their order exactly as they are — `test_render.py` asserts them.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render_sweep.py decision_lab/tests/test_render.py -q`
Expected: PASS — the new file, and slice B's `test_render.py` unchanged and still green

- [ ] **Step 8: Commit**

```bash
git add decision_lab/render.py decision_lab/tests/test_render_sweep.py
git commit -m "feat(decision_lab): the report grows its ranking, agreement and per-candidate seats"
```

---

### Task 10: The `sweep` command, and `report` reading a sweep

**Files:**
- Modify: `decision_lab/cli.py`
- Create: `decision_lab/tests/conftest.py`
- Test: `decision_lab/tests/test_cli_sweep.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces:
  - `python -m decision_lab sweep --corpus <id> [--configs PATH] [--budget N] [--full] [--seed N] [--data PATH] [--regimes PATH] [--scoring-timeframe TF] [--verbose]`
  - `report` gains `--matrix <digest>`
  - `COMMANDS` gains `("sweep", "")`; exit codes `EXIT_CANDIDATE` (4) and `EXIT_BUDGET` (5) wired

**Naming hazard:** `cli.py` already imports `calibration_days as cd`. Rename that to `cday` and update its three existing call sites (`dataset_days` twice, `_dayset_digest` once), so `cd` can be `candidates` — the name every other module in this slice uses.

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/conftest.py`:

```python
"""Fixtures shared across the CLI tests.

`built_corpus_id` reuses slice B's own end-to-end builder rather than writing a second one: a
corpus assembled by a different code path would be a corpus these tests agree with and the tool
does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from decision_lab import registry
from decision_lab.tests.test_slice_b_end_to_end import built_corpus


@pytest.fixture
def built_corpus_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A verified dataset, a pinned day set and one reference pass, all under `tmp_path`."""
    monkeypatch.setattr(registry, "workspace_root", lambda: tmp_path / "workspace")
    yield built_corpus(tmp_path, monkeypatch, shock_up=(5,), shock_down=(9,))
```

Create `decision_lab/tests/test_cli_sweep.py`:

```python
"""The `sweep` command's refusals, each with its own exit code (spec §13, §15).

A distinct code per distinct refusal, so a script can tell "you forgot a key" from "you ran out of
budget" without parsing a log line — the convention the bot's own CLI follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab import cli
from decision_lab import registry


def test_an_unreachable_evaluation_exits_4_and_leaves_a_registry_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built_corpus_id: str
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(cd.DEFAULT_MATRIX)])

    assert code == cli.EXIT_CANDIDATE
    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert rows[-1].status == "provider_unavailable"
    assert "OPENROUTER_API_KEY" in rows[-1].note


def test_an_invalid_matrix_exits_4_before_any_spend(tmp_path: Path, built_corpus_id: str) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[[candidates]]\nid = "x"\nqualified_majority = "9"\n\n'
        '  [[candidates.seats]]\n  seat_id = "s"\n  role = "r"\n'
        '  provider_id = "stub"\n  model = "varied-technical"\n',
        encoding="utf-8",
    )

    assert cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(bad)]) == (
        cli.EXIT_CANDIDATE
    )


def test_a_budget_of_zero_exits_5_with_partial_results_written(
    tmp_path: Path, built_corpus_id: str
) -> None:
    code = cli.main(
        ["sweep", "--corpus", built_corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "0"]
    )

    assert code == cli.EXIT_BUDGET


def test_a_plumbing_sweep_runs_and_the_report_says_what_it_was(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert (
        cli.main(
            [
                "sweep",
                "--corpus",
                built_corpus_id,
                "--configs",
                str(cd.STUB_MATRIX),
                "--budget",
                "1",
            ]
        )
        == cli.EXIT_OK
    )

    out = tmp_path / "report.md"
    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "PLUMBING CHECK" in text
    assert "## Candidates, by regime" in text


def test_report_without_a_sweep_still_scores_the_reference_pass(
    tmp_path: Path, built_corpus_id: str
) -> None:
    out = tmp_path / "report.md"

    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert "## Panel, by regime" in text
    assert "## Candidates, by regime" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_sweep.py -q`
Expected: FAIL — argparse exits 2 with `invalid choice: 'sweep'`

- [ ] **Step 3: Rename the import and add the parser**

At the top of `cli.py`, change `from decision_lab import calibration_days as cd` to:

```python
from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import compare as cmp
from decision_lab import registry
from decision_lab import sampling
from decision_lab import sweep as sw
```

…and update the three existing `cd.` call sites in `dataset_days` and `_dayset_digest` to `cday.`.

In `parse_args`, after the `corpus` block:

```python
    sweep_ = commands.add_parser(
        "sweep", help="run every candidate in a matrix over one corpus and record the result"
    )
    sweep_.add_argument("--corpus", required=True, help="corpus id from `corpus build`")
    sweep_.add_argument(
        "--configs",
        type=Path,
        default=cd.DEFAULT_MATRIX,
        help="candidate matrix TOML; defaults to config/sweep.toml, which is an evaluation",
    )
    sweep_.add_argument(
        "--budget", type=_decimal_arg, default=Decimal(0), help="hard USD ceiling for this run"
    )
    sweep_.add_argument("--full", action="store_true", help="every entry, not a sample")
    sweep_.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sweep_.add_argument("--data", type=Path, default=None)
    sweep_.add_argument("--regimes", type=Path, default=None)
    sweep_.add_argument("--scoring-timeframe", default="")
    sweep_.add_argument("--verbose", action="store_true")
```

And on the `report` parser:

```python
    report_.add_argument(
        "--matrix", default="", help="matrix digest, when more than one sweep ran on this corpus"
    )
```

- [ ] **Step 4: Add the sweep handler**

```python
async def sweep_command(args: argparse.Namespace) -> int:
    """Run a matrix over a corpus. Every refusal happens before spend (§7.2)."""
    clock = SystemClock()
    corpus = cp.load(args.corpus)
    data_dir = args.data or Path(corpus.meta.dataset_directory)
    audit = ds.require_verified(data_dir)
    dataset = ReplayDataset.load(data_dir, clock)

    matrix = cd.load_matrix(args.configs, reference=corpus.meta.reference_basket)
    row = _registry_row(corpus, matrix, clock, seed=args.seed)
    try:
        cd.require_reachable(matrix)
    except ConfigError as error:
        registry.record(
            row.model_copy(update={"status": "provider_unavailable", "note": str(error)})
        )
        logger.error("sweep refused; nothing was spent", extra={"reason": str(error)})
        return EXIT_CANDIDATE

    if not matrix.is_evaluation:
        logger.warning(
            "this matrix binds the offline stub, so the run is a plumbing check and measures "
            "no model's judgement",
            extra={"bindings": list(matrix.stub_bindings)},
        )

    timeframe = args.scoring_timeframe or dataset.timeframes[0]
    regime_index = (await rg.index_dataset(dataset, timeframe)).with_windows(
        rg.load_windows(args.regimes or rg.DEFAULT_REGIMES_TOML)
    )
    sample = sampling.stratified(
        corpus,
        regimes=regime_index,
        reference_instrument=dataset.instruments[0].key,
        pinned=_pinned_days(data_dir),
        seed=args.seed,
        full=args.full,
    )

    result = await sw.run(corpus, matrix, sample=sample, clock=clock, budget_usd=args.budget)
    sw.write_meta(result)
    registry.record(
        row.model_copy(
            update={
                "status": result.status.value,
                "evaluation": result.evaluation,
                "on_fallback": result.on_fallback,
                "contaminated": result.contaminated,
                "cost_usd": result.spent_usd,
                "note": result.halted_on,
            }
        )
    )
    logger.info(
        "sweep complete",
        extra={
            "status": result.status.value,
            "candidates": len(matrix.candidates),
            "evaluated": result.evaluated,
            "cached": result.cached,
            "contaminated": result.contaminated,
            "spent": str(result.spent_usd),
        },
    )
    # Both halts keep everything they bought and both are re-runnable; one distinguishes them on
    # the row and in the log, not by the exit code, because to a script the action is the same:
    # fix the cause, run again, and the cache makes the repeat free.
    if result.status in (sw.SweepStatus.HALTED_BUDGET, sw.SweepStatus.HALTED_FALLBACK):
        return EXIT_BUDGET
    _ = audit
    return EXIT_OK


def _registry_row(
    corpus: cp.Corpus, matrix: cd.Matrix, clock: SystemClock, *, seed: int
) -> registry.RunRow:
    """One row per sweep, identified *before* the run so a refusal is recorded too (§11)."""
    return registry.RunRow(
        recorded_at=clock.now(),
        scenario="sweep",
        dataset_digest=corpus.meta.dataset_digest,
        corpus_id=corpus.meta.corpus_id,
        matrix_digest=matrix.matrix_digest,
        dayset_digest=_dayset_digest(Path(corpus.meta.dataset_directory)),
        cadence_seconds=corpus.meta.cadence_seconds,
        sample_seed=seed,
        evaluation=matrix.is_evaluation,
        on_fallback=matrix.on_fallback.value,
    )


def _pinned_days(data_dir: Path) -> tuple[date, ...]:
    """The pinned set if there is one. A sweep does not require it — §15 requires it of a
    *calibration* — but when it exists those days are taken whole (§7.3)."""
    try:
        return cday.require_pinned(data_dir).all_days
    except ConfigError:
        return ()
```

- [ ] **Step 5: Extend `report` to read the sweep beside the reference pass**

In `report`, after `scored = sc.score_records(...)` and before building `LabReport`:

```python
    corpus_obj = cp.load(args.corpus)
    result = (
        sw.read_meta(meta.corpus_id, args.matrix)
        if args.matrix
        else sw.latest_meta(meta.corpus_id)
    )
    ranking: tuple[cmp.Ranked, ...] = ()
    agreement: tuple[cmp.Agreement, ...] = ()
    candidate_seats: tuple[rd.CandidateSeats, ...] = ()
    by_candidate: dict[str, tuple[sc.ScoredDecision, ...]] = {}
    if result is not None:
        matrix = cd.load_matrix(Path(result.matrix_source), reference=meta.reference_basket)
        blocks = []
        for candidate in matrix.candidates:
            rows = sw.read_rows(
                sw.rows_path(meta.corpus_id, result.matrix_digest, candidate.candidate_id)
            )
            records = sw.records_from_rows(corpus_obj, rows)
            candidate_scored = sc.score_records(
                records, index=index, regimes=regime_index, params=params
            )
            by_candidate[candidate.candidate_id] = candidate_scored
            blocks.append(
                rd.CandidateSeats(
                    candidate_id=candidate.candidate_id,
                    seats=st.score_seats(records, candidate_scored, panel=candidate.panel),
                )
            )
        ranking = cmp.ranking(by_candidate)
        agreement = cmp.agreement(by_candidate)
        candidate_seats = tuple(blocks)
```

Pass the new fields into `rd.LabReport(...)`:

```python
        plumbing_check=result is not None and not result.evaluation,
        matrix_digest=result.matrix_digest if result else "",
        matrix_source=result.matrix_source if result else "",
        on_fallback=result.on_fallback if result else "",
        sweep_status=result.status.value if result else "",
        halted_on=result.halted_on if result else "",
        sample=result.sample if result else None,
        budget_usd=result.budget_usd if result else ZERO,
        spent_usd=result.spent_usd if result else ZERO,
        contaminated=result.contaminated if result else 0,
        ranking=ranking,
        agreement=agreement,
        candidate_seats=candidate_seats,
```

And after `rd.write_report(...)`, update each candidate's registry row with its headline metrics:

```python
    for candidate_id, rows_scored in by_candidate.items():
        normal = next((m for m in sc.by_regime(rows_scored) if m.regime == "NORMAL"), None)
        registry.record(
            _registry_row(corpus_obj, matrix, SystemClock(), seed=result.sample.seed).model_copy(
                update={
                    "candidate_id": candidate_id,
                    "status": result.status.value,
                    "scored": normal.scored if normal else 0,
                    "accuracy": normal.accuracy if normal else ZERO,
                    "precision_on_action": normal.precision_on_action if normal else ZERO,
                    "cost_usd": normal.cost_usd if normal else ZERO,
                }
            )
        )
```

Register the command:

```python
COMMANDS: dict[tuple[str, str], Callable[[argparse.Namespace], Coroutine[Any, Any, int]]] = {
    ("dataset", "verify"): dataset_verify,
    ("dataset", "days"): dataset_days,
    ("corpus", "build"): corpus_build,
    ("sweep", ""): sweep_command,
    ("report", ""): report,
}
```

Two things the compiler will catch and the plan should not have left to it:

- `cli.py` does not currently import `ZERO`. Add `from tradebot.core.money import ZERO` beside the
  existing `to_decimal` import.
- The registry-update loop reads `matrix`, which is only bound inside `if result is not None`. It
  is unreachable when `result` is `None` because `by_candidate` is then empty — but mypy will call
  it possibly-undefined and it is genuinely fragile. Initialise `matrix: cd.Matrix | None = None`
  beside `by_candidate`, and guard the loop with `if result is not None and matrix is not None:`.

> **Note for the implementer:** check how `COMMANDS` is keyed for the existing single-word `report` command and follow that exactly — if `report` is keyed `("report", "")` then `sweep` is `("sweep", "")`; if the dispatch uses `getattr(args, "action", "")` then nothing extra is needed. Do not change the dispatch mechanism.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_sweep.py decision_lab/tests/test_cli_dataset.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add decision_lab/cli.py decision_lab/tests/test_cli_sweep.py decision_lab/tests/conftest.py
git commit -m "feat(decision_lab): the sweep command, and report reading the sweep beside it"
```

---

### Task 11: The slice end to end, and the docs

**Files:**
- Create: `decision_lab/tests/test_slice_c_end_to_end.py`
- Modify: `decision_lab/PROGRESS.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything. Nothing new is produced.

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_slice_c_end_to_end.py`:

```python
"""Slice C end to end: corpus → sweep → report → registry, offline and free (spec §16).

The slice's exit criterion, driven through `cli.main` exactly as an operator would — the same
shape slices A and B take, and for the same reason: a handler called directly proves the handler,
while the operator's failure is usually in the wiring between them.

On the stub matrix, so the whole run is a plumbing check and says so — which is itself one of the
things asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import registry
from decision_lab import sweep as sw


def test_a_sweep_ranks_its_candidates_and_files_the_result(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert (
        cli_sweep(built_corpus_id) == 0
    ), "the stub matrix needs no key and reaches no network"

    out = tmp_path / "slice-c.md"
    from decision_lab import cli

    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")

    assert "PLUMBING CHECK — NOT AN EVALUATION" in text
    assert "## Candidates, by regime" in text
    assert "### Agreement" in text
    assert "| SHOCK_DOWN |" in text, "every regime is always rendered (§8.3)"
    assert "### Seats — varied-three~max_rounds=1" in text
    assert "### Seats — varied-three~max_rounds=3" in text


def test_a_second_sweep_buys_nothing_and_the_registry_holds_one_row_per_identity(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert cli_sweep(built_corpus_id) == 0
    digest = _digest(built_corpus_id)
    first = sw.read_meta(built_corpus_id, digest)

    assert cli_sweep(built_corpus_id) == 0
    second = sw.read_meta(built_corpus_id, digest)

    assert first is not None and second is not None
    assert first.evaluated > 0
    assert second.evaluated == 0, "§7.6: an already-complete sweep re-runs nothing"

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert len({row.run_id for row in rows}) == len(rows), "identical parameters update (§11)"


def cli_sweep(corpus_id: str) -> int:
    from decision_lab import cli

    return cli.main(
        ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    )


def _digest(corpus_id: str) -> str:
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=cp.load(corpus_id).meta.reference_basket)
    return matrix.matrix_digest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_slice_c_end_to_end.py -q`
Expected: FAIL until every earlier task is in place

- [ ] **Step 3: Make it pass**

No new implementation should be needed. If it fails, the defect is in Tasks 1–10 — fix it there, not by loosening an assertion here.

- [ ] **Step 4: Run the whole gate**

Run: `.\decision_lab\check.ps1`
Expected: format, lint, mypy and every test pass

Run: `.\check.ps1`
Expected: the root gate is unaffected

Run: `git diff --stat main -- tradebot/`
Expected: **empty**. A non-empty diff means the separation contract was broken — revert that change and find another way.

- [ ] **Step 5: Update the tracker**

In `decision_lab/PROGRESS.md`:

- move slice C's row in the "At a glance" table to ✅ and change its status line from "two slices of five" to three;
- tick all five of slice C's checkboxes;
- add to "What you can run today":

```powershell
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --budget 40   # needs OPENROUTER_API_KEY
```

- replace the "No real panel has ever been scored" bullet with: a sweep against real models is now one command and needs `OPENROUTER_API_KEY`; `sweep-stub.toml` remains a plumbing check and is stamped as one on every page it produces.

- [ ] **Step 6: Update the conventions**

In `CLAUDE.md`, in the `decision_lab` section:

- extend the module tree with `candidates.py`, `sampling.py`, `sweep.py`, `compare.py`, `registry.py` in dependency order, each with its one-line purpose, matching the existing indent style;
- change "five slices, of which **A (integrity, day set, corpus) and B (regimes, scoring, per-seat, report) have shipped**" to include C, and drop C from the "not built" list;
- add these three rules to the "easy to get backwards" list, in the house voice:

> - **A stub binding makes the run a plumbing check, and the *binding* decides — never a flag.** A
>   flag would leave a registry of rows that behaved differently under identical recorded
>   configuration, which is the same argument that keeps `varied-*` in panel data. An evaluation
>   also refuses before spend when any declared key is missing — not merely when a seat is fully
>   silenced, because a partly-reachable seat is one that answers on its backup. Deliberately
>   stricter than ADR 0023: degrade-and-continue is right for a trading system and wrong for a
>   measuring one.
> - **A substitute model is not the panel under test, and it contaminates the whole cycle** — under
>   `blind_then_debate` the peers read its arguments, and under either protocol its vote reaches
>   `reach_consensus`. `on_fallback` (`halt` by default, or `exclude`) decides only whether the run
>   stops; a contaminated decision is never scored either way. An **abstention is not a fallback**:
>   the configured seat answered nothing, `WAIT (PANEL_DEGRADED)` is a real outcome of the real
>   panel, and §9.5 already reports the degradation rate.
> - **The cache is shared across matrices; the result files are not.** The key is
>   `blake2s(snapshot.digest + panel_digest)` — everything that determines the answer and nothing
>   else — so adding one candidate re-pays for none of the others. The `sweep-<matrix_digest>/`
>   directories are scoped, because two matrices both hold a `baseline` and a flat layout would
>   resume one experiment into the other's file.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/tests/test_slice_c_end_to_end.py decision_lab/PROGRESS.md CLAUDE.md
git commit -m "feat(decision_lab): slice C end to end, and the conventions it adds"
```

---

## Self-review notes

Checked against the spec after writing:

- **§7.1** matrix, axes, `matrix_digest` → Task 1, Task 2. **§7.2** validation and the two run kinds → Task 2, Task 3. **§7.3** sampling → Task 4. **§7.4** cache → Task 5. **§7.5** budget → Task 6. **§7.6** resume → Tasks 5, 6. **§7.7** the substitute rule → Tasks 5, 6.
- **§9.6** agreement and tradable divergence → Task 8. **§11** registry → Task 7. **§13** the `sweep` row and exit codes 4/5 → Task 10. **§14** one report command → Tasks 9, 10. **§15** failure semantics → Tasks 2, 6, 10.
- **§16**: matrix expansion and cap (Task 1), cache keys (Task 5), budget accounting (Task 6), registry identity (Task 7), the structural float guard (Task 3), the scenario run (Task 11).

Two spec items are **deliberately** not covered here and belong to slice D, as stated in the scope block: §12's dashboard, which is what §7.6's append-as-you-go exists to enable, and §10's scenarios, whose registry fields Task 7 reserves.

One thing an implementer will hit that the plan cannot settle in advance: `decision_lab/tests/factories.py` needs `snapshot_at` and `corpus_with_entries` (Task 4, Step 3), and the exact constructor arguments for `ContextSnapshot`, `InstrumentContext`, `Quote` and `IndicatorReading` must be read off `tradebot/core/` rather than guessed. Build them the way `tests/unit` already does.
