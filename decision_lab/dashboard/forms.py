"""A flat form to a matrix document, and nothing else (spec §12.3).

The document this produces is handed to `candidates.load_matrix`, which is the only validation
there is: nothing here restates a rule the loader already owns, exactly as the bot's own
`tradebot/dashboard/forms.py` defers to its pydantic models on the Configure page. The wire format
is the same one `matrices.dumps`'s inverse (`decision_lab/tests/test_lab_dashboard_edit.py`'s
`_flatten`) produces: a dotted path per leaf value, `candidates.0.seats.1.role` naming an integer
list index at every numeric segment.

Coercion is by **path**, not by field name alone, because the same field name means two different
things at two different paths. `max_rounds` is a plain count at `candidates.<i>.max_rounds`, but
`[expand] max_rounds = [1, 3]` is one of §7.1's four matrix axes and `candidates._axes` refuses
anything under `[expand]` that is not a list — "must be a list of values" — even when the operator
means to vary just one value. So every path under `[expand]` other than `limit` is always a list,
comma-split, with all-digit entries coerced to `int`; `limit` is the one scalar count the block
holds. Outside `[expand]`, coercion is the plain field-name table below.

Money and every ratio (`qualified_majority`, `max_cost_usd_per_cycle`) stay strings all the way
through: a value parsed to a float here is the binary rounding error the whole package forbids
(`test_discipline.py`, ADR 0001, one level over). The seat editor also deliberately does not carry
`SeatConfig.temperature` — it is not one of §7.1's axes, no shipped matrix sets it, and a form
field for it would put a TOML float into this package's own code path for no measurement at all.

Failure semantics: a name that is not a recognised control field and does not parse as a path is
placed as best it can rather than raising — a form is reachable only by someone who already holds
the dashboard token, but a malformed field name must never be able to take the editor away from an
operator mid-edit. `nest` never raises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

#: Control fields a submitted form carries alongside the document — the action button pressed,
#: and (for a parametrised action) the row it names. None of these are part of a matrix: left in,
#: each would land as a spurious top-level key (`document["add_seat"] = "0"`) that `matrices.dumps`
#: would then refuse to write, since it is neither a table, an array of tables, nor a scalar this
#: schema is built from.
CONTROL_FIELDS: Final = frozenset(
    {"action", "add_candidate", "remove_candidate", "add_seat", "remove_seat", "add_fallback"}
)

#: Counts. `max_rounds` is a number of debate rounds and `limit` an expansion cap — both scalars,
#: outside `[expand]`'s own always-a-list rule (see the module docstring).
INT_FIELDS: Final = frozenset({"max_rounds", "limit"})

#: Checkboxes. An unchecked box posts nothing at all, so every one of these is written `False` when
#: absent from the submission — a field that vanished from the document would be a field deleted
#: from the seat set on the next save, the same hazard `_panel.html` names one level up.
BOOL_FIELDS: Final = frozenset({"devils_advocate"})

#: Comma-separated lists, outside `[expand]`.
LIST_FIELDS: Final = frozenset({"providers", "evidence"})


def nest(items: Mapping[str, str]) -> dict[str, Any]:
    """`candidates.0.seats.1.role` -> `{"candidates": [{"seats": [_, {"role": …}]}]}`.

    An integer path segment is a list index, filled directly at that position rather than by
    order of appearance — so the iteration order of `items` (a `dict`, unordered as far as this
    function is concerned) never matters. Indices are dense after parsing: the editor renders them
    contiguously and every add/remove is a server round-trip that re-renders the whole form, so a
    gap can only come from a hand-built request, where dropping the stray high index is the
    fail-closed answer rather than growing a document with holes in it.
    """
    document: dict[str, Any] = {}
    for name, raw in items.items():
        if name in CONTROL_FIELDS or not raw.strip():
            # An empty value is omitted, not stored as empty — the same rule the bot's own
            # `tradebot/dashboard/forms.py` states for the identical reason: the editor always
            # renders every optional field, even ones a particular document never set (`prompt`,
            # `qualified_majority`, a seat's fallbacks), so a candidate that never touched one must
            # not gain an explicit blank on the next save. `_panel_for` includes a candidate field
            # whenever its key is merely *present*, so a stored `""` would reach `PanelConfig` as
            # an unparseable ratio instead of the field simply being absent and its own default
            # applying — turning "never set" into a refusal the operator never asked for.
            continue
        segments = name.split(".")
        if not all(segments):
            continue
        _place(document, segments, _coerce(segments, raw))
    _default_missing_checkboxes(document)
    return document


def _coerce(path: Sequence[str], raw: str) -> Any:
    """One submitted value, coerced by its full path rather than by its last segment alone."""
    if path[0] == "expand" and path[-1] != "limit":
        return _expand_axis(raw)
    field = path[-1]
    if field in INT_FIELDS:
        return int(raw) if raw.strip().lstrip("-").isdigit() else raw
    if field in BOOL_FIELDS:
        return raw.lower() in ("on", "true", "1", "yes")
    if field in LIST_FIELDS:
        return _split(raw)
    return raw


def _split(raw: str) -> list[str]:
    return [one.strip() for one in raw.split(",") if one.strip()]


def _expand_axis(raw: str) -> list[Any]:
    """An `[expand]` value other than `limit`: always a list (`candidates._axes`'s own rule),
    each all-digit entry an `int` (`max_rounds = [1, 3]`), everything else a string
    (`protocol = [...]`, `prompts.<seat_id> = [...]`)."""
    return [int(one) if one.lstrip("-").isdigit() else one for one in _split(raw)]


def _place(document: dict[str, Any], segments: Sequence[str], value: Any) -> None:
    """Descend `segments` into `document`, creating tables and lists as needed.

    A segment can conflict with what an *earlier* value in the same submission already placed —
    `candidates.x` and `candidates.0.id` disagree on whether `candidates` is a table or a list —
    and the docstring above promises `nest` never raises. The old code called `setdefault` blind
    to what was already there: on a list where a table was wanted (or the reverse) it either
    silently returned the existing, wrongly-shaped container and then indexed or assigned into it
    incorrectly (`list["x"] = …` raises `TypeError`; `dict[0]` raises `KeyError`), and which one
    happened depended on `items`' iteration order — a form is reachable only by someone who
    already holds the token, but a malformed name must never be able to take the page away from an
    operator mid-edit. The fix checks the existing value's shape before trusting it: a conflict
    means this whole leaf is dropped (`return`) rather than corrupting or crashing, and whichever
    of the two conflicting paths was placed *first* is the one that survives — deterministic, if
    not meaningful, and only a hand-built request can produce it at all, since every name the
    editor itself submits was rendered from `document` at `GET` time and cannot disagree with
    itself.
    """
    cursor: dict[str, Any] = document
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        following = segments[index + 1] if not last else ""
        if segment.isdigit():
            continue  # handled by the parent below, which knows the list it is filling
        if last:
            cursor[segment] = value
            return
        if following.isdigit():
            bucket = cursor.get(segment, [])
            if not isinstance(bucket, list):
                return  # `segment` already names a table here; a list would corrupt it
            cursor[segment] = bucket
            position = int(following)
            while len(bucket) <= position:
                bucket.append({})
            cursor = bucket[position]
        else:
            table = cursor.get(segment, {})
            if not isinstance(table, dict):
                return  # `segment` already names a list here; a table would corrupt it
            cursor[segment] = table
            cursor = table


def _default_missing_checkboxes(document: dict[str, Any]) -> None:
    """Every seat gets every boolean field, present in the submission or not (see `BOOL_FIELDS`).

    Skips anything not shaped like a list of candidate tables, each holding a list of seat tables.
    `nest`'s own contract is that it never raises, and `_place`'s conflict handling can legitimately
    leave `document["candidates"]` as something other than a list of dicts — a submission that
    disagreed with itself about the shape of `candidates` (see `_place`) has already had its second,
    conflicting path dropped, and this must not then crash on what the first path left behind.
    """
    candidates = document.get("candidates", ())
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        seats = candidate.get("seats", ())
        if not isinstance(seats, list):
            continue
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            for field in BOOL_FIELDS:
                seat.setdefault(field, False)
