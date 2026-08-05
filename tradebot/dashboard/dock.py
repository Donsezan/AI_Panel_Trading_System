"""The control dock and the risk-control pane: what an operator may do, and to what.

Pure assembly over already-fetched state, like `blotter.py`: the route fetches, this decides what
a control *offers*. So the rules below are testable without a browser, and the one that matters
most — that a button's meaning is the current state reversed — is asserted rather than eyeballed.

The four mechanisms the workspace keeps apart are the same four Control kept apart, and the
distinction is the reason this module names them separately rather than as one "status":

* **Stop** pauses cycling. It cancels nothing at the venue, needs no phrase, and is never refused.
* **A pause** is the operator's intent, published as a new basket version.
* **A halt** is the system stopping a basket for cause: database state, cleared only by a human
  typing the phrase.
* **A quarantine** is versioned configuration that lets the cycle run and refuses only the order
  (ADR 0022) — which is why releasing it is one click and pausing is not.

Two consequences are encoded here rather than left to a template:

* **An instrument excluded by its basket cannot be released by its own toggle.** `inherited` says
  so, and the row renders as excluded with no button that would silently do nothing.
* **A quarantine over a held position is consequential**, so `held_within` names exactly what the
  bot is about to stop managing — the second-click warning's material (ADR 0022).

Failure semantics: nothing here reads a store or a venue, so nothing here fails. A selection
naming a basket that is not in service yields no rows, which is what "nothing to act on" looks
like; the operator's way back to the full list is clearing the selection.
"""

from __future__ import annotations

from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tradebot.control.config_store import ConfigRecord
from tradebot.core.config import Basket
from tradebot.core.enums import BasketStatus
from tradebot.core.instrument import Instrument
from tradebot.dashboard.scope import Scope

__all__ = [
    "KILL_PHRASE",
    "QUARANTINE_CONFIRM",
    "WHOLE_BASKET",
    "BasketControls",
    "InstrumentControls",
    "PendingQuarantine",
    "Quarantine",
    "build",
    "held_within",
    "quarantines",
]

#: Typed to trip the kill switch by hand. Distinct from the re-arm phrase on purpose: the two acts
#: are opposites, and a single phrase that did both could be typed for the wrong one.
KILL_PHRASE = "STOP TRADING NOW"

#: Sent by the second click that quarantines a scope holding a position. Deliberately *not* a typed
#: phrase — quarantine is reversible configuration, and demanding one would make it feel like a
#: halt. What the click buys is that the consequence was read: from that moment the bot is
#: hands-off the position, and only a manual close will move it (ADR 0022).
QUARANTINE_CONFIRM = "quarantine-anyway"

#: The empty instrument key that means "the whole basket", in the form, in this module, and in
#: `RiskPolicy.with_quarantine`. One convention, so a blank field can never be read as a symbol.
WHOLE_BASKET = ""


@dataclass(frozen=True, slots=True)
class InstrumentControls:
    """One instrument, and the two things an operator may do to it from the dock."""

    basket_id: str
    instrument: Instrument
    #: Quarantined *by name* — the state this row's toggle reverses.
    named: bool
    #: Quarantined at all, by name or through its basket.
    excluded: bool
    #: The portfolio's holding, or `None` when flat.
    position: Any | None
    #: Whether a manual close may be built for it: held, and held by a basket in service.
    closable: bool

    @property
    def key(self) -> str:
        return self.instrument.key

    @property
    def scope(self) -> Scope:
        return Scope(self.basket_id, self.instrument.key)

    @property
    def inherited(self) -> bool:
        """Excluded because its whole basket is, so releasing it by name would do nothing."""
        return self.excluded and not self.named


@dataclass(frozen=True, slots=True)
class BasketControls:
    """One basket in the selection, its instruments, and what may be done to it as a whole."""

    record: ConfigRecord[Basket]
    instruments: tuple[InstrumentControls, ...]
    #: Why the *system* stopped this basket, or empty. Never the operator's own pause.
    halted_reason: str

    @property
    def basket(self) -> Basket:
        return self.record.document

    @property
    def basket_id(self) -> str:
        return self.record.ref.config_id

    @property
    def scope(self) -> Scope:
        return Scope(self.basket_id)

    @property
    def paused(self) -> bool:
        """The operator's own intent, which is the only thing the pause button reverses."""
        return not self.basket.status.may_trade

    @property
    def next_status(self) -> str:
        """What the pause/resume button publishes: this basket's status, reversed.

        Never derived from the halt: a halt is the system's doing and is cleared by its own typed
        act, so a resume published here leaves a halted basket halted — deliberately.
        """
        return (BasketStatus.ACTIVE if self.paused else BasketStatus.PAUSED).value

    @property
    def quarantined(self) -> bool:
        return self.basket.risk_policy.quarantined

    @property
    def closable(self) -> tuple[InstrumentControls, ...]:
        return tuple(row for row in self.instruments if row.closable)


