"""The basket editor's view model: what the form shows that the draft does not say outright.

Pure assembly over the draft dict, like `blotter.py` and `dock.py`, and for the same reason — a
rule that decides what a control *offers* is testable without a browser only if it lives outside
the template. Nothing here reads a store, a venue or a request.

Four things the redesigned form can say that the old scroll could not:

* **Which tab a row action returns to** (`focus_for`), so adding a seat does not throw the operator
  back to the top of a 64-field page.
* **That two seats resolve to the same provider and model.** Heterogeneity is a design control
  (DESIGN §6.5, L5) and `PANEL_HOMOGENEOUS` only fires once a cycle has run; losing it should be
  visible while the panel is being configured.
* **How many seats a provider serves.** `PanelConfig` already refuses a seat bound to an undeclared
  provider; the count stops the operator discovering that at publish.
* **That another basket already holds this instrument** (ADR 0026), read where the instrument is
  picked rather than as a refusal after the fact.

Failure semantics: a draft is whatever the operator has typed so far, so every accessor here
tolerates a half-built one and returns the empty answer rather than raising. Nothing here validates
— the models do that, and a second opinion is the one that eventually disagrees.
"""

from __future__ import annotations

import re
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The two panels a basket carries. The challenger is edited by the same macro as the champion, so
#: a field cannot exist on one panel's form and not the other's (ADR 0018).
SHADOW_PATH = "shadow_panel"
PANEL_PATHS = ("panel", SHADOW_PATH)

_INDEX = re.compile(r"\[(\d+)\]")


# ---------------------------------------------------------------------- tab focus


def focus_for(path: str) -> dict[str, str]:
    """Which tabs a row action should return to, keyed by radio group name without its `ui.`.

    "Add seat" that lands back on Identity is the same lost-place complaint the whole redesign
    exists to fix, one level down. An unrecognised path selects nothing rather than guessing: a
    control field that is not one of ours must not move the operator at all.
    """
    head, _, _ = path.partition("[")
    root = head.split(".")[0]
    if root == "instruments":
        return {"section": "instruments"}
    if root not in PANEL_PATHS:
        return {}

    focus = {"section": "panel", "panel": root}
    tail = path[len(root) :].lstrip(".")
    if _names(tail, "providers"):
        focus[f"tab.{root}"] = "providers"
    elif _names(tail, "seats"):
        focus[f"tab.{root}"] = "seats"
        if (index := _INDEX.search(tail)) is not None:
            focus[f"seat.{root}"] = index.group(1)
    return focus


def _names(tail: str, segment: str) -> bool:
    """Whether `tail` names `segment` as its own path element, not a field merely spelled like it.

    A plain `startswith` would open the seats tab for a hypothetical `seats_extra` field. Nothing
    on `PanelConfig` collides today, so this is latent rather than live — but it costs nothing to
    require the next character be a separator (`.`, `[`) or the end of the path.
    """
    if not tail.startswith(segment):
        return False
    rest = tail[len(segment) :]
    return rest == "" or rest[0] in ".["


# ---------------------------------------------------------------------- panel rows


@dataclass(frozen=True, slots=True)
class SeatRow:
    """One seat as the master list shows it, before its detail pane is opened."""

    index: int
    seat_id: str
    #: `provider · model`, or empty for a seat the operator has not bound yet.
    binding: str
    #: Another seat in this panel resolves to the same provider *and* model.
    homogeneous: bool


def seat_rows(panel: Mapping[str, Any]) -> tuple[SeatRow, ...]:
    """The seat list, each row flagged when it shares a binding with another seat.

    An *unbound* seat is never flagged against another unbound one: two blank rows an operator has
    just added are not a lost design control, and a warning there would train them to ignore it.
    """
    seats = _rows(panel, "seats")
    bindings = [(_text(row, "provider_id"), _text(row, "model")) for row in seats]
    return tuple(
        SeatRow(
            index=index,
            seat_id=_text(row, "seat_id"),
            binding=f"{binding[0]} · {binding[1]}" if all(binding) else "",
            homogeneous=all(binding) and bindings.count(binding) > 1,
        )
        for index, (row, binding) in enumerate(zip(seats, bindings, strict=True))
    )


