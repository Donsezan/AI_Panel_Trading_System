"""Verifying a basket's instruments against the venue (ADR 0025).

Two questions with different answers, and these tests are organised around keeping them apart:
what a **publish** refuses, and what **drift under a running system** costs — which depends on
whether this mode's cycles are the promotion evidence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.reference import (
    DriftWatch,
    changed,
    exclusive_findings,
    findings_for,
    holders_of,
    overlaps,
    store_basket,
    verify_publish,
)
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, ConfigRef, GlobalRiskPolicy, PanelConfig, SeatConfig
from tradebot.core.enums import AssetClass, BasketStatus, ConfigKind, Mode
from tradebot.core.errors import ConfigError, VenueError
from tradebot.core.events import Event, EventType
from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel
from tradebot.interfaces.exchange import IdType, VenueMarket
from tradebot.marketdata.catalogue import SimCatalogue
from tradebot.persistence.database import SingleWriter, create_database
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


def _record(basket: Basket, version: int) -> ConfigRecord[Basket]:
    """A stored basket as `ConfigStore.baskets()` hands it back."""
    return ConfigRecord(
        ref=ConfigRef(kind=ConfigKind.BASKET, config_id=basket.basket_id, version=version),
        document=basket,
    )


class Unreachable:
    """A venue that is simply not answering right now — an outage, not a disagreement."""

    venue_id = "sim"
    asset_class = AssetClass.CRYPTO
    source: str = ""
    as_of: datetime | None = None

    def __init__(self) -> None:
        self.asked = 0

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        self.asked += 1
        raise VenueError("sim is unreachable")

    async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket:
        return (await self.list_markets())[0]


class _SlowPublishConfigStore(ConfigStore):
    """A `ConfigStore` whose `put` pauses before doing its real work.

    Nothing in `store_basket`'s read-check chain — `configs.latest`, `configs.baskets()`,
    `SimCatalogue.resolve` — ever actually suspends: none of it is real I/O, so two concurrent
    `store_basket` calls run their whole read-check synchronously and only ever interleave at
    `configs.put`'s real `run_in_executor` call, by which point one has usually already committed
    (observed empirically: over a dozen unlocked runs against real thread timing, the second
    call's read consistently already saw the first's write, so the bug never showed up — see the
    report). This subclass makes the window deterministic instead of leaving it to who wins that
    race: delaying `put` guarantees both calls finish their *own* read-check while neither has
    written yet, exactly the interleaving `ConfigStore.publishing()` exists to rule out.
    """

    async def put(
        self, config_id: str, document: DomainModel, *, actor: str, note: str = ""
    ) -> ConfigRecord[Any]:
        await asyncio.sleep(0.05)
        return await super().put(config_id, document, actor=actor, note=note)


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

    @property
    def exclusivity_events(self) -> list[Event]:
        return [
            event
            for event in self.store.read_all()
            if event.type is EventType.RISK_EVENT
            and event.payload.get("rule") == "instrument_exclusivity"
        ]

    def halt_reason(self, basket_id: str) -> str:
        """The reason on the most recent halt of this basket — the summary an operator reads."""
        events = [
            event
            for event in self.store.read_all()
            if event.type is EventType.BASKET_STATUS_CHANGED
            and event.payload.get("basket_id") == basket_id
            and event.payload.get("status") == BasketStatus.HALTED.value
        ]
        return str(events[-1].payload["reason"])


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

        touched = changed(edited.instruments, current.instruments)

        assert touched == ()
        assert await verify_publish(unreachable, touched) == ()
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
        basket = stored_basket(pinned(lot_size=Decimal(1)))
        findings = await verify_publish(catalogue, basket.instruments)
        assert findings and "lot_size" in findings[0]

    async def test_an_unreachable_venue_refuses_the_publish_and_names_it(self) -> None:
        basket = stored_basket()
        (finding,) = await verify_publish(Unreachable(), basket.instruments)
        assert "could not be reached to verify sim:BTC/USDT" in finding
        assert "Nothing was published" in finding


class TestStoreBasketIsTheOnlyWritePath:
    async def test_a_disagreeing_document_is_never_stored(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        with pytest.raises(ConfigError, match="was not published"):
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


class TestExclusivity:
    """An instrument belongs to exactly one basket in service (ADR 0026).

    Positions are keyed by `instrument_key` alone and baskets cycle as concurrent tasks, so two
    baskets holding one instrument oversell a holding through reduce-only, leave a protective leg
    resting against a position that is gone, attribute a round trip to whichever closed it, and
    double every metering limit.
    """

    def test_holders_names_every_basket_holding_each_key(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        held = holders_of((_record(alpha, 1), _record(beta, 1)))

        assert held == {"sim:BTC/USDT": ("alpha", "beta")}

    def test_an_untaken_instrument_produces_no_finding(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        free = pinned(symbol="SOL/USDT", base_currency="SOL")

        assert exclusive_findings((_record(alpha, 1),), "beta", (free,)) == ()

    def test_a_basket_does_not_conflict_with_its_own_previous_version(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})

        assert exclusive_findings((_record(alpha, 1),), "alpha", (pinned(),)) == ()

    def test_taking_another_baskets_instrument_is_refused_by_name(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})

        findings = exclusive_findings((_record(alpha, 1),), "beta", (pinned(),))

        assert len(findings) == 1
        assert "sim:BTC/USDT" in findings[0]
        assert "'alpha'" in findings[0]

    async def test_publishing_a_basket_that_takes_a_held_instrument_is_refused(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        await harness.publish(alpha)
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        with pytest.raises(ConfigError) as refusal:
            await store_basket(harness.configs, catalogue, beta, actor="test", note="")

        assert "already held by basket 'alpha'" in str(refusal.value)
        assert {r.ref.config_id for r in harness.configs.baskets()} == {"alpha"}

    async def test_pausing_a_basket_that_already_overlaps_is_allowed(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """The exemption that keeps fail-closed from meaning fail-useless.

        A database written before this rule existed can hold an overlap, and the operator's way
        out of it is to pause or edit a basket. A check that blocked the fix would be a safety
        hazard rather than a safety mechanism.
        """
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})
        await harness.publish(alpha)
        await harness.publish(beta)

        paused = beta.model_copy(update={"status": BasketStatus.PAUSED})
        record = await store_basket(harness.configs, catalogue, paused, actor="test", note="")

        assert record.document.status is BasketStatus.PAUSED

    async def test_a_new_version_keeping_its_own_instruments_publishes(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        await harness.publish(alpha)

        edited = alpha.model_copy(update={"name": "renamed"})
        record = await store_basket(harness.configs, catalogue, edited, actor="test", note="")

        assert record.ref.version == 2

    async def test_concurrent_publishes_over_the_same_instrument_cannot_both_land(
        self, tmp_path: Path, clock: ManualClock, catalogue: SimCatalogue
    ) -> None:
        """The race this task exists to close (DESIGN §5: one asyncio process).

        `configs.latest` and `configs.baskets()` are plain reads outside `SingleWriter`'s lock,
        which only ever serializes `put` itself. Without `ConfigStore.publishing()` held across
        the whole read-check-write, two concurrent publishes of two *different* baskets over the
        same currently-free instrument can each read before either commits, each pass
        `exclusive_findings` against a snapshot that does not yet show the other's write, and both
        land — exactly the overlap ADR 0026 exists to prevent.

        `_SlowPublishConfigStore` forces the interleaving deterministically rather than leaving it
        to thread-timing luck (which, empirically, does not reproduce this — see the report): it
        delays `put` itself, guaranteeing both calls finish their own read-check while neither has
        written yet, which is exactly the window `configs.publishing()` must close.

        A file-backed database, not the shared in-memory one `harness` uses: `SingleWriter` writes
        on its own thread, and only a real per-checkout connection lets that write and a
        concurrent read behave as they would outside the test, rather than risking the StaticPool
        rollback-on-checkin trap the in-memory harness is prone to (see `CLAUDE.md`).
        """
        engine = create_database(tmp_path / "exclusivity.db")
        writer = SingleWriter(engine)
        try:
            configs = _SlowPublishConfigStore(engine, writer, EventStore(engine, writer), clock)
            alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
            beta = stored_basket().model_copy(update={"basket_id": "beta"})

            results = await asyncio.gather(
                store_basket(configs, catalogue, alpha, actor="test", note=""),
                store_basket(configs, catalogue, beta, actor="test", note=""),
                return_exceptions=True,
            )

            succeeded = [r for r in results if isinstance(r, ConfigRecord)]
            refused = [r for r in results if isinstance(r, ConfigError)]
            assert len(succeeded) == 1
            assert len(refused) == 1
            assert {r.ref.config_id for r in configs.baskets()} == {succeeded[0].ref.config_id}
        finally:
            writer.close()

    def test_overlaps_reports_both_sides(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        found = overlaps((_record(alpha, 1), _record(beta, 1)))

        assert set(found) == {"alpha", "beta"}
        assert "'beta'" in found["alpha"][0]
        assert "'alpha'" in found["beta"][0]

    @pytest.mark.parametrize("mode", [Mode.SIM, Mode.PAPER, Mode.LIVE])
    async def test_an_overlap_halts_every_basket_involved_in_every_mode(
        self, harness: Harness, catalogue: SimCatalogue, mode: Mode
    ) -> None:
        """Unlike venue drift, this is not keyed to the mode.

        Drift is an outside event whose sim analogue is inert — a committed capture cannot change
        under a running system. An overlap is an internally inconsistent configuration, equally
        wrong everywhere, and it corrupts round-trip attribution and the loss streak, which is
        what `report promotion` reads.
        """
        for basket_id in ("alpha", "beta"):
            await harness.publish(stored_basket().model_copy(update={"basket_id": basket_id}))

        found = await harness.watch(mode, catalogue).check()

        assert set(found) == {"alpha", "beta"}
        assert harness.states.status_of("alpha") is BasketStatus.HALTED
        assert harness.states.status_of("beta") is BasketStatus.HALTED
        assert {e.payload["action_taken"] for e in harness.exclusivity_events} == {"halted"}

    async def test_an_overlap_is_reported_once(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """A resync tick every thirty seconds must not re-alert for as long as it stands."""
        for basket_id in ("alpha", "beta"):
            await harness.publish(stored_basket().model_copy(update={"basket_id": basket_id}))
        watch = harness.watch(Mode.SIM, catalogue)

        await watch.check()
        await watch.check()

        assert len(harness.exclusivity_events) == 2  # one per basket, not four

    async def test_a_basket_with_its_own_instruments_is_left_alone(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        await harness.publish(stored_basket().model_copy(update={"basket_id": "alpha"}))

        assert await harness.watch(Mode.SIM, catalogue).check() == {}
        assert harness.states.status_of("alpha") is BasketStatus.ACTIVE

    async def test_a_basket_with_both_faults_names_both_in_the_halt_reason(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """`_reason` promises one halt naming everything, so an operator fixes it in one pass.

        Without this, an operator who saw only the overlap text would republish having fixed
        just that, and hit the same halt again from drift they were never told about.
        """
        await harness.publish(
            stored_basket(pinned(min_notional=Decimal("10"))).model_copy(
                update={"basket_id": "alpha"}
            )
        )
        await harness.publish(stored_basket().model_copy(update={"basket_id": "beta"}))

        await harness.watch(Mode.PAPER, catalogue).check()

        reason = harness.halt_reason("alpha")
        assert "held by more than one basket" in reason
        assert "min_notional" in reason

    async def test_an_overlap_still_halts_when_the_venue_is_unreachable(
        self, harness: Harness
    ) -> None:
        """`overlaps` reads only the baskets already loaded here — no venue call at all — so a
        venue outage must not suppress it. This is the whole point of the task: an overlap halts
        in every mode, not only in the modes where the venue happens to be answering.
        """
        for basket_id in ("alpha", "beta"):
            await harness.publish(stored_basket().model_copy(update={"basket_id": basket_id}))

        found = await harness.watch(Mode.PAPER, Unreachable()).check()

        assert set(found) == {"alpha", "beta"}
        assert harness.states.status_of("alpha") is BasketStatus.HALTED
        assert harness.states.status_of("beta") is BasketStatus.HALTED
        assert harness.exclusivity_events
        assert not harness.drift_events
