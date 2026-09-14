"""The seat-set editor over §12.3's `matrices.py` store: list, edit, save.

Three routes. `GET /matrices` lists every stored seat set and the shipped templates it can be
duplicated from. `GET /matrices/{name}` is the editor — the latest saved version, an older one
named by `?version=`, or a shipped template named by `?from_template=` when nothing has been
saved under this name yet. `POST /matrices/{name}` dispatches on `action`: `save` is the only one
that writes, and every other recognised action mutates the submitted document's shape (a row
added or removed) and re-renders the form unsaved, so the editor works one round-trip at a time
with scripting off.

Failure semantics: an unrecognised `action` re-renders the form unchanged — the fail-closed answer
for a request nobody's browser produced. A `save` that does not validate is refused with
`candidates.load_matrix`'s own message and the store is left exactly as it was; nothing here ever
restates one of that loader's rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from decision_lab import matrices
from decision_lab.corpus import CorpusMeta
from decision_lab.dashboard import forms
from decision_lab.dashboard.views import render, state_of
from decision_lab.params import CORPUS_META, workspace_root
from tradebot.app import demo_basket, select_panel
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError
from tradebot.marketdata.catalogue import sim_catalogue

router = APIRouter()


@router.get("/matrices", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Every stored seat set and its versions, plus the shipped templates."""
    state = state_of(request)
    names = matrices.names(workspace=state.workspace)
    stored = {name: matrices.versions(name, workspace=state.workspace) for name in names}
    return render(request, "matrices.html", stored=stored, shipped=matrices.templates())


@router.get("/matrices/{name}", response_class=HTMLResponse)
async def edit_form(
    request: Request, name: str, version: int | None = None, from_template: str = ""
) -> HTMLResponse:
    """The editor: the named version, the latest, a shipped template, or a blank document.

    `from_template` wins when given — it is how a new seat set is started — and otherwise falls
    back to a blank document rather than raising, because visiting a name nothing has been saved
    under yet (no `?from_template=` either) is exactly how an operator starts one from scratch.
    """
    state = state_of(request)
    shown_version: int | None = None
    error = ""
    if from_template:
        try:
            document = matrices.read_template(from_template)
        except ConfigError as problem:
            document, error = {"candidates": []}, str(problem)
    else:
        try:
            shown_version, document = matrices.read(name, version, workspace=state.workspace)
        except ConfigError:
            # Nothing saved under this name yet (and no `?from_template=` either) — a blank
            # document, exactly what starting a seat set from scratch looks like.
            document = {"candidates": []}
    return render(
        request,
        "matrix_edit.html",
        name=name,
        document=document,
        version=shown_version,
        saved=request.query_params.get("saved", ""),
        digest=request.query_params.get("digest", ""),
        reference_label=request.query_params.get("reference", ""),
        error=error,
    )


async def _reference_basket(workspace: Path | None) -> tuple[Basket, str]:
    """What a candidate is validated against: any built corpus's own reference basket, or the
    offline demo basket when the workspace holds none (spec §12.3, corrected).

    A sweep validates every candidate against its corpus's reference basket, so preferring one
    here keeps a saved seat set aligned with the run it will actually face. The fallback needs
    neither a corpus nor a dataset — `demo_basket` is async, offline and built from the committed
    `sim_markets.json` capture — and it is safe precisely because validation never reaches the
    stored digest: `candidates._basket_for` dumps the reference, replaces `panel` with the
    submitted candidate's own panel, and re-validates; `Matrix.matrix_digest` is hashed over each
    candidate's `panel_digest` alone (`candidates.py`). A fallback reference therefore cannot mint
    a different digest for the same panel and cannot corrupt the §10.6 gate key — it can only
    change *whether* a candidate's other, unedited fields (instruments, risk policy, schedule)
    happen to validate, which is exactly what an operator needs told, not hidden.
    """
    root = workspace or workspace_root()
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            meta_path = entry / CORPUS_META
            if not meta_path.is_file():
                continue
            try:
                meta = CorpusMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # An interrupted build or a schema an older version wrote leaves a `corpus.json`
                # this process cannot read. This function is a best-effort scan for *any* usable
                # reference, not a request for one specific corpus by name — `corpus.load` is that
                # request, and it raises bare, deliberately, because a name the operator typed
                # should fail loudly. Here, a save must not be blocked by an unrelated, unreadable
                # directory sitting in the workspace when another corpus — or the offline fallback
                # below — validates it just as well: skip it and keep looking.
                continue
            return meta.reference_basket, f"corpus {meta.corpus_id}"
    basket = await demo_basket(sim_catalogue(), select_panel("stub"))
    return basket, "the demo basket (no corpus is built in this workspace)"


