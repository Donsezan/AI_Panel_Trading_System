# Equity Market Data (Phase 12 Piece 2, Stage A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire an Alpaca `VenueGateway` so an equity-only basket completes a full decision cycle, removing gate 4 of Piece 2 and nothing else.

**Architecture:** One new venue-aware class, `AlpacaGateway`. `VenueMarketData` is already venue-agnostic over a `VenueGateway` and already builds its own `VenueCatalogue`, so the market-data provider *and* the instrument catalogue both fall out of code that is already written and tested. Everything that is a fact about the **US equity market** rather than about **Alpaca** — the trading rules, the exchange timezone, the regular-session bounds — is extracted to a shared `us_equities` module so a future venue swap costs one gateway and one transport.

**Tech Stack:** Python 3.11, `httpx` (no vendor SDK), `pydantic` v2 domain models, `pytest` + `httpx.MockTransport`, `Decimal`-only money.

**Spec:** [docs/PHASE_12_STAGE_A_EQUITY_MARKET_DATA.md](../../PHASE_12_STAGE_A_EQUITY_MARKET_DATA.md)

## Global Constraints

- **Money is `Decimal`, always.** Never `float`, never `Decimal(some_float)`. Use `tradebot.core.money`. Enforced by `tests/unit/test_money_discipline.py`, which walks `marketdata/`.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never call `datetime.now()` in library code.
- **Errors are classified**: `RetryableError` / `FailClosedError` / `FatalError`. A bare `except: pass` is a defect. Parse failures raise `DataStaleError`; an unlisted symbol or timeframe raises `ConfigError`.
- **Comments explain *why*, and cite the spec section** (`DESIGN §6.2`, `PLAN §3.1`, `ADR 0025`). Docstrings state failure semantics at module level.
- **Nothing outside `app.py` may import a concrete adapter.** Task 8 enforces this for the new modules.
- **Prefer dispatch over branching**; enum behaviour lives on the enum.
- Run `.\check.ps1` before every commit. Coverage gates: `core/`, `risk/`, `execution/`, `ledger/` ≥ 95%; everything else ≥ 80%.
- Tests are offline and free. No test may reach a network.

**Values fixed by the spec, copied verbatim:**
- `MIN_TICK = Decimal("0.01")` — SEC Rule 612 · **review: November 2026 regime change**
- `WHOLE_SHARE_LOT = Decimal(1)`; `min_qty = 1`; `min_notional = ZERO`
- `feed = "sip"`; `DataCapabilities.delay = timedelta(minutes=15)`
- `adjustment = "all"` — passed explicitly, never left to Alpaca's `raw` default

---

## Three decisions this plan makes that the spec did not settle

All were found while grounding the plan in the actual endpoint shapes and the harness. Each is implemented as described below and should be reviewed before Task 4 is accepted.

**1. `fetch_top_of_book` derives from the most recent closed bar.** Alpaca's `latest quote` / `latest trade` endpoints are real-time by definition, so on the free plan with `feed=sip` they are **not available** — the 15-minute rule forbids them. Rather than mixing feeds (real-time IEX quotes beside delayed SIP bars, which would put two different views of the market into one snapshot), the gateway derives its quote from the newest bar it can legitimately see. `bid == ask == last == that bar's close`, and `observed_at` is that bar's close time, so every staleness check downstream sees the truth rather than a fabricated freshness. **The stated cost: no spread information.** A real spread requires the paid real-time SIP feed, and that is now a known precondition for live equity trading rather than a discovery made in Stage D.

**2. Session tagging uses fixed regular-session bounds, and half-days are a known imprecision.** Alpaca's bars carry no session marker, so `session_of` compares the bar's open time against 09:30–16:00 America/New_York. On an early-close day (13:00 ET) the 13:00–16:00 bars are extended-hours prints that this will tag `REGULAR`, so thin prints could enter an ATR window. Mitigations: daily bars — the common equity timeframe — are regular-session by construction at Alpaca, and the misclassification is bounded to a handful of half-days per year. Fixing it properly needs the venue calendar inside the parser, which would couple the gateway to a `TradingCalendar`; that is deliberately deferred and recorded in the ADR.

**3. The mark staleness tolerance must outlive the feed delay, and that is now enforced at wiring.** This one would have made the Stage A exit criterion fail silently. A quote from a 15-minute-delayed feed carries an `observed_at` at least 15 minutes in the past — truthfully, by design. But `GlobalRiskPolicy.mark_staleness_seconds` defaults to **300**, so `Marks.price_of` would return `None` for every equity mark, `aggregate` would freeze on every evaluation, and every cycle would record `BLOCKED` for a portfolio that is perfectly healthy. `PortfolioWatch` already refuses a tolerance below `3 ×` the sweep cadence for exactly this class of reason (a permanently frozen portfolio wired in at 03:00); it gains the sibling check against the provider's **declared delay**, which it can read from `capabilities()`. Task 8 implements it, and an equity basket therefore requires `mark_staleness_seconds ≥ 1200`.

---

## File Structure

| File | Responsibility |
|---|---|
| `tradebot/core/money.py` *(modify)* | gains `loads_exact` — JSON decoding that cannot produce a float |
| `tradebot/marketdata/us_equities.py` *(create)* | US equity market structure: the trading rules, the exchange timezone, regular-session bounds. Venue-independent. |
| `tradebot/venues/alpaca_transport.py` *(modify)* | role-keyed host assertion; `AlpacaDataTransport`; both transports decode through `loads_exact` |
| `tradebot/marketdata/alpaca.py` *(create)* | Alpaca wire format → exact decimals (pure functions), then `AlpacaGateway` |
| `tradebot/execution/brokers/alpaca.py` *(modify)* | imports `EXCHANGE_TZ` from `us_equities` instead of defining it |
| `tradebot/app.py` *(modify)* | `_alpaca_stack` builds the gateway; `_feed_for` learns Alpaca |
| `tradebot/control/valuation.py` *(modify)* | the wiring check also refuses a mark tolerance shorter than the feed's delay |
| `tests/unit/test_us_equities.py` *(create)* | the shared rule set and session bounds |
| `tests/unit/test_alpaca_gateway.py` *(create)* | wire parsing, exactness, the query parameters that must never be defaulted |
| `tests/unit/test_valuation_boundary.py` *(modify)* | the tolerance-versus-delay refusal |
| `tests/unit/test_venue_boundary.py` *(create)* | structural: only `app.py` imports the venue modules |
| `tests/contract/test_market_data_contract.py` *(modify)* | `AlpacaGateway` joins the provider suite |
| `tests/contract/test_catalogue_contract.py` *(modify)* | an equity-catalogue class beside the shared cases |
| `tests/scenario/test_equity_basket.py` *(create)* | an equity-only basket completes a cycle |
| `docs/adr/0028-*.md`, `CLAUDE.md`, `DESIGN.md` *(modify)* | the decision record and the conventions |

---

### Task 1: `loads_exact` — JSON decoding that cannot produce a float

**Files:**
- Modify: `tradebot/core/money.py`
- Test: `tests/unit/test_money.py`

**Interfaces:**
- Consumes: nothing
- Produces: `tradebot.core.money.loads_exact(text: str | bytes) -> Any` — `json.loads` with `parse_float=Decimal`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_money.py`:

```python
class TestLoadsExact:
    """ADR 0001 in its general form: never let a float exist.

    Reading "the venue's string field" is the Binance-shaped statement of the rule. Alpaca
    publishes prices as unquoted JSON numbers, so there is no string field to read and the
    guarantee has to live in the decoder instead (PHASE_12 Stage A, Finding 3).
    """

    def test_a_json_number_becomes_an_exact_decimal(self) -> None:
        payload = loads_exact('{"c": 178.21}')
        assert payload["c"] == Decimal("178.21")
        assert isinstance(payload["c"], Decimal)

    def test_precision_survives_that_a_float_would_destroy(self) -> None:
        # float("178.235733") is not exactly 178.235733; the literal text is.
        assert loads_exact('{"vw": 178.235733}')["vw"] == Decimal("178.235733")

    def test_no_float_survives_anywhere_in_a_nested_payload(self) -> None:
        payload = loads_exact('{"bars": {"AAPL": [{"o": 1.5, "v": 1118}]}}')
        bar = payload["bars"]["AAPL"][0]
        assert not isinstance(bar["o"], float)
        assert bar["o"] == Decimal("1.5")
        # Integers stay integers: only floats are re-parsed.
        assert bar["v"] == 1118

    def test_it_accepts_bytes_as_well_as_text(self) -> None:
        assert loads_exact(b'{"c": 2.5}')["c"] == Decimal("2.5")

    def test_a_non_json_body_raises_value_error_for_the_caller_to_classify(self) -> None:
        with pytest.raises(ValueError):
            loads_exact("<html>not json</html>")
```

Add `loads_exact` to the existing `from tradebot.core.money import ...` line at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_money.py::TestLoadsExact -v`
Expected: FAIL — `ImportError: cannot import name 'loads_exact'`

- [ ] **Step 3: Implement**

In `tradebot/core/money.py`, add `import json` to the imports and this function after `to_decimal`:

```python
def loads_exact(text: str | bytes) -> Any:
    """Decode JSON so that no number in it can arrive as a `float`.

    ADR 0001's operative sentence is "read the venue's *string* fields", which is the shape the
    rule takes at Binance — it publishes `"0.01634790"` precisely so the value survives. Alpaca
    publishes `178.21`, an unquoted JSON number, and `json.loads` renders that as a `float` before
    any of our code sees it. There is no string field to read, so the guarantee has to live here.

    `parse_float=Decimal` builds the `Decimal` from the *literal text*, so scale and precision
    survive exactly. Integers are untouched: only floats are re-parsed, so a volume of `1118`
    stays an `int` and a bar count stays countable.

    Raises `ValueError` on a body that is not JSON, which the caller classifies — a transport
    turns it into `VenueError`, because a non-JSON body means this is not the endpoint we think.
    """
    return json.loads(text, parse_float=Decimal)
```

Add `"loads_exact"` to `__all__` if the module defines one.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_money.py -v`
Expected: PASS, and no existing test in the file regresses.

- [ ] **Step 5: Commit**

```bash
git add tradebot/core/money.py tests/unit/test_money.py
git commit -m "feat(core): loads_exact, so a venue publishing JSON numbers cannot make a float"
```

---

### Task 2: `us_equities` — market structure that is not any venue's

**Files:**
- Create: `tradebot/marketdata/us_equities.py`
- Modify: `tradebot/execution/brokers/alpaca.py` (import `EXCHANGE_TZ` rather than define it)
- Test: `tests/unit/test_us_equities.py`

