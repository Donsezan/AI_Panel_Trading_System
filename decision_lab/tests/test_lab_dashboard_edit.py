"""§12.3 — building a seat set in the page, and the two things that must never happen.

The form round-trips the whole document, so a control that stopped being rendered would delete
that part of the seat set on the next save — the bot's `_panel.html` hazard, one level up. And an
edit mints a new `matrix_digest`, a third of the §10.6 gate key, so the page has to say that at
the moment it saves rather than leaving it to be discovered as an exit 6.
"""

from __future__ import annotations

import tomllib
import warnings
from pathlib import Path
from typing import Any

from decision_lab import matrices
from decision_lab.dashboard import forms

# See test_lab_dashboard_read.py's own note: starlette's TestClient now prefers `httpx2`, which is
# not one of this repo's pinned dependencies, and falls back to `httpx` with a
# `StarletteDeprecationWarning` the root `filterwarnings = ["error"]` would otherwise turn into a
# collection failure of this module, unrelated to anything decision_lab does.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

SHIPPED = Path(__file__).resolve().parents[1] / "config"


def test_nest_builds_candidates_and_seats_from_dotted_names() -> None:
    flat = {
        "sweep.on_fallback": "halt",
        "candidates.0.id": "baseline",
        "candidates.0.max_rounds": "3",
        "candidates.0.providers": "openrouter, gemini",
        "candidates.0.seats.0.seat_id": "technical",
        "candidates.0.seats.0.devils_advocate": "on",
        "candidates.0.seats.0.evidence": "indicators, position",
        "expand.limit": "4",
    }

    document = forms.nest(flat)

    assert document["sweep"]["on_fallback"] == "halt"
    assert document["candidates"][0]["max_rounds"] == 3, "an int field is an int"
    assert document["candidates"][0]["providers"] == ["openrouter", "gemini"]
    assert document["candidates"][0]["seats"][0]["devils_advocate"] is True
    assert document["expand"]["limit"] == 4


def test_money_and_ratios_stay_strings() -> None:
    """A ratio parsed to a float here is the binary rounding error the whole package forbids."""
    document = forms.nest(
        {"candidates.0.qualified_majority": "0.5", "candidates.0.max_cost_usd_per_cycle": "0.02"}
    )
    assert document["candidates"][0]["qualified_majority"] == "0.5"
    assert document["candidates"][0]["max_cost_usd_per_cycle"] == "0.02"


def test_an_expansion_axis_is_a_list_even_though_its_field_name_is_not(tmp_path: Path) -> None:
    """The controller's correction: `_coerce` reads the full path, not the last segment alone.

    `expand.max_rounds` shares a field name with the scalar `candidates.<i>.max_rounds`, but
    `candidates._axes` refuses anything under `[expand]` that is not a list — so the two must
    coerce differently even though a naive by-name `_coerce` would treat them the same.
    """
    document = forms.nest({"expand.max_rounds": "1, 3", "expand.limit": "4"})

    assert document["expand"]["max_rounds"] == [1, 3]
    assert document["expand"]["limit"] == 4


def test_an_absent_checkbox_is_false_not_missing() -> None:
    """An unchecked box posts nothing, and a field that vanished would be a field deleted."""
    document = forms.nest({"candidates.0.seats.0.seat_id": "s"})
    assert document["candidates"][0]["seats"][0]["devils_advocate"] is False


def test_an_empty_field_is_omitted_not_stored_as_empty() -> None:
    """The editor renders every optional field even on a candidate that never set it (`prompt`,
    `qualified_majority`) — so a blank one must vanish rather than become an explicit `""` that
    `_panel_for` would then hand to `PanelConfig` as an unparseable ratio."""
    document = forms.nest(
        {
            "candidates.0.id": "c",
            "candidates.0.qualified_majority": "",
            "candidates.0.seats.0.seat_id": "s",
            "candidates.0.seats.0.prompt": "",
        }
    )
    assert "qualified_majority" not in document["candidates"][0]
    assert "prompt" not in document["candidates"][0]["seats"][0]


def test_the_editor_lists_the_shipped_templates(lab_client: TestClient) -> None:
    page = lab_client.get("/matrices").text
    assert "sweep-stub" in page and "sweep" in page


def test_saving_mints_a_version_and_warns_that_the_gate_is_now_shut(
    lab_client: TestClient, tmp_path: Path
) -> None:
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    posted = _flatten(document) | {"action": "save"}

    response = lab_client.post("/matrices/mine", data=posted, follow_redirects=True)

    assert response.status_code == 200
    assert "version 1" in response.text
    assert "uncalibrated" in response.text.lower()
    assert "calibrate normal" in response.text and "calibrate shock" in response.text
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == (1,)


def test_an_invalid_seat_set_is_refused_with_the_loaders_own_message(
    lab_client: TestClient, tmp_path: Path
) -> None:
    posted = {"candidates.0.id": "c", "candidates.0.seats.0.seat_id": "s", "action": "save"}

    response = lab_client.post("/matrices/mine", data=posted)

    assert response.status_code == 400
    assert "not a valid basket" in response.text or "Field required" in response.text
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == ()


def test_adding_a_seat_re_renders_the_submitted_form_without_saving(
    lab_client: TestClient, tmp_path: Path
) -> None:
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    posted = _flatten(document) | {"action": "add_seat", "add_seat": "0"}

    response = lab_client.post("/matrices/mine", data=posted)

    assert response.status_code == 200
    assert response.text.count("seats.0.seat_id") >= 1
    assert "candidates.0.seats.3.seat_id" in response.text, "a fourth, empty seat block"
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == (), "nothing was saved"


def test_a_real_button_click_carries_no_bare_action_field(
    lab_client: TestClient, tmp_path: Path
) -> None:
    """An HTML `<button name="add_seat" value="0">`, once activated, is the *only* name/value
    pair the browser submits for it — there is no accompanying `action` field, because a single
    element has one name and one value and the row index has nowhere else to travel. A dispatcher
    that defaulted a missing `action` to `save` would turn every real add/remove click into a
    silent, unwanted persist; this is the shape `matrix_edit.html`'s own buttons actually submit,
    unlike this file's other tests, which spell out `action` for brevity."""
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    posted = _flatten(document) | {"add_seat": "0"}

    response = lab_client.post("/matrices/mine", data=posted)

    assert response.status_code == 200
    assert "candidates.0.seats.3.seat_id" in response.text, "a fourth, empty seat block"
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == (), (
        "an add_seat click must never save the document"
    )


def test_every_field_of_a_stored_seat_set_is_rendered_as_an_input(
    lab_client: TestClient, tmp_path: Path
) -> None:
    """Two-sided, like the bot's own configure test: a control that stops being rendered deletes
    that part of the document on the next save."""
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    lab_client.post("/matrices/mine", data=_flatten(document) | {"action": "save"})

    page = lab_client.get("/matrices/mine").text

    for name in _flatten(document):
        assert f'name="{name}"' in page, f"{name} is not editable, so a save would drop it"


def _flatten(document: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """The inverse of `forms.nest`, for building a POST body out of a TOML document."""
    flat: dict[str, str] = {}
    for key, value in document.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat |= _flatten(value, f"{name}.")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            for index, entry in enumerate(value):
                flat |= _flatten(entry, f"{name}.{index}.")
        elif isinstance(value, list):
            flat[name] = ", ".join(str(one) for one in value)
        elif isinstance(value, bool):
            if value:
                flat[name] = "on"
        else:
            flat[name] = str(value)
    return flat
