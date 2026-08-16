# Phase 12 Piece 2, Stage A — equity market data

> Authoritative specs remain [DESIGN.md](../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
> This is the design pass for the first stage of
> [PHASE_12 Piece 2](PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md#piece-2--crypto-and-equity-in-one-basket-and-one-portfolio),
> written before any code changes, per the standing rule that a change touching the money path gets
> a design pass first. Conventions that outlive it move to [CLAUDE.md](../CLAUDE.md); decisions move
> to `docs/adr/`.

**Status: proposed. Nothing here is built.**

Stage A removes **gate 4** of Piece 2 — "equities are unwired" — and nothing else. Gates 1
(one quote currency), 2 (one venue) and 3 (venue/catalogue match) stand unchanged and still fail
closed. What Stage A delivers is an **equity-only basket that completes a full cycle**, which is a
far smaller step than a mixed one and proves the adapter before anything depends on it.

---

## Table of Contents

- [1. The idea](#1-the-idea)
- [2. What the audit found](#2-what-the-audit-found)
- [3. The solution](#3-the-solution)
- [4. Rules that are easy to get backwards](#4-rules-that-are-easy-to-get-backwards)
- [5. Tests](#5-tests-must-have-not-nice-to-have)
- [6. Definition of Done](#6-definition-of-done)
- [7. What Stage A deliberately does not do](#7-what-stage-a-deliberately-does-not-do)
- [8. Risks](#8-risks)
- [9. Decisions taken by the operator](#9-decisions-taken-by-the-operator)
- [Appendix — audit evidence](#appendix--audit-evidence)

---

## 1. The idea

An equity instrument can be configured, verified against its venue, priced, and traded, using the
**same** provider, catalogue, runner and risk code a crypto instrument uses. Nothing above the
venue layer learns that equities exist.

The unit of work is therefore **one `VenueGateway`**, not a parallel market-data stack.
[`VenueMarketData`](../tradebot/marketdata/venue.py) is already venue-agnostic over a gateway and
already constructs its own [`VenueCatalogue`](../tradebot/marketdata/catalogue.py), so a gateway
yields both of the things Piece 2 asked for — the provider *and* the catalogue — through code that
is already written, already tested, and shared bar-for-bar with Binance.

A second aim, equal in weight: **the venue must be replaceable.** Alpaca is one plausible equity
venue among several, and the design is judged by what swapping it costs. Everything that is a fact
about the *US equity market* rather than about *Alpaca* is therefore extracted, shared, and cited
once — see [§3.2](#32-us-equity-market-structure-is-not-alpacas).

## 2. What the audit found

Verified against Alpaca's live documentation on 2026-08-17, not from recollection. Sources are in
the [appendix](#appendix--audit-evidence). Four findings, and each changes the design.

### Finding 1 (decisive) — Alpaca publishes no trading rules for US equities

[`Instrument`](../tradebot/core/instrument.py) requires `lot_size`, `tick_size`, `min_qty` and
`min_notional`, and [ADR 0025](adr/0025-instrument-trading-rules-are-venue-reference-data.md) says
they come from an `InstrumentCatalogue` **and nowhere else**.

Alpaca's `GET /v2/assets` returns `id`, `symbol`, `name`, `class`, `exchange`, `status`,
`tradable`, `marginable`, `shortable`, `fractionable`, `borrow_status`, the two margin
requirements, `attributes` and `cusip`. The three fields that would map — `min_order_size`,
`min_trade_increment`, `price_increment` — are documented **"available for crypto only."**

So for a US equity there is nothing to fetch. The real rules are market structure:

| Field | Truth for a whole-share US equity order | Kind |
|---|---|---|
| `lot_size` | `1` | fact |
| `min_qty` | `1` | fact |
| `min_notional` | `0` — Alpaca imposes no notional floor on whole-share orders; the $1 floor is a *fractional/notional* order rule | fact |
| `tick_size` | `0.01` at price ≥ $1.00 (SEC Rule 612 sub-penny rule) | **regulation** |

Three of the four are facts about whole-share trading. Exactly one is asserted rather than
observed, it is a *regulation* rather than a guess, and it is wrong only in the safe direction:
Rule 612 sets a **floor**, not a grid, so quoting a sub-$1 stock at penny increments is legal —
merely coarse.

**It carries an expiry.** The SEC's amended tick-size regime is expected to take effect around
November 2026, roughly three months from this writing. A hardcoded tick without a review date is
exactly the drift ADR 0025 exists to prevent, so the date is recorded in the code and in the ADR.

### Finding 2 (high) — the default bar adjustment is `raw`

`GET /v2/stocks/bars` takes `adjustment` ∈ `{raw, split, dividend, all}` and **defaults to `raw`**.

Unadjusted bars put a 4:1 split into the tape as a 75% single-bar crash. Nothing downstream would
catch it: `require_fresh` checks staleness, not plausibility. ATR would read fabricated volatility,
sizing would divide by a stop distance no market ever offered, and Tier-2's ±5% price collar would
veto every order the panel proposed — a basket that appears to be working and decides nothing, for
reasons no event explains.

### Finding 3 (high) — Alpaca publishes prices as JSON *numbers*

Binance publishes `"0.01634790"` — a string, precisely so it survives. Alpaca publishes `178.21`.

```json
{"bars":{"AAPL":[{"c":178.21,"h":178.26,"l":178.21,"n":65,"o":178.26,
                  "t":"2022-01-03T09:00:00Z","v":1118,"vw":178.235733}]}}
```

`httpx`'s `response.json()` calls `json.loads`, which renders a JSON number as a Python `float`.
[`schema.parse_money`](../tradebot/core/schema.py) raises `MoneyError` on a float *by design* —
"only our own code can put one there" — so the existing
[`AlpacaTransport._decode`](../tradebot/venues/alpaca_transport.py#L220) would hand floats straight
at the money layer.

This also shows that [ADR 0001](adr/0001-decimal-only-money-arithmetic.md)'s operative wording —
"read the venue's *string* fields" — is a Binance-shaped statement of a more general rule. At
Alpaca **there is no string field.** The durable rule is *never let a float exist*, and
`json.loads(text, parse_float=Decimal)` is what makes it true regardless of how a venue publishes.

Alpaca's *trading* API does return strings (`"qty": "10"`), which is why
[`execution/brokers/alpaca.py`](../tradebot/execution/brokers/alpaca.py) is correct today. Only the
data API is affected — but the decoder is shared by both, because a rule applied to one of two
transports is a rule the next endpoint forgets.

### Finding 4 (medium) — the free data feed is IEX, ~2.5% of volume

Alpaca's Basic (free) plan serves **IEX only** for equities. SIP — the consolidated tape from CTA
and UTP — is available on the free plan but **only for data at least 15 minutes old**; real-time
SIP requires the $99/month Algo Trader Plus subscription.

IEX is the trap. It *looks* real-time and is unrepresentative: an IEX quote may sit meaningfully
off the NBBO, and that price feeds [`marketable_price`](../tradebot/control/basket_runner.py#L75),
Tier-2's price collar, and every ATR that sizes a position. It is the failure ADR 0027 was written
about — a number that is "differently wrong, in whichever direction the market moved" — arriving
through the data door instead of the valuation one.

Delayed SIP is *correctly* stale, and this system already models staleness as a first-class fact:
[`DataCapabilities.delay`](../tradebot/interfaces/market_data.py#L26) exists and
[`ContextBuilder._assert_feed_keeps_up`](../tradebot/control/context_builder.py#L134) already
refuses a feed slower than the basket's cadence, by name, at wiring — rather than letting it
surface as an endless fog of `DATA_STALE` cycles.

## 3. The solution

### 3.1 One gateway; the provider and the catalogue already exist

```
AlpacaGateway (VenueGateway)                       ← the only new venue-aware class
  ├─ AlpacaDataTransport  → data.alpaca.markets      bars, quotes
  └─ AlpacaTransport      → api.alpaca.markets       /v2/assets, /v2/clock
        both share ONE VenueRateLimiter with the broker's transport (ADR 0010)

VenueMarketData(gateway, clock, asset_class=EQUITY)      ← existing, untouched
   └─ .catalogue = VenueCatalogue(gateway, clock, EQUITY) ← existing, untouched
```

New module `tradebot/marketdata/alpaca.py`, mirroring
[`marketdata/binance.py`](../tradebot/marketdata/binance.py): pure parse functions that do no I/O,
then the gateway. [`_alpaca_stack`](../tradebot/app.py#L574) stops raising and builds it.

The gateway's own job is only the genuinely venue-specific half — **which symbols exist, are they
tradable, and what does this venue's wire format look like.** Everything else is shared.

### 3.2 US equity market structure is not Alpaca's

The four rules of [Finding 1](#finding-1-decisive--alpaca-publishes-no-trading-rules-for-us-equities)
are facts about the **US equity market**, not about Alpaca. Placing them in `marketdata/alpaca.py`
would mean a future `TradierGateway` or `IBKRGateway` re-derives the same regulation independently
— and two venues silently disagreeing about Rule 612 is exactly the class of defect ADR 0025
exists to prevent, merely relocated one layer down.

New shared module `tradebot/marketdata/us_equities.py`:

```
MIN_TICK             Decimal("0.01")   SEC Rule 612 · review: Nov 2026 regime change
WHOLE_SHARE_LOT      Decimal(1)
MAJOR_EXCHANGES      frozenset{NYSE, NASDAQ, ARCA, AMEX, BATS}
whole_share_market(symbol, *, tradable) -> VenueMarket
```

One regulation, one citation, **one review date**. Every US equity gateway consumes it, so a venue
swap cannot fork or lose it.

### 3.3 Exact decoding is a money primitive, not an Alpaca workaround

`core/money.py` gains `loads_exact(text) -> Any` — `json.loads` with `parse_float=Decimal`, cited
to ADR 0001. Both Alpaca transports decode through it. Any future venue that publishes JSON numbers
inherits the money guarantee by using the shared decoder rather than by remembering a trick.

### 3.4 Two hosts, and mode safety across both

[`assert_host`](../tradebot/venues/alpaca_transport.py#L250) pins exactly one host per mode; the
data host is `data.alpaca.markets` in **every** mode. It becomes role-keyed —
`ALPACA_HOSTS[role][mode]` — so the data host is *asserted* rather than defaulted past.

The guarantee that matters is untouched: the **trading** host still differs per mode, and
credentials still come from mode-specific environment variables, so a paper process holds only
paper keys and can reach only the paper exchange.

One divergence is recorded rather than glossed. [`VenueTransport`](../tradebot/interfaces/exchange.py#L38)
documents that it "asserts it holds no credentials, because a data client that could sign an order
is a data client that might". Alpaca's data API *requires* a key, so that separation is unavailable
here. `AlpacaDataTransport` preserves it **structurally** instead: no `call` method, no `is_order`
path, and a base URL that cannot reach `/v2/orders`. Classification and decoding live in shared
module functions rather than being duplicated across the two transports.

### 3.5 The feed is declared, and its delay is declared with it

`feed="sip"` with `DataCapabilities.delay = timedelta(minutes=15)`. The delay is not a caveat in a
comment: it is the value `_assert_feed_keeps_up` reads, so an equity basket configured to cycle
faster than its feed publishes is **refused at wiring, naming both numbers**.

`feed` is a constructor parameter, so moving to real-time SIP after a subscription is a wiring
change rather than a rewrite, and `delay` moves with it.

**A delayed feed has three consequences, and two of them are refusals at wiring.** Found while
grounding the implementation plan, and each is the same hazard wearing a different hat — a system
that would appear to work while deciding nothing:

- **The mark staleness tolerance must exceed the delay.** A quote from this feed carries an
  `observed_at` at least fifteen minutes in the past, truthfully. But
  `GlobalRiskPolicy.mark_staleness_seconds` defaults to **300**, so `Marks.price_of` would return
  `None` for every equity mark, `aggregate` would freeze on every evaluation, and every cycle would
  record `BLOCKED` — for a portfolio that is entirely healthy, with nothing in the log naming the
  cause. `PortfolioWatch` already refuses a tolerance below `3 ×` the sweep cadence for exactly this
  reason; it gains the sibling clause against the provider's declared delay. An equity basket
  therefore needs `mark_staleness_seconds ≥ 1200`.
- **A basket cannot cycle faster than its feed publishes**, which `_assert_feed_keeps_up` already
  enforces — this is that existing check finally having a venue that exercises it.
- **There is no live spread.** `fetch_top_of_book` derives from the newest closed bar, so
  `bid == ask == last`. Alpaca's latest-quote and latest-trade endpoints are real-time by
  definition and therefore forbidden on this plan; the alternative — a real-time IEX quote beside
  delayed SIP bars — would put two different views of the market into one decision. **Real-time
  SIP is therefore a precondition for live equity trading**, discovered here rather than in
  Stage D.

### 3.6 Sessions are tagged

Equity bars carry `MarketSession.REGULAR` or `EXTENDED` rather than `CONTINUOUS`, so
[`is_indicator_input`](../tradebot/core/enums.py#L51) can keep thin extended-hours prints out of
ATR — machinery that has existed since Phase 3 and has never had a venue to serve.

### 3.7 What a venue swap costs, enumerated

| Replacing Alpaca with venue X | Work |
|---|---|
| Bars, quotes, asset list | new `XGateway(VenueGateway)` + its transport |
| Trading rules | **nothing** — shared `us_equities` builder |
| Exact decimals | **nothing** — shared `loads_exact` |
| Market-data provider | **nothing** — `VenueMarketData` wraps any gateway |
| Catalogue | **nothing** — `VenueCatalogue` wraps any gateway |
| Wiring | one `_STACKS` entry, one `BrokerChoice` member |
| Calendar, corporate actions | new implementations of the existing `TradingCalendar` / `CorporateActionSource` protocols |
| Contract coverage | one new parameter in each of the two suites |

**Broker and data need not be the same venue.** `VenueStack` already separates `broker` from
`prices`/`catalogue`, and `_alpaca_stack` already honours a `request.feed.catalogue` override, so
"Alpaca broker, Polygon data" is expressible at the composition root. `VenueGateway`'s four methods
are deliberately **not** split to chase that: the composition root is already the seam, and
widening the protocol would touch Binance and sim for a case nobody has yet.

## 4. Rules that are easy to get backwards

- **A derived trading rule is venue-layer, never operator input.** ADR 0025 is amended, not broken.
  The rules stay below the catalogue seam, are cited to their source, and no GUI field may ever
  accept one. What changes is only that for US equities the source is a *regulation* rather than a
  venue's published filter — because the venue publishes none.
- **`tick_size = 0.01` is a floor, not a grid.** Rule 612 forbids quoting NMS stocks priced ≥ $1.00
  in increments finer than a penny; below $1.00 it permits sub-penny. Quoting a sub-$1 stock at
  penny increments is therefore legal and merely coarse. Reversing this — assuming 0.0001 is
  "safer" because it is finer — produces orders the venue rejects on every listed name.
- **A default we rely on silently is one release away from changing.** `adjustment=all` is passed
  explicitly and asserted on the wire in a test. The same reasoning applies to `feed`.
- **Never let a float exist.** Reading "the venue's string field" is the Binance-shaped form of the
  rule; here there is no string field, and the general form is the decoder. A `float` that reaches
  `parse_money` is a `MoneyError` by design, so this fails loudly — but it fails at the first live
  quote, and the point is to fail in CI instead.
- **A stale feed is honest; an unrepresentative one is not.** 15-minute-old consolidated prices are
  a declared limitation the system already enforces against. Real-time IEX quotes carry no marker
  at all and would be trusted exactly as Binance's are.
- **A tolerance shorter than the delay is a permanent freeze, not a tight limit.** Every mark is
  stale the moment it arrives, so a healthy portfolio blocks every cycle and no event says why.
  This is the sweep-cadence hazard arriving through the other door, and it is refused in the same
  place, for the same reason: at wiring, not at 03:00.
- **A venue that publishes no rule is not a venue with permissive rules.** An asset outside
  `MAJOR_EXCHANGES`, inactive, non-tradable, or not `us_equity` is simply **not listed** by the
  catalogue, so `resolve` refuses it in the venue's own terms through the existing `ConfigError`
  path. Listing it with invented rules would be ADR 0025's original defect.

## 5. Tests (must-have, not nice-to-have)

Wire parsing is what has to be exhaustive, and it is testable offline with plain dictionaries and
`httpx.MockTransport` — the same shape as `test_binance_gateway.py` and the existing Alpaca broker
contract, so the whole suite stays free and repeatable.

**`tests/unit/test_alpaca_gateway.py`** (new):

- a bar's OHLC parses to **exact** `Decimal` from a JSON *number* literal — `178.21` becomes
  `Decimal("178.21")`, never a float, and never `Decimal(178.21)`
- a `float` anywhere in a decoded payload fails loudly rather than reaching the money layer
- `adjustment=all` is on the wire of every bars request, asserted against the request itself
- `feed=sip` is on the wire, and `capabilities().delay` is 15 minutes
- only closed bars are returned; a forming bar is dropped (the `VenueGateway` contract)
- bars are tagged `REGULAR` / `EXTENDED`, never `CONTINUOUS`
- every timeframe in our vocabulary maps to an Alpaca timeframe, and an unsupported one raises
  `ConfigError` rather than defaulting
- an empty or zero book fails closed as `DataStaleError`, as Binance's does
- `fetch_markets` lists only `us_equity` ∧ `active` ∧ `tradable` ∧ a major exchange
- a listed asset resolves to `lot_size=1`, `min_qty=1`, `min_notional=0`, `tick_size=0.01`
- a non-tradable asset is published with `tradable=False`, so the catalogue refuses it as
  *delisted* rather than as *unknown*

**Both contract suites gain an `AlpacaGateway` parameter.** They are currently parameterized over
*wrappers* driven by a `FakeGateway`, so Alpaca would not join them for free. Adding it as a
parameter is a small change to each and makes "every gateway behaves identically" a checked claim
rather than one proven only against a fake:

- `tests/contract/test_market_data_contract.py` — point-in-time cutoff, `observed_at` stamping,
  limit slicing, oldest-first ordering, and failing closed on no data
- `tests/contract/test_catalogue_contract.py` — listing, resolution, case/whitespace insensitivity,
  the delisted-vs-unknown distinction, provenance, and the ISIN refusal

**`tests/unit/test_venue_boundary.py`** (new, structural): the set of modules importing
`tradebot.marketdata.alpaca` is **exactly** `{tradebot.app}`, and likewise for
`tradebot.venues.alpaca_transport`. In the manner of `test_valuation_boundary.py` and
`test_dashboard_chart.py` — it makes minimal blast radius a checked claim, and it would catch the
first convenience import that erodes it, which is how `BinanceSpotGateway` came to be imported by
[`marketdata/factory.py`](../tradebot/marketdata/factory.py#L28). The gateway takes its transport
by injection and imports only the `VenueTransport` *protocol*, so the boundary holds by
construction rather than by discipline.

**`tests/unit/test_us_equities.py`** (new): `lot_size`, `min_qty` and `tick_size` are strictly
positive `Decimal`s and `min_notional` is exactly `ZERO` — **not** "every rule is positive", which
is the sim rule set's invariant and is deliberately false here: whole-share equity orders have no
notional floor, and asserting one would reintroduce the invented number this design removed.
`whole_share_market` refuses a symbol it cannot build a legal market for, and the Rule 612 review
date is present so the constant cannot outlive its citation unnoticed.

**Scenario**: an equity-only basket completes a full cycle end to end against a mocked Alpaca — the
Stage A exit criterion, in `tests/scenario/`.

## 6. Definition of Done

In addition to the standing per-module DoD in
[PLAN §6](../IMPLEMENTATION_PLAN.md#6-definition-of-done-applies-to-every-module).

1. **An equity-only basket completes a full cycle**, asserted in `tests/scenario/`.
2. **`_alpaca_stack` no longer raises**, and gate 4 is gone. Gates 1, 2 and 3 are untouched and
   still refuse.
3. **No US equity trading rule is written down twice.** `grep` finds `MIN_TICK` and
   `WHOLE_SHARE_LOT` defined in exactly one module, with the Rule 612 citation and the November
   2026 review date beside them.
4. **No float exists on the Alpaca data path**, proven by test, and `loads_exact` is shared rather
   than reimplemented.
5. **`adjustment=all`, `feed=sip` and `sort=desc` are asserted on the wire**, not assumed from a
   default. `sort` belongs here: with `sort=asc` and no `start`, Alpaca returns the *oldest* bars of
   available history, so every series would open in 2016 and abort as `DATA_STALE`.
6. **The provider contract suite runs against `AlpacaGateway`**, and the catalogue suite carries an
   equity class proving an equity venue's answers travel through the shared resolution semantics
   intact. The shared catalogue cases stay Binance-shaped deliberately: `VenueCatalogue` is the
   same class for both, so parameterizing them would re-test shared code with different data.
7. **A mark tolerance below the feed's declared delay is refused at wiring**, beside the existing
   sweep-cadence refusal, and the message names both numbers and the remedy.
8. **The venue boundary holds**: only `tradebot.app` imports the Alpaca modules, enforced by test.
9. **An ADR records the amendment to ADR 0025** — derived market-structure rules where a venue
   publishes none — with its review date.
10. **CLAUDE.md gains the Stage A rules** from [§4](#4-rules-that-are-easy-to-get-backwards).
11. **DESIGN §6.2 states the equity feed's declared delay**, and §8.1 keeps its existing
    `DATA_STALE` row (no new row is needed — a delayed feed is refused at wiring, not at runtime).
12. `.\check.ps1` clean; coverage gates hold.

## 7. What Stage A deliberately does not do

No venue routing. No N ledgers. No mixed baskets. No fractional shares. No per-instrument
calendars. No changes to `_quote_currency`, to the single `VenueStack`, or to gate 3.

An **equity-only** Alpaca process is the whole deliverable, and it works because gates 1 and 3 only
fire on *mixing*: with every instrument `alpaca:AAPL` quoted in USD, `_quote_currency` returns
`USD`, and `catalogue.venue_id == instrument.venue`. Stages B–D remain as
[PHASE_12 §2.3](PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md#23-the-solution) describes them.

Two things Stage A will inform, and should not pre-empt:

- **The daily-loss day boundary.** [`Watchdog`](../tradebot/risk/watchdog.py#L317) and
  `AlertDispatcher` each hold **one** `TradingCalendar`. DESIGN §6.6 says the boundary is "UTC for
  crypto and the exchange session for equities" — but daily loss is one limit on one portfolio
  equity figure, so with two venues that specification is self-contradictory. Stage A is
  single-venue and unaffected; **Stage B must settle it**, and it likely needs an ADR.
- **Tier-2's per-venue split.** Every rule in [`tier2.py`](../tradebot/risk/tier2.py) is pure over
  `RiskProposal`, and Piece 1 already feeds it exposures computed over the whole universe. Pointing
  `aggregate` at N ledgers makes the cross-venue ceilings correct with **no rule changes**. The
  per-venue instance DESIGN describes is an *additional* constraint, never a replacement —
  collapsing the two, or substituting one for the other, loosens limits in the direction that
  looks safe.

## 8. Risks

- **The November 2026 tick-size change lands inside this system's likely soak window.** A
  hardcoded `0.01` is correct today and has a known expiry roughly three months out. Mitigated by
  the single definition, the citation, and the review date — but it is a diary entry, not a
  guarantee, and the constant must be re-checked before any equity trades live.
- **Delayed SIP forbids fast equity baskets by construction.** An operator who configures a
  5-minute equity schedule gets a refusal at wiring. That is the intended behaviour, and it will
  read as a bug to whoever meets it first. The refusal message must name the delay, the cadence,
  and the subscription that would remove it.
- **The `VenueMarket` model fits equities only because the universe is narrowed.** Fractional
  shares, sub-$1 names quantized to a penny, and tick-pilot symbols are all handled by *exclusion*
  or by *coarseness*. If the operator later wants fractional equity trading, `Instrument.tick_size`
  being a static field becomes a real constraint and the protocol question deferred here returns.
- **Corporate actions become reachable for the first time.** `adjustment=all` fixes the *indicator*
  half; the ledger half — `AlpacaAnnouncements` and the reconciler's `CORPORATE_ACTION` branch —
  has still never run in a soak. Stage A makes it possible to rehearse against a known historical
  split, and that rehearsal should happen before Stage B, not after.
- **Alpaca paper credentials are needed to exercise this for real.** Every test here is offline, so
  CI is unaffected; but the Stage A exit criterion ("completes a cycle against Alpaca paper") needs
  a key in the environment, and the `-m smoke` suite is where that belongs.

## 9. Decisions taken by the operator

Recorded here because both were live choices with real trade-offs, taken 2026-08-17:

1. **Trading rules: conservative static, narrow universe.** Derive one safe rule set in the venue
   layer and refuse any asset for which it would be wrong. Fractional shares are out of v1. The
   alternative — deriving per-asset rules from `fractionable` and a price band — was rejected
   because `Instrument.tick_size` is static, so a stale tick after a price crosses $1.00 means
   rejected orders on every subsequent attempt.
2. **Feed: SIP delayed 15 minutes, free.** Correct prices, honestly stale, and the staleness is
   modelled rather than hidden. Real-time IEX was rejected as unrepresentative; real-time SIP
   ($99/month) remains one wiring change away.

---

## Appendix — audit evidence

Verified 2026-08-17 against Alpaca's published documentation.

- [Get Assets — `GET /v2/assets`](https://docs.alpaca.markets/reference/get-v2-assets-1) —
  the equity asset payload; `min_order_size`, `min_trade_increment` and `price_increment` are
  documented as crypto-only.
- [Historical Stock Bars](https://docs.alpaca.markets/us/reference/stockbars) — `adjustment`
  ∈ `{raw, split, dividend, all}`, **default `raw`**; OHLC published as unquoted JSON numbers.
- [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) — Basic plan
  is IEX-only for equities; Algo Trader Plus at $99/month for full SIP.
- [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) — SIP is queryable on the
  free plan when `end` is at least 15 minutes old; IEX is ~2.5% of US equity volume.
- [Fractional Trading](https://docs.alpaca.markets/us/docs/fractional-trading) — `qty` and
  `notional` accept up to 9 decimal places; the $1 minimum applies to fractional/notional orders.

Import boundary as it stands before Stage A, which the new structural test freezes:

```
from tradebot.execution.brokers.alpaca import ...   → tradebot/app.py only
from tradebot.venues.alpaca_transport import ...    → tradebot/app.py only

from tradebot.marketdata.binance import ...         → app.py, execution/brokers/binance.py,
                                                       marketdata/factory.py      ← already eroded
```
