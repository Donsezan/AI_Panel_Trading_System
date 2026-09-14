"""§14's second front door, checked without adding a Jupyter dependency.

Nothing here executes the notebook: it needs a kernel, a dataset and a corpus. What is asserted
is what a committed notebook can get wrong on its own — unparseable JSON, a code cell that does
not compile, and stored outputs, which are diff noise at best and a leaked key at worst.
"""

from __future__ import annotations

import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "tuning.ipynb"


def test_the_notebook_is_valid_json_of_the_expected_format() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert document["nbformat"] == 4
    assert document["cells"], "an empty notebook is not a front door"


def test_every_code_cell_compiles() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for index, cell in enumerate(document["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        assert "%" not in source.split("\n")[0][:1], f"cell {index} starts with a magic"
        compile(source, f"{NOTEBOOK.name}:cell{index}", "exec")


def test_no_cell_carries_stored_output() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for index, cell in enumerate(document["cells"]):
        assert not cell.get("outputs"), f"cell {index} has stored output"
        assert cell.get("execution_count") in (None, 0), f"cell {index} has an execution count"


def test_the_notebook_uses_the_same_library_the_page_does() -> None:
    """§14: one implementation, two front doors. A notebook that re-derived would be a third."""
    source = NOTEBOOK.read_text(encoding="utf-8")
    assert "decision_lab.analysis" in source or "from decision_lab import analysis" in source
    assert "score_records" not in source, "scoring belongs behind analysis.analyse_matrix"
