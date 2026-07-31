"""The versioned ConfigStore: what makes a past decision auditable, and what it refuses.

Four properties are load-bearing and each is tested directly:

* **Nothing is overwritten.** An update is a new version, and every old one still resolves — the
  event log pins version numbers, so an overwrite would leave those pins dangling.
* **Retirement keeps history.** A deleted basket's versions stay readable for the cycles that ran
  on them.
* **No secret can be stored.** Configuration references secrets by env-var *name* (PLAN §3.2).
* **Reads fail closed.** An unparseable document raises rather than falling back to a default —
  a bot that invents a risk policy is a bot that trades past a limit somebody set.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import update

from tradebot.control.config_store import SINGLETON_ID, ConfigStore
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, ConfigRef, GlobalRiskPolicy, PanelConfig, ProviderSettings
from tradebot.core.enums import BasketStatus, ConfigKind, ProviderKind
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.logging import SECRETS
from tradebot.persistence.schema import config_versions
from tradebot.persistence.store import EventStore

ACTOR = "test"


@pytest.fixture
def configs(store: EventStore, clock: ManualClock) -> ConfigStore:
    return ConfigStore(store.engine, store._writer, store, clock)


async def publish(configs: ConfigStore, basket: Basket, note: str = "") -> ConfigRef:
    record = await configs.put(basket.basket_id, basket, actor=ACTOR, note=note)
    return record.ref


class TestVersioning:
    async def test_the_first_version_is_one(self, configs: ConfigStore, basket: Basket) -> None:
        assert (await publish(configs, basket)).version == 1

    async def test_an_update_creates_a_version_rather_than_overwriting(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)
        renamed = basket.model_copy(update={"name": "renamed"})

        ref = await publish(configs, renamed)

        assert ref.version == 2
        assert configs.at(ConfigRef(kind=ConfigKind.BASKET, config_id="b1", version=1)).document
        assert configs.latest(ConfigKind.BASKET, "b1").document.name == "renamed"

    async def test_an_old_version_still_resolves_after_an_update(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        """A cycle pins a version; resolving it is how its decision stays auditable."""
        first = await publish(configs, basket)
        await publish(configs, basket.model_copy(update={"name": "renamed"}))

        assert configs.at(first).document.name == basket.name

    async def test_history_reads_oldest_first(self, configs: ConfigStore, basket: Basket) -> None:
        await publish(configs, basket, note="one")
        await publish(configs, basket.model_copy(update={"name": "two"}), note="two")

        assert [record.note for record in configs.history(ConfigKind.BASKET, "b1")] == [
            "one",
            "two",
        ]

    async def test_a_pin_that_was_never_stored_is_refused(self, configs: ConfigStore) -> None:
        with pytest.raises(ConfigError, match="no stored configuration"):
            configs.at(ConfigRef(kind=ConfigKind.BASKET, config_id="ghost", version=7))


class TestRetirement:
    async def test_a_retired_basket_leaves_the_current_set(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)

        await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR, reason="done")

        assert configs.baskets() == ()

    async def test_a_retired_basket_still_resolves_by_version(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        ref = await publish(configs, basket)
        await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR, reason="done")

        assert configs.at(ref).document.basket_id == "b1"

    async def test_retirement_carries_the_document_it_retired(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        """An operator reading the history sees *what* was retired, not an empty tombstone."""
        await publish(configs, basket)

        record = await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR, reason="done")

        assert record.retired
        assert record.document.basket_id == "b1"

    async def test_retiring_something_that_never_existed_is_refused(
        self, configs: ConfigStore
    ) -> None:
        with pytest.raises(ConfigError, match="no versions"):
            await configs.retire(ConfigKind.BASKET, "ghost", actor=ACTOR)

    async def test_republishing_after_retirement_brings_a_basket_back(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)
        await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR)

        ref = await publish(configs, basket)

        assert ref.version == 3
        assert len(configs.baskets()) == 1


class TestSingletons:
    async def test_the_global_policy_has_one_address_whatever_id_is_passed(
        self, configs: ConfigStore
    ) -> None:
        await configs.put("whatever", GlobalRiskPolicy(), actor=ACTOR)
        await configs.put(SINGLETON_ID, GlobalRiskPolicy(), actor=ACTOR)

        assert configs.global_risk().ref.version == 2

    async def test_no_policy_reads_as_none_rather_than_a_default(
        self, configs: ConfigStore
    ) -> None:
        """Fail closed: inventing Tier-2 limits nobody chose is worse than refusing to run."""
        assert configs.global_risk() is None

    async def test_a_retired_policy_is_not_in_force(self, configs: ConfigStore) -> None:
        await configs.put(SINGLETON_ID, GlobalRiskPolicy(), actor=ACTOR)
        await configs.retire(ConfigKind.GLOBAL_RISK, SINGLETON_ID, actor=ACTOR)

        assert configs.global_risk() is None

    async def test_retiring_a_singleton_under_any_id_retires_the_one_that_exists(
        self, configs: ConfigStore
    ) -> None:
        """A singleton has one address on the way out as well as on the way in."""
        await configs.put(SINGLETON_ID, GlobalRiskPolicy(), actor=ACTOR)

        await configs.retire(ConfigKind.GLOBAL_RISK, "whatever", actor=ACTOR)

        assert configs.global_risk() is None
        assert [r.ref.version for r in configs.history(ConfigKind.GLOBAL_RISK, SINGLETON_ID)] == [
            1,
            2,
        ]


class TestSecrets:
    async def test_a_document_carrying_a_registered_secret_is_refused(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        """`secret_ref` is only a control if something enforces it (PLAN §3.2)."""
        SECRETS.register("sk-live-0123456789abcdef")
        try:
            leaky = basket.model_copy(
                update={
                    "panel": PanelConfig(
                        panel_id="leaky",
                        seats=basket.panel.seats,
                        providers=(
                            ProviderSettings(
                                provider_id="stub",
                                kind=ProviderKind.OPENAI_COMPAT,
                                base_url="https://example.test/v1",
                                secret_ref="sk-live-0123456789abcdef",
                            ),
                        ),
                    )
                }
            )
            with pytest.raises(ConfigError, match="looks like a secret"):
                await publish(configs, leaky)
        finally:
            SECRETS.clear()

    async def test_a_secret_ref_by_name_is_stored_happily(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        SECRETS.register("sk-live-0123456789abcdef")
        try:
            named = basket.model_copy(
                update={
                    "panel": PanelConfig(
                        panel_id="named",
                        seats=basket.panel.seats,
                        providers=(
                            ProviderSettings(
                                provider_id="stub",
                                kind=ProviderKind.OPENAI_COMPAT,
                                base_url="https://example.test/v1",
                                secret_ref="OPENROUTER_API_KEY",
                            ),
                        ),
                    )
                }
            )
            assert (await publish(configs, named)).version == 1
        finally:
            SECRETS.clear()


class TestFailClosed:
    async def test_an_unparseable_document_raises_rather_than_defaulting(
        self, configs: ConfigStore, basket: Basket, store: EventStore
    ) -> None:
        await publish(configs, basket)
        with store.engine.begin() as connection:
            connection.execute(update(config_versions).values(document_json='{"basket_id": 1}'))

        with pytest.raises(ConfigError, match="does not validate"):
            configs.latest(ConfigKind.BASKET, "b1")

    async def test_a_model_that_is_not_a_config_kind_is_refused(
        self, configs: ConfigStore, instrument: Instrument
    ) -> None:
        with pytest.raises(ConfigError, match="not a stored configuration kind"):
            await configs.put("x", instrument, actor=ACTOR)


class TestAudit:
    async def test_publishing_emits_one_config_changed_event(
        self, configs: ConfigStore, basket: Basket, store: EventStore
    ) -> None:
        """The row and its audit record are written together or not at all."""
        await publish(configs, basket, note="why")

        events = [event for event in store.read_all() if event.type is EventType.CONFIG_CHANGED]
        assert len(events) == 1
        assert events[0].payload["version"] == 1
        assert events[0].payload["note"] == "why"
        assert events[0].payload["actor"] == ACTOR

    async def test_a_config_event_correlates_to_its_basket(
        self, configs: ConfigStore, basket: Basket, store: EventStore
    ) -> None:
        await publish(configs, basket)

        event = next(e for e in store.read_all() if e.type is EventType.CONFIG_CHANGED)
        assert event.basket_id == "b1"

    async def test_a_paused_basket_is_stored_and_read_back_paused(
        self, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket.model_copy(update={"status": BasketStatus.PAUSED}))

        assert configs.baskets()[0].document.status is BasketStatus.PAUSED

    async def test_decimal_limits_survive_the_round_trip_exactly(
        self, configs: ConfigStore
    ) -> None:
        """Money is stored as text; a numeric column would round-trip through a float."""
        policy = GlobalRiskPolicy(max_drawdown_pct=Decimal("7.25"))
        await configs.put(SINGLETON_ID, policy, actor=ACTOR)

        assert configs.global_risk().document.max_drawdown_pct == Decimal("7.25")