**Interfaces:**
- Consumes: `tradebot.interfaces.exchange.VenueMarket`, `tradebot.core.enums.MarketSession`
- Produces:
  - `MIN_TICK: Decimal`, `WHOLE_SHARE_LOT: Decimal`, `EXCHANGE_TZ: ZoneInfo`, `MAJOR_EXCHANGES: frozenset[str]`
  - `whole_share_market(symbol: str, *, tradable: bool = True) -> VenueMarket`
  - `session_of(at: datetime) -> MarketSession`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_us_equities.py`:

```python
"""US equity market structure — shared by every US equity venue, owned by none of them."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.marketdata.us_equities import (
    MIN_TICK,
    WHOLE_SHARE_LOT,
    session_of,
    whole_share_market,
)


class TestTheRuleSet:
    """Three facts about whole-share trading, and one cited regulation (ADR 0025 as amended)."""

    def test_a_whole_share_market_carries_the_venue_independent_rules(self) -> None:
        market = whole_share_market("AAPL")
        assert market.symbol == "AAPL"
        assert market.lot_size == WHOLE_SHARE_LOT == Decimal(1)
        assert market.min_qty == Decimal(1)
        assert market.tick_size == MIN_TICK == Decimal("0.01")
        assert market.tradable is True

    def test_there_is_no_notional_floor_on_a_whole_share_order(self) -> None:
        """Deliberately not "every rule is positive", which is the sim rule set's invariant.

        Alpaca imposes no notional minimum on whole-share orders; the $1 floor is a
        fractional/notional order rule. Asserting a floor here would reintroduce exactly the
        invented number ADR 0025 exists to prevent.
        """
        assert whole_share_market("AAPL").min_notional == ZERO

    def test_an_equity_is_quoted_in_usd_and_based_on_its_own_ticker(self) -> None:
        market = whole_share_market("AAPL")
        assert market.quote_currency == "USD"
        assert market.base_currency == "AAPL"

    def test_a_non_tradable_asset_is_published_as_untradable_not_omitted(self) -> None:
        """The catalogue must be able to refuse it as *delisted* rather than as *unknown*."""
        assert whole_share_market("XYZ", tradable=False).tradable is False

    @pytest.mark.parametrize("symbol", ["", "   "])
    def test_a_blank_symbol_is_refused_rather_than_building_a_nameless_market(
        self, symbol: str
    ) -> None:
        with pytest.raises(ConfigError, match="symbol"):
            whole_share_market(symbol)

    def test_the_symbol_is_normalised_so_one_asset_has_one_key(self) -> None:
        assert whole_share_market("  aapl  ").symbol == "AAPL"


class TestSessionBounds:
    """Extended-hours prints are thin and wide; an ATR across them misstates the stop distance."""

    @pytest.mark.parametrize(
        ("utc_hour", "utc_minute", "expected"),
        [
            (14, 30, MarketSession.REGULAR),  # 09:30 ET — the open
            (18, 0, MarketSession.REGULAR),  # 13:00 ET — midday
            (20, 59, MarketSession.REGULAR),  # 15:59 ET — the last regular minute
            (21, 0, MarketSession.EXTENDED),  # 16:00 ET — the close is exclusive
            (13, 0, MarketSession.EXTENDED),  # 08:00 ET — pre-market
            (1, 0, MarketSession.EXTENDED),  # 20:00 ET the previous day — after hours
        ],
    )
    def test_regular_hours_are_bounded_at_both_ends(
        self, utc_hour: int, utc_minute: int, expected: MarketSession
    ) -> None:
        # 2026-03-02 is a Monday; EST/EDT is handled by the zone, not by an offset here.
        at = datetime(2026, 3, 2, utc_hour, utc_minute, tzinfo=UTC)
        assert session_of(at) is expected

    def test_it_is_never_continuous(self) -> None:
        """`CONTINUOUS` means "no session structure", which is a claim only crypto may make."""
        at = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
        assert session_of(at) is not MarketSession.CONTINUOUS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_us_equities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.marketdata.us_equities'`

- [ ] **Step 3: Implement**

Create `tradebot/marketdata/us_equities.py`:

```python
"""US equity market structure: the rules every US equity venue shares, owned by none of them.

[ADR 0025](../../docs/adr/0025-instrument-trading-rules-are-venue-reference-data.md) says an
instrument's trading rules are venue reference data, fetched and never typed. For US equities
there is nothing to fetch: Alpaca's `/v2/assets` publishes `min_order_size`,
`min_trade_increment` and `price_increment` for **crypto only**, and no US equity venue publishes
a per-symbol tick or lot at all. The rules are market structure.

So they live here rather than in any one venue's gateway. Three of the four are facts about
whole-share trading; the fourth is a regulation, cited, with a review date. A future
`TradierGateway` or `IBKRGateway` consumes this same module, so two venues cannot come to
disagree about Rule 612 — which would be ADR 0025's original defect, merely relocated.

Failure semantics: no I/O, so nothing to fail. A symbol that cannot name a market raises
`ConfigError`, because asking for an instrument with no identifier is a configuration defect.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.interfaces.exchange import VenueMarket

#: The minimum price increment for an NMS stock priced at or above $1.00, set by **SEC Rule 612**
#: (the sub-penny rule). It is a *floor*, not a grid: quoting a sub-$1 stock at penny increments is
#: legal, merely coarse — which is why one constant serves every listed name and errs safe.
#:
#: **REVIEW: November 2026.** The SEC's amended tick-size regime is expected to take effect then,
#: and this constant must be re-checked against it before any equity trades live.
MIN_TICK: Final = Decimal("0.01")

#: Whole shares only. Fractional trading is out of scope in v1: `Instrument.tick_size` is a static
#: field, so a fractional regime whose tick depends on the current price would go stale the moment
#: a price crossed $1.00, and every subsequent order would be rejected.
WHOLE_SHARE_LOT: Final = Decimal(1)

#: The exchange session's own timezone. A "trading day" is a New York date, not a UTC one: an
#: extended-hours print at 01:00 UTC belongs to the previous session, and rolling the daily-loss
#: baseline at UTC midnight would reset it in the middle of one.
EXCHANGE_TZ: Final = ZoneInfo("America/New_York")

#: The regular session, in exchange-local time. `REGULAR_CLOSE` is exclusive, so the 16:00 bar is
#: the first extended one.
REGULAR_OPEN: Final = time(9, 30)
REGULAR_CLOSE: Final = time(16, 0)