@dataclass(frozen=True, slots=True)
class ProviderRow:
    """One declared endpoint, and how much of the panel depends on it."""

    index: int
    provider_id: str
    kind: str
    #: Seats binding it as primary or anywhere in a fallback chain. Seats, not bindings: a chain
    #: that names one provider twice is one seat's dependency, not two.
    used_by: int


def provider_rows(panel: Mapping[str, Any]) -> tuple[ProviderRow, ...]:
    usage = _usage(panel)
    return tuple(
        ProviderRow(
            index=index,
            provider_id=_text(row, "provider_id"),
            kind=_text(row, "kind"),
            used_by=usage.get(_text(row, "provider_id"), 0),
        )
        for index, row in enumerate(_rows(panel, "providers"))
    )


def _usage(panel: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for seat in _rows(panel, "seats"):
        named = {_text(seat, "provider_id")} | {
            _text(binding, "provider_id") for binding in _rows(seat, "fallbacks")
        }
        for provider_id in named - {""}:
            counts[provider_id] = counts.get(provider_id, 0) + 1
    return counts


# ---------------------------------------------------------------------- instrument rows


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    """One instrument row's state, beyond the values in its inputs."""

    index: int
    symbol: str
    venue: str
    key: str
    #: Named a venue this process is not wired to, so its rules cannot be verified here and the
    #: prices it would be sized from come off a different book.
    foreign: bool
    #: Excluded from automated trading, per the *stored* basket. Read-only here; the act lives on
    #: the workspace, which has the held-position guard this form does not (ADR 0022).
    quarantined: bool
    #: Another basket in service holding this key, or empty. ADR 0026's refusal, shown early.
    held_by: str


def instrument_rows(
    draft: Mapping[str, Any],
    *,
    venue_id: str,
    quarantined: Container[str],
    holders: Mapping[str, Sequence[str]],
    basket_id: str,
) -> tuple[InstrumentRow, ...]:
    rows = draft.get("instruments")
    return tuple(
        _instrument_row(index, row, venue_id, quarantined, holders, basket_id)
        for index, row in enumerate(rows if isinstance(rows, list) else ())
        if isinstance(row, dict)
    )


def _instrument_row(
    index: int,
    row: Mapping[str, Any],
    venue_id: str,
    quarantined: Container[str],
    holders: Mapping[str, Sequence[str]],
    basket_id: str,
) -> InstrumentRow:
    venue = _text(row, "venue")
    symbol = _text(row, "symbol")
    key = f"{venue}:{symbol}" if venue and symbol else ""
    others = [held for held in holders.get(key, ()) if held != basket_id]
    return InstrumentRow(
        index=index,
        symbol=symbol,
        venue=venue,
        key=key,
        foreign=bool(venue) and venue != venue_id,
        quarantined=bool(key) and key in quarantined,
        held_by=others[0] if others else "",
    )


# ---------------------------------------------------------------------- draft accessors


def instrument_keys(draft: Mapping[str, Any]) -> tuple[str, ...]:
    """`venue:symbol` for each complete instrument row — the only scopes a quarantine may name.

    Read from the draft rather than from the stored document, so an instrument added in this same
    edit is immediately selectable, exactly as a provider added here appears in every seat's
    picker. `Basket` still refuses a key it does not hold, so this is a convenience, not the check.
    """
    rows = draft.get("instruments")
    return tuple(
        f"{_text(row, 'venue')}:{_text(row, 'symbol')}"
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, dict) and _text(row, "venue") and _text(row, "symbol")
    )


def panel_providers(draft: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    """One panel's provider rows, tolerating a draft that is half built."""
    panel = draft.get(path)
    return _rows(panel, "providers") if isinstance(panel, Mapping) else []


def declared_providers(draft: Mapping[str, Any], path: str) -> tuple[str, ...]:
    """Provider ids one panel declares — the only options its seats' pickers may offer."""
    return tuple(
        provider_id
        for row in panel_providers(draft, path)
        if (provider_id := _text(row, "provider_id"))
    )


def _rows(node: Any, key: str) -> list[dict[str, Any]]:
    rows = node.get(key) if isinstance(node, Mapping) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _text(row: Mapping[str, Any], key: str) -> str:
    return str(row.get(key, "") or "").strip()
