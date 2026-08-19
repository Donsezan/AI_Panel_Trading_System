"""Panel configuration: per-seat fallback chains, and the validation a GUI form leans on.

A panel is data, and Phase 6 puts a form in front of that data. Everything a form can get wrong
has to be caught here, at configuration time, because the alternative is a seat that looks
configured and is quietly short of the backup it promised — the failure that is hardest to notice
and most expensive to diagnose (DESIGN §6.5, §6.10, R11).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.config import (
    FREE,
    ModelPricing,
    PanelConfig,
    PriceList,
    ProviderBinding,
    ProviderSettings,
    SeatConfig,
)
from tradebot.core.enums import ProviderKind
from tradebot.decision.presets import FREE_PANEL, LOCAL_PANEL, PANELS, STUB_PANEL

OPENROUTER = ProviderSettings(
    provider_id="openrouter",
    kind=ProviderKind.OPENAI_COMPAT,
    base_url="https://openrouter.ai/api/v1",
    secret_ref="OPENROUTER_API_KEY",
)
LOCAL = ProviderSettings(
    provider_id="lmstudio",
    kind=ProviderKind.OPENAI_COMPAT,
    base_url="http://localhost:1234/v1",
)


def seat(seat_id: str, model: str, *fallbacks: ProviderBinding) -> SeatConfig:
    return SeatConfig(
        seat_id=seat_id,
        role="Analyst",
        provider_id="openrouter",
        model=model,
        fallbacks=fallbacks,
    )


class TestPerSeatChains:
    def test_each_seat_carries_its_own_chain(self) -> None:
        """The whole point: seat A's backup is not seat B's."""
        panel = PanelConfig(
            panel_id="p",
            providers=(OPENROUTER, LOCAL),
            seats=(
                seat("a", "model-a", ProviderBinding(provider_id="lmstudio", model="local-1")),
                seat("b", "model-b", ProviderBinding(provider_id="lmstudio", model="local-2")),
            ),
        )
        assert panel.fallback_plan() == {
            "a": ("openrouter:model-a", "lmstudio:local-1"),
            "b": ("openrouter:model-b", "lmstudio:local-2"),
        }

    def test_a_chain_is_attempted_in_configured_order(self) -> None:
        chain = (
            ProviderBinding(provider_id="lmstudio", model="local-1"),
            ProviderBinding(provider_id="openrouter", model="other"),
        )
        assert [b.fingerprint for b in seat("a", "model-a", *chain).bindings] == [
            "openrouter:model-a",
            "lmstudio:local-1",
            "openrouter:other",
        ]

    def test_a_seat_may_fall_back_to_the_same_provider_on_a_different_model(self) -> None:
        """Two models on one LM Studio is legitimate — it is the same *binding* that is not."""
        chain = seat("a", "model-a", ProviderBinding(provider_id="openrouter", model="model-z"))
        assert len(chain.bindings) == 2

    def test_repeating_a_binding_is_rejected(self) -> None:
        """That is a retry of something that just failed, not a fallback."""
        with pytest.raises(ValueError, match="openrouter:model-a"):
            seat("a", "model-a", ProviderBinding(provider_id="openrouter", model="model-a"))

    def test_repeating_a_binding_deeper_in_the_chain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="lmstudio:local-1"):
            seat(
                "a",
                "model-a",
                ProviderBinding(provider_id="lmstudio", model="local-1"),
                ProviderBinding(provider_id="lmstudio", model="local-1"),
            )


class TestSeatInstruction:
    """The operator's standing instruction for one seat — the panel's tunable text.

    It is versioned configuration like every other seat field, so a cycle's pinned basket version
    records the exact wording the panel deliberated under (ADR 0013).
    """

    def test_a_seat_carries_no_instruction_by_default(self) -> None:
        assert seat("a", "model-a").instruction == ""

    def test_an_instruction_survives_the_json_round_trip_the_store_persists_it_through(
        self,
    ) -> None:
        """`ConfigStore` writes `document_json` and reads it back; line breaks are the wording."""
        text = "Favour 4h structure over 15m noise." + chr(10) + "A failed breakout counts."
        original = SeatConfig(
            seat_id="a", role="Analyst", provider_id="openrouter", model="m", instruction=text
        )

        restored = SeatConfig.model_validate(original.model_dump(mode="json"))

        assert restored.instruction == text

    def test_an_instruction_longer_than_the_cap_is_refused(self) -> None:
        """Billed per seat, per round, per cycle — an accidental paste is a standing cost."""
        with pytest.raises(ValueError, match="at most 4000 characters"):
            SeatConfig(
                seat_id="a",
                role="Analyst",
                provider_id="openrouter",
                model="model-a",
                instruction="x" * 4001,
            )


class TestPanelDeclaresItsProviders:
    def test_a_binding_naming_an_undeclared_provider_is_rejected(self) -> None:
        """A GUI typo must fail here, not become a seat that silently skips its backup."""
        with pytest.raises(ValueError, match="a → gemni"):
            PanelConfig(
                panel_id="p",
                providers=(OPENROUTER,),
                seats=(seat("a", "model-a", ProviderBinding(provider_id="gemni", model="x")),),
            )

    def test_the_error_lists_what_is_available(self) -> None:
        with pytest.raises(ValueError, match="Declared: openrouter"):
            PanelConfig(
                panel_id="p",
                providers=(OPENROUTER,),
                seats=(seat("a", "model-a", ProviderBinding(provider_id="typo", model="x")),),
            )

    def test_a_primary_binding_is_checked_too(self) -> None:
        with pytest.raises(ValueError, match="a → openrouter"):
            PanelConfig(panel_id="p", providers=(LOCAL,), seats=(seat("a", "model-a"),))

    def test_duplicate_provider_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider ids must be unique"):
            PanelConfig(
                panel_id="p", providers=(OPENROUTER, OPENROUTER), seats=(seat("a", "model-a"),)
            )

    def test_a_panel_declaring_no_providers_is_wired_by_the_caller(self) -> None:
        """How the suite and the scenario harness build a panel: providers come from wiring."""
        panel = PanelConfig(panel_id="p", seats=(seat("a", "model-a"),))
        assert panel.providers == ()

    def test_a_declared_provider_can_be_looked_up(self) -> None:
        panel = PanelConfig(
            panel_id="p",
            providers=(LOCAL,),
            seats=(SeatConfig(seat_id="a", role="r", provider_id="lmstudio", model="local-1"),),
        )
        assert panel.provider("lmstudio").base_url.endswith(":1234/v1")
        with pytest.raises(KeyError):
            panel.provider("nope")


class TestProviderSettings:
    def test_an_http_provider_needs_an_endpoint(self) -> None:
        with pytest.raises(ValueError, match="needs a base_url"):
            ProviderSettings(provider_id="p", kind=ProviderKind.GEMINI)

    def test_the_stub_provider_needs_none(self) -> None:
        assert ProviderSettings(provider_id="stub", kind=ProviderKind.STUB).base_url == ""

    def test_settings_never_hold_a_key_only_its_name(self) -> None:
        """The indirection is the control: a key can then be absent from every row and log."""
        assert "OPENROUTER_API_KEY" in OPENROUTER.model_dump_json()
        assert OPENROUTER.secret_ref == "OPENROUTER_API_KEY"

    def test_prices_default_to_free(self) -> None:
        assert PriceList().for_model("anything") is FREE

    def test_prices_are_per_model(self) -> None:
        prices = PriceList(models={"paid": ModelPricing(prompt_per_million=Decimal("3"))})
        assert prices.for_model("paid").cost(1_000_000, 0) == Decimal(3)
        assert prices.for_model("free-slot").is_free


class TestSeededPanels:
    @pytest.mark.parametrize("panel_id", sorted(PANELS))
    def test_every_seeded_panel_is_internally_consistent(self, panel_id: str) -> None:
        """Validation runs at construction, so importing the presets is the assertion."""
        panel = PANELS[panel_id]
        declared = {p.provider_id for p in panel.providers}
        assert declared
        assert all(b.provider_id in declared for s in panel.seats for b in s.bindings)

    def test_the_free_panel_gives_each_seat_a_different_backup(self) -> None:
        """Three seats sharing one backup is one outage away from a panel of identical clones."""
        first_fallbacks = {
            seat.fallbacks[0].fingerprint for seat in FREE_PANEL.seats if seat.fallbacks
        }
        assert len(first_fallbacks) == len(FREE_PANEL.seats)

    def test_the_free_panel_seats_are_three_different_families(self) -> None:
        assert FREE_PANEL.is_heterogeneous
        assert FREE_PANEL.seat_count == 3

    def test_the_free_panel_has_exactly_one_devils_advocate(self) -> None:
        assert sum(seat.devils_advocate for seat in FREE_PANEL.seats) == 1

    def test_the_free_panel_chains_leave_openrouter_entirely(self) -> None:
        """A chain that stays inside one vendor does not survive that vendor's outage (R11)."""
        for seat_config in FREE_PANEL.seats:
            assert any(b.provider_id != "openrouter" for b in seat_config.fallbacks)

    def test_the_local_panel_needs_no_key_and_no_hosted_provider(self) -> None:
        assert all(p.secret_ref is None for p in LOCAL_PANEL.providers)
        assert LOCAL_PANEL.max_cost_usd_per_cycle == Decimal(0)

    def test_the_stub_panel_declares_only_the_offline_provider(self) -> None:
        assert [p.kind for p in STUB_PANEL.providers] == [ProviderKind.STUB]
