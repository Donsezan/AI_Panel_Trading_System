"""`client_order_id` — the property that makes a retry safe.

The orphan-order recovery in PLAN §7 rests entirely on these guarantees.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.core.ids import (
    VENUE_ID_PATTERN,
    assert_venue_safe,
    client_order_id,
    new_uuid,
    owns_client_order_id,
)

BASE = {"mode": Mode.SIM, "basket_id": "b1", "cycle_id": "c1", "instrument": "BTC/USDT", "seq": 0}


def test_same_inputs_always_produce_the_same_id() -> None:
    """A resumed submit must reach the venue's existing order, never mint a second one."""
    assert client_order_id(**BASE) == client_order_id(**BASE)


@pytest.mark.parametrize(
    "change",
    [
        {"basket_id": "b2"},
        {"cycle_id": "c2"},
        {"instrument": "ETH/USDT"},
        {"seq": 1},
        {"mode": Mode.PAPER},
    ],
)
def test_any_component_change_produces_a_different_id(change: dict[str, object]) -> None:
    assert client_order_id(**{**BASE, **change}) != client_order_id(**BASE)  # type: ignore[arg-type]


def test_id_fits_the_tightest_venue_constraint() -> None:
    """Binance spot caps `newClientOrderId` at 36 chars with a restricted charset."""
    generated = client_order_id(**BASE)
    assert len(generated) <= 36
    assert VENUE_ID_PATTERN.match(generated)


@given(
    basket=st.text(min_size=1, max_size=64),
    cycle=st.text(min_size=1, max_size=64),
    instrument=st.text(min_size=1, max_size=32),
    seq=st.integers(min_value=0, max_value=10_000),
)
def test_ids_are_venue_safe_for_any_input(
    basket: str, cycle: str, instrument: str, seq: int
) -> None:
    """Arbitrary symbols and basket names must not be able to produce a rejected id."""
    generated = client_order_id(
        mode=Mode.LIVE, basket_id=basket, cycle_id=cycle, instrument=instrument, seq=seq
    )
    assert VENUE_ID_PATTERN.match(generated)


@pytest.mark.parametrize(
    ("mode", "prefix"), [(Mode.SIM, "sim"), (Mode.PAPER, "pap"), (Mode.LIVE, "liv")]
)
def test_mode_prefix_keeps_environments_distinguishable(mode: Mode, prefix: str) -> None:
    """A paper id must never be mistakable for a live one (PLAN §2.4)."""
    assert client_order_id(**{**BASE, "mode": mode}).startswith(f"{prefix}-")  # type: ignore[arg-type]


def test_ownership_check_adopts_only_our_orders() -> None:
    """The reconciler adopts our orders by prefix and leaves a human's manual orders alone."""
    ours = client_order_id(**BASE)
    assert owns_client_order_id(ours, Mode.SIM)
    assert not owns_client_order_id(ours, Mode.LIVE)
    assert not owns_client_order_id("web_abc123", Mode.SIM)


@pytest.mark.parametrize("bad", ["", "a" * 37, "has space", "emoji✨"])
def test_venue_safety_check_rejects_invalid_ids(bad: str) -> None:
    with pytest.raises(ConfigError, match="not venue-safe"):
        assert_venue_safe(bad)


def test_uuids_are_unique() -> None:
    assert len({new_uuid() for _ in range(100)}) == 100