#: Listing venues whose names this system recognises. An asset anywhere else — OTC, in particular
#: — is simply not listed by the catalogue, so `resolve` refuses it in the venue's own terms rather
#: than inventing rules for it.
MAJOR_EXCHANGES: Final[frozenset[str]] = frozenset(
    {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA"}
)


def whole_share_market(symbol: str, *, tradable: bool = True) -> VenueMarket:
    """The trading rules a US equity venue would publish for a whole-share order, if it did.

    `min_notional` is `ZERO` and that is not an oversight: a whole-share order has no notional
    floor, and the $1 minimum an operator may have read about applies to *fractional* orders.
    Writing `1` here would be an invented number, which is the failure ADR 0025 exists to prevent.

    `base_currency` is the ticker itself, mirroring how a spot pair's base asset names the thing
    held. Nothing values it as cash: `value_cash`'s rung 3 zeroes any currency that is a configured
    instrument's base asset, and an equity position is held as a position, never as a balance
    (PHASE_12 §3.3).
    """
    normalised = symbol.strip().upper()
    if not normalised:
        raise ConfigError(
            "a US equity market needs a symbol; a market with no identifier names no tradable thing"
        )
    return VenueMarket(
        symbol=normalised,
        base_currency=normalised,
        quote_currency="USD",
        lot_size=WHOLE_SHARE_LOT,
        tick_size=MIN_TICK,
        min_qty=WHOLE_SHARE_LOT,
        min_notional=ZERO,
        tradable=tradable,
    )


def session_of(at: datetime) -> MarketSession:
    """Which session a bar opening at `at` belongs to.

    Never `CONTINUOUS`: that value means "this market has no session structure", which is a claim
    only crypto may make. Mixing extended-hours bars into an indicator average blends two
    liquidity regimes, and an ATR computed across them misstates the stop distance sizing divides
    by (DESIGN §6.2).

    **Known imprecision: early closes.** On a half-day the exchange shuts at 13:00 ET, so the
    13:00–16:00 bars are extended-hours prints this will tag `REGULAR`. Correcting it needs the
    venue's own calendar inside the parser, which would couple a wire-format module to a
    `TradingCalendar`. Deferred deliberately: daily bars are regular-session by construction, and
    the misclassification is bounded to a handful of days a year.
    """
    local = at.astimezone(EXCHANGE_TZ).time()
    regular = REGULAR_OPEN <= local < REGULAR_CLOSE
    return MarketSession.REGULAR if regular else MarketSession.EXTENDED
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_us_equities.py -v`
Expected: PASS (11 cases).

- [ ] **Step 5: Move `EXCHANGE_TZ` out of the Alpaca broker**

In `tradebot/execution/brokers/alpaca.py`, delete the `EXCHANGE_TZ` definition and its comment (around line 57–60), delete the now-unused `from zoneinfo import ZoneInfo` import, and add:

```python
from tradebot.marketdata.us_equities import EXCHANGE_TZ
```

The exchange timezone is a fact about the US market, not about Alpaca — the same argument as the trading rules. `execution/brokers/binance.py` already imports from `marketdata/binance.py`, so the direction has precedent.

- [ ] **Step 6: Run the affected suites**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_alpaca_broker.py tests/unit/test_us_equities.py -q`
Expected: PASS — the calendar and session-day behaviour is unchanged, only the constant moved.

- [ ] **Step 7: Commit**

```bash
git add tradebot/marketdata/us_equities.py tradebot/execution/brokers/alpaca.py tests/unit/test_us_equities.py
git commit -m "feat(marketdata): US equity market structure, shared by any equity venue"
```

---

### Task 3: the data transport, and mode safety across two hosts

**Files:**
- Modify: `tradebot/venues/alpaca_transport.py`
- Test: `tests/unit/test_signed_transports.py`

**Interfaces:**
- Consumes: `tradebot.core.money.loads_exact` (Task 1)
- Produces:
  - `ALPACA_DATA_HOST: str`
  - `AlpacaDataTransport(client, clock, *, mode, key_id, secret_key, base_url=None, limiter=None, timeout_seconds=...)` with `venue_id`, `limiter`, `async get(endpoint, params, *, weight) -> Any`, `async close()`
  - `assert_host(base_url: str, mode: Mode, *, data: bool = False) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_signed_transports.py` (match the file's existing fixture/import style):

```python
class TestAlpacaDataTransportIsStructurallyUnableToTrade:
    """Alpaca's data API requires a key, so `VenueTransport`'s "holds no credentials" separation
    is unavailable here. It is preserved structurally instead."""

    def test_it_has_no_order_placing_call(self) -> None:
        assert not hasattr(AlpacaDataTransport, "call")

    def test_its_host_is_the_data_host_in_every_mode(self) -> None:
        for mode in (Mode.SIM, Mode.PAPER, Mode.LIVE):
            transport = AlpacaDataTransport(
                httpx.AsyncClient(), ManualClock(NOW), mode=mode, key_id="k", secret_key="s"
            )
            assert ALPACA_DATA_HOST in transport.base_url

    def test_a_host_that_is_not_the_data_host_is_refused(self) -> None:
        with pytest.raises(ModeConfusionError, match="data"):
            AlpacaDataTransport(
                httpx.AsyncClient(),
                ManualClock(NOW),
                mode=Mode.PAPER,
                key_id="k",
                secret_key="s",
                base_url="https://api.alpaca.markets",
            )

    def test_plaintext_is_refused_outright(self) -> None:
        with pytest.raises(ConfigError, match="https"):
            AlpacaDataTransport(
                httpx.AsyncClient(),
                ManualClock(NOW),
                mode=Mode.PAPER,
                key_id="k",
                secret_key="s",
                base_url="http://data.alpaca.markets",
            )


class TestAlpacaDecodesWithoutFloats:
    async def test_a_json_number_in_a_data_response_arrives_as_a_decimal(self) -> None:
        """Alpaca publishes prices unquoted; `response.json()` would hand the money layer a
        float, which `parse_money` refuses by design (PHASE_12 Stage A, Finding 3)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='{"bars": {"AAPL": [{"c": 178.21}]}}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = AlpacaDataTransport(
            client, ManualClock(NOW), mode=Mode.PAPER, key_id="k", secret_key="s"
        )
        payload = await transport.get("/v2/stocks/bars", {"symbols": "AAPL"}, weight=1)
        assert payload["bars"]["AAPL"][0]["c"] == Decimal("178.21")
        await client.aclose()

    async def test_the_trading_transport_decodes_the_same_way(self) -> None:
        """One rule, both transports. A rule applied to one of two is a rule the next endpoint
        forgets."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='{"equity": 1234.56}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = AlpacaTransport(
            client, ManualClock(NOW), mode=Mode.PAPER, key_id="k", secret_key="s"
        )
        payload = await transport.call("GET /v2/account", {}, weight=1)
        assert payload["equity"] == Decimal("1234.56")
        await client.aclose()
```

Add the needed imports at the top of the file: `ALPACA_DATA_HOST`, `AlpacaDataTransport` from `tradebot.venues.alpaca_transport`, and `ModeConfusionError` if not already imported.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_signed_transports.py -k "AlpacaData or DecodesWithoutFloats" -v`
Expected: FAIL — `ImportError: cannot import name 'AlpacaDataTransport'`

- [ ] **Step 3: Make the host assertion role-aware**

In `tradebot/venues/alpaca_transport.py`, add the constant beside `ALPACA_HOSTS`:

```python
#: The market-data host. **The same in every mode**, unlike the trading hosts: reading prices
#: moves no money, so there is no paper/live pair to keep apart. It is still *asserted* rather than
#: defaulted past, because a data client that silently accepted any host is one that could be
#: pointed anywhere (PLAN §2.4).
ALPACA_DATA_HOST: Final = "data.alpaca.markets"
```

Replace `assert_host` with:

```python
def assert_host(base_url: str, mode: Mode, *, data: bool = False) -> None:
    """Refuse a host that contradicts the mode, and refuse plaintext outright (PLAN §2.4).

    Role-keyed. The **trading** host differs per mode, which is what makes a paper key physically
    unable to reach the live exchange. The **data** host is one host in every mode; asserting it
    anyway is what stops a typo or an override from pointing the price feed at something else.
    """
    parsed = httpx.URL(base_url)
    if parsed.scheme != "https":
        raise ConfigError(
            f"alpaca endpoint {base_url!r} is not https; credentials would cross the wire in clear"
        )
    expected = ALPACA_DATA_HOST if data else ALPACA_HOSTS[mode]
    if parsed.host != expected:
        role = "data" if data else "trading"
        raise ModeConfusionError(
            f"alpaca {role} resolved to {parsed.host!r} in {mode.value} mode, which requires "
            f"{expected!r}. Refusing to start: a paper run must not be able to reach the live "
            "exchange."
        )
```

- [ ] **Step 4: Extract the shared response handling**

The two transports must classify and decode identically. Move the bodies of `_classify`, `_forbidden`, `_decode` and `_excerpt` to module-level functions (`classify_response`, `forbidden_error`, `decode_response`, `excerpt_of`), have `AlpacaTransport`'s methods delegate to them, and change the decode to use `loads_exact`:

```python
def decode_response(response: httpx.Response) -> Any:
    """Decode a body so that no price in it can arrive as a `float`.

    Alpaca's *data* API publishes prices as unquoted JSON numbers, so `response.json()` would
    produce floats and `parse_money` refuses those by design (ADR 0001). Applied to both
    transports rather than only the data one: a rule applied to one of two is a rule the next
    endpoint forgets.
    """
    if len(response.content) > MAX_BYTES:
        raise VenueError(f"alpaca returned {len(response.content)} bytes, above the ceiling")
    if not response.content:
        return {}
    try:
        return loads_exact(response.content)
    except ValueError as exc:
        raise VenueError(f"alpaca returned a non-JSON body: {excerpt_of(response)}") from exc
```

Add `from typing import Any` (already present) and `from tradebot.core.money import loads_exact`.

- [ ] **Step 5: Add the data transport**

```python
class AlpacaDataTransport:
    """`VenueTransport` for Alpaca market data. Reads prices; cannot place an order.

    `VenueTransport` documents that it "asserts it holds no credentials, because a data client
    that could sign an order is a data client that might". Alpaca's data API *requires* a key, so
    that separation is unavailable at this venue. It is preserved **structurally** instead: there
    is no `call` method, no `is_order` path, and the base URL is asserted to be the data host,
    which cannot reach `/v2/orders`.

    It shares the trading transport's `VenueRateLimiter` when given one, because a venue bans an
    IP and a key rather than a code path (ADR 0010, PLAN §3.1).
    """

    venue_id = VENUE_ID

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        mode: Mode,
        key_id: str,
        secret_key: str,
        base_url: str | None = None,
        budget: RateBudget | None = None,
        limiter: VenueRateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.mode = mode
        self._client = client
        self.base_url = (base_url or f"https://{ALPACA_DATA_HOST}").rstrip("/")
        self._limiter = limiter or VenueRateLimiter(
            VENUE_ID, clock, budget or DEFAULT_ALPACA_BUDGET
        )
        self._timeout = timeout_seconds
        assert_credentials(key_id, secret_key)
        assert_host(self.base_url, mode, data=True)
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }

    @property
    def limiter(self) -> VenueRateLimiter:
        return self._limiter

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        """One unauthenticated-shaped read, charged against the venue's budget.

        Never `is_order`: this transport has no order path, so nothing here may spend the order
        windows that a submit depends on.
        """
        await self._limiter.acquire(weight, is_order=False)
        body = {key: value for key, value in params.items() if value is not None}
        request = self._client.build_request(
            "GET",
            f"{self.base_url}{endpoint}",
            headers=self._headers,
            timeout=self._timeout,
            params=body,
        )
        try:
            response = await self._client.send(request)
        except (httpx.TimeoutException, TimeoutError) as exc:
            self._limiter.record_failure()
            raise VenueError(f"alpaca data GET {endpoint} timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            self._limiter.record_failure()
            raise VenueError(f"alpaca data GET {endpoint} transport failure: {exc}") from exc

        self._limiter.observe_used_weight(dict(response.headers))
        failure = classify_response("GET", endpoint, response)
        if failure is not None:
            self._limiter.record_failure()
            if isinstance(failure, RateLimitedError):
                self._limiter.penalise(_retry_after(response))
            raise failure
        self._limiter.record_success()
        return decode_response(response)

    async def close(self) -> None:
        """Nothing to release: the client belongs to whoever created it."""
```

Also expose `base_url` on `AlpacaTransport` (rename `self._base_url` to `self.base_url`, updating its uses) so both transports report their resolved host identically.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_signed_transports.py tests/unit/test_alpaca_broker.py tests/contract/test_broker_contract.py -q`
Expected: PASS. The broker contract must be unaffected — only the decoder and the host helper changed.

- [ ] **Step 7: Commit**

```bash
git add tradebot/venues/alpaca_transport.py tests/unit/test_signed_transports.py
git commit -m "feat(venues): alpaca data transport, role-keyed hosts, float-proof decoding"
```

---

### Task 4: the Alpaca wire format — pure parsers

**Files:**
- Create: `tradebot/marketdata/alpaca.py`
- Test: `tests/unit/test_alpaca_gateway.py`

**Interfaces:**
- Consumes: `us_equities.whole_share_market`, `us_equities.session_of`, `us_equities.MAJOR_EXCHANGES` (Task 2)
- Produces:
  - `VENUE_ID: str`, `TIMEFRAMES: Mapping[str, str]`, `MAX_BARS: int`, `DATA_DELAY: timedelta`, `DEFAULT_FEED: str`, `BAR_ADJUSTMENT: str`
  - `to_alpaca_timeframe(timeframe: str) -> str`
  - `parse_bar(row: Mapping[str, Any], interval: timedelta) -> Candle`
  - `parse_asset(payload: Mapping[str, Any]) -> VenueMarket | None`
  - `book_from_bar(bar: Candle) -> TopOfBook`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_alpaca_gateway.py` with the parser cases (the gateway cases arrive in Task 5):

```python
"""Alpaca wire format → exact decimals.

This is the boundary where a venue's numbers become the numbers an order is sized from. Two
Alpaca-specific hazards are corrected rather than propagated: prices arrive as unquoted JSON
*numbers*, and the bars endpoint defaults to `adjustment=raw` (PHASE_12 Stage A, Findings 2–3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.money import ZERO, loads_exact
from tradebot.marketdata.alpaca import (
    parse_asset,
    parse_bar,
    to_alpaca_timeframe,
)

HOUR = timedelta(hours=1)
#: 14:30 UTC is 09:30 New York — the open, so these bars are regular-session.
OPEN_UTC = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def bar_row(close: str = "178.21", at: datetime = OPEN_UTC) -> dict[str, Any]:
    """One bar as Alpaca publishes it — decoded the way the transport decodes it.

    Built through `loads_exact` rather than as a literal dict, so the test exercises the same
    Decimal-bearing payload the gateway will really receive.
    """
    return loads_exact(
        '{"t": "%s", "o": %s, "h": %s, "l": %s, "c": %s, "v": 1118, "n": 65, "vw": %s}'
        % (at.isoformat().replace("+00:00", "Z"), close, close, close, close, close)
    )


def asset(symbol: str = "AAPL", **overrides: Any) -> dict[str, Any]:
    payload = {
        "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "class": "us_equity",
        "exchange": "NASDAQ",
        "symbol": symbol,
        "name": f"{symbol} Inc. Common Stock",
        "status": "active",
        "tradable": True,
        "marginable": True,
        "shortable": True,
        "fractionable": True,
    }
    return payload | overrides


class TestTimeframeMapping:
    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [("1m", "1Min"), ("5m", "5Min"), ("15m", "15Min"), ("1h", "1Hour"), ("4h", "4Hour"),
         ("1d", "1Day")],
    )
    def test_every_timeframe_in_our_vocabulary_maps(self, ours: str, theirs: str) -> None:
        assert to_alpaca_timeframe(ours) == theirs

    def test_an_unsupported_timeframe_refuses_rather_than_defaulting(self) -> None:
        with pytest.raises(ConfigError, match="1w"):
            to_alpaca_timeframe("1w")


class TestBarParsing:
    def test_prices_keep_full_decimal_precision(self) -> None:
        """Alpaca publishes `178.21` unquoted; a float here would lose the guarantee."""
        bar = parse_bar(bar_row("178.235733"), HOUR)
        assert bar.close == Decimal("178.235733")
        assert not isinstance(bar.close, float)

    def test_close_time_is_the_exclusive_boundary(self) -> None:
        """Alpaca stamps a bar with its *open*; consecutive bars must report no gap."""
        bar = parse_bar(bar_row(at=OPEN_UTC), HOUR)
        assert bar.open_time == OPEN_UTC
        assert bar.close_time == OPEN_UTC + HOUR

    def test_a_regular_session_bar_is_tagged_regular(self) -> None:
        assert parse_bar(bar_row(at=OPEN_UTC), HOUR).session is MarketSession.REGULAR

    def test_a_pre_market_bar_is_tagged_extended(self) -> None:
        pre_market = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)  # 08:00 ET
        assert parse_bar(bar_row(at=pre_market), HOUR).session is MarketSession.EXTENDED

    def test_an_equity_bar_is_never_continuous(self) -> None:
        assert parse_bar(bar_row(), HOUR).session is not MarketSession.CONTINUOUS

    def test_a_missing_price_field_fails_closed(self) -> None:
        row = bar_row()
        del row["c"]
        with pytest.raises(DataStaleError, match="c"):
            parse_bar(row, HOUR)

    def test_an_unparseable_timestamp_fails_closed(self) -> None:
        with pytest.raises(DataStaleError, match="timestamp"):
            parse_bar(bar_row() | {"t": "not-a-time"}, HOUR)

    def test_a_float_that_slipped_past_the_decoder_is_refused(self) -> None:
        """Defence in depth: if a caller decodes with `json.loads`, this must not size an order."""
        with pytest.raises(DataStaleError):
            parse_bar(bar_row() | {"c": 178.21}, HOUR)


class TestAssetParsing:
    def test_a_listed_equity_gets_the_shared_whole_share_rules(self) -> None:
        market = parse_asset(asset())
        assert market is not None
        assert market.symbol == "AAPL"
        assert market.tick_size == Decimal("0.01")
        assert market.lot_size == Decimal(1)
        assert market.min_qty == Decimal(1)
        assert market.min_notional == ZERO
        assert market.quote_currency == "USD"

    def test_an_inactive_asset_is_published_as_untradable(self) -> None:
        """Listed but not tradable, so the catalogue refuses it as *delisted*, not as unknown."""
        market = parse_asset(asset(status="inactive"))
        assert market is not None and market.tradable is False

    def test_a_non_tradable_asset_is_published_as_untradable(self) -> None:
        market = parse_asset(asset(tradable=False))
        assert market is not None and market.tradable is False

    def test_a_crypto_asset_is_not_listed_at_all(self) -> None:
        """This gateway answers for US equities. Crypto has real published rules and must not be
        given the whole-share ones."""
        assert parse_asset(asset(**{"class": "crypto"})) is None

    def test_an_asset_off_a_major_exchange_is_not_listed(self) -> None:
        """OTC names are where a penny-tick assumption would hurt most; excluded rather than
        given invented rules (ADR 0025)."""
        assert parse_asset(asset(exchange="OTC")) is None

    def test_an_asset_with_no_symbol_is_not_listed(self) -> None:
        assert parse_asset(asset(symbol="")) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_alpaca_gateway.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.marketdata.alpaca'`

- [ ] **Step 3: Implement the parsers**

Create `tradebot/marketdata/alpaca.py`:

```python
"""Alpaca US equities: the one equity venue whose wire format this system understands.

Everything here is Alpaca-specific and nothing here does I/O — the same split as
`marketdata/binance.py`, and for the same reason: the code that turns a venue's JSON into the
decimals an order is sized from is the code most worth testing exhaustively, and it is testable
here with plain dictionaries.

What is *not* here is anything true of the US equity market generally. The trading rules, the
exchange timezone and the session bounds live in `marketdata/us_equities.py`, so a second equity
venue consumes them rather than re-deriving them (PHASE_12 Stage A §3.2).

Three Alpaca facts are corrected rather than propagated:

* **Prices arrive as unquoted JSON numbers.** `178.21`, not `"178.21"`. The transport decodes
  with `loads_exact`, so what reaches these parsers is already `Decimal`; a `float` arriving here
  means someone decoded with plain `json.loads`, and it fails closed rather than sizing an order.
* **`adjustment` defaults to `raw`.** Unadjusted bars put a 4:1 split in the tape as a 75%
  single-bar crash — fabricated ATR, and a price collar that vetoes everything for reasons no
  event explains. `BAR_ADJUSTMENT` is passed on every request.
* **There is no usable latest-quote endpoint on a delayed feed.** The free plan serves SIP only
  for data at least 15 minutes old, so "latest" is forbidden by construction. `book_from_bar`
  derives the quote from the newest bar instead, which keeps one consistent point-in-time view
  rather than mixing a real-time IEX quote into a delayed SIP snapshot.

Failure semantics: parse failures and missing fields raise `DataStaleError` (the response is not
usable, so no decision is taken from it); an unsupported timeframe raises `ConfigError`.
Transport-level failures are already classified by `AlpacaDataTransport`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from tradebot.core.clock import ensure_utc
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO
from tradebot.interfaces.exchange import TopOfBook, VenueMarket
from tradebot.marketdata.us_equities import MAJOR_EXCHANGES, session_of, whole_share_market

VENUE_ID: Final = "alpaca"

#: Our timeframe vocabulary → Alpaca's. Explicit rather than computed, so an unsupported one is a
#: refusal at the boundary instead of a request the venue answers with something else.
TIMEFRAMES: Final[Mapping[str, str]] = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "4h": "4Hour",
    "1d": "1Day",
}

#: Alpaca's cap on bars per page. We never paginate: a single page at this depth is far more than
#: any indicator's warm-up needs, and a paginating fetch would spend an unbounded rate budget.
MAX_BARS: Final = 10_000

#: Every Alpaca call costs one unit of a request-count budget (`venues/alpaca_transport.py`).
WEIGHT: Final = 1

#: The consolidated tape. Correct prices, honestly stale — see `DATA_DELAY`. The alternative,
#: `iex`, is real-time and covers ~2.5% of US volume, so its quotes may sit meaningfully off the
#: NBBO while carrying no marker saying so (PHASE_12 Stage A, Finding 4).
DEFAULT_FEED: Final = "sip"

#: How far behind the free plan's SIP data runs. **Not a caveat in a comment**: it is the value
#: `DataCapabilities.delay` carries, which `ContextBuilder._assert_feed_keeps_up` reads to refuse
#: an equity basket configured to cycle faster than its feed publishes.
DATA_DELAY: Final = timedelta(minutes=15)

#: Split *and* dividend adjusted. Passed explicitly on every request, and asserted on the wire by
#: a test, because a default we rely on silently is one release away from changing.
BAR_ADJUSTMENT: Final = "all"

#: Alpaca's own name for a US stock.
US_EQUITY_CLASS: Final = "us_equity"


def to_alpaca_timeframe(timeframe: str) -> str:
    """Our timeframe → Alpaca's, refusing anything we have no mapping for."""
    try:
        return TIMEFRAMES[timeframe]
    except KeyError:
        raise ConfigError(
            f"alpaca equities do not serve {timeframe!r} here; available: {sorted(TIMEFRAMES)}"
        ) from None


def _decimal(row: Mapping[str, Any], key: str) -> Decimal:
    """One price field, refusing anything that is not already exact.

    A `float` here means the response was decoded with plain `json.loads` rather than
    `loads_exact`, which is the one way a venue price can lose precision before anything sees it
    (ADR 0001). Defence in depth: the transport already prevents it.
    """
    try:
        value = row[key]
    except KeyError:
        raise DataStaleError(f"alpaca bar is missing {key!r}") from None
    if isinstance(value, float):
        raise DataStaleError(
            f"alpaca bar field {key!r} arrived as a float ({value!r}); it must be decoded with "
            "loads_exact so the venue's own precision survives"
        )
    if value is None:
        raise DataStaleError(f"alpaca bar field {key!r} is null")
    try:
        return Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise DataStaleError(f"alpaca bar field {key!r} is unusable: {value!r}") from exc


def _timestamp(row: Mapping[str, Any]) -> datetime:
    raw = row.get("t")
    if not isinstance(raw, str):
        raise DataStaleError(f"alpaca bar timestamp is missing or not a string: {raw!r}")
    try:
        return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError as exc:
        raise DataStaleError(f"alpaca bar timestamp is unusable: {raw!r}") from exc


def parse_bar(row: Mapping[str, Any], interval: timedelta) -> Candle:
    """One Alpaca bar → one `Candle`, with the exclusive close boundary and its session.

    Alpaca stamps a bar with its *open* time, so the close is computed. Propagating a stamped
    close would be guessing at a convention the venue has not stated.
    """
    open_time = _timestamp(row)
    return Candle(
        open_time=open_time,
        close_time=open_time + interval,
        open=_decimal(row, "o"),
        high=_decimal(row, "h"),
        low=_decimal(row, "l"),
        close=_decimal(row, "c"),
        volume=_decimal(row, "v"),
        session=session_of(open_time),
    )


def parse_asset(payload: Mapping[str, Any]) -> VenueMarket | None:
    """One `/v2/assets` entry → its trading rules, or `None` if this venue does not list it here.

    `None` rather than a refusal: a catalogue lists what it can answer for, and an asset outside
    this gateway's remit is simply absent — so `resolve` refuses it as *unknown*, in the venue's
    own terms, through the path every catalogue already shares.

    An asset that *is* in remit but is inactive or non-tradable is listed with `tradable=False`,
    which is a different answer: the catalogue refuses it as **delisted**, and an operator needs
    the difference between "we stopped trading this" and "we never heard of it".
    """
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    if payload.get("class") != US_EQUITY_CLASS:
        return None
    if str(payload.get("exchange") or "").upper() not in MAJOR_EXCHANGES:
        return None
    tradable = bool(payload.get("tradable")) and payload.get("status") == "active"
    return whole_share_market(symbol, tradable=tradable)


def book_from_bar(bar: Candle) -> TopOfBook:
    """Top of book derived from the newest bar this feed may legitimately show.

    Alpaca's latest-quote and latest-trade endpoints are real-time by definition, so on the free
    plan with `feed=sip` they are forbidden — that feed serves data at least 15 minutes old.
    Rather than mixing a real-time IEX quote into an otherwise delayed snapshot, which would put
    two different views of the market into one decision, the quote *is* the newest bar.

    **The stated cost: no spread.** `bid == ask == last`, and `observed_at` is the bar's close, so
    every staleness check downstream sees the truth instead of a fabricated freshness. A real
    spread requires the paid real-time SIP feed, which is therefore a precondition for live equity
    trading rather than an optimisation.
    """
    if bar.close <= ZERO:
        raise DataStaleError(
            f"alpaca published a non-positive close ({bar.close}) for the newest bar; "
            "an absent price is not a price"
        )
    return TopOfBook(bid=bar.close, ask=bar.close, last=bar.close, observed_at=bar.close_time)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_alpaca_gateway.py -v`
Expected: PASS (all parser cases).

- [ ] **Step 5: Commit**

```bash
git add tradebot/marketdata/alpaca.py tests/unit/test_alpaca_gateway.py
git commit -m "feat(marketdata): alpaca wire format to exact decimals"
```

---

### Task 5: `AlpacaGateway`

**Files:**
- Modify: `tradebot/marketdata/alpaca.py`
- Test: `tests/unit/test_alpaca_gateway.py`

**Interfaces:**
- Consumes: Task 4's parsers; `AlpacaDataTransport` and `AlpacaTransport` by *protocol* (`VenueTransport`, `TradingTransport`) — never imported concretely, which is what keeps Task 9's boundary test true
- Produces: `AlpacaGateway(data, trading, clock, *, feed=DEFAULT_FEED)` satisfying `VenueGateway`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_alpaca_gateway.py`:

```python
class _FakeDataTransport:
    """Records what was asked for, so the wire parameters are assertable."""

    venue_id = "alpaca"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def get(self, endpoint: str, params: Any, *, weight: int) -> Any:
        self.calls.append((endpoint, dict(params), weight))
        return self.responses[endpoint]

    async def close(self) -> None:
        pass


class _FakeTradingTransport:
    venue_id = "alpaca"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def call(self, endpoint: str, params: Any, *, weight: int, is_order: bool = False) -> Any:
        self.calls.append((endpoint, dict(params), weight))
        return self.responses[endpoint]

    async def close(self) -> None:
        pass


def build_gateway(
    bars: list[dict[str, Any]] | None = None,
    assets: list[dict[str, Any]] | None = None,
    *,
    now: datetime = OPEN_UTC + HOUR,
) -> tuple[AlpacaGateway, _FakeDataTransport, _FakeTradingTransport]:
    """A gateway over canned payloads.

    `bars` are **newest first**, because the gateway asks for `sort=desc` — see
    `test_bars_are_requested_newest_first`. The gateway reverses them.
    """
    data = _FakeDataTransport({BARS_ENDPOINT: {"bars": {"AAPL": bars or [bar_row()]}}})
    trading = _FakeTradingTransport(
        {ASSETS_ENDPOINT: assets if assets is not None else [asset()],
         CLOCK_ENDPOINT: {"timestamp": "2026-03-02T14:30:00Z"}}
    )
    return AlpacaGateway(data, trading, ManualClock(now)), data, trading


class TestTheParametersThatMustNeverBeDefaulted:
    async def test_every_bars_request_asks_for_fully_adjusted_data(self) -> None:
        """`adjustment` defaults to `raw` at Alpaca. Unadjusted bars put a split in the tape as a
        crash, so ATR reads fabricated volatility (PHASE_12 Stage A, Finding 2)."""
        gateway, data, _ = build_gateway()
        await gateway.fetch_bars("AAPL", "1h", 10)
        assert data.calls[0][1]["adjustment"] == "all"

    async def test_every_bars_request_names_its_feed(self) -> None:
        gateway, data, _ = build_gateway()
        await gateway.fetch_bars("AAPL", "1h", 10)
        assert data.calls[0][1]["feed"] == "sip"

    async def test_bars_are_requested_newest_first(self) -> None:
        """With `sort=asc` and no `start`, Alpaca returns the first bars of available history —
        which begins in 2016. Every series would be a decade old and abort as `DATA_STALE`."""
        gateway, data, _ = build_gateway()
        await gateway.fetch_bars("AAPL", "1h", 10)
        assert data.calls[0][1]["sort"] == "desc"
        assert "start" not in data.calls[0][1]

    async def test_the_request_is_bounded_by_the_cutoff(self) -> None:
        gateway, data, _ = build_gateway()
        await gateway.fetch_bars("AAPL", "1h", 10, end=OPEN_UTC + HOUR)
        assert data.calls[0][1]["end"].startswith("2026-03-02T15:30:00")

    async def test_the_timeframe_is_translated_to_the_venues_vocabulary(self) -> None:
        gateway, data, _ = build_gateway()
        await gateway.fetch_bars("AAPL", "4h", 10)
        assert data.calls[0][1]["timeframe"] == "4Hour"


class TestCapabilitiesTellTheTruthAboutTheDelay:
    def test_the_declared_delay_is_the_feeds_real_one(self) -> None:
        """`ContextBuilder._assert_feed_keeps_up` reads this to refuse a basket cycling faster
        than the feed publishes."""
        gateway, _, _ = build_gateway()
        assert gateway.capabilities().delay == timedelta(minutes=15)

    def test_point_in_time_is_supported_so_a_replay_can_match(self) -> None:
        gateway, _, _ = build_gateway()
        assert gateway.capabilities().supports_point_in_time is True

    def test_it_serves_our_whole_timeframe_vocabulary(self) -> None:
        gateway, _, _ = build_gateway()
        assert set(gateway.capabilities().timeframes) == set(TIMEFRAMES)


class TestBarsAreClosedAndOrdered:
    async def test_the_series_is_returned_oldest_first(self) -> None:
        """`CandleSeries` refuses anything else, and the venue was asked for newest-first."""
        gateway, _, _ = build_gateway(
            bars=[bar_row("101", OPEN_UTC + HOUR), bar_row("100", OPEN_UTC)],
            now=OPEN_UTC + timedelta(hours=3),
        )
        bars = await gateway.fetch_bars("AAPL", "1h", 10)
        assert [bar.open_time for bar in bars] == [OPEN_UTC, OPEN_UTC + HOUR]

    async def test_a_forming_bar_is_never_returned(self) -> None:
        """A forming bar's close moves, so an indicator on it differs between two reads of the
        same instant, which destroys replay (DESIGN [L12])."""
        gateway, _, _ = build_gateway(
            bars=[bar_row("101", OPEN_UTC + HOUR), bar_row("100", OPEN_UTC)],
            now=OPEN_UTC + HOUR + timedelta(minutes=30),
        )
        bars = await gateway.fetch_bars("AAPL", "1h", 10)
        assert [bar.open_time for bar in bars] == [OPEN_UTC]

    async def test_the_limit_is_honoured_after_the_forming_bar_is_dropped(self) -> None:
        gateway, data, _ = build_gateway(
            bars=[bar_row("102", OPEN_UTC + 2 * HOUR), bar_row("101", OPEN_UTC + HOUR),
                  bar_row("100", OPEN_UTC)],
            now=OPEN_UTC + timedelta(hours=4),
        )
        assert len(await gateway.fetch_bars("AAPL", "1h", 2)) == 2
        # One extra is requested, because the newest may still be forming.
        assert data.calls[0][1]["limit"] == 3

    async def test_an_absent_symbol_in_the_payload_yields_no_bars(self) -> None:
        gateway = AlpacaGateway(
            _FakeDataTransport({BARS_ENDPOINT: {"bars": {}}}),
            _FakeTradingTransport({}),
            ManualClock(OPEN_UTC + HOUR),
        )
        assert await gateway.fetch_bars("AAPL", "1h", 10) == ()


class TestTopOfBookComesFromTheNewestBar:
    async def test_the_quote_is_the_newest_closed_bars_close(self) -> None:
        gateway, _, _ = build_gateway(
            bars=[bar_row("178.21", OPEN_UTC + HOUR), bar_row("100", OPEN_UTC)],
            now=OPEN_UTC + timedelta(hours=3),
        )
        book = await gateway.fetch_top_of_book("AAPL")
        assert book.last == Decimal("178.21")
        assert book.bid == book.ask == book.last

    async def test_observed_at_is_the_bars_close_so_the_delay_is_visible(self) -> None:
        """The quote must *not* claim to be current. Every staleness check downstream reads this,
        and a fabricated freshness is what would hide a 15-minute-old view."""
        gateway, _, _ = build_gateway(
            bars=[bar_row("178.21", OPEN_UTC)], now=OPEN_UTC + timedelta(hours=3)
        )
        book = await gateway.fetch_top_of_book("AAPL")
        assert book.observed_at == OPEN_UTC + timedelta(minutes=1)

    async def test_no_bars_at_all_fails_closed(self) -> None:
        gateway = AlpacaGateway(
            _FakeDataTransport({BARS_ENDPOINT: {"bars": {"AAPL": []}}}),
            _FakeTradingTransport({}),
            ManualClock(OPEN_UTC + HOUR),
        )
        with pytest.raises(DataStaleError, match="no bars"):
            await gateway.fetch_top_of_book("AAPL")


class TestMarketsAreNarrowedNotInvented:
    async def test_only_listed_us_equities_survive(self) -> None:
        gateway, _, _ = build_gateway(
            assets=[asset("AAPL"), asset("BTCUSD", **{"class": "crypto"}), asset("PNK",
                                                                                exchange="OTC")]
        )
        markets = await gateway.fetch_markets()
        assert {market.symbol for market in markets} == {"AAPL"}

    async def test_the_asset_list_is_asked_for_equities_only(self) -> None:
        gateway, _, trading = build_gateway()
        await gateway.fetch_markets()
        assert trading.calls[0][1]["asset_class"] == "us_equity"

    async def test_an_empty_asset_list_fails_closed(self) -> None:
        gateway, _, _ = build_gateway(assets=[])
        with pytest.raises(DataStaleError, match="no assets"):
            await gateway.fetch_markets()
```

Add to the imports at the top of the file: `ASSETS_ENDPOINT`, `BARS_ENDPOINT`, `CLOCK_ENDPOINT`, `TIMEFRAMES`, `AlpacaGateway` from `tradebot.marketdata.alpaca`, and `ManualClock` from `tradebot.core.clock`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_alpaca_gateway.py -k "Parameters or Capabilities or Closed or TopOfBook or Narrowed" -v`
Expected: FAIL — `ImportError: cannot import name 'AlpacaGateway'`

- [ ] **Step 3: Implement the gateway**

Append to `tradebot/marketdata/alpaca.py`:

```python
#: Endpoints, named as constants so a test can assert the wire without repeating a path literal.
BARS_ENDPOINT: Final = "/v2/stocks/bars"
ASSETS_ENDPOINT: Final = "GET /v2/assets"
CLOCK_ENDPOINT: Final = "GET /v2/clock"


class AlpacaGateway:
    """`VenueGateway` for Alpaca US equities. Read-only: this class cannot place an order.

    It holds **two** transports because Alpaca splits the answers across two hosts: bars come from
    `data.alpaca.markets`, while the asset list that becomes the catalogue and the venue clock come
    from the trading host. Both share one rate limiter, because a venue bans an IP and a key rather
    than a code path (ADR 0010).

    Neither is imported concretely: they arrive by injection as a `VenueTransport` and a
    `TradingTransport`, so this module stays importable by `app.py` alone and a venue swap costs a
    gateway rather than a graph (PHASE_12 Stage A §3.7).
    """

    venue_id = VENUE_ID

    def __init__(
        self,
        data: VenueTransport,
        trading: TradingTransport,
        clock: Clock,
        *,
        feed: str = DEFAULT_FEED,
    ) -> None:
        self._data = data
        self._trading = trading
        self._clock = clock
        self._feed = feed

    async def fetch_bars(
        self, symbol: str, timeframe: str, limit: int, *, end: datetime | None = None
    ) -> tuple[Candle, ...]:
        """The newest `limit` closed bars at or before the cutoff, oldest first.

        **`sort=desc` is load-bearing, not a preference.** Alpaca takes `start`, `end` and `limit`,
        and with `sort=asc` and no `start` it returns the *first* `limit` bars of available history
        — which begins in 2016. Every series would be a decade old and every cycle would abort as
        `DATA_STALE`. Requesting newest-first and reversing gives Binance's `endTime + limit`
        semantics exactly, and avoids computing a `start`, which for equities cannot be derived
        from the bar count anyway: 260 daily bars span thirteen calendar months, not 260 days.
        """
        interval = timeframe_interval(timeframe)
        cutoff = ensure_utc(end) if end is not None else self._clock.now()
        params: dict[str, Any] = {
            "symbols": symbol.upper(),
            "timeframe": to_alpaca_timeframe(timeframe),
            # One extra bar, because the newest may still be forming and is dropped below.
            "limit": min(max(limit, 1) + 1, MAX_BARS),
            # Neither of these may be left to a default: `adjustment` defaults to `raw`, and an
            # unnamed feed is a feed nobody can audit from the log.
            "adjustment": BAR_ADJUSTMENT,
            "feed": self._feed,
            "sort": "desc",
            "end": cutoff.isoformat().replace("+00:00", "Z"),
        }
        payload = await self._data.get(BARS_ENDPOINT, params, weight=WEIGHT)
        rows = self._rows_for(payload, symbol.upper())
        bars = tuple(parse_bar(row, interval) for row in reversed(rows))
        # Only closed bars. Alpaca's `end` filters on the bar's *open*, so the newest row may
        # still be forming and is dropped rather than trusted (DESIGN [L12]).
        closed = tuple(bar for bar in bars if bar.close_time <= cutoff)
        return closed[-limit:]

    async def fetch_top_of_book(self, symbol: str) -> TopOfBook:
        """The newest closed bar, as a book. See `book_from_bar` for why there is no live quote."""
        bars = await self.fetch_bars(symbol, "1m", 1)
        if not bars:
            raise DataStaleError(
                f"alpaca published no bars for {symbol} at or before now, so there is no price to "
                "quote; on a delayed feed the newest bar is the only admissible book"
            )
        return book_from_bar(bars[-1])

    async def fetch_markets(self) -> tuple[VenueMarket, ...]:
        payload = await self._trading.call(
            ASSETS_ENDPOINT, {"status": "active", "asset_class": US_EQUITY_CLASS}, weight=WEIGHT
        )
        if not isinstance(payload, list) or not payload:
            raise DataStaleError("alpaca returned no assets; the catalogue would list nothing")
        markets = tuple(
            market for entry in payload if (market := parse_asset(entry)) is not None
        )
        if not markets:
            raise DataStaleError(
                "alpaca listed assets but none was a tradable US equity on a recognised exchange"
            )
        return markets

    async def server_time(self) -> datetime:
        payload = await self._trading.call(CLOCK_ENDPOINT, {}, weight=WEIGHT)
        if not isinstance(payload, Mapping):
            raise DataStaleError("alpaca clock returned a non-object payload")
        raw = payload.get("timestamp")
        if not isinstance(raw, str):
            raise DataStaleError(f"alpaca clock timestamp is unusable: {raw!r}")
        try:
            return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError as exc:
            raise DataStaleError(f"alpaca clock timestamp is unusable: {raw!r}") from exc

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=tuple(TIMEFRAMES),
            max_history=MAX_BARS,
            # The free plan's SIP data runs 15 minutes behind. Declared rather than hidden, so a
            # basket configured to cycle faster is refused at wiring by name.
            delay=DATA_DELAY,
            supports_point_in_time=True,
        )

    async def close(self) -> None:
        await self._data.close()
        await self._trading.close()

    @staticmethod
    def _rows_for(payload: Any, symbol: str) -> list[Any]:
        """Alpaca's multi-symbol shape, narrowed to the one symbol asked for.

        An absent symbol is an empty series, not an error: the venue has no bars for it in this
        window, which `VenueMarketData` turns into `DataStaleError` with the instrument named.
        """
        if not isinstance(payload, Mapping):
            raise DataStaleError(f"alpaca bars returned {type(payload).__name__}, expected an object")
        bars = payload.get("bars")
        if not isinstance(bars, Mapping):
            raise DataStaleError("alpaca bars payload has no 'bars' object")
        rows = bars.get(symbol) or []
        if not isinstance(rows, list):
            raise DataStaleError(f"alpaca bars for {symbol} is not a list")
        return rows
