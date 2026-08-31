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
whole file — safe against a concurrent reader or writer because a sweep is a single process and
nothing else holds it open, and safe against a **crash mid-write** because the rewrite lands in a
temporary file in the same directory first and is swapped into place with `os.replace`, atomic on
both Windows and POSIX. Without that, a process killed between the write and the close leaves a
truncated final line, and `read_all`'s `model_validate_json` then raises on every future `sweep`
and `report` until someone hand-edits the file back to valid JSON.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
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
    """Append, or replace the row with this identity in place (§11).

    Written to a temporary file in the registry's own directory, then swapped in with
    `os.replace` — atomic on both Windows and POSIX, so a process dying mid-write leaves the file
    `read_all` already trusts untouched rather than truncated on its final line (see the module
    docstring). The temp file is on the same filesystem by construction (`dir=path.parent`),
    which is what makes the replace atomic rather than a copy that could itself be interrupted.
    """
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
    payload = "\n".join(entry.model_dump_json() for entry in existing) + "\n"
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{REGISTRY_FILE}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        tmp_path.replace(path)  # `Path.replace` is `os.replace`: atomic on Windows and POSIX
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
