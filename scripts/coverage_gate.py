"""Enforce per-package branch-coverage gates from a `coverage json` report.

The money-touching packages are held to a higher bar than the rest, because they are what
stands between a hallucination and an order (PLAN §7). A package below its gate fails the
build; an *unknown* package is held to the default gate rather than silently passing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MONEY_PACKAGES = frozenset({"core", "risk", "execution", "ledger"})
MONEY_GATE = 95.0
DEFAULT_GATE = 80.0


def package_of(path: str) -> str:
    parts = Path(path).as_posix().split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "tradebot" else "tradebot"


def main(report_path: str = "coverage.json") -> int:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    totals: dict[str, list[int]] = {}
    for path, data in report["files"].items():
        summary = data["summary"]
        covered = summary["covered_lines"] + summary["covered_branches"]
        total = summary["num_statements"] + summary["num_branches"]
        bucket = totals.setdefault(package_of(path), [0, 0])
        bucket[0] += covered
        bucket[1] += total

    failures = []
    for package in sorted(totals):
        covered, total = totals[package]
        gate = MONEY_GATE if package in MONEY_PACKAGES else DEFAULT_GATE
        percent = 100.0 * covered / total if total else 100.0
        status = "ok " if percent >= gate else "FAIL"
        print(f"  [{status}] {package:<12} {percent:6.2f}%  (gate {gate:.0f}%)")
        if percent < gate:
            failures.append(f"{package} {percent:.2f}% < {gate:.0f}%")

    if failures:
        print("\ncoverage gate failed: " + "; ".join(failures))
        return 1
    print("\ncoverage gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
