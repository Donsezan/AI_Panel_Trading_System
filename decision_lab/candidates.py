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

import itertools
import tomllib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from tradebot.core.errors import ConfigError

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
