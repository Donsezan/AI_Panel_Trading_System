"""The import direction is one-way, and asserted rather than intended (spec §2.1).

`decision_lab` imports `tradebot`. Nothing under `tradebot/` may name `decision_lab` — not an
import, not an attribute, not a string. The bot must be buildable, testable and shippable with
this folder deleted, and a boundary that depends on nobody breaking it is not a boundary.

The check is AST-based, so a `#` comment mentioning the tool is fine: comments are not code and
cannot create a dependency. A docstring is a `Constant` and *is* checked, deliberately — a module
docstring explaining what `decision_lab` does belongs in `decision_lab`.

Same class of structural guard as `tests/unit/test_money_discipline.py` and the float boundary in
`tests/unit/test_dashboard_chart.py`: a rule CI can prove, not a review comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tradebot

TOOL = "decision_lab"
BOT_ROOT = Path(tradebot.__file__).parent


def bot_sources() -> list[tuple[Path, ast.Module]]:
    files = sorted(BOT_ROOT.rglob("*.py"))
    assert files, "tradebot package not found — the check would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


SOURCES = bot_sources()


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_bot_module_imports_the_tool(path: Path, tree: ast.Module) -> None:
    """An import is the hard failure: it would make the tool a runtime dependency of the bot."""
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == TOOL]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == TOOL:
            offenders.append(node.module or "")
    assert not offenders, f"{path.name} imports {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_bot_module_names_the_tool(path: Path, tree: ast.Module) -> None:
    """A name or a string is the soft failure, and still a dependency worth refusing."""
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == TOOL)
        or (isinstance(node, ast.Attribute) and node.attr == TOOL)
        or (isinstance(node, ast.Constant) and isinstance(node.value, str) and TOOL in node.value)
    ]
    assert not offenders, f"{path.name} names {TOOL}: {offenders}"


def test_the_guard_can_actually_fail() -> None:
    """A structural test that cannot fail is a comment. Prove the detector works."""
    tree = ast.parse("from decision_lab.corpus import Corpus\n")
    found = [
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == TOOL
    ]
    assert found == ["decision_lab.corpus"]
