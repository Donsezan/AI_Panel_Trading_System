"""Comparing a stored basket's instruments against what the venue publishes now (ADR 0025).

A **Look up** button is convenience. This module is the guarantee: once every publish re-resolves
what it changed, it no longer matters whether a lot size was typed, pasted, or edited in devtools
— the only documents that can be stored are ones the venue agrees with.

Two questions, one comparison:

* **At publish** — do the instruments this edit *changed* still match the venue? Only the changed
  ones, deliberately. An instrument identical to the one in the current version keeps its pinned
  rules without a venue call, because otherwise a venue outage would block an operator from
  tightening a stop loss, turning a safety mechanism into a safety hazard.
* **While running** — has the venue changed a filter underneath us? Checked at startup and on the
  supervisor's resync tick, over *every* configured instrument.

**A venue that cannot be reached is not drift**, and the two callers part on exactly that. A
transport failure propagates out of `findings_for`: a publish converts it into a refusal, because
a basket whose rules cannot be verified is not a basket that gets published; the runtime watch
logs it and does nothing, because halting every basket on one bad second is an outage amplifier.
A `ConfigError` — the venue does not list it, lists it as delisted, or publishes no catalogue at
all — is a *finding*, because those are answers.

Failure semantics: nothing here writes, halts, or raises on a disagreement. It returns findings,
and the caller decides what they are worth in its mode (`DriftWatch`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.enums import ConfigKind, Mode, RiskTier
from tradebot.core.errors import ConfigError, TradebotError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.interfaces.exchange import InstrumentCatalogue
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.watchdog import Watchdog

logger = get_logger(__name__)

#: What a catalogue answers for, and therefore what is compared. Every one of these reaches a
#: money path: the four rules through `quantize_order` and the Tier-2 minimum check, and the two
#: currencies through the portfolio's quote currency and the base asset a position is held in.
#: `venue` and `asset_class` are checked separately, because a mismatch there means the catalogue
#: was never the right one to ask.
VERIFIED_FIELDS = (
    "base_currency",
    "quote_currency",
    "lot_size",
    "tick_size",
    "min_qty",
    "min_notional",
)

#: Which modes treat a runtime disagreement as a reason to stop. Live and paper do, and the reason
#: paper is strict has nothing to do with the word "sim": per DESIGN §9 the soak's primary venue is
#: `SimBroker` fed by live Binance data, and those cycles stamp `venue: sim` and *are* the evidence
#: base `report promotion` reads. A wrong lot size there makes the report describe a system that is
#: not the one which will trade. In `Mode.SIM` the same class is doing rehearsal against a
#: committed capture that cannot change without a human editing a file, so the check has nothing to
#: catch and halting on it would be cost without benefit.
HALTS_ON_DRIFT = frozenset({Mode.LIVE, Mode.PAPER})

DRIFT_RULE = "instrument_rules"


@dataclass(frozen=True, slots=True)
class RuleDrift:
    """One field of one instrument on which the stored document and the venue disagree."""

    instrument_key: str
    field: str
    pinned: str
    published: str

    def __str__(self) -> str:
        return (
            f"{self.instrument_key} {self.field}: this basket pins {self.pinned}, "
            f"the venue publishes {self.published}"
        )


def changed(
    instruments: Sequence[Instrument], previous: Sequence[Instrument]
) -> tuple[Instrument, ...]:
    """The instruments an edit actually touched — a new key, or any field of an existing one.

    The exemption that keeps fail-closed from meaning fail-useless. Comparing whole `Instrument`s
    rather than keys is what makes it safe: an operator who edits a lot size has changed the
    instrument, so it is re-resolved and refused; an operator who edits a stop multiple has not,
    so the publish costs no venue call and survives an outage.
    """
    pinned = {instrument.key: instrument for instrument in previous}
    return tuple(
        instrument for instrument in instruments if pinned.get(instrument.key) != instrument
    )


async def findings_for(
    catalogue: InstrumentCatalogue, instruments: Sequence[Instrument]
) -> tuple[str, ...]:
    """Every way these instruments disagree with what the venue publishes now.

    Raises whatever the transport raises when the venue cannot be reached — that is not a
    disagreement, and the caller's mode decides what it means.
    """
    findings: list[str] = []
    for instrument in instruments:
        findings.extend(await _findings_for_one(catalogue, instrument))
    return tuple(findings)


async def _findings_for_one(
    catalogue: InstrumentCatalogue, instrument: Instrument
) -> tuple[str, ...]:
    if instrument.venue != catalogue.venue_id:
        return (
            f"{instrument.key} names venue {instrument.venue!r}, but this process is wired to "
            f"{catalogue.venue_id!r}; its trading rules cannot be verified here, and the prices "
            "it would be sized from come off a different book",
        )
    try:
        market = await catalogue.resolve(instrument.symbol)
    except ConfigError as exc:
        return (str(exc),)
    if instrument.asset_class is not catalogue.asset_class:
        return (
            f"{instrument.key} is configured as {instrument.asset_class.value}, but "
            f"{catalogue.venue_id} lists it as {catalogue.asset_class.value}",
        )
    return tuple(
        str(RuleDrift(instrument.key, field, str(getattr(instrument, field)), str(published)))
        for field in VERIFIED_FIELDS
        if (published := getattr(market, field)) != getattr(instrument, field)
    )


async def verify_publish(
    catalogue: InstrumentCatalogue, basket: Basket, previous: Basket | None
) -> tuple[str, ...]:
    """Refusals for a basket about to be stored. Empty means the venue agrees with every change.

    Called from *every* path that publishes a basket, not only the edit form. The changed-only
    rule makes that free for the ones that touch no instrument — a quarantine toggle re-publishes
    the whole document and spends nothing here — which is what lets there be no side door.
    """
    edited = changed(basket.instruments, previous.instruments if previous else ())
    if not edited:
        return ()
    try:
        return await findings_for(catalogue, edited)
    except TradebotError as exc:
        return (
            f"{catalogue.venue_id} could not be reached to verify "
            f"{', '.join(sorted(i.key for i in edited))}: {exc}. Nothing was published — a basket "
            "whose trading rules cannot be checked against the venue is not one that gets stored",
        )


async def store_basket(
    configs: ConfigStore,
    catalogue: InstrumentCatalogue,
    basket: Basket,
    *,
    actor: str,
    note: str,
) -> ConfigRecord[Basket]:
    """Publish a basket, refusing any document whose trading rules the venue disagrees with.

    **The one write path for a basket**, so verification is not something a caller can forget —
    the pattern `control/manual_close.py` uses for orders, applied to configuration. It costs
    nothing for the acts that do not touch instruments: a pause and a quarantine toggle
    round-trip the stored document unchanged, so `changed` finds nothing and no venue is called.
    That also means an operator can still pause or quarantine a basket *whose rules have already
    drifted*, which is exactly when they most need to.

    Raises `ConfigError` carrying every finding, which the dashboard already renders as a refusal.
    """
    previous = configs.latest(ConfigKind.BASKET, basket.basket_id)
    findings = await verify_publish(
        catalogue, basket, previous.document if previous and previous.usable else None
    )
    if findings:
        raise ConfigError(
            f"{catalogue.venue_id} does not agree with this basket's instruments, so it was not "
            f"published: {'; '.join(findings)}"
        )
    return await configs.put(basket.basket_id, basket, actor=actor, note=note)


class DriftWatch:
    """Re-verifies configured instruments against the venue while the system runs.

    Two callers, one behaviour: the startup sequence, before anything cycles, and the supervisor's
    resync tick, so a filter changed mid-soak is caught in minutes rather than at the next restart.
    Both go through `check`, which is idempotent and safe to call as often as anyone likes — the
    catalogue caches its fetch, so a tick that finds nothing costs one dictionary comparison.
    """

    def __init__(
        self,
        catalogue: InstrumentCatalogue,
        configs: ConfigStore,
        watchdog: Watchdog,
        states: RiskStateStore,
        store: EventStore,
        clock: Clock,
        *,
        mode: Mode,
    ) -> None:
        self._catalogue = catalogue
        self._configs = configs
        self._watchdog = watchdog
        self._states = states
        self._store = store
        self._clock = clock
        self._mode = mode

    async def check(self) -> dict[str, tuple[str, ...]]:
        """Compare every basket in service, record what disagrees, and halt where it matters.

        Baskets are read fresh rather than held, because one published while the process runs is
        exactly the case a periodic check exists for. Returns the findings per basket so the
        startup sequence can report them; the halt and the `RISK_EVENT` already happened.
        """
        found: dict[str, tuple[str, ...]] = {}
        for record in self._configs.baskets():
            basket_id = record.ref.config_id
            try:
                findings = await findings_for(self._catalogue, record.document.instruments)
            except TradebotError as exc:
                # An unreachable venue is not drift. Halting every basket over one bad minute
                # would turn a transient outage into an incident that needs a human to clear.
                logger.warning(
                    "could not verify instrument rules against the venue",
                    extra={"venue": self._catalogue.venue_id, "error": str(exc)},
                )
                return found
            if findings and not self._already_halted(basket_id):
                found[basket_id] = findings
                await self._report(basket_id, findings)
        return found

    def _already_halted(self, basket_id: str) -> bool:
        """Whether this basket has already stopped for cause.

        Without it the resync tick would re-halt and re-alert every thirty seconds for as long as
        the disagreement stands, which is how an operator learns to ignore the alert that matters.
        The `RISK_EVENT` is written once, when the difference is first seen.
        """
        return not self._states.status_of(basket_id).may_trade

    async def _report(self, basket_id: str, findings: tuple[str, ...]) -> None:
        detail = "; ".join(findings)
        await self._store.append(
            EventFactory(clock=self._clock, basket_id=basket_id, cycle_id="reference").risk_event(
                tier=RiskTier.RECONCILIATION,
                rule=DRIFT_RULE,
                scope=basket_id,
                action="halted" if self._mode in HALTS_ON_DRIFT else "recorded",
                detail=detail,
            )
        )
        if self._mode not in HALTS_ON_DRIFT:
            logger.warning(
                "instrument rules disagree with the venue; sim keeps cycling",
                extra={"basket_id": basket_id, "detail": detail},
            )
            return
        await self._watchdog.halt_basket(
            basket_id,
            f"instrument trading rules disagree with {self._catalogue.venue_id}: {detail}. "
            "Re-publish the basket to re-resolve them",
        )