```

Add to the module's imports: `from tradebot.core.clock import Clock, ensure_utc`, `from tradebot.interfaces.exchange import TopOfBook, TradingTransport, VenueMarket, VenueTransport`, `from tradebot.interfaces.market_data import DataCapabilities`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_alpaca_gateway.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the money-discipline boundary still holds**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_money_discipline.py -q`
Expected: PASS — `marketdata/alpaca.py` introduces no `float(` call and no `Decimal(some_float)`.

- [ ] **Step 6: Commit**

```bash
git add tradebot/marketdata/alpaca.py tests/unit/test_alpaca_gateway.py
git commit -m "feat(marketdata): AlpacaGateway over two hosts, one rate budget"
```

---

### Task 6: the two contract suites

**Files:**
- Modify: `tests/contract/test_market_data_contract.py`
- Modify: `tests/contract/test_catalogue_contract.py`

**Interfaces:**
- Consumes: `AlpacaGateway` (Task 5)
- Produces: nothing importable — this task only widens existing coverage

**Note on scope.** The spec's DoD item 6 said "both contract suites run against `AlpacaGateway`". Grounding the plan showed the catalogue suite's shared cases are hardcoded to Binance symbols (`BTC/USDT`, lot `0.00001`, wire form `BTCUSDT`, `AssetClass.CRYPTO`) and that the class under test — `VenueCatalogue` — is *literally the same class* Alpaca uses. Parameterizing those cases would re-test shared code with different data at the cost of a large refactor to a well-written suite. So: the **provider** suite gains a real parameter, and the **catalogue** suite gains a focused equity class beside the shared cases. Update DoD 6 in the spec to match.

