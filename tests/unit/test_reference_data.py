"""Verifying a basket's instruments against the venue (ADR 0025).

Two questions with different answers, and these tests are organised around keeping them apart:
what a **publish** refuses, and what **drift under a running system** costs — which depends on
whether this mode's cycles are the promotion evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine

from tradebot.control.config_store import ConfigStore
from tradebot.control.reference import (
    DriftWatch,
    changed,
    findings_for,
    store_basket,
    verify_publish,
)
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy, PanelConfig, SeatConfig
from tradebot.core.enums import AssetClass, BasketStatus, ConfigKind, Mode
from tradebot.core.errors import ConfigError, VenueError
from tradebot.core.events import Event, EventType
from tradebot.core.instrument import Instrument
from tradebot.interfaces.exchange import IdType, VenueMarket
from tradebot.marketdata.catalogue import SimCatalogue
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.watchdog import Watchdog

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: What the venue publishes in every test below. Real Binance numbers, so a wrong one reads the
#: way it would in production rather than as an obviously invented value.
BTC = VenueMarket(
    symbol="BTC/USDT",
    base_currency="BTC",
    quote_currency="USDT",
    lot_size=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("5"),
)

PANEL = PanelConfig(
    panel_id="p1",
    seats=(SeatConfig(seat_id="s1", role="Analyst", provider_id="stub", model="stub"),),
)


def pinned(**overrides: Any) -> Instrument:
    """The instrument the catalogue agrees with — unless a test overrides a field to disagree."""
    fields: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "venue": "sim",
        "asset_class": AssetClass.CRYPTO,
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "lot_size": Decimal("0.00001"),
        "tick_size": Decimal("0.01"),
        "min_qty": Decimal("0.00001"),
        "min_notional": Decimal("5"),
    }
    return Instrument.model_validate(fields | overrides)


def stored_basket(*instruments: Instrument) -> Basket:
    return Basket(
        basket_id="demo", name="Demo", instruments=instruments or (pinned(),), panel=PANEL
    )


class Unreachable:
    """A venue that is simply not answering right now — an outage, not a disagreement."""

    venue_id = "sim"
    asset_class = AssetClass.CRYPTO

    def __init__(self) -> None:
        self.asked = 0

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        self.asked += 1
        raise VenueError("sim is unreachable")

    async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket:
        return (await self.list_markets())[0]


@pytest.fixture
def catalogue() -> SimCatalogue:
    return SimCatalogue([BTC], source="test capture", as_of=NOW)


@dataclass(frozen=True, slots=True)
class Harness:
    """One database and the four collaborators a `DriftWatch` writes through."""

    configs: ConfigStore
    states: RiskStateStore
    store: EventStore
    watchdog: Watchdog
    clock: ManualClock

    def watch(self, mode: Mode, catalogue: Any) -> DriftWatch:
        return DriftWatch(
            catalogue, self.configs, self.watchdog, self.states, self.store, self.clock, mode=mode
        )

    async def publish(self, basket: Basket) -> None:
        """Store a basket without verifying it — how a document written before drift got there."""
        await self.configs.put(basket.basket_id, basket, actor="test", note="")

    @property
    def drift_events(self) -> list[Event]:
        return [
            event
            for event in self.store.read_all()
            if event.type is EventType.RISK_EVENT
            and event.payload.get("rule") == "instrument_rules"
        ]


@pytest.fixture
def harness(database: tuple[Engine, SingleWriter], clock: ManualClock) -> Harness:
    engine, writer = database
    store = EventStore(engine, writer)
    states = RiskStateStore(engine, writer, clock)
    return Harness(
        configs=ConfigStore(engine, writer, store, clock),
        states=states,
        store=store,
        watchdog=Watchdog(GlobalRiskPolicy(), states, store, clock),
        clock=clock,
    )


class TestWhatCountsAsChanged:
    """The exemption that keeps fail-closed from meaning fail-useless."""

    def test_an_untouched_instrument_is_not_re_resolved(self) -> None:
        held = pinned()
        assert changed((held,), (held,)) == ()

    def test_a_new_instrument_is_changed(self) -> None:
        added = pinned(symbol="ETH/USDT", base_currency="ETH")
        assert changed((pinned(), added), (pinned(),)) == (added,)

    def test_editing_any_rule_makes_it_changed(self) -> None:
        edited = pinned(min_notional=Decimal("10"))
        assert changed((edited,), (pinned(),)) == (edited,)

    async def test_a_publish_that_touches_no_instrument_asks_no_venue(self) -> None:
        """Pausing, quarantining or tightening a stop must survive a venue outage — otherwise the
        safety mechanism becomes a safety hazard."""
        unreachable = Unreachable()
        current = stored_basket()
        edited = current.model_copy(update={"status": BasketStatus.PAUSED})

        assert await verify_publish(unreachable, edited, current) == ()
        assert unreachable.asked == 0


class TestFindings:
    async def test_an_agreeing_instrument_produces_nothing(self, catalogue: SimCatalogue) -> None:
        assert await findings_for(catalogue, (pinned(),)) == ()

    @pytest.mark.parametrize(
        ("field", "wrong"),
        [
            ("lot_size", Decimal("0.001")),
            ("tick_size", Decimal("1")),
            ("min_qty", Decimal("1")),
            ("min_notional", Decimal("10")),
            ("quote_currency", "USDC"),
            ("base_currency", "WBTC"),
        ],
    )
    async def test_every_verified_field_is_compared(
        self, catalogue: SimCatalogue, field: str, wrong: Any
    ) -> None:
        """Each one reaches a money path: four through quantization and the Tier-2 minimum, and
        two through the portfolio's quote currency and the asset a position is held in."""
        (finding,) = await findings_for(catalogue, (pinned(**{field: wrong}),))
        assert field in finding
        assert str(getattr(BTC, field)) in finding

    async def test_an_instrument_on_another_venue_cannot_be_verified_here(
        self, catalogue: SimCatalogue
    ) -> None:
        (finding,) = await findings_for(catalogue, (pinned(venue="binance"),))
        assert "wired to 'sim'" in finding

    async def test_a_symbol_the_venue_does_not_list_is_the_venues_own_refusal(
        self, catalogue: SimCatalogue
    ) -> None:
        (finding,) = await findings_for(
            catalogue, (pinned(symbol="FOO/USDT", base_currency="FOO"),)
        )
        assert "sim does not list 'FOO/USDT'" in finding

    async def test_a_delisted_symbol_is_refused_as_delisted(self) -> None:
        catalogue = SimCatalogue([BTC.model_copy(update={"tradable": False})])
        (finding,) = await findings_for(catalogue, (pinned(),))
        assert "delisted" in finding

    async def test_an_unreachable_venue_is_raised_not_reported_as_disagreement(self) -> None:
        """The two callers part exactly here: a publish turns this into a refusal, the runtime
        watch ignores it. Neither could tell the difference if it arrived as a finding."""
        with pytest.raises(VenueError):
            await findings_for(Unreachable(), (pinned(),))


