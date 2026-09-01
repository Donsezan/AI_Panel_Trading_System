"""No `float` in `decision_lab` (spec §9.2, §16 structural row).

The bot's own guard walks `core/`, `risk/`, `execution/` and `ledger/`. This package is outside
those, so it asserts the same rule over itself: a band derived from ATR, a realised volatility, a
percentile threshold and a profit figure are all money-path arithmetic, and a `float` in any of
them is the binary rounding error `tradebot.core.money` exists to keep out.

Annotations are checked wherever they appear — a field, an argument, a return — rather than only
on `x: float`, because `def band(k: float) -> float` is the same defect one syntax node over.

Slice A predicted `candidates.py` would need an exemption for `SeatConfig.temperature`, since
`tomllib` parses a TOML float as a Python `float`. It does not: `_panel_for` passes each seat
through as an opaque `dict(seat)`, handed to `Basket.model_validate` for pydantic to coerce, so no
`float` is ever named — as a call or an annotation — anywhere in that module. The exemption set is
empty, and a future entry needs the same standard of justification a real one would have met: a
`float` the package genuinely cannot avoid, never one it merely passes through unread.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import decision_lab

TOOL_ROOT = Path(decision_lab.__file__).parent
#: Modules permitted to name `float`. Empty: `candidates.py` was predicted to need an entry for
#: `SeatConfig.temperature`, but seat documents pass through it as opaque dicts and pydantic does
#: the coercion, so no `float` is ever named there. See the module docstring.
FLOAT_EXEMPT: frozenset[str] = frozenset()


def tool_sources() -> list[tuple[Path, ast.Module]]:
    files = [p for p in sorted(TOOL_ROOT.rglob("*.py")) if "tests" not in p.parts]
    assert files, "decision_lab has no modules — the check would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


SOURCES = tool_sources()


def annotation_nodes(tree: ast.Module) -> Iterator[tuple[int, ast.expr]]:
    """Every annotation expression in the module, with the line it sits on."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield node.lineno, node.annotation
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            declared = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ]
            for argument in declared:
                if argument is not None and argument.annotation is not None:
                    yield argument.lineno, argument.annotation
            if node.returns is not None:
                yield node.lineno, node.returns


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_float_calls(path: Path, tree: ast.Module) -> None:
    if path.name in FLOAT_EXEMPT:
        pytest.skip(f"{path.name} is a declared exemption")
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "float"
    ]
    assert not offenders, f"float() calls: {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_float_annotations(path: Path, tree: ast.Module) -> None:
    if path.name in FLOAT_EXEMPT:
        pytest.skip(f"{path.name} is a declared exemption")
    offenders = [
        f"{path.name}:{lineno}"
        for lineno, annotation in annotation_nodes(tree)
        if any(
            isinstance(inner, ast.Name) and inner.id == "float" for inner in ast.walk(annotation)
        )
    ]
    assert not offenders, f"float annotations: {offenders}"


def test_the_guard_can_actually_fail() -> None:
    """A structural test that cannot fail is a comment. Prove both detectors work."""
    tree = ast.parse("def band(k: float) -> tuple[float, ...]:\n    return (float(k),)\n")

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"
    ]
    floats = [
        lineno
        for lineno, annotation in annotation_nodes(tree)
        if any(isinstance(i, ast.Name) and i.id == "float" for i in ast.walk(annotation))
    ]

    assert len(calls) == 1
    assert floats == [1, 1], "the argument and the return are both annotations"
