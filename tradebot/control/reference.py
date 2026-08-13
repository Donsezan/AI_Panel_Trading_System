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

#: The rule name on the `RISK_EVENT` an overlap writes. Separate from `DRIFT_RULE` because they are
#: different faults with different severities: drift is the venue changing something under us, this
#: is a configuration that is internally inconsistent.
EXCLUSIVITY_RULE = "instrument_exclusivity"


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


def holders_of(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]:
    """Every basket in service holding each instrument key.

    A tuple rather than one id, because the whole point is to notice when there is more than one.
    `ConfigStore.baskets()` already excludes retired documents: a retired basket cycles nothing, so
    it cannot be a second writer, and counting it would make an id unusable forever.
    """
    held: dict[str, tuple[str, ...]] = {}
    for record in records:
        for instrument in record.document.instruments:
            held[instrument.key] = (*held.get(instrument.key, ()), record.ref.config_id)
    return held


def exclusive_findings(
    records: Sequence[ConfigRecord[Basket]], basket_id: str, edited: Sequence[Instrument]
) -> tuple[str, ...]:
    """Refusals for instruments this edit takes from another basket (ADR 0026).

    Over `edited` only, exactly as the venue verification is, and for the same reason: a database
    written before this rule can already hold an overlap, and the operator's way out of it is to
    pause or edit a basket. A check that blocked the fix would be a safety hazard.
    """
    held = holders_of(records)
    return tuple(
        f"{instrument.key} is already held by basket {other!r}. An instrument belongs to exactly "
        "one basket: positions are the portfolio's and are keyed by instrument alone, so a second "
        "basket would size against a holding it does not own, leave its protective legs resting "
        "over someone else's exit, and split this instrument's cooldown and daily cap in two"
        for instrument in edited
        for other in held.get(instrument.key, ())
        if other != basket_id
    )