@dataclass(frozen=True, slots=True)
class PendingQuarantine:
    """A quarantine that would leave a held position unmanaged, waiting for a second click."""

    basket_id: str
    #: Empty for a whole-basket quarantine — `WHOLE_BASKET`, as everywhere else here.
    instrument_key: str
    held: tuple[str, ...]

    @property
    def scope(self) -> str:
        return self.instrument_key or f"basket {self.basket_id}"


@dataclass(frozen=True, slots=True)
class Quarantine:
    """One exclusion in force, for the risk-control pane's "what is not being traded" list."""

    basket_id: str
    #: Empty for a whole-basket quarantine — `WHOLE_BASKET`, the form's own convention.
    instrument_key: str
    #: The positions this exclusion is currently holding the bot hands-off.
    held: tuple[str, ...]

    @property
    def whole_basket(self) -> bool:
        return self.instrument_key == WHOLE_BASKET

    @property
    def label(self) -> str:
        return self.instrument_key or "whole basket"


def build(
    records: Sequence[ConfigRecord[Basket]],
    *,
    positions: Sequence[Any],
    halted: dict[str, str],
    closable: Sequence[tuple[str, str]],
    scope: Scope | None = None,
) -> tuple[BasketControls, ...]:
    """The baskets the selection names, or every one of them when nothing is selected.

    Narrowing to the selection is what makes the dock a dock rather than a second Control page —
    but the fallback is deliberate: clearing the selection is always the way back to every action,
    so no exit from a position is ever more than one navigation away.
    """
    held = {row.instrument_key: row for row in positions if row.qty > 0}
    closable_keys = set(closable)
    return tuple(
        _basket_controls(record, held, closable_keys, halted, scope)
        for record in records
        if scope is None or scope.basket_id == record.ref.config_id
    )


def quarantines(
    records: Sequence[ConfigRecord[Basket]], *, positions: Sequence[Any]
) -> tuple[Quarantine, ...]:
    """Every exclusion currently in force, whole-basket ones first within each basket.

    Listed from configuration rather than from a projection because that is where a quarantine
    lives: it is a versioned document field, not a state the system records about itself.
    """
    held = {row.instrument_key for row in positions if row.qty > 0}
    return tuple(
        Quarantine(
            basket_id=record.ref.config_id,
            instrument_key=key,
            held=held_within(record.document, key, held),
        )
        for record in records
        for key in _excluded_keys(record.document)
    )


def held_within(basket: Basket, instrument_key: str, held: Container[str]) -> tuple[str, ...]:
    """The positions a quarantine of this scope leaves the bot hands-off.

    The material for the second-click warning, and for the risk-control pane's standing note that
    something excluded is also something still held. Inaction can compound a loss as readily as
    action can cause one, so the operator is told exactly what they are no longer managing.
    """
    scope = (instrument_key,) if instrument_key else tuple(i.key for i in basket.instruments)
    return tuple(key for key in scope if key in held)


def _excluded_keys(basket: Basket) -> Iterable[str]:
    """The scopes this basket excludes: the basket itself, then each instrument named."""
    policy = basket.risk_policy
    if policy.quarantined:
        yield WHOLE_BASKET
    yield from policy.quarantined_instruments


def _basket_controls(
    record: ConfigRecord[Basket],
    held: dict[str, Any],
    closable: set[tuple[str, str]],
    halted: dict[str, str],
    scope: Scope | None,
) -> BasketControls:
    basket_id = record.ref.config_id
    policy = record.document.risk_policy
    return BasketControls(
        record=record,
        instruments=tuple(
            InstrumentControls(
                basket_id=basket_id,
                instrument=instrument,
                named=instrument.key in policy.quarantined_instruments,
                excluded=policy.excludes(instrument.key),
                position=held.get(instrument.key),
                closable=(basket_id, instrument.key) in closable,
            )
            for instrument in record.document.instruments
            if scope is None or scope.instrument_key in (None, instrument.key)
        ),
        halted_reason=halted.get(basket_id, ""),
    )
