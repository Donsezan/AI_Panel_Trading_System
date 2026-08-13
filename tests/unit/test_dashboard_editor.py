"""The basket editor's view model: what the form shows that the draft does not say outright.

Pure assembly over a draft dict, like `blotter.py` and `dock.py`, so every rule here is asserted
without a browser or an HTTP round trip. The two that carry weight beyond the pass:

* **Homogeneity is visible while it is being configured**, not only after a cycle ran.
  Heterogeneity is a design control (DESIGN §6.5, L5), and `PANEL_HOMOGENEOUS` fires too late to
  stop an operator building a panel that has already lost it.
* **A row says who else holds the instrument**, so ADR 0026's refusal is read where the instrument
  is picked rather than at publish.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradebot.dashboard.editor import (
    declared_providers,
    focus_for,
    instrument_keys,
    instrument_rows,
    provider_rows,
    seat_rows,
)


def panel(*seats: dict[str, Any], providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "seats": list(seats),
        "providers": providers
        if providers is not None
        else [{"provider_id": "or", "kind": "openai_compat"}],
    }


def seat(seat_id: str, provider_id: str, model: str, **extra: Any) -> dict[str, Any]:
    return {"seat_id": seat_id, "provider_id": provider_id, "model": model, **extra}


class TestFocus:
    """Which tab a row action returns to. Keys are radio group names without their `ui.` prefix."""

    def test_an_instrument_action_selects_the_instruments_section(self) -> None:
        assert focus_for("instruments") == {"section": "instruments"}

    def test_a_seat_action_selects_the_panel_section_and_the_seats_tab(self) -> None:
        assert focus_for("panel.seats") == {
            "section": "panel",
            "panel": "panel",
            "tab.panel": "seats",
        }

    def test_a_nested_seat_action_also_selects_the_seat(self) -> None:
        assert focus_for("shadow_panel.seats[1].fallbacks") == {
            "section": "panel",
            "panel": "shadow_panel",
            "tab.shadow_panel": "seats",
            "seat.shadow_panel": "1",
        }

    def test_a_provider_action_selects_the_providers_tab(self) -> None:
        assert focus_for("panel.providers[0].price_rows") == {
            "section": "panel",
            "panel": "panel",
            "tab.panel": "providers",
        }

    def test_an_unrecognised_path_selects_nothing(self) -> None:
        """A control field that is not one of ours must not throw the operator to a random tab."""
        assert focus_for("") == {}
        assert focus_for("nonsense") == {}

    def test_a_non_row_panel_field_selects_the_panel_without_a_tab(self) -> None:
        """`panel_id` is neither a seat action nor a provider action, so no tab is forced open."""
        assert focus_for("panel.panel_id") == {"section": "panel", "panel": "panel"}

    def test_a_field_merely_spelled_like_seats_does_not_open_the_seats_tab(self) -> None:
        """A near-miss field name (`seats_extra`) must not match `seats` without a boundary."""
        assert focus_for("panel.seats_extra") == {"section": "panel", "panel": "panel"}


class TestSeatRows:
    def test_a_seat_shows_its_binding(self) -> None:
        rows = seat_rows(panel(seat("technical", "or", "deepseek-r1")))
        assert rows[0].seat_id == "technical"
        assert rows[0].binding == "or · deepseek-r1"
        assert rows[0].index == 0

    def test_two_seats_on_one_binding_are_both_flagged(self) -> None:
        rows = seat_rows(panel(seat("a", "or", "x"), seat("b", "or", "x")))
        assert [row.homogeneous for row in rows] == [True, True]

    def test_the_same_model_on_different_providers_is_not_homogeneous(self) -> None:
        """A model id only means something to the provider serving it (DESIGN §6.5)."""
        rows = seat_rows(panel(seat("a", "or", "x"), seat("b", "lm", "x")))
        assert [row.homogeneous for row in rows] == [False, False]

    def test_an_unbound_seat_is_not_flagged_against_another_unbound_one(self) -> None:
        """Two blank rows an operator has just added are not a lost design control."""
        rows = seat_rows(panel(seat("a", "", ""), seat("b", "", "")))
        assert [row.homogeneous for row in rows] == [False, False]


class TestProviderRows:
    def test_usage_counts_primary_and_fallback_bindings(self) -> None:
        built = panel(
            seat("a", "or", "x"),
            seat("b", "lm", "y", fallbacks=[{"provider_id": "or", "model": "z"}]),
            providers=[
                {"provider_id": "or", "kind": "openai_compat"},
                {"provider_id": "lm", "kind": "openai_compat"},
            ],
        )
        rows = {row.provider_id: row.used_by for row in provider_rows(built)}
        assert rows == {"or": 2, "lm": 1}

    def test_a_seat_naming_one_provider_twice_counts_once(self) -> None:
        built = panel(
            seat("a", "or", "x", fallbacks=[{"provider_id": "or", "model": "y"}]),
            providers=[{"provider_id": "or", "kind": "openai_compat"}],
        )
        assert provider_rows(built)[0].used_by == 1

    def test_an_unused_provider_reads_zero(self) -> None:
        built = panel(seat("a", "or", "x"), providers=[{"provider_id": "spare", "kind": "stub"}])
        assert provider_rows(built)[0].used_by == 0


class TestInstrumentRows:
    def draft(self, **row: Any) -> dict[str, Any]:
        return {"instruments": [{"symbol": "BTC/USDT", "venue": "sim", **row}]}

    def test_a_row_on_the_wired_venue_is_not_foreign(self) -> None:
        rows = instrument_rows(
            self.draft(), venue_id="sim", quarantined=(), holders={}, basket_id="demo"
        )
        assert rows[0].key == "sim:BTC/USDT"
        assert not rows[0].foreign

    def test_a_row_on_another_venue_is_foreign(self) -> None:
        """Its rules cannot be verified here and its prices come off a different book."""
        rows = instrument_rows(
            self.draft(venue="alpaca"), venue_id="sim", quarantined=(), holders={}, basket_id="demo"
        )
        assert rows[0].foreign

    def test_a_quarantined_row_says_so(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=("sim:BTC/USDT",),
            holders={},
            basket_id="demo",
        )
        assert rows[0].quarantined

    def test_a_row_another_basket_holds_names_it(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=(),
            holders={"sim:BTC/USDT": ("alpha",)},
            basket_id="demo",
        )
        assert rows[0].held_by == "alpha"

    def test_this_baskets_own_holding_is_not_a_conflict(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=(),
            holders={"sim:BTC/USDT": ("demo",)},
            basket_id="demo",
        )
        assert rows[0].held_by == ""


class TestMovedHelpers:
    def test_instrument_keys_skips_half_built_rows(self) -> None:
        draft = {"instruments": [{"symbol": "BTC/USDT", "venue": "sim"}, {"venue": "sim"}, {}]}
        assert instrument_keys(draft) == ("sim:BTC/USDT",)

    @pytest.mark.parametrize("draft", [{}, {"panel": {}}, {"panel": {"providers": "not a list"}}])
    def test_declared_providers_tolerates_a_half_built_draft(self, draft: dict[str, Any]) -> None:
        assert declared_providers(draft, "panel") == ()
