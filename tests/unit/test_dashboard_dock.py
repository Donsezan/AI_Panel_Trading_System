"""The control dock's rows: which act is offered, for what, and with which label.

The load-bearing assertions are the ones about what an operator would *believe*:

* A button's label is the current state reversed, so pressing it does what it says.
* An instrument excluded through its basket offers no release of its own — one that published a
  version changing nothing would read as a released instrument that is still excluded.
* Resuming a basket is never derived from its halt: a halt is the system's doing and is cleared by
  its own typed act (ADR 0021), so a pause toggle must not offer to undo it.
* A quarantine over a held position names what it strands, because that is the material for the
  second click and for the standing note beside it (ADR 0022).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tradebot.control.config_store import ConfigRecord
from tradebot.core.config import Basket, ConfigRef, PanelConfig, RiskPolicy
from tradebot.core.enums import BasketStatus, ConfigKind
from tradebot.core.instrument import Instrument
from tradebot.dashboard import dock
from tradebot.dashboard.scope import Scope

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def record(
    basket_id: str,
    instruments: tuple[Instrument, ...],
    panel: PanelConfig,
    *,
    status: BasketStatus = BasketStatus.ACTIVE,
    policy: RiskPolicy | None = None,
) -> ConfigRecord[Basket]:
    return ConfigRecord(
        ref=ConfigRef(kind=ConfigKind.BASKET, config_id=basket_id, version=1),
        document=Basket(
            basket_id=basket_id,
            name=f"basket {basket_id}",
            instruments=instruments,
            panel=panel,
            status=status,
            risk_policy=policy or RiskPolicy(),
        ),
        actor="test",
        created_at=NOW,
    )


def position(instrument_key: str, qty: str = "0.5") -> SimpleNamespace:
    return SimpleNamespace(instrument_key=instrument_key, qty=Decimal(qty))


def build(records: Any, **overrides: Any) -> tuple[dock.BasketControls, ...]:
    defaults: dict[str, Any] = {"positions": (), "halted": {}, "closable": ()}
    return dock.build(records, **{**defaults, **overrides})


@pytest.fixture
def two(
    instrument: Instrument, second_instrument: Instrument, panel: PanelConfig
) -> tuple[ConfigRecord[Basket], ...]:
    return (
        record("b1", (instrument, second_instrument), panel),
        record("b2", (instrument,), panel),
    )


# ---------------------------------------------------------------- the selection


def test_no_selection_offers_every_basket(two: Any) -> None:
    """Clearing the selection is the way back to every act, so no exit is ever hidden."""
    rows = build(two)

    assert [row.basket_id for row in rows] == ["b1", "b2"]
    assert [len(row.instruments) for row in rows] == [2, 1]


def test_a_basket_scope_offers_that_basket_alone(two: Any) -> None:
    rows = build(two, scope=Scope("b2"))

    assert [row.basket_id for row in rows] == ["b2"]


def test_an_instrument_scope_narrows_to_that_instrument(two: Any, instrument: Instrument) -> None:
    rows = build(two, scope=Scope("b1", instrument.key))

    assert [line.key for line in rows[0].instruments] == [instrument.key]


def test_a_scope_naming_nothing_in_service_offers_nothing(two: Any) -> None:
    assert build(two, scope=Scope("ghost")) == ()


# ---------------------------------------------------------------- pause and halt


def test_the_pause_button_publishes_the_reverse_of_the_current_status(two: Any) -> None:
    assert build(two)[0].next_status == BasketStatus.PAUSED.value


def test_a_paused_basket_offers_resume(instrument: Instrument, panel: PanelConfig) -> None:
    row = build((record("b1", (instrument,), panel, status=BasketStatus.PAUSED),))[0]

    assert row.paused
    assert row.next_status == BasketStatus.ACTIVE.value


def test_a_halted_basket_still_reverses_only_its_pause(two: Any) -> None:
    """A resume published here leaves the halt exactly where it was — that is the whole point."""
    row = build(two, halted={"b1": "three consecutive failed cycles"})[0]

    assert row.halted_reason == "three consecutive failed cycles"
    assert row.next_status == BasketStatus.PAUSED.value


# ---------------------------------------------------------------- quarantine


def test_an_instrument_quarantined_by_name_offers_its_own_release(
    instrument: Instrument, panel: PanelConfig
) -> None:
    policy = RiskPolicy(quarantined_instruments=(instrument.key,))
    row = build((record("b1", (instrument,), panel, policy=policy),))[0]

    assert row.instruments[0].named
    assert row.instruments[0].excluded
    assert not row.instruments[0].inherited


def test_an_instrument_excluded_by_its_basket_offers_no_release_of_its_own(
    instrument: Instrument, panel: PanelConfig
) -> None:
    """Releasing it by name would publish a version that excludes it exactly as before."""
    policy = RiskPolicy(quarantined=True)
    row = build((record("b1", (instrument,), panel, policy=policy),))[0]

    assert row.instruments[0].inherited
    assert not row.instruments[0].named


def test_quarantines_in_force_lists_the_basket_before_its_instruments(
    instrument: Instrument, second_instrument: Instrument, panel: PanelConfig
) -> None:
    policy = RiskPolicy(quarantined=True, quarantined_instruments=(instrument.key,))
    rows = dock.quarantines(
        (record("b1", (instrument, second_instrument), panel, policy=policy),), positions=()
    )

    assert [row.label for row in rows] == ["whole basket", instrument.key]
    assert rows[0].whole_basket


def test_a_basket_with_nothing_excluded_contributes_no_row(two: Any) -> None:
    assert dock.quarantines(two, positions=()) == ()


def test_a_quarantine_names_the_positions_it_strands(
    instrument: Instrument, second_instrument: Instrument, panel: PanelConfig
) -> None:
    """Inaction compounds a loss as readily as action causes one, so it is named, not implied."""
    policy = RiskPolicy(quarantined=True)
    rows = dock.quarantines(
        (record("b1", (instrument, second_instrument), panel, policy=policy),),
        positions=(position(instrument.key),),
    )

    assert rows[0].held == (instrument.key,)


def test_held_within_a_whole_basket_quarantine_is_everything_it_holds(
    instrument: Instrument, second_instrument: Instrument, basket: Basket
) -> None:
    whole = basket.model_copy(update={"instruments": (instrument, second_instrument)})

    assert dock.held_within(whole, dock.WHOLE_BASKET, {instrument.key}) == (instrument.key,)


def test_held_within_a_named_quarantine_is_that_instrument_alone(
    instrument: Instrument, second_instrument: Instrument, basket: Basket
) -> None:
    whole = basket.model_copy(update={"instruments": (instrument, second_instrument)})
    held = {instrument.key, second_instrument.key}

    assert dock.held_within(whole, second_instrument.key, held) == (second_instrument.key,)


def test_nothing_held_is_nothing_stranded(instrument: Instrument, basket: Basket) -> None:
    assert dock.held_within(basket, instrument.key, set()) == ()


# ---------------------------------------------------------------- closing


def test_only_a_holding_a_basket_in_service_lists_is_closable(
    two: Any, instrument: Instrument, second_instrument: Instrument
) -> None:
    """`closable` is `ManualCloser`'s own answer, so the dock cannot offer a close it would refuse.

    A position belongs to the portfolio, not to a basket (DESIGN §4), so the same instrument under
    a second basket is a second, differently-policied way to close the *same* holding — and only
    the pairs the closer accepts may appear.
    """
    rows = build(
        two,
        positions=(position(instrument.key), position(second_instrument.key)),
        closable=(("b1", instrument.key),),
    )

    assert [line.key for line in rows[0].closable] == [instrument.key]
    assert rows[1].closable == ()


def test_a_flat_instrument_carries_no_position(two: Any, instrument: Instrument) -> None:
    rows = build(two, positions=(position(instrument.key, qty="0"),))

    assert rows[0].instruments[0].position is None
