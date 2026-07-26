"""Static enforcement of the money-safety rules — the ones discipline alone would not hold.

`ruff` cannot express these (they need type and name awareness), so they are AST checks that
run with the rest of the suite. A violation fails the build, not a review comment (PLAN §2.1).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tradebot.core import money

PACKAGE_ROOT = Path(money.__file__).parent.parent
MONEY_PACKAGES = ("core", "risk", "execution", "ledger")

#: Field-name fragments whose value is money and therefore must be `Decimal`.
MONEY_FIELD_HINTS = (
    "qty",
    "quantity",
    "price",
    "notional",
    "amount",
    "balance",
    "equity",
    "fee",
    "pnl",
    "budget",
    "cost",
    "size",
)
#: Names that read like money but are not: counts, ratios, and venue precision descriptors.
MONEY_FIELD_EXEMPT = frozenset(
    {"size_hint", "digest_size", "batch_size", "pnl_pct", "price_source", "cost_usd_by_seat"}
)

#: The single sanctioned float→Decimal crossing (`money.from_measurement`).
FLOAT_CROSSING = "from_measurement"


def money_path_sources() -> list[tuple[Path, ast.Module]]:
    files = [
        path
        for package in MONEY_PACKAGES
        for path in (PACKAGE_ROOT / package).rglob("*.py")
        if (PACKAGE_ROOT / package).exists()
    ]
    assert files, "money packages not found — the check would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


SOURCES = money_path_sources()


def enclosing_function(tree: ast.Module, node: ast.AST) -> str | None:
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.FunctionDef) and node in ast.walk(candidate):
            return candidate.name
    return None


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_float_calls_in_money_paths(path: Path, tree: ast.Module) -> None:
    """`float(...)` in a money path reintroduces binary rounding error."""
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "float"
        and enclosing_function(tree, node) != FLOAT_CROSSING
    ]
    assert not offenders, f"float() calls in money paths: {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_money_semantic_fields_are_decimal(path: Path, tree: ast.Module) -> None:
    """A money-named field typed `float` is the rounding bug the ban exists to prevent."""
    offenders = [
        f"{path.name}:{node.lineno} {node.target.id}"
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id not in MONEY_FIELD_EXEMPT
        and any(hint in node.target.id for hint in MONEY_FIELD_HINTS)
        and ast.unparse(node.annotation).replace("None", "").find("float") >= 0
    ]
    assert not offenders, f"money-semantic fields typed float: {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_half_rounding_is_never_referenced_in_code(path: Path, tree: ast.Module) -> None:
    """Half-rounding rounds *up* half the time — the one thing sizing must never do.

    Checked over identifiers rather than raw text, so prose explaining the ban does not
    trip the ban.
    """
    offenders = [
        f"{path.name}:{node.lineno} {node.id}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id.startswith("ROUND_HALF")
    ]
    assert not offenders, f"half-rounding referenced in a money path: {offenders}"


def test_money_context_traps_invalid_operations() -> None:
    """An invalid operation must raise, never return NaN into a price or a size."""
    from decimal import DivisionByZero, InvalidOperation, Overflow

    for trap in (InvalidOperation, DivisionByZero, Overflow):
        assert money.MONEY_CONTEXT.traps[trap]
