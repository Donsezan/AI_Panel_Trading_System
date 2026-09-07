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
A version number is minted by exclusive create rather than by counting what is already there, so
two saves racing take two numbers instead of one of them quietly erasing the other. An absent store
reads as no seat sets, never as an error.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
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


#: `O_EXCL` is the whole mint: it is atomic on both Windows and POSIX, so of two savers that
#: computed the same next number exactly one creates the file. `O_BINARY` matters only on Windows,
#: where `os.open` otherwise honours the C runtime's text mode and rewrites every `\n` in the
#: payload — the version lands with `\r\r\n` endings, a bare CR that `tomllib.loads` refuses
#: outright. There is no such flag on POSIX, where there is nothing to turn off.
_MINT_FLAGS: Final = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


def _mint(name: str, payload: bytes, *, workspace: Path | None) -> int:
    """Claim the lowest free version number, exclusively, and write `payload` into it.

    Not `versions(...)[-1] + 1` followed by a write: nothing excludes a second saver between
    those two halves, and `Path.replace` is a rename, which succeeds onto an existing path by
    design — so two submits (two browser tabs, a double-submit) computing the same number would
    leave one of them silently erased, the one thing §12.3's "every save is a new version and
    nothing is overwritten" exists to rule out. The read is still where the search *starts*, so a
    store holding forty versions does not cost forty failed opens; a number already taken is
    stepped over, which costs a retry and nothing else.
    """
    existing = versions(name, workspace=workspace)
    candidate = (existing[-1] if existing else 0) + 1
    while True:
        try:
            descriptor = os.open(path_for(name, candidate, workspace=workspace), _MINT_FLAGS)
        except FileExistsError:
            candidate += 1
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        return candidate


def save(
    name: str,
    document: Mapping[str, Any],
    *,
    reference: Basket,
    workspace: Path | None = None,
) -> tuple[int, Matrix]:
    """Validate through `load_matrix`, then mint the next version exclusively.

    The draft is written into the seat set's own directory and the *same bytes* are what land in
    the version, so `load_matrix` reads exactly the file the CLI will later be pointed at — a
    validator run against a different rendering of the same document proves nothing about the one
    that was stored.

    The draft is removed on every exit — a refusal, a successful mint, or a filesystem error —
    because a `.draft.toml` left in the store is a half-written matrix sitting beside the real
    ones, and `versions()`'s `path.stem.isdigit()` filter is the only thing keeping it out.
    """
    directory = root(workspace=workspace) / _require_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    payload = dumps(document).encode("utf-8")
    draft = directory / ".draft.toml"
    draft.write_bytes(payload)
    try:
        matrix = load_matrix(draft, reference=reference)
        return _mint(name, payload, workspace=workspace), matrix
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