- [ ] **Step 1: Add Alpaca to the provider contract suite**

In `tests/contract/test_market_data_contract.py`, the `provider` fixture currently returns a provider while `venue_instrument` is a fixed Binance instrument. Widen both so the provider travels with the instrument it can serve:

```python
@pytest.fixture
def equity_instrument() -> Instrument:
    return Instrument(
        symbol="AAPL",
        venue="alpaca",
        asset_class=AssetClass.EQUITY,
        base_currency="AAPL",
        quote_currency="USD",
        lot_size=Decimal(1),
        tick_size=Decimal("0.01"),
        min_qty=Decimal(1),
    )


@pytest.fixture
def alpaca_venue(clock: ManualClock) -> VenueMarketData:
    """`VenueMarketData` over the real Alpaca parsing, serving the same bars as every other
    provider in this suite — so the point-in-time cutoff and `observed_at` stamping are proven
    over Alpaca's wire format rather than only over a fake."""
    return VenueMarketData(
        AlpacaGateway(_AlpacaBarsTransport(recorded_rows()), _NoTradingTransport(), clock),
        clock,
        asset_class=AssetClass.EQUITY,
    )


@pytest.fixture(params=["replay", "venue", "cached_venue", "alpaca"])
def provider_and_instrument(
    request: pytest.FixtureRequest,
    replay: ReplayMarketData,
    venue: VenueMarketData,
    alpaca_venue: VenueMarketData,
    clock: ManualClock,
    venue_instrument: Instrument,
    equity_instrument: Instrument,
) -> tuple[MarketDataProvider, Instrument]:
    return {
        "replay": (replay, venue_instrument),
        "venue": (venue, venue_instrument),
        "cached_venue": (CachingMarketData(venue, clock), venue_instrument),
        "alpaca": (alpaca_venue, equity_instrument),
    }[request.param]
```

