"""HTML forms ↔ domain documents. One parser, and the models are the only validation.

DESIGN §6.10 is explicit that server-side validation is *the same pydantic schemas the engine
uses*. So nothing here re-implements a rule: a flat form is turned into the nested document
shape and handed to `model_validate`, and whatever it says is what the operator is shown.
`PanelConfig` already proves every seat binding resolves to a declared provider and that no
chain repeats a binding — re-checking that in a form handler would create a second opinion, and
the second opinion is the one that eventually disagrees.

The wire format is a path in the field name:

    doc.name                              → {"name": ...}
    doc.instruments[0].symbol             → {"instruments": [{"symbol": ...}]}
    doc.panel.seats[1].fallbacks[0].model → nested the same way
    doc.timeframes[]                      → {"timeframes": [...]}, repeated key, order preserved

Only `doc.`-prefixed fields become the document. Control fields — the confirmation phrase, the
add/remove buttons — sit outside that namespace, so a stray control field can never reach a
model whose config is `extra="forbid"`.

Two conventions carry weight:

* **An empty value is omitted, not stored as empty.** The form is always rendered pre-filled, so
  a blank field means the operator cleared it, and omitting it lets the model's own default and
  its validator speak. The one place this could loosen something silently — clearing the
  per-order notional cap — is caught by the Tier-2 loosening confirmation (`risk/loosening.py`).
* **Empty rows are dropped.** A row added and left blank, or removed client-side leaving a gap,
  must not become a half-built instrument. Compaction happens before validation.

Failure semantics: parsing never raises for bad *content*. It produces a document that either
validates or yields field-located errors the form renders in place; a draft that does not
validate is re-rendered with the operator's input intact rather than discarded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import ValidationError

from tradebot.core.schema import DomainModel

#: Field-name prefix marking a form input as part of the document being edited.
DOCUMENT_PREFIX = "doc."

_SEGMENT = re.compile(r"^([^\[\]]+)(?:\[(\d*)\])?$")

T = TypeVar("T", bound=DomainModel)


@dataclass(frozen=True, slots=True)
class FieldError:
    """One validation failure, located at the same path the form used as a field name."""

    field: str
    message: str

    @property
    def label(self) -> str:
        """The path without its indices — what a form section heading reads as."""
        return re.sub(r"\[\d*\]", "", self.field)


def nest(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Flat `(name, value)` form pairs → the nested document they describe."""
    root: dict[str, Any] = {}
    for name, raw in items:
        if not name.startswith(DOCUMENT_PREFIX):
            continue
        value = _text(raw)
        if value is not None:
            _assign(root, name[len(DOCUMENT_PREFIX) :].split("."), value)
    compacted = _compact(root)
    assert isinstance(compacted, dict)  # `_compact` preserves the container it is given
    return compacted


def parse(
    model: type[T], items: Iterable[tuple[str, Any]]
) -> tuple[T | None, tuple[FieldError, ...]]:
    """Validate a submitted form into `model`. Returns the document, or where it went wrong."""
    return validate(model, nest(items))


def _text(raw: Any) -> str | None:
    """A submitted value as text. `None` means "not a form value at all", and is skipped.

    A browser sends strings; a draft round-tripped from `draft_of` carries the JSON types. Both
    have to arrive at the model the same way, and an absent optional must stay absent rather
    than becoming the string `"None"` — which would set a limit to a value nobody typed.
    """
    if isinstance(raw, str):
        # A `<textarea>` is submitted with CRLF line breaks (HTML §4.10.5.4). The document is
        # versioned rather than diffed, so a stray CR rides along in every later version and
        # makes a GUI-authored prompt differ from an identical one written anywhere else.
        return raw.replace("\r\n", "\n").strip()
    if raw is None:
        return ""
    if isinstance(raw, int | float | Decimal):
        return str(raw)
    return None


def validate(model: type[T], document: dict[str, Any]) -> tuple[T | None, tuple[FieldError, ...]]:
    try:
        return model.model_validate(document), ()
    except ValidationError as exc:
        return None, errors_of(exc)


def errors_of(exc: ValidationError) -> tuple[FieldError, ...]:
    """Pydantic's locations rendered as the form's own field names, so messages land in place.

    A model-level validator — every cross-field rule, such as "take profit must exceed the
    stop" — has no location, and gets the empty field. The form shows those at the top rather
    than guessing which input to attach them to: pinning a cross-field message to one of the
    fields involved tells the operator the wrong thing about which value is wrong.
    """
    return tuple(
        FieldError(field=path_of(error["loc"]), message=str(error["msg"])) for error in exc.errors()
    )


def path_of(loc: Sequence[Any]) -> str:
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return path


def draft_of(document: DomainModel) -> dict[str, Any]:
    """A stored document as the form renders it. Money becomes its exact string, never a float."""
    return dict(document.model_dump(mode="json"))


def add_row(draft: dict[str, Any], path: str) -> None:
    """Append a blank row to the list at `path` — the "add instrument / seat / provider" button."""
    container = _list_at(draft, path)
    if container is not None:
        container.append({})


def remove_row(draft: dict[str, Any], path: str) -> None:
    """Drop the indexed row `path` points at, e.g. `panel.seats[1]`. Out of range is a no-op."""
    head, _, tail = path.rpartition("[")
    if not head or not tail.endswith("]"):
        return
    container = _list_at(draft, head)
    index = int(tail[:-1]) if tail[:-1].isdigit() else -1
    if container is not None and 0 <= index < len(container):
        del container[index]


# ---------------------------------------------------------------------- internals


def _assign(node: dict[str, Any], path: list[str], value: str) -> None:
    match = _SEGMENT.match(path[0])
    if match is None:
        return
    key, index = match.group(1), match.group(2)
    last = len(path) == 1

    if index is None:
        if last:
            if value:
                node[key] = value
            return
        child = node.setdefault(key, {})
        if isinstance(child, dict):
            _assign(child, path[1:], value)
        return

    bucket = node.setdefault(key, [])
    if not isinstance(bucket, list):
        return
    if index == "":
        # Repeated key: a multi-select or checkbox group. Order of appearance is the order the
        # operator sees, and the hidden empty sentinel is what makes "nothing selected" reachable.
        if value:
            bucket.append(value)
        return

    slot = int(index)
    while len(bucket) <= slot:
        bucket.append({})
    if last:
        if value:
            bucket[slot] = value
        return
    row = bucket[slot]
    if isinstance(row, dict):
        _assign(row, path[1:], value)


def _compact(value: Any) -> Any:
    """Drop rows the operator left blank, or removed leaving a gap, at every depth."""
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    if isinstance(value, list):
        rows = [_compact(item) for item in value]
        return [row for row in rows if row != {} and row != []]
    return value


def _list_at(draft: dict[str, Any], path: str) -> list[Any] | None:
    """The list `path` names, creating containers along the way. `None` if the path is not one."""
    node: Any = draft
    segments = path.split(".")
    for depth, segment in enumerate(segments):
        match = _SEGMENT.match(segment)
        if match is None or not isinstance(node, dict):
            return None
        key, index = match.group(1), match.group(2)
        last = depth == len(segments) - 1
        if index is None:
            node = node.setdefault(key, [] if last else {})
            continue
        bucket = node.setdefault(key, [])
        if not isinstance(bucket, list) or not index.isdigit() or int(index) >= len(bucket):
            return None
        node = bucket[int(index)]
    return node if isinstance(node, list) else None
