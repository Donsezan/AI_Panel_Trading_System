"""The blotter's rows: what a state label means, and which row a position belongs to.

The load-bearing assertions here are the precedence ones. Halt, pause and quarantine are three
different mechanisms with three different ways out (ADR 0021, ADR 0022), and a row that showed
the mildest one in force would tell an operator the bot is doing something it is not.
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
from tradebot.dashboard import blotter
from tradebot.dashboard.blotter import ACTIVE, HALTED, QUARANTINED, BasketRow
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
    document = Basket(
        basket_id=basket_id,
        name=f"basket {basket_id}",
        instruments=instruments,
        panel=panel,
        status=status,
        risk_policy=policy or RiskPolicy(),
    )
    return ConfigRecord(
        ref=ConfigRef(kind=ConfigKind.BASKET, config_id=basket_id, version=1),
        document=document,
        actor="test",
        created_at=NOW,
    )


def position(instrument_key: str, qty: str = "0.5") -> SimpleNamespace:
    return SimpleNamespace(
        instrument_key=instrument_key,
        qty=Decimal(qty),
        avg_entry=Decimal(100),
        realized_pnl=Decimal("2.5"),
    )


def build(records: Any, **overrides: Any) -> tuple[BasketRow, ...]:
    defaults: dict[str, Any] = {
        "positions": (),
        "decisions": (),
        "halted": {},
        "cycles_today": {},
        "trades_today": {},
        "next_due": lambda _basket_id: None,
    }
    return blotter.build(records, **{**defaults, **overrides})


@pytest.fixture
def one(instrument: Instrument, panel: PanelConfig) -> ConfigRecord[Basket]:
    return record("b1", (instrument,), panel)


# ---------------------------------------------------------------- state precedence


def test_an_ordinary_basket_is_active(one: ConfigRecord[Basket]) -> None:
    assert build((one,))[0].state == ACTIVE


def test_an_operator_pause_shows_as_paused(instrument: Instrument, panel: PanelConfig) -> None:
    paused = record("b1", (instrument,), panel, status=BasketStatus.PAUSED)
    assert build((paused,))[0].state == BasketStatus.PAUSED.value


def test_a_quarantined_basket_shows_as_quarantined(
    instrument: Instrument, panel: PanelConfig
) -> None:
    """It still cycles — only the order is refused — so it is not a pause (ADR 0022)."""
    quarantined = record("b1", (instrument,), panel, policy=RiskPolicy(quarantined=True))
    assert build((quarantined,))[0].state == QUARANTINED


def test_a_halt_outranks_everything_else(instrument: Instrument, panel: PanelConfig) -> None:
    """The one an operator has to clear before anything else matters, and the only typed one."""
    both = record("b1", (instrument,), panel, policy=RiskPolicy(quarantined=True))
    row = build((both,), halted={"b1": "three consecutive failed cycles"})[0]
    assert row.state == HALTED
    assert row.halted_reason == "three consecutive failed cycles"


# ---------------------------------------------------------------- rows


def test_a_position_appears_under_every_basket_that_lists_it(
    instrument: Instrument, panel: PanelConfig
) -> None:
    """There is one position; two baskets sharing an instrument share it (DESIGN §4)."""
    rows = build(
        (record("b1", (instrument,), panel), record("b2", (instrument,), panel)),
        positions=(position(instrument.key),),
    )
    assert all(row.instruments[0].held for row in rows)
    assert all(row.held for row in rows)


def test_a_flat_position_is_no_position(one: ConfigRecord[Basket], instrument: Instrument) -> None:
    """An empty cell must never be read as a holding of zero."""
    row = build((one,), positions=(position(instrument.key, qty="0"),))[0]
    assert row.instruments[0].position is None
    assert not row.held


def test_a_decision_belongs_to_the_basket_that_made_it(
    instrument: Instrument, panel: PanelConfig
) -> None:
    decided = SimpleNamespace(basket_id="b1", instrument_key=instrument.key, action="BUY")
    rows = build(
        (record("b1", (instrument,), panel), record("b2", (instrument,), panel)),
        decisions=(decided,),
    )
    assert rows[0].instruments[0].decision is decided
    assert rows[1].instruments[0].decision is None


def test_an_instrument_quarantine_marks_only_that_instrument(
    instrument: Instrument, second_instrument: Instrument, panel: PanelConfig
) -> None:
    excluded = record(
        "b1",
        (instrument, second_instrument),
        panel,
        policy=RiskPolicy(quarantined_instruments=(instrument.key,)),
    )
    row = build((excluded,))[0]
    assert [line.quarantined for line in row.instruments] == [True, False]
    assert row.state == ACTIVE


def test_the_daily_cap_is_the_rule_the_engine_enforces(one: ConfigRecord[Basket]) -> None:
    row = build((one,), trades_today={"b1": 6})[0]
    assert row.trade_cap == RiskPolicy().max_trades_per_day
    assert row.at_cap


def test_a_basket_below_its_cap_is_not_flagged(one: ConfigRecord[Basket]) -> None:
    assert not build((one,), trades_today={"b1": 1})[0].at_cap


def test_counters_absent_from_the_projections_are_zero(one: ConfigRecord[Basket]) -> None:
    row = build((one,))[0]
    assert (row.cycles_today, row.trades_today) == (0, 0)


def test_nothing_waiting_to_cycle_is_no_next_fire(one: ConfigRecord[Basket]) -> None:
    """Different from "not soon": supervision stopped means there is no next cycle at all."""
    assert build((one,))[0].next_due is None


def test_next_fire_is_read_from_the_worker(one: ConfigRecord[Basket]) -> None:
    assert build((one,), next_due=lambda _b: NOW)[0].next_due == NOW


# ---------------------------------------------------------------- selection


def test_a_basket_scope_selects_the_basket_and_no_instrument(
    one: ConfigRecord[Basket],
) -> None:
    row = build((one,), scope=Scope("b1"))[0]
    assert row.selected
    assert not any(line.selected for line in row.instruments)


def test_an_instrument_scope_selects_the_instrument_and_not_its_basket(
    one: ConfigRecord[Basket], instrument: Instrument
) -> None:
    row = build((one,), scope=Scope("b1", instrument.key))[0]
    assert not row.selected
    assert [line.selected for line in row.instruments] == [True]


def test_a_scope_naming_another_basket_selects_nothing(one: ConfigRecord[Basket]) -> None:
    row = build((one,), scope=Scope("elsewhere"))[0]
    assert not row.selected
    assert not any(line.selected for line in row.instruments)


def test_no_scope_selects_nothing(one: ConfigRecord[Basket]) -> None:
    """The workspace opens on no selection, which is a legitimate state, not a missing one."""
    row = build((one,))[0]
    assert not row.selected


def test_a_rows_scope_round_trips_through_the_url(
    one: ConfigRecord[Basket], instrument: Instrument
) -> None:
    from tradebot.dashboard.scope import parse

    row = build((one,))[0]
    assert parse(str(row.scope)) == row.scope
    assert parse(str(row.instruments[0].scope)) == Scope("b1", instrument.key)
