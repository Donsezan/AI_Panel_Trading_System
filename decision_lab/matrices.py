"""Seat sets, versioned, in the workspace (spec §12.3).

The shipped `config/*.toml` stay read-only templates: they are curated, commented and in git, and
a form writer would destroy the first two and dirty the third from a browser click. What the
dashboard saves goes to `workspace/matrices/<name>/<version>.toml`, where every save is a new
version and nothing is overwritten — so the seat set that produced the best row in the registry
can still be opened.

One format serves both front doors. The CLI's `--configs` takes a path, `matrix_digest` is
computed exactly as it is today, and the writer here is the only new code: `tomllib` reads TOML
and does not write it, and §2.1's "no new dependency" is a property worth more than a
dependency-free writer costs. It handles exactly the matrix schema's value kinds and **refuses
anything else** — notably a float, which would be a `SeatConfig.temperature` this package has no
way to carry (see `test_discipline.py`).

Failure semantics: validation is `candidates.load_matrix` itself, run against a temporary file
before any version is minted, so a matrix that will not load leaves the store exactly as it was.
A version number is claimed by exclusive create rather than by counting what is already there,
and the file is published by a single rename, so two saves racing take two numbers and a reader
never opens one half-written. An absent store reads as no seat sets, never as an error.
"""

from __future__ import annotations

import os
import re
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from decision_lab.candidates import Matrix, load_matrix
from decision_lab.params import MATRICES_DIR, workspace_root
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError

#: A seat-set name is a directory name, so it may not traverse, and it is short enough to read in
#: a URL. Refused rather than sanitised: a name silently rewritten is one an operator cannot find
#: again.
NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

CONFIG_DIR: Final = Path(__file__).parent / "config"

__all__ = [
    "MATRICES_DIR",
    "NAME_PATTERN",
    "dumps",
    "names",
    "path_for",
    "read",
    "read_template",
    "root",
    "save",
    "templates",
    "versions",
]


# ---------------------------------------------------------------- the writer


def dumps(document: Mapping[str, Any]) -> str:
    """The matrix schema as TOML text. Scalars, then tables, then arrays of tables."""
    lines: list[str] = []
    _emit(lines, document, prefix=())
    return "\n".join(lines).strip("\n") + "\n"


def _is_table(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_table(one) for one in value)


def _emit(lines: list[str], table: Mapping[str, Any], *, prefix: tuple[str, ...]) -> None:
    """Every scalar first, because a key written after a `[header]` belongs to that header."""
    for key, value in table.items():
        if not _is_table(value) and not _is_table_array(value):
            lines.append(f"{key} = {_scalar(value, key='.'.join((*prefix, key)))}")
    for key, value in table.items():
        path = ".".join((*prefix, key))
        if _is_table(value):
            lines += ["", f"[{path}]"]
            _emit(lines, value, prefix=(*prefix, key))
        elif _is_table_array(value):
            for entry in value:
                lines += ["", f"[[{path}]]"]
                _emit(lines, entry, prefix=(*prefix, key))


def _scalar(value: Any, *, key: str) -> str:
    """`key` is this value's dotted path, carried down from `_emit` for the refusal alone.

    "a matrix holds no value of type float" says nothing about *which* field to fix, and a seat
    set has a dozen fields that could hold one — so the refusal names the path, the way
    `read_document`, `_require_name` and `_basket_for` all name what they are refusing.
    """
    # `bool` before `int`: `isinstance(True, int)` is True, and `max_rounds = true` is nonsense.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(one, key=key) for one in value) + "]"
    raise ConfigError(
        f"{key} is a {type(value).__name__} ({value!r}), which a matrix has no place for. Money "
        "and every ratio is a quoted string, and a seat's temperature is deliberately not "
        "editable here"
    )


