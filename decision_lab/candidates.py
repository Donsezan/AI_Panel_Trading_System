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
    """`[expand]` as ordered (kind, name) -> values. `prompts.<seat_id>` varies one seat.

    TOML's dotted-key syntax (`prompts.risk = [...]`) nests rather than naming a literal
    `"prompts.risk"` key, so `[expand]` parses to `{"prompts": {"risk": [...]}, ...}` — the
    `prompts` table is unpacked into one axis per seat before the remaining keys are read as
    plain fields.
    """
    block = document.get("expand", {})
    axes: list[tuple[tuple[str, str], Sequence[Any]]] = []
    prompt_axes = block.get("prompts", {})
    if not isinstance(prompt_axes, Mapping):
        raise ConfigError(
            f"[expand] prompts must be a table of seat -> values, got {prompt_axes!r}"
        )
    for name, values in sorted(prompt_axes.items()):
        if not isinstance(values, list):
            raise ConfigError(f"[expand] prompts.{name} must be a list of values, got {values!r}")
        axes.append((("prompt", name), values))
    for key, values in sorted(block.items()):
        if key in ("limit", "prompts"):
            continue
        if not isinstance(values, list):
            raise ConfigError(f"[expand] {key} must be a list of values, got {values!r}")
        axes.append((("field", key), values))
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

    §7.2 requires a refusal to name "the seat, the binding and the environment variable" — a
    provider id alone does not say *which* seat's vote would be affected, and that is exactly the
    fact an operator needs when only one seat's fallback touches the absent key. `reach_of`
    already separates *degraded* seats (a working binding remains — the substitution §7.2 refuses)
    from *silenced* ones (no binding left — the seat abstains instead); the wording keeps that
    distinction rather than flattening both into one sentence.
    """
    findings: list[str] = []
    for candidate in matrix.candidates:
        reach = reach_of(candidate.panel, environ)
        if not reach.missing:
            continue
        secret_of = {entry.provider_id: entry.secret_ref for entry in reach.missing}
        degraded, silenced = set(reach.degraded), set(reach.silenced)
        named: set[str] = set()
        for seat in candidate.panel.seats:
            if seat.seat_id not in degraded and seat.seat_id not in silenced:
                continue
            consequence = (
                "would answer on its backup" if seat.seat_id in degraded else "would abstain"
            )
            for binding in seat.bindings:
                secret_ref = secret_of.get(binding.provider_id)
                if secret_ref is None:
                    continue
                named.add(binding.provider_id)
                findings.append(
                    f"{candidate.candidate_id}: seat {seat.seat_id!r} binds {binding.fingerprint}, "
                    f"which has no {secret_ref} in the environment — the seat {consequence}"
                )
        # A declared provider no seat binds cannot be attributed to a seat; still block, because
        # nothing would ever exercise it and the fail-closed default is "not a measurement".
        findings += [
            f"{candidate.candidate_id}: {entry}"
            for entry in reach.missing
            if entry.provider_id not in named
        ]
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
            "would substitute a binding or abstain, measuring a panel that was never "
            "configured: " + "; ".join(missing)
        )
