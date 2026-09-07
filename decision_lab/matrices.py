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
An absent store reads as no seat sets, never as an error.
"""

from __future__ import annotations

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
            lines.append(f"{key} = {_scalar(value)}")
    for key, value in table.items():
        path = ".".join((*prefix, key))
        if _is_table(value):
            lines += ["", f"[{path}]"]
            _emit(lines, value, prefix=(*prefix, key))
        elif _is_table_array(value):
            for entry in value:
                lines += ["", f"[[{path}]]"]
                _emit(lines, entry, prefix=(*prefix, key))


def _scalar(value: Any) -> str:
    # `bool` before `int`: `isinstance(True, int)` is True, and `max_rounds = true` is nonsense.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(one) for one in value) + "]"
    raise ConfigError(
        f"a matrix holds no value of type {type(value).__name__} ({value!r}). Money and every "
        "ratio is a quoted string, and a seat's temperature is deliberately not editable here"
    )


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


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


def save(
    name: str,
    document: Mapping[str, Any],
    *,
    reference: Basket,
    workspace: Path | None = None,
) -> tuple[int, Matrix]:
    """Validate through `load_matrix`, then mint the next version.

    The temporary file is written into the seat set's own directory so `load_matrix` reads exactly
    the bytes that will be stored — a validator run against a different rendering of the same
    document proves nothing about the file the CLI will later be pointed at.
    """
    directory = root(workspace=workspace) / _require_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    draft = directory / ".draft.toml"
    draft.write_text(dumps(document), encoding="utf-8")
    try:
        matrix = load_matrix(draft, reference=reference)
    except ConfigError:
        draft.unlink(missing_ok=True)
        raise
    existing = versions(name, workspace=workspace)
    version = (existing[-1] if existing else 0) + 1
    draft.replace(path_for(name, version, workspace=workspace))
    return version, matrix


def templates() -> tuple[str, ...]:
    """The shipped matrices, as starting points. Read-only: nothing here ever writes to them."""
    return tuple(sorted(path.stem for path in CONFIG_DIR.glob("sweep*.toml")))


def read_template(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{_require_name(name)}.toml"
    if not path.is_file():
        raise ConfigError(f"no shipped matrix named {name!r}; there are {list(templates())}")
    return tomllib.loads(path.read_text(encoding="utf-8"))