#: TOML basic strings require every control character in U+0000-U+001F except tab, plus U+007F
#: (DEL), to be escaped — not merely `\`, `"` and `\n`. `\r` is the one that bites: a seat's
#: `instruction` is browser-textarea text and arrives CRLF, and an unescaped `\r` produces output
#: that `tomllib.loads` itself refuses, so the writer would have produced a file its own reader
#: could not read back.
_NAMED_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _quoted(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        if char in _NAMED_ESCAPES:
            escaped.append(_NAMED_ESCAPES[char])
        elif char == "\x7f" or ord(char) < 0x20:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


# ---------------------------------------------------------------- the store


def root(*, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / MATRICES_DIR


def _require_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ConfigError(
            f"{name!r} is not a usable seat-set name: lowercase letters, digits, '-' and '_', "
            "starting with a letter or digit, at most 64 characters. It becomes a directory and "
            "a URL, so it is refused rather than rewritten into something you cannot find again"
        )
    return name


def names(*, workspace: Path | None = None) -> tuple[str, ...]:
    directory = root(workspace=workspace)
    if not directory.is_dir():
        return ()
    return tuple(sorted(one.name for one in directory.iterdir() if one.is_dir()))


def versions(name: str, *, workspace: Path | None = None) -> tuple[int, ...]:
    directory = root(workspace=workspace) / name
    if not directory.is_dir():
        return ()
    found = []
    for path in directory.glob("*.toml"):
        if path.stem.isdigit():
            found.append(int(path.stem))
    return tuple(sorted(found))


def path_for(name: str, version: int, *, workspace: Path | None = None) -> Path:
    return root(workspace=workspace) / _require_name(name) / f"{version}.toml"


def read(
    name: str, version: int | None = None, *, workspace: Path | None = None
) -> tuple[int, dict[str, Any]]:
    """One stored version, or the latest. The version is returned with it, never inferred later."""
    available = versions(_require_name(name), workspace=workspace)
    if not available:
        raise ConfigError(f"no seat set named {name!r} has been saved")
    chosen = available[-1] if version is None else version
    if chosen not in available:
        raise ConfigError(f"seat set {name!r} has no version {chosen}; it has {list(available)}")
    path = path_for(name, chosen, workspace=workspace)
    return chosen, tomllib.loads(path.read_text(encoding="utf-8"))


#: A version number is *claimed* by creating this marker, and the claim is what excludes a second
#: saver: `O_CREAT | O_EXCL` is atomic on both Windows and POSIX, so of two savers computing the
#: same next number exactly one gets it. It is deliberately not named `<n>.toml` — a marker the
#: `*.toml` glob could reach would be a version number existing before its bytes do, which is the
#: very thing `_mint` publishes by rename to avoid.
_CLAIM_FLAGS: Final = os.O_CREAT | os.O_EXCL | os.O_WRONLY


def _claim(directory: Path, version: int) -> Path:
    return directory / f".claim-{version}.tmp"


def _mint(name: str, draft: Path, *, workspace: Path | None) -> int:
    """Claim the lowest free version number, then publish `draft` into it with one rename.

    There are two races here and the two halves close one each.

    *Writer against writer.* `versions(...)[-1] + 1` followed by a write is a read-then-write with
    nothing excluding a second saver between the halves, and `Path.replace` overwrites by design —
    so two submits (two browser tabs, a double-submit) computing the same number would leave one
    of them silently erased, the one thing §12.3's "every save is a new version and nothing is
    overwritten" exists to rule out. The read is still where the search *starts*, so a store
    holding forty versions does not cost forty failed opens; a number found taken is stepped over,
    which costs a retry and nothing else.

    *Writer against reader.* The publish is a rename, never a create-then-write. `versions()` and
    `read()` glob `*.toml` with no readiness check, so a version that exists before its bytes do
    is one a reader can open half-written — `tomllib` raising `TOMLDecodeError` out of a module
    whose whole contract is to raise `ConfigError`. A rename is atomic, so `N.toml` is never
    partial.

    The marker is released on the way out rather than kept, so the directory does not silt up with
    them — and the target is therefore re-checked *while the claim is held*, because a saver whose
    `versions()` read predates a completed save would find that save's marker already gone and
    rename straight over it. A process that dies mid-save leaves its marker, and that number is
    skipped for good: a gap in the numbering is honest, since a version is an opaque identifier
    and not a count, and reusing the number would mean two different documents both calling
    themselves version 3.
    """
    existing = versions(name, workspace=workspace)
    candidate = (existing[-1] if existing else 0) + 1
    while True:
        # Bound before the loop body touches `candidate`: the release below must name the marker
        # that was taken, not the number the search has moved on to — releasing that one would
        # both strand this claim and free a number another saver is holding.
        claim = _claim(draft.parent, candidate)
        try:
            descriptor = os.open(claim, _CLAIM_FLAGS)
        except FileExistsError:
            candidate += 1
            continue
        os.close(descriptor)
        try:
            target = path_for(name, candidate, workspace=workspace)
            if target.exists():
                candidate += 1
                continue
            draft.replace(target)
        finally:
            claim.unlink(missing_ok=True)
        return candidate


def save(
    name: str,
    document: Mapping[str, Any],
    *,
    reference: Basket,
    workspace: Path | None = None,
) -> tuple[int, Matrix]:
    """Validate through `load_matrix`, then publish that exact file under the next version.

    The draft `load_matrix` reads is the file `_mint` renames into place, so the bytes that were
    validated are the bytes stored — a validator run against a different rendering of the same
    document, or against a file some other save has since rewritten, proves nothing about what the
    CLI will later be pointed at.

    Its name carries a `uuid4` for that second reason: on one shared `.draft.toml`, two saves of
    the same seat set would read each other's bytes, and A could return a `Matrix` describing B's
    document while publishing A's. `.tmp` rather than `.toml`, so a draft is never reachable by
    the `*.toml` glob `versions()` reads, and it is removed on every exit — a refusal, a
    successful publish (where the rename has already taken it) or a filesystem error.

    `source` is re-pointed at the published file: `load_matrix` records the path it read, and a
    returned matrix naming a temporary file that no longer exists is the same misdescription one
    field over.
    """
    directory = root(workspace=workspace) / _require_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    draft = directory / f".draft-{uuid.uuid4().hex}.tmp"
    draft.write_bytes(dumps(document).encode("utf-8"))
    try:
        matrix = load_matrix(draft, reference=reference)
        version = _mint(name, draft, workspace=workspace)
        return version, replace(matrix, source=path_for(name, version, workspace=workspace))
    finally:
        draft.unlink(missing_ok=True)


def templates() -> tuple[str, ...]:
    """The shipped matrices, as starting points. Read-only: nothing here ever writes to them."""
    return tuple(sorted(path.stem for path in CONFIG_DIR.glob("sweep*.toml")))


def read_template(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{_require_name(name)}.toml"
    if not path.is_file():
        raise ConfigError(f"no shipped matrix named {name!r}; there are {list(templates())}")
    return tomllib.loads(path.read_text(encoding="utf-8"))