class TestPublishRefusals:
    async def test_a_new_basket_has_every_instrument_verified(
        self, catalogue: SimCatalogue
    ) -> None:
        findings = await verify_publish(catalogue, stored_basket(pinned(lot_size=Decimal(1))), None)
        assert findings and "lot_size" in findings[0]

    async def test_an_unreachable_venue_refuses_the_publish_and_names_it(self) -> None:
        (finding,) = await verify_publish(Unreachable(), stored_basket(), None)
        assert "could not be reached to verify sim:BTC/USDT" in finding
        assert "Nothing was published" in finding


class TestStoreBasketIsTheOnlyWritePath:
    async def test_a_disagreeing_document_is_never_stored(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        with pytest.raises(ConfigError, match="does not agree"):
            await store_basket(
                harness.configs,
                catalogue,
                stored_basket(pinned(min_notional=Decimal("10"))),
                actor="test",
                note="",
            )

        assert harness.configs.latest(ConfigKind.BASKET, "demo") is None

    async def test_an_agreeing_document_is_stored(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        record = await store_basket(
            harness.configs, catalogue, stored_basket(), actor="test", note="seeded"
        )
        assert record.ref.version == 1


class TestDriftUnderARunningSystem:
    """Decision 5: the response scales with whether this mode's cycles are evidence."""

    @pytest.mark.parametrize("mode", [Mode.LIVE, Mode.PAPER])
    async def test_live_and_paper_halt_the_affected_basket(
        self, harness: Harness, catalogue: SimCatalogue, mode: Mode
    ) -> None:
        await harness.publish(stored_basket(pinned(min_notional=Decimal("10"))))

        found = await harness.watch(mode, catalogue).check()

        assert "demo" in found
        assert not harness.states.status_of("demo").may_trade
        assert harness.drift_events

    async def test_sim_records_it_and_keeps_cycling(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """A committed capture cannot change without a human editing a file, so there is nothing
        to catch — and halting a rehearsal is cost without benefit."""
        await harness.publish(stored_basket(pinned(min_notional=Decimal("10"))))

        found = await harness.watch(Mode.SIM, catalogue).check()

        assert "demo" in found
        assert harness.states.status_of("demo").may_trade
        assert harness.drift_events

    async def test_an_agreeing_basket_is_left_alone(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        await harness.publish(stored_basket())

        assert await harness.watch(Mode.PAPER, catalogue).check() == {}
        assert harness.states.status_of("demo").may_trade
        assert not harness.drift_events

    async def test_an_already_halted_basket_is_not_re_halted_every_sweep(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """The sweep runs every thirty seconds. Re-alerting for as long as the disagreement stands
        is how an operator learns to ignore the alert that matters."""
        await harness.publish(stored_basket(pinned(min_notional=Decimal("10"))))
        watch = harness.watch(Mode.PAPER, catalogue)

        await watch.check()
        await watch.check()

        assert len(harness.drift_events) == 1

    async def test_an_unreachable_venue_halts_nothing(self, harness: Harness) -> None:
        """Turning one bad minute into an incident a human must clear is an outage amplifier."""
        await harness.publish(stored_basket(pinned(min_notional=Decimal("10"))))

        assert await harness.watch(Mode.PAPER, Unreachable()).check() == {}
        assert harness.states.status_of("demo").may_trade
        assert not harness.drift_events