def overlaps(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]:
    """Per basket, a finding for every instrument another basket in service also holds.

    Both sides are reported and both are halted: there is no way to tell which basket is the
    mistake, and leaving one cycling means it keeps trading an instrument whose position history
    is already contaminated by the other.
    """
    held = holders_of(records)
    found = {}
    for record in records:
        basket_id = record.ref.config_id
        findings = tuple(
            f"{instrument.key} is also held by basket {other!r}; an instrument belongs to exactly "
            "one basket in service. Remove it from all but one and re-publish"
            for instrument in record.document.instruments
            for other in held.get(instrument.key, ())
            if other != basket_id
        )
        if findings:
            found[basket_id] = findings
    return found


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
    catalogue: InstrumentCatalogue, edited: Sequence[Instrument]
) -> tuple[str, ...]:
    """Refusals for a basket about to be stored. Empty means the venue agrees with every change.

    Called from *every* path that publishes a basket, not only the edit form. `edited` comes from
    the caller so that one `changed()` result serves both this and the exclusivity check, which
    share the exemption exactly.
    """
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

    Two checks over the same `changed()` set: the venue must agree with every trading rule, and no
    other basket in service may already hold the instrument (ADR 0026). Sharing the exemption is
    what lets a pause or a quarantine still be published on a basket that is *already* wrong in
    either way — which is exactly when an operator needs it.

    Raises `ConfigError` carrying every finding, which the dashboard already renders as a refusal.

    Held under `configs.publishing()` end to end. `configs.latest` and `configs.baskets()` are
    plain reads outside `SingleWriter`'s lock, which only serializes `put` itself — without this,
    two concurrent publishes of two *different* baskets over the same currently-free instrument
    could each read before either commits, each pass `exclusive_findings` against a snapshot that
    does not yet include the other's write, and both land, producing exactly the overlap this
    function exists to prevent. The lock closes that window by making the whole read-check-write
    one unit, not only the write at the end.
    """
    # Read (latest, baskets), decide (the two checks below), write (put) — held as one unit so
    # two concurrent publishes of different baskets over the same free instrument cannot both
    # read before either commits and both pass exclusive_findings. See the docstring above.
    async with configs.publishing():
        previous = configs.latest(ConfigKind.BASKET, basket.basket_id)
        current = previous.document if previous and previous.usable else None
        edited = changed(basket.instruments, current.instruments if current else ())
        findings = await verify_publish(catalogue, edited) + exclusive_findings(
            configs.baskets(), basket.basket_id, edited
        )
        if findings:
            raise ConfigError(f"this basket was not published: {'; '.join(findings)}")
        return await configs.put(basket.basket_id, basket, actor=actor, note=note)


class DriftWatch:
    """Re-verifies configured instruments against the venue, and against each other, while the
    system runs.

    Two callers, one behaviour: the startup sequence, before anything cycles, and the supervisor's
    resync tick, so a filter changed mid-soak is caught in minutes rather than at the next restart.
    Both go through `check`, which is idempotent and safe to call as often as anyone likes — the
    catalogue caches its fetch, so a tick that finds nothing costs one dictionary comparison.

    `check` enforces two independent things: whether the venue still agrees with each basket's
    instruments, and whether two baskets in service now hold the same instrument (ADR 0026) — the
    latter is pure configuration, needs no venue at all, and must keep working when the venue does
    not.
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

        Overlap and drift are gathered in two separate passes before anything is reported. Only
        `_drift_for` makes a venue call, and only it can fail — `overlaps` reads nothing but the
        baskets already loaded here. Computing them independently, and reporting from both maps
        together, is what keeps a venue outage from silently disabling overlap detection: a
        basket sharing an instrument with another must halt whether or not the venue answers.
        """
        records = self._configs.baskets()
        shared = overlaps(records)
        drifted = await self._drift_for(records)
        found: dict[str, tuple[str, ...]] = {}
        for record in records:
            basket_id = record.ref.config_id
            drift = drifted.get(basket_id, ())
            taken = shared.get(basket_id, ())
            if not (drift or taken) or self._already_halted(basket_id):
                continue
            found[basket_id] = drift + taken
            # Two rules, two events: the log must say which fault this was, and they do not share
            # a severity. Venue drift is an outside event whose sim analogue is inert — a committed
            # capture cannot change under a running system — so it is keyed to the mode. An overlap
            # is an internally inconsistent configuration, equally wrong in sim, and it corrupts
            # round-trip attribution and the loss streak, which is what `report promotion` reads.
            halts = bool(taken) or self._mode in HALTS_ON_DRIFT
            if drift:
                await self._record(basket_id, DRIFT_RULE, drift, halts=halts)
            if taken:
                await self._record(basket_id, EXCLUSIVITY_RULE, taken, halts=True)
            if halts:
                await self._watchdog.halt_basket(basket_id, self._reason(drift, taken))
        return found

    async def _drift_for(
        self, records: Sequence[ConfigRecord[Basket]]
    ) -> dict[str, tuple[str, ...]]:
        """Venue drift for every basket checked before the venue, if it fails, stops answering.

        Isolated from the overlap check on purpose: `findings_for` is the only venue call this
        module makes while running, and an unreachable venue is not drift — halting every later
        basket over one bad minute would turn a transient outage into an incident that needs a
        human to clear. So this stops at the first failure and hands back whatever it already
        has, rather than raising and taking the overlap check (which asked the venue nothing)
        down with it.
        """
        drifted: dict[str, tuple[str, ...]] = {}
        for record in records:
            try:
                drifted[record.ref.config_id] = await findings_for(
                    self._catalogue, record.document.instruments
                )
            except TradebotError as exc:
                logger.warning(
                    "could not verify instrument rules against the venue",
                    extra={"venue": self._catalogue.venue_id, "error": str(exc)},
                )
                break
        return drifted

    def _already_halted(self, basket_id: str) -> bool:
        """Whether this basket has already stopped for cause.

        Without it the resync tick would re-halt and re-alert every thirty seconds for as long as
        the disagreement stands, which is how an operator learns to ignore the alert that matters.
        The `RISK_EVENT` is written once, when the difference is first seen.
        """
        return not self._states.status_of(basket_id).may_trade

    async def _record(
        self, basket_id: str, rule: str, findings: tuple[str, ...], *, halts: bool
    ) -> None:
        await self._store.append(
            EventFactory(clock=self._clock, basket_id=basket_id, cycle_id="reference").risk_event(
                tier=RiskTier.RECONCILIATION,
                rule=rule,
                scope=basket_id,
                action="halted" if halts else "recorded",
                detail="; ".join(findings),
            )
        )
        if not halts:
            logger.warning(
                "instrument rules disagree with the venue; sim keeps cycling",
                extra={"basket_id": basket_id, "detail": "; ".join(findings)},
            )

    def _reason(self, drift: tuple[str, ...], taken: tuple[str, ...]) -> str:
        """One halt, naming everything that caused it, so the operator fixes it in one pass.

        A basket can have both faults at once, and dropping either would send the operator
        through a second halt for the one this summary didn't mention — precisely the "one pass"
        promise this exists to keep. Each clause is only appended when it applies, so a
        single-fault basket reads exactly as it always has.
        """
        reasons = []
        if taken:
            reasons.append(f"instruments are held by more than one basket: {'; '.join(taken)}")
        if drift:
            reasons.append(
                f"instrument trading rules disagree with {self._catalogue.venue_id}: "
                f"{'; '.join(drift)}. Re-publish the basket to re-resolve them"
            )
        return " Also, ".join(reasons)