Update each `TestProviderContract` case to take `provider_and_instrument` and unpack it, replacing its `provider` and `venue_instrument` arguments.

The two doubles, added to the same file:

```python
class _AlpacaBarsTransport:
    """Serves this suite's candles in Alpaca's wire shape, newest first as `sort=desc` asks.

    Deliberately a `VenueTransport` double rather than a `FakeGateway`: what this parameter adds
    to the suite is that the point-in-time cutoff and `observed_at` stamping hold over Alpaca's
    *real parsing*, not over a fake that has none.
    """

    venue_id = "alpaca"

    def __init__(self, series: dict[str, tuple[Candle, ...]]) -> None:
        self._series = series

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        bars = self._series[params["timeframe"]]
        rows = [
            {
                "t": bar.open_time.isoformat().replace("+00:00", "Z"),
                "o": bar.open,
                "h": bar.high,
                "l": bar.low,
                "c": bar.close,
                "v": bar.volume,
            }
            for bar in reversed(bars)
        ]
        return {"bars": {"AAPL": rows[: params["limit"]]}}

    async def close(self) -> None:
        pass


class _NoTradingTransport:
    """The provider contract must never need the trading host: bars and quotes are data-side."""

    venue_id = "alpaca"

    async def call(self, endpoint: str, params: Any, *, weight: int, is_order: bool = False) -> Any:
        raise AssertionError(f"the provider contract reached the trading host: {endpoint}")

    async def close(self) -> None:
        pass
```

Build the fixture's series from the suite's existing `recorded()` helper, keyed by the *Alpaca*
timeframe name so `_AlpacaBarsTransport` can look it up:

```python
@pytest.fixture
def alpaca_venue(clock: ManualClock) -> VenueMarketData:
    series = {to_alpaca_timeframe(tf): recorded(tf).candles for tf in TIMEFRAMES}
    return VenueMarketData(
        AlpacaGateway(_AlpacaBarsTransport(series), _NoTradingTransport(), clock),
        clock,
        asset_class=AssetClass.EQUITY,
    )
```

Add `from tradebot.marketdata.alpaca import AlpacaGateway, to_alpaca_timeframe` and
`from collections.abc import Mapping` to the file's imports.

Two notes on why this parameter is narrower than the others:

- **Do not** add Alpaca to `TestReplayAndVenueAgree`. That class proves the replay and live paths
  build byte-identical snapshots for *the same instrument*; an equity instrument is a different
  one, so the comparison would be meaningless rather than merely failing.
- **`recorded()` produces continuous bars**, so `session_of` will tag them by wall-clock. That is
  fine here — the suite asserts cutoff and ordering semantics, not session tagging, which
  `test_alpaca_gateway.py` owns.

- [ ] **Step 2: Run the provider suite**

Run: `.venv\Scripts\python.exe -m pytest tests/contract/test_market_data_contract.py -v`
Expected: PASS, with the `alpaca` parameter visible in the test ids.

- [ ] **Step 3: Add the equity catalogue class**

Append to `tests/contract/test_catalogue_contract.py`:

```python
class TestAnEquityCatalogueIsTheSameCatalogue:
    """`VenueCatalogue` over an equity gateway. The resolution semantics above are the same class
    and are not re-tested; what is proven here is that an equity venue's answers travel through
    them intact — including the rules no equity venue publishes (PHASE_12 Stage A, Finding 1)."""

    @pytest.fixture
    def equities(self, clock: ManualClock) -> VenueCatalogue:
        gateway = FakeGateway(
            [],
            venue_id="alpaca",
            markets=[whole_share_market("AAPL"), whole_share_market("DELI", tradable=False)],
        )
        return VenueCatalogue(gateway, clock, asset_class=AssetClass.EQUITY)

    async def test_it_resolves_a_listed_equity_to_the_shared_rules(
        self, equities: VenueCatalogue
    ) -> None:
        market = await equities.resolve("AAPL")
        assert market.tick_size == Decimal("0.01")
        assert market.lot_size == Decimal(1)
        assert market.min_notional == Decimal(0)
        assert market.quote_currency == "USD"

    async def test_an_untradable_equity_is_refused_as_delisted(
        self, equities: VenueCatalogue
    ) -> None:
        with pytest.raises(ConfigError, match="delisted"):
            await equities.resolve("DELI")

    async def test_an_unlisted_ticker_is_refused_as_unknown_naming_the_venue(
        self, equities: VenueCatalogue
    ) -> None:
        with pytest.raises(ConfigError, match="alpaca does not list 'ZZZZ'"):
            await equities.resolve("ZZZZ")

    async def test_a_resolved_equity_is_stamped_with_the_catalogues_asset_class(
        self, equities: VenueCatalogue
    ) -> None:
        instrument = await instrument_of(equities, "AAPL")
        assert instrument.key == "alpaca:AAPL"
        assert instrument.asset_class is AssetClass.EQUITY
```

Add `from tradebot.marketdata.us_equities import whole_share_market` to the file's imports.

- [ ] **Step 4: Run the catalogue suite**

Run: `.venv\Scripts\python.exe -m pytest tests/contract/test_catalogue_contract.py -v`
Expected: PASS, including the four new equity cases.

- [ ] **Step 5: Confirm the spec already matches**

The spec's DoD item 6 was amended when this plan was written and should already read "the
**provider** contract suite runs against `AlpacaGateway`, and the catalogue suite carries an equity
class". Verify it does; if not, make it so before committing.

- [ ] **Step 6: Run both contract suites together**

Run: `.venv\Scripts\python.exe -m pytest -m contract -q`
Expected: PASS — the broker and LLM contracts must be unaffected.

- [ ] **Step 7: Commit**

```bash
git add tests/contract/test_market_data_contract.py tests/contract/test_catalogue_contract.py
git commit -m "test: alpaca joins the provider contract; equities join the catalogue contract"
```

---

### Task 7: wire it — `_alpaca_stack` stops refusing

**Files:**
- Modify: `tradebot/app.py:574-611` (`_alpaca_stack`), `tradebot/app.py:1247-1274` (`_feed_for`)
- Test: `tests/unit/test_live_wiring.py`