def _index(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _pair(value: str) -> tuple[int | None, int | None]:
    head, _, tail = value.partition(".")
    return _index(head), _index(tail)


def _add_candidate(document: dict[str, Any], _value: str) -> dict[str, Any]:
    document.setdefault("candidates", []).append({"seats": []})
    return document


def _remove_candidate(document: dict[str, Any], value: str) -> dict[str, Any]:
    candidates = document.get("candidates", [])
    index = _index(value)
    if index is not None and 0 <= index < len(candidates):
        del candidates[index]
    return document


def _add_seat(document: dict[str, Any], value: str) -> dict[str, Any]:
    candidates = document.get("candidates", [])
    index = _index(value)
    if index is not None and 0 <= index < len(candidates):
        candidates[index].setdefault("seats", []).append({"fallbacks": []})
    return document


def _remove_seat(document: dict[str, Any], value: str) -> dict[str, Any]:
    candidates = document.get("candidates", [])
    candidate_index, seat_index = _pair(value)
    if candidate_index is None or not (0 <= candidate_index < len(candidates)):
        return document
    seats = candidates[candidate_index].get("seats", [])
    if seat_index is not None and 0 <= seat_index < len(seats):
        del seats[seat_index]
    return document


def _add_fallback(document: dict[str, Any], value: str) -> dict[str, Any]:
    candidates = document.get("candidates", [])
    candidate_index, seat_index = _pair(value)
    if candidate_index is None or not (0 <= candidate_index < len(candidates)):
        return document
    seats = candidates[candidate_index].get("seats", [])
    if seat_index is not None and 0 <= seat_index < len(seats):
        seats[seat_index].setdefault("fallbacks", []).append({})
    return document


#: What a submit button does. Dispatch over a table rather than a chain of `if`s: a button whose
#: action is not in here re-renders the form unchanged, which is the fail-closed answer for a
#: request nobody's browser produced. `save` is handled separately, above, because it is the only
#: entry that writes rather than reshaping the document in memory.
ACTIONS: Final[dict[str, Callable[[dict[str, Any], str], dict[str, Any]]]] = {
    "add_candidate": _add_candidate,
    "remove_candidate": _remove_candidate,
    "add_seat": _add_seat,
    "remove_seat": _remove_seat,
    "add_fallback": _add_fallback,
}

#: The four row-mutating buttons each carry their own row index as their *value* — a single HTML
#: `<button name="add_seat" value="0">` has one name and one value to give, and the row has
#: nowhere else to travel. Their real submission therefore carries no separate `action` field at
#: all: only the activated button's own name/value pair reaches the server. `save` and
#: `add_candidate` take no row argument, so they are the two plain buttons posted as
#: `action=<name>` instead — matching `matrix_edit.html`.
_ROW_ACTIONS: Final = ("remove_candidate", "add_seat", "remove_seat", "add_fallback")


def _requested_action(items: Mapping[str, str]) -> tuple[str, str]:
    """Which button was pressed, and the row argument it carried, if any.

    Checked in this order because a row button never posts `action` and a hand-built request
    naming both (as this module's own tests do, for brevity) must resolve to the row button, not
    silently fall through to `save` — the one defect this function exists to close: reading only
    `items.get("action", "save")` treated every real add/remove click, which posts no `action`
    field at all, as a request to persist the document.
    """
    for name in _ROW_ACTIONS:
        if name in items:
            return name, items[name]
    # `action` is absent for a request nobody's browser produced (docstring's fail-closed case),
    # never defaulted to "save" — that would turn silence into a persist.
    return items.get("action", ""), ""


@router.post("/matrices/{name}", response_class=HTMLResponse)
async def edit_post(request: Request, name: str) -> Response:
    state = state_of(request)
    form = await request.form()
    items = {key: str(value) for key, value in form.items()}
    action, row = _requested_action(items)
    document = forms.nest(items)

    if action == "save":
        reference, reference_label = await _reference_basket(state.workspace)
        try:
            version, matrix = matrices.save(
                name, document, reference=reference, workspace=state.workspace
            )
        except ConfigError as error:
            page = render(
                request,
                "matrix_edit.html",
                name=name,
                document=document,
                version=None,
                saved="",
                digest="",
                error=str(error),
                reference_label=reference_label,
            )
            page.status_code = 400
            return page
        return RedirectResponse(
            f"/matrices/{name}?saved={version}&digest={matrix.matrix_digest}"
            f"&reference={quote(reference_label)}",
            status_code=303,
        )

    mutator = ACTIONS.get(action)
    if mutator is not None:
        document = mutator(document, row)
    return render(
        request,
        "matrix_edit.html",
        name=name,
        document=document,
        version=None,
        saved="",
        digest="",
        error="",
    )
