"""The live panel probe: proving a seat can get an answer at all (ADR 0020).

A socket test would pass for a model id that no longer resolves and for a key the endpoint
rejects — R11 happening now rather than in theory. So the probe is a real completion, and these
tests are about what it does with the three answers it can get: yes, yes-but-on-the-backup, and no.
"""

from __future__ import annotations

import pytest

from tradebot.core.config import PanelConfig, ProviderBinding, ProviderSettings, SeatConfig
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import ProviderError
from tradebot.decision.probe import (
    PROBE_MAX_TOKENS,
    probe_panel,
)
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

PRIMARY = ProviderBinding(provider_id="hosted", model="free-slot")
BACKUP = ProviderBinding(provider_id="local", model="local-model")


class FakeProvider:
    """Answers, or fails the way every provider adapter is required to fail."""

    def __init__(self, provider_id: str, *, down: bool = False) -> None:
        self.provider_id = provider_id
        self._down = down
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if self._down:
            raise ProviderError(f"{self.provider_id} is unreachable")
        return CompletionResult(text="OK", model_fingerprint=f"{self.provider_id}/{request.model}")


def panel_with(*bindings: ProviderBinding) -> PanelConfig:
    """One seat whose chain is exactly these bindings, in order."""
    primary, *fallbacks = bindings
    return PanelConfig(
        panel_id="p",
        providers=tuple(
            ProviderSettings(
                provider_id=binding.provider_id,
                kind=ProviderKind.OPENAI_COMPAT,
                base_url=f"https://{binding.provider_id}.example/v1",
            )
            for binding in bindings
        ),
        seats=(
            SeatConfig(
                seat_id="technical",
                role="Technical Analyst",
                provider_id=primary.provider_id,
                model=primary.model,
                fallbacks=tuple(fallbacks),
            ),
        ),
    )


@pytest.fixture
def panel() -> PanelConfig:
    return panel_with(PRIMARY, BACKUP)


class TestReachable:
    async def test_a_seat_answering_on_its_primary_is_clean(self, panel: PanelConfig) -> None:
        result = await probe_panel(panel, {"hosted": FakeProvider("hosted")})
        assert result.ok
        assert result.substitutions == ()

    async def test_the_chain_stops_at_the_first_answer(self, panel: PanelConfig) -> None:
        """A cycle would stop there too, and every extra call is spend for nothing."""
        backup = FakeProvider("local")
        await probe_panel(panel, {"hosted": FakeProvider("hosted"), "local": backup})
        assert backup.requests == []

    async def test_a_seat_on_its_fallback_is_reported_but_not_a_failure(
        self, panel: PanelConfig
    ) -> None:
        """The chain exists so an outage is survivable (R11)."""
        result = await probe_panel(
            panel, {"hosted": FakeProvider("hosted", down=True), "local": FakeProvider("local")}
        )
        assert result.ok
        assert result.substitutions == (f"technical on {BACKUP.fingerprint}",)


class TestUnreachable:
    async def test_a_seat_with_no_working_binding_fails_by_name(self, panel: PanelConfig) -> None:
        result = await probe_panel(
            panel,
            {
                "hosted": FakeProvider("hosted", down=True),
                "local": FakeProvider("local", down=True),
            },
        )
        assert not result.ok
        assert "technical" in result.failures[0]
        assert "local is unreachable" in result.failures[0]

    async def test_an_undeclared_provider_fails_rather_than_raising(
        self, panel: PanelConfig
    ) -> None:
        """A panel whose providers the composition root never wired reaches nothing at all."""
        result = await probe_panel(panel, {})
        assert not result.ok
        assert "not among the panel's declared providers" in result.failures[0]

    async def test_the_label_names_the_basket_being_probed(self, panel: PanelConfig) -> None:
        result = await probe_panel(panel, {}, label="live")
        assert result.failures[0].startswith("live seat")


class TestCost:
    async def test_the_probe_is_small_short_and_asks_nothing_about_trading(
        self, panel: PanelConfig
    ) -> None:
        """A liveness check must not become a cost line, and an unvalidated trading opinion must
        not enter the log with no cycle to gate it."""
        provider = FakeProvider("hosted")
        await probe_panel(panel, {"hosted": provider})
        request = provider.requests[0]
        assert request.max_tokens == PROBE_MAX_TOKENS
        assert request.json_mode is False
        assert request.timeout_seconds <= 30.0
        assert "OK" in request.user