**Interfaces:**
- Consumes: `AlpacaGateway` (Task 5), `AlpacaDataTransport` (Task 3)
- Produces: a `VenueStack` for `BrokerChoice.ALPACA` with a real `prices` and a real `catalogue`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_live_wiring.py`:

```python
class TestTheAlpacaStackIsWired:
    """Gate 4 of Piece 2: `_alpaca_stack` used to refuse for want of an equity feed."""

    def test_it_builds_without_an_injected_market_data_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret")
        stack = _alpaca_stack(
            StackRequest(
                instruments=(APPLE,),
                clock=ManualClock(NOW),
                mode=Mode.PAPER,
                feed=PriceFeed(),
                start_equity=Decimal(0),
                quote_currency="USD",
            )
        )
        assert stack.prices is not None
        assert stack.catalogue.venue_id == "alpaca"
        assert stack.catalogue.asset_class is AssetClass.EQUITY

    def test_reading_prices_cannot_move_the_venue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A venue stack has no bridge: `read_only_prices` and `prices` are the same object,
        because reading a real venue changes nothing (PHASE_10 decision 4)."""
        monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret")
        stack = _alpaca_stack(
            StackRequest((APPLE,), ManualClock(NOW), Mode.PAPER, PriceFeed(), Decimal(0), "USD")
        )
        assert stack.read_only_prices is stack.prices

    def test_the_catalogue_is_no_longer_the_unavailable_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key")
        monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret")
        stack = _alpaca_stack(
            StackRequest((APPLE,), ManualClock(NOW), Mode.PAPER, PriceFeed(), Decimal(0), "USD")
        )
        assert not isinstance(stack.catalogue, UnavailableCatalogue)
```

Define `APPLE` at module scope as an `Instrument` with `symbol="AAPL"`, `venue="alpaca"`, `asset_class=AssetClass.EQUITY`, `base_currency="AAPL"`, `quote_currency="USD"`, `lot_size=Decimal(1)`, `tick_size=Decimal("0.01")`, `min_qty=Decimal(1)`. Import `_alpaca_stack`, `StackRequest`, `PriceFeed` from `tradebot.app` and `UnavailableCatalogue` from `tradebot.marketdata.catalogue`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_live_wiring.py::TestTheAlpacaStackIsWired -v`
Expected: FAIL — `ConfigError: the alpaca broker needs an equity market-data provider`

- [ ] **Step 3: Rewrite `_alpaca_stack`**

Replace the body of `_alpaca_stack` in `tradebot/app.py`:

```python
def _alpaca_stack(request: StackRequest) -> VenueStack:
    """Alpaca equities: prices, catalogue, broker, exchange calendar and corporate actions.

    Two transports over two hosts, sharing one rate limiter — bars come from the data host, while
    the asset list that becomes the catalogue and the venue clock come from the trading host. The
    limiter is shared because a venue bans an IP and a key, not a code path (ADR 0010).

    The catalogue is this venue's own rather than the caller's, for the same reason Binance's is:
    the orders go here, so the rules that decide whether they are legal are the ones this
    connection can be asked for. A caller may still override the *prices* — "Alpaca broker, some
    other data provider" is a legitimate composition (PHASE_12 Stage A §3.7).
    """
    clock, mode = request.clock, request.mode
    key_id, secret_key = credentials("alpaca", mode)
    client = httpx.AsyncClient()
    transport = AlpacaTransport(client, clock, mode=mode, key_id=key_id, secret_key=secret_key)
    data_transport = AlpacaDataTransport(
        client, clock, mode=mode, key_id=key_id, secret_key=secret_key, limiter=transport.limiter
    )
    broker = AlpacaBroker(transport, clock, instruments=request.instruments)
    provider = VenueMarketData(
        AlpacaGateway(data_transport, transport, clock), clock, asset_class=AssetClass.EQUITY
    )
    prices = request.feed.prices or provider
    return VenueStack(
        broker=broker,
        prices=prices,
        catalogue=request.feed.catalogue or provider.catalogue,
        # A venue stack has no bridge: reading a real venue changes nothing, so an observer and a
        # cycle read the same object (PHASE_10 decision 4).
        read_only_prices=prices,
        calendar=AlpacaCalendar(transport, clock),
        announcements=AlpacaAnnouncements(transport),
        preflight=VenuePreflight(broker, clock, mode=mode),
        closers=(client.aclose,),
    )
```

Add the imports to `app.py`: `from tradebot.marketdata.alpaca import AlpacaGateway`, `from tradebot.venues.alpaca_transport import AlpacaDataTransport, AlpacaTransport`, and `from tradebot.marketdata.venue import VenueMarketData` if not already present. `UnavailableCatalogue` may now be unused — remove the import if `ruff` reports it.

- [ ] **Step 4: Update `_feed_for`'s docstring**

Its last line reads "Alpaca gets nothing either way, and `_alpaca_stack` refuses by name." Replace with:

```
    Alpaca gets nothing here either, and for the same reason as Binance: its own stack builds two
    transports over one limiter, so a feed built here would give one key a second, independent
    rate budget.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_live_wiring.py tests/unit/test_alpaca_broker.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradebot/app.py tests/unit/test_live_wiring.py
git commit -m "feat(app): wire the alpaca equity stack; gate 4 of Piece 2 is gone"
```

---

### Task 8: a mark tolerance below the feed delay freezes the portfolio forever

**Files:**
- Modify: `tradebot/control/valuation.py` (`PortfolioWatch._assert_tolerance_outlives_the_sweep`)
- Test: `tests/unit/test_valuation_boundary.py`

**Interfaces:**
- Consumes: `MarketDataProvider.capabilities().delay`
- Produces: no new symbol — an existing wiring assertion gains a second clause

**Why this exists.** A quote from a 15-minute-delayed feed carries an `observed_at` at least 15 minutes in the past, truthfully. `GlobalRiskPolicy.mark_staleness_seconds` defaults to **300**, so `Marks.price_of` would return `None` for every equity mark, `aggregate` would freeze on every evaluation, and every cycle would record `BLOCKED` — for a portfolio that is entirely healthy, with no event explaining why. `PortfolioWatch` already refuses a tolerance below `3 ×` the sweep cadence for exactly this reason; the feed's own delay is the same hazard arriving through the other door.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_valuation_boundary.py`:

```python
class TestTheToleranceMustOutliveTheFeed:
    """A tolerance shorter than the feed's delay freezes the portfolio permanently.

    Every mark is stale before it arrives, so `aggregate` freezes on every evaluation and every
    cycle records `BLOCKED` — a healthy portfolio, denied, with nothing in the log naming the
    cause. Caught at wiring rather than at 03:00, exactly as the sweep-cadence check is
    (PHASE_12 Stage A, decision 3).
    """

    def test_a_tolerance_below_the_declared_delay_is_refused(self) -> None:
        policy = GlobalRiskPolicy(mark_staleness_seconds=300)
        with pytest.raises(ConfigError, match="publishes with a"):
            PortfolioWatch(
                Ledger(ManualClock(NOW), venue="alpaca"),
                Marks(),
                lambda: (),
                _watchdog(),
                _store(),
                ManualClock(NOW),
                market_data=_delayed_provider(timedelta(minutes=15)),
                catalogue=_catalogue(),
                notional_currency="USD",
                policy_of=lambda: policy,
                resync_seconds=30.0,
            )

    def test_the_refusal_names_both_numbers_and_the_remedy(self) -> None:
        policy = GlobalRiskPolicy(mark_staleness_seconds=300)
        with pytest.raises(ConfigError) as caught:
            PortfolioWatch(
                Ledger(ManualClock(NOW), venue="alpaca"),
                Marks(),
                lambda: (),
                _watchdog(),
                _store(),
                ManualClock(NOW),
                market_data=_delayed_provider(timedelta(minutes=15)),
                catalogue=_catalogue(),
                notional_currency="USD",
                policy_of=lambda: policy,
                resync_seconds=30.0,
            )
        message = str(caught.value)
        assert "300" in message and "900" in message
        assert "mark_staleness_seconds" in message

    def test_a_tolerance_above_the_delay_wires_cleanly(self) -> None:
        policy = GlobalRiskPolicy(mark_staleness_seconds=1200)
        PortfolioWatch(
            Ledger(ManualClock(NOW), venue="alpaca"),
            Marks(),
            lambda: (),
            _watchdog(),
            _store(),
            ManualClock(NOW),
            market_data=_delayed_provider(timedelta(minutes=15)),
            catalogue=_catalogue(),
            notional_currency="USD",
            policy_of=lambda: policy,
            resync_seconds=30.0,
        )

    def test_a_real_time_feed_is_unaffected(self) -> None:
        """Crypto declares no delay, so the existing default keeps working unchanged."""
        policy = GlobalRiskPolicy(mark_staleness_seconds=300)
        PortfolioWatch(
            Ledger(ManualClock(NOW), venue="binance"),
            Marks(),
            lambda: (),
            _watchdog(),
            _store(),
            ManualClock(NOW),
            market_data=_delayed_provider(timedelta(0)),
            catalogue=_catalogue(),
            notional_currency="USDT",
            policy_of=lambda: policy,
            resync_seconds=30.0,
        )
```

Add module-level helpers `_watchdog()`, `_store()`, `_catalogue()` returning whatever minimal doubles the file already uses (follow its existing construction), and:

```python
def _delayed_provider(delay: timedelta) -> MarketDataProvider:
    """A provider that declares a publication delay and serves nothing else."""

    class _Delayed:
        provider_id = "test"

        async def get_candles(self, instrument, timeframe, limit, end=None):  # type: ignore[no-untyped-def]
            raise AssertionError("the wiring check must not fetch")

        async def get_quote(self, instrument):  # type: ignore[no-untyped-def]
            raise AssertionError("the wiring check must not fetch")

        def capabilities(self) -> DataCapabilities:
            return DataCapabilities(timeframes=("1h",), max_history=100, delay=delay)

    return _Delayed()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_valuation_boundary.py::TestTheToleranceMustOutliveTheFeed -v`
Expected: FAIL — the first two cases raise nothing, because no such check exists yet.

- [ ] **Step 3: Implement**

In `tradebot/control/valuation.py`, rename the check and add the second clause. It must run *after* `self._market_data` is assigned:

```python
    def _assert_tolerance_is_reachable(self, resync_seconds: float) -> None:
        """Refuse a tolerance no mark could ever satisfy, for either of the two reasons.

        A tolerance below the **sweep cadence** means every mark is stale before the next refresh.
        A tolerance below the **feed's declared delay** means every mark is stale the moment it
        arrives — a 15-minute-delayed equity feed stamps its quotes 15 minutes in the past, and it
        is telling the truth (PHASE_12 Stage A §3.5). Either way the portfolio freezes permanently,
        every cycle records `BLOCKED`, and nothing in the log says why.

        Caught here rather than in the model, because `core/` may not import the supervisor's
        cadence or a provider's capabilities — and caught at wiring rather than at 03:00.
        """
        tolerance = self._policy_of().mark_tolerance.total_seconds()
        floor = resync_seconds * MIN_TOLERANCE_MULTIPLE
        if tolerance < floor:
            raise ConfigError(
                f"mark_staleness_seconds is {tolerance:.0f}s but the portfolio is only swept every "
                f"{resync_seconds:.0f}s; a tolerance below {MIN_TOLERANCE_MULTIPLE}× the sweep "
                "freezes the portfolio permanently and nothing would ever trade"
            )
        delay = (
            self._market_data.capabilities().delay.total_seconds()
            if self._market_data is not None
            else 0.0
        )
        if tolerance < delay:
            raise ConfigError(
                f"mark_staleness_seconds is {tolerance:.0f}s but "
                f"{self._market_data.provider_id} publishes with a {delay:.0f}s delay, so every "
                "mark is stale the moment it arrives and the portfolio would freeze permanently. "
                f"Raise mark_staleness_seconds above {delay:.0f}s, or subscribe to a real-time feed"
            )
```

Update the constructor's call site to `self._assert_tolerance_is_reachable(resync_seconds)`, moved
below the `self._market_data = market_data` assignment.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_valuation_boundary.py tests/unit/test_supervisor.py tests/scenario/test_valuation.py -q`
Expected: PASS — no existing wiring regresses, because every current provider declares `delay=0`.

- [ ] **Step 5: Commit**

```bash
git add tradebot/control/valuation.py tests/unit/test_valuation_boundary.py
git commit -m "feat(control): refuse a mark tolerance shorter than the feed's own delay"
```

---

### Task 9: freeze the venue boundary

**Files:**
- Create: `tests/unit/test_venue_boundary.py`

**Interfaces:**
- Consumes: nothing at runtime — this is a structural test over the source tree
- Produces: nothing

- [ ] **Step 1: Write the test**

Create `tests/unit/test_venue_boundary.py`:

```python
"""Only the composition root may name a venue's concrete adapter.

`CLAUDE.md`'s layering rule says nothing outside `app.py` imports a concrete adapter. For the
Alpaca modules that is currently true, and this freezes it — because "minimal blast radius when we
swap venues" is a claim, and a claim about structure should be checked rather than intended.

The same erosion already happened to Binance: `marketdata/factory.py` imports `BinanceSpotGateway`
directly, so swapping Binance now costs a module nobody would think to look in. Asserted in the
manner of `test_valuation_boundary.py` and `test_dashboard_chart.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "tradebot"

#: Modules whose whole purpose is to know what Alpaca is. Swapping venues rewrites these and
#: nothing else.
VENUE_MODULES = (
    "tradebot.marketdata.alpaca",
    "tradebot.venues.alpaca_transport",
    "tradebot.execution.brokers.alpaca",
)

#: The composition root is the one place allowed to name a concrete adapter.
ALLOWED_IMPORTERS = {"tradebot.app"}


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("venue_module", VENUE_MODULES)
def test_only_the_composition_root_imports_a_venue_module(venue_module: str) -> None:
    importers = {
        _module_name(path)
        for path in PACKAGE.rglob("*.py")
        if venue_module in _imports_of(path) and _module_name(path) != venue_module
    }
    assert importers == ALLOWED_IMPORTERS, (
        f"{venue_module} is imported by {sorted(importers - ALLOWED_IMPORTERS)}. Swapping this "
        "venue is supposed to cost one gateway, one transport and one line of wiring; every extra "
        "importer is another module a swap has to find."
    )


def test_the_gateway_depends_on_transport_protocols_not_on_a_transport() -> None:
    """The gateway takes its transports by injection, so the boundary above holds by construction
    rather than by discipline."""
    imports = _imports_of(PACKAGE / "marketdata" / "alpaca.py")
    assert "tradebot.venues.alpaca_transport" not in imports
    assert "tradebot.interfaces.exchange" in imports
```

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_venue_boundary.py -v`
Expected: PASS. If it fails, the fix is to remove the offending import, **not** to widen `ALLOWED_IMPORTERS`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_venue_boundary.py
git commit -m "test: freeze the alpaca import boundary at the composition root"
```

---

### Task 10: an equity basket completes a cycle

**Files:**
- Create: `tests/scenario/test_equity_basket.py`

**Interfaces:**
- Consumes: everything above
- Produces: nothing — this is the Stage A exit criterion

**Why `build_sim` and not `Harness`.** `tests/scenario/harness.py` hardcodes `venue="sim"`,
`balances={"USDT": ...}` and `notional_currency="USDT"`, so it cannot express a USD-quoted equity
portfolio without being rewritten. `build_sim(market_data=..., catalogue=...)` goes through the real
composition root, which is better anyway: it exercises `_quote_currency` resolving to `USD`, the
ledger being seeded in USD, and `PortfolioWatch` being wired with the Alpaca catalogue — the three
places an equity basket differs from a crypto one.

- [ ] **Step 1: Write the scenario**

Create `tests/scenario/test_equity_basket.py`:

```python
"""Stage A's exit criterion: an equity-only basket completes a full decision cycle.

Nothing above the venue layer learns that equities exist — the same runner, the same risk engines,
the same ledger and the same event log a crypto basket uses. That identity is the whole point: a
separate equity path would mean the thing a soak validated is not the thing that trades (ADR 0020).

Gates 1 and 3 of Piece 2 are untouched and still stand. They only fire on *mixing*: with every
instrument quoted in USD on one venue, `_quote_currency` resolves and the catalogue answers for the
instrument's own venue.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from tradebot.app import build_sim
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy, PanelConfig, Schedule, SeatConfig
from tradebot.core.enums import AssetClass, CycleOutcome
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.marketdata.alpaca import AlpacaGateway, to_alpaca_timeframe
from tradebot.marketdata.catalogue import VenueCatalogue
from tradebot.marketdata.us_equities import whole_share_market
from tradebot.marketdata.venue import VenueMarketData
from tradebot.persistence.schema import cycles

pytestmark = pytest.mark.scenario

#: 2026-03-02 is a Monday. 20:00 UTC is 15:00 New York — inside the regular session, so the bars
#: this scenario builds are `REGULAR` and admissible as indicator input.
NOW = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)

APPLE = Instrument(
    symbol="AAPL",
    venue="alpaca",
    asset_class=AssetClass.EQUITY,
    base_currency="AAPL",
    quote_currency="USD",
    lot_size=Decimal(1),
    tick_size=Decimal("0.01"),
    min_qty=Decimal(1),
)

#: A 15-minute-delayed feed cannot back a faster cycle, and marks must outlive the delay — so the
#: tolerance is 20 minutes and the schedule is 30 (PHASE_12 Stage A §3.5, decision 3).
EQUITY_POLICY = GlobalRiskPolicy(mark_staleness_seconds=1200)


class _BarsTransport:
    """Alpaca-shaped bars ending at `NOW`, newest first as `sort=desc` asks for."""

    venue_id = "alpaca"

    def __init__(self, count: int = 300) -> None:
        self.count = count

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        minutes = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "4Hour": 240, "1Day": 1440}
        step = timedelta(minutes=minutes[params["timeframe"]])
        rows = [
            {
                "t": (NOW - step * (index + 1)).isoformat().replace("+00:00", "Z"),
                "o": Decimal("178.00"),
                "h": Decimal("178.50"),
                "l": Decimal("177.50"),
                "c": Decimal("178.21"),
                "v": Decimal(1000),
            }
            for index in range(self.count)
        ]
        return {"bars": {"AAPL": rows[: params["limit"]]}}

    async def close(self) -> None:
        pass


class _AssetsTransport:
    venue_id = "alpaca"

    async def call(self, endpoint: str, params: Any, *, weight: int, is_order: bool = False) -> Any:
        return {"timestamp": NOW.isoformat().replace("+00:00", "Z")}

    async def close(self) -> None:
        pass


def equity_basket(*, every_seconds: int = 1800) -> Basket:
    return Basket(
        basket_id="equities",
        name="US equities",
        instruments=(APPLE,),
        panel=PanelConfig(
            panel_id="stub",
            seats=(SeatConfig(seat_id="analyst", role="analyst", provider_id="stub", model="stub"),),
        ),
        schedule=Schedule(every_seconds=every_seconds),
        timeframes=("1h", "1d"),
    )


def alpaca_feed(clock: ManualClock) -> tuple[VenueMarketData, VenueCatalogue]:
    """The provider and the catalogue, both from one gateway — Stage A's whole shape."""
    provider = VenueMarketData(
        AlpacaGateway(_BarsTransport(), _AssetsTransport(), clock),
        clock,
        asset_class=AssetClass.EQUITY,
    )
    return provider, provider.catalogue


async def test_an_equity_only_basket_runs_a_cycle_end_to_end() -> None:
    clock = ManualClock(NOW)
    provider, catalogue = alpaca_feed(clock)
    app = await build_sim(
        clock=clock,
        market_data=provider,
        catalogue=catalogue,
        baskets=(equity_basket(),),
        global_policy=EQUITY_POLICY,
        start_equity=Decimal(50_000),
    )
    try:
        # The account is USD because the instruments are, resolved by `_quote_currency` rather
        # than assumed — gate 1 of Piece 2 passing rather than firing.
        assert app.quote_currency == "USD"

        await app.recover()
        results = await app.supervisor.run_once()

        assert len(results) == 1
        result = results[0]
        assert result.basket_id == "equities"
        # Any recorded outcome is a pass: the claim is that the loop *completes* over an equity
        # instrument, not that the stub panel chose to trade.
        assert isinstance(result.outcome, CycleOutcome)
        assert result.outcome is not CycleOutcome.FAILED
        assert result.outcome is not CycleOutcome.BLOCKED

        # The portfolio can be valued, which is what a frozen aggregate would have denied.
        assert app.valuation().frozen is False
        assert app.marks.price_of(
            "alpaca:AAPL", now=clock.now(), tolerance=EQUITY_POLICY.mark_tolerance
        ) == Decimal("178.21")

        with app.startup._store._engine.connect() as connection:  # noqa: SLF001 — projection read
            recorded = connection.execute(select(cycles.c.outcome)).scalars().all()
        assert len(recorded) == 1
    finally:
        await app.shutdown()


async def test_the_quote_admits_it_is_delayed() -> None:
    """The feed is 15 minutes behind and says so. A quote claiming to be current is what would
    hide the delay from every staleness check downstream (PHASE_12 Stage A §3.5)."""
    clock = ManualClock(NOW)
    provider, _ = alpaca_feed(clock)
    quote = await provider.get_quote(APPLE)
    assert quote.observed_at < NOW
    assert provider.capabilities().delay == timedelta(minutes=15)


async def test_a_basket_cycling_faster_than_the_feed_publishes_is_refused() -> None:
    """15-minute-old data cannot back a 5-minute cycle. Refused once at the boundary rather than
    producing `DATA_STALE` forever (DESIGN §6.2)."""
    clock = ManualClock(NOW)
    provider, catalogue = alpaca_feed(clock)
    app = await build_sim(
        clock=clock,
        market_data=provider,
        catalogue=catalogue,
        baskets=(equity_basket(every_seconds=300),),
        global_policy=EQUITY_POLICY,
        start_equity=Decimal(50_000),
    )
    try:
        with pytest.raises(ConfigError, match="delay"):
            await app.supervisor.run_once()
    finally:
        await app.shutdown()


async def test_a_mark_tolerance_below_the_feed_delay_is_refused_at_wiring() -> None:
    """The default 300s tolerance would make every equity mark stale on arrival, freezing the
    portfolio permanently for a healthy account (PHASE_12 Stage A, decision 3)."""
    clock = ManualClock(NOW)
    provider, catalogue = alpaca_feed(clock)
    with pytest.raises(ConfigError, match="mark_staleness_seconds"):
        await build_sim(
            clock=clock,
            market_data=provider,
            catalogue=catalogue,
            baskets=(equity_basket(),),
            global_policy=GlobalRiskPolicy(),  # the 300s default
            start_equity=Decimal(50_000),
        )
```

**If `run_once` swallows the `ConfigError`** in the third test — `BasketWorker._cycle` catches every
exception and counts a failure rather than raising — assert instead that the cycle recorded no
result and that the worker's `failures` is 1, and that the failure detail names the delay. Check
`BasketWorker._cycle`'s behaviour first and write whichever assertion is true; do **not** weaken the
test to `pytest.raises(Exception)`.

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/scenario/test_equity_basket.py -v`
Expected: PASS (4 cases).

- [ ] **Step 3: Run the whole scenario suite**

Run: `.venv\Scripts\python.exe -m pytest -m scenario -q`
Expected: PASS — no existing scenario regresses.

- [ ] **Step 4: Commit**

```bash
git add tests/scenario/test_equity_basket.py
git commit -m "test(scenario): an equity-only basket completes a cycle"
```

---

### Task 11: the decision record and the conventions

**Files:**
- Create: `docs/adr/0028-us-equity-trading-rules-are-market-structure.md`
- Modify: `CLAUDE.md`, `DESIGN.md`, `docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md`

- [ ] **Step 1: Write ADR 0028**

Follow the shape of `docs/adr/0025-instrument-trading-rules-are-venue-reference-data.md` (Context / Decision / Consequences). It must record:

- **Context:** ADR 0025 requires trading rules to come from the catalogue and nowhere else. Alpaca — and every US equity venue — publishes no per-symbol tick, lot, minimum quantity or minimum notional; `min_order_size`, `min_trade_increment` and `price_increment` are crypto-only fields.
- **Decision:** for US equities the rules are **market structure**, derived once in `marketdata/us_equities.py`, below the catalogue seam, cited to their source. Three are facts about whole-share trading; `tick_size` is SEC Rule 612. The universe is narrowed so the static set is never wrong in a dangerous direction: `us_equity` ∧ `active` ∧ `tradable` ∧ a major exchange, whole shares only, no fractional trading.
- **ADR 0025 is amended, not overturned:** rules stay venue-layer and are never operator input; no GUI field may accept one. What changes is only that where a venue publishes nothing, the source is a cited regulation rather than a fetched filter.
- **Review date: November 2026**, when the SEC's amended tick regime is expected to take effect. The constant must be re-checked before any equity trades live.
- **Consequences:** no fractional shares in v1; sub-$1 names quantize coarsely (legal — Rule 612 is a floor, not a grid); OTC names are not listed at all; a second equity venue consumes the same module, so two venues cannot disagree about the regulation.
- **The three consequences of a delayed feed**, each recorded with its reasoning: no live spread
  (`bid == ask == last` from the newest bar, so real-time SIP is a precondition for live equity
  trading); an equity basket cannot cycle faster than 15 minutes; and `mark_staleness_seconds` must
  exceed the delay or the portfolio freezes permanently — both of the latter refused at wiring.
- **One deferral:** half-day session mis-tagging, which needs the venue calendar inside the parser.

- [ ] **Step 2: Add the Stage A rules to CLAUDE.md**

Add a `### Phase 12 Stage A — equity market data` subsection under the existing Phase 12 section, carrying the rules from spec §4 in the house style (one bolded claim per bullet, the reasoning after it):

- a derived trading rule is venue-layer, never operator input — ADR 0025 amended, not broken
- `tick_size = 0.01` is a floor, not a grid; reversing it rejects every listed order
- `adjustment=all` and `feed=sip` are asserted on the wire, never left to a default
- never let a float exist — `loads_exact`, because at Alpaca there is no string field to read
- a stale feed is honest; an unrepresentative one is not
- **the mark tolerance must outlive the feed's delay**, or every mark is stale on arrival and a
  healthy portfolio freezes permanently — refused at wiring, beside the sweep-cadence check
- a venue that publishes no rule is not a venue with permissive rules — narrow the universe instead
- US equity market structure lives in one module, so a venue swap costs one gateway

- [ ] **Step 3: Update DESIGN.md §6.2**

Add one sentence to the market-data section stating that the equity feed declares a 15-minute
publication delay, and that a basket configured to cycle faster is refused at wiring rather than
producing `DATA_STALE` at runtime. No new §8.1 row is needed — the existing `DATA_STALE` row covers
the runtime case, and this one is a wiring refusal.

- [ ] **Step 4: Mark Stage A shipped in the Piece 2 doc**

In `docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md`, update the gate table so gate 4 reads as
removed, and mark Slice 4 / Stage A as shipped with a pointer to
`PHASE_12_STAGE_A_EQUITY_MARKET_DATA.md`. Leave gates 1, 2 and 3 exactly as they are.

- [ ] **Step 5: Full check**

Run: `.\check.ps1`
Expected: clean — format, lint, mypy, tests, and both coverage gates.

- [ ] **Step 6: Commit**

```bash
git add docs/ CLAUDE.md DESIGN.md
git commit -m "docs: ADR 0028 and the Stage A conventions"
```

---

## Post-Stage-A: what Stage B inherits

Not tasks — the two questions Stage A deliberately did not answer, recorded so they are not
rediscovered:

1. **The daily-loss day boundary.** `Watchdog` and `AlertDispatcher` each hold one
   `TradingCalendar`. DESIGN §6.6 says the boundary is "UTC for crypto and the exchange session for
   equities", but daily loss is one limit on one portfolio equity figure, so with two venues that
   specification contradicts itself. Stage B must settle it, probably with an ADR.
2. **Tier-2's per-venue split.** Every rule in `tier2.py` is pure over `RiskProposal`, and Piece 1
   already feeds it exposures computed over the whole universe — so pointing `aggregate` at N
   ledgers makes the cross-venue ceilings correct with no rule changes. The per-venue instance
   DESIGN describes is an *additional* constraint, never a replacement.
