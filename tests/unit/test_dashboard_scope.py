"""The selection encoding is a URL contract, so it is tested as a round trip and as a refusal.

Two properties carry the weight. Anything this module builds must parse back to itself, because
the workspace writes scopes into `hx-push-url` and reads them back on the next request. And
anything it cannot understand must be *no selection* rather than a guess: a hand-edited URL that
resolved to the wrong basket would scope the operation log, the chart and the scoped controls to
an instrument nobody chose.
"""

from __future__ import annotations

import pytest

from tradebot.dashboard.scope import Scope, parse

#: Instrument keys carry their own colon (`Instrument.key` is `venue:symbol`), which is the whole
#: reason the instrument form splits a bounded number of times.
KEY = "binance:BTC/USDT"


@pytest.mark.parametrize(
    "scope", [Scope("demo"), Scope("demo", KEY), Scope("a-b_c", "alpaca:AAPL")]
)
def test_a_scope_survives_the_url(scope: Scope) -> None:
    assert parse(str(scope)) == scope


def test_a_basket_scope_selects_no_instrument() -> None:
    assert parse("basket:demo") == Scope("demo", None)


def test_an_instrument_scope_keeps_the_key_intact() -> None:
    """Split once past the basket: the venue prefix belongs to the key, not to the encoding."""
    selected = parse(f"instrument:demo:{KEY}")
    assert selected == Scope("demo", KEY)
    assert selected is not None
    assert selected.instrument_key == KEY


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "demo",  # no kind
        "basket:",  # a kind naming nothing
        "instrument:demo",  # an instrument scope with no instrument
        "instrument:demo:",  # ... nor with an empty one
        "instrument::binance:BTC/USDT",  # ... nor with no basket
        "cycle:demo",  # a kind this module does not serve
        "BASKET:demo",  # kinds are not case-folded; a near-miss is a miss
    ],
)
def test_an_unusable_scope_is_no_selection(raw: str | None) -> None:
    assert parse(raw) is None
