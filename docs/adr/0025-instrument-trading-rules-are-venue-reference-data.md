# ADR 0025 — Instrument trading rules are venue reference data

*Status: accepted. Supersedes nothing; extends [ADR 0020](0020-live-is-the-paper-wiring-minus-headroom.md)
and [ADR 0013](0013-configuration-is-versioned-and-pinned-per-cycle.md).*

## Context

`lot_size`, `tick_size`, `min_qty` and `min_notional` are not preferences. They are the venue's
own published constraints, and three money paths read them:

- `quantize_order` ([risk/tier1.py](../../tradebot/risk/tier1.py)) rounds every quantity and price
  against them, so a wrong `lot_size` produces an order the venue rejects;
- the Tier-2 minimum check ([risk/tier2.py](../../tradebot/risk/tier2.py)) turns a shrink below
  `min_notional` into a veto, so a wrong minimum decides whether an order *exists*;
- `BinanceSpotBroker(instruments=…)` quantizes against the same numbers on the way out.

[interfaces/exchange.py](../../tradebot/interfaces/exchange.py) has said since Phase 3 that these
are *"fetched, never hand-configured … a stale `min_notional` lets through an order the risk layer
sized against the wrong floor."* The CLI honoured that — `backtest fetch` resolved symbols against
`exchangeInfo`. The dashboard did not: the basket editor rendered four free-text boxes, and
`demo_basket()` wrote the numbers out as literals.

It was already wrong. The seeded demo basket pinned `min_notional = 10` for `BTC/USDT`; Binance
publishes **5**. Every fresh database disagreed with the venue about which orders exist, and
nothing in the system could notice.

## Decision

**An instrument's trading rules come from a venue catalogue, in every mode, and are re-verified
whenever the document changes or the system starts.**

### 1. One protocol, every venue, including the simulated one

`InstrumentCatalogue` answers two questions — what a venue lists, and one symbol's rules — and
`Application.catalogue` is **not** optional. `SimCatalogue` serves a rule set *recorded from a real
venue* and committed to the repository; `VenueCatalogue` fetches `exchangeInfo` through the same
gateway market data uses; `UnavailableCatalogue` refuses by naming the limitation.

Sim simulates a venue. It is not a mode with a second data path. A flow that fetched from Binance
but fell back to hand-typing under sim would mean the thing a soak validated is not the thing that
trades — the failure ADR 0020 exists to prevent — so
[tests/contract/test_catalogue_contract.py](../../tests/contract/test_catalogue_contract.py) runs
one suite over every implementation.

### 2. The catalogue answers for the venue whose *prices* are read

Not the venue taking the orders. DESIGN §9's primary paper shape is `SimBroker` fed by live Binance
data: its orders reach no venue, but its lot sizes must be Binance's or the fills it simulates are
not the fills the live system would get.

### 3. Verification at publish, in every mode

`control/reference.store_basket` is the only path that writes a basket. It re-resolves the
instruments the edit **changed** and refuses any document the venue disagrees with. Once that
exists it no longer matters whether a field was typed, pasted, or edited in devtools.

*Changed only* is the exemption that keeps fail-closed from meaning fail-useless: an instrument
identical to the one in the current version keeps its pinned rules without a venue call, so a venue
outage cannot stop an operator tightening a stop loss, and a pause or a quarantine toggle spends
nothing. A venue that cannot be reached while an instrument *did* change is a refusal naming the
venue — a basket whose rules cannot be checked is not a basket that gets stored.

### 4. Drift after publish scales with whether the cycles are evidence

Rules change under a running system. Every start re-verifies every configured instrument, and so
does the supervisor's resync sweep, so a mid-soak filter change is caught in minutes.

| Mode | Mechanism | On drift |
|---|---|---|
| Live | identical | `RISK_EVENT` + halt the affected basket + alert |
| Paper | identical | `RISK_EVENT` + halt the affected basket + alert |
| Sim | identical | one `RISK_EVENT`, keep cycling |

The reason paper is strict and sim is not has nothing to do with the word "sim". Per DESIGN §9 the
soak's primary venue *is* `SimBroker`, and those cycles stamp `venue: sim` and are the evidence
base `report promotion` reads — a wrong `lot_size` there makes the report describe a system that is
not the one which will trade. In `Mode.SIM` the same class is doing rehearsal against a committed
capture that cannot change without a human editing a file, so the check has nothing to catch.

A basket, never the process: the rest of the system is sound and the other baskets keep their
evidence coming. The halt is cleared by re-publishing the basket, which re-resolves. An unreachable
venue is **not** drift and halts nothing — turning one bad minute into an incident a human must
clear is an outage amplifier.

### 5. ISIN is designed for and deliberately unserved

Neither Binance nor Alpaca's free API publishes an ISIN→symbol mapping. `resolve` takes an
`IdType`, validates an ISIN's check digit locally so a typo is caught first, then refuses with the
venue's actual limitation. Faking a mapping would invent the identity of a tradable thing.

## Consequences

- The demo basket's `min_notional` is now 5, because that is what Binance publishes. Existing
  databases keep their stored basket and the drift check says so on the next start.
- `tradebot catalogue fetch` re-records the committed capture, so refreshing it is a reviewable
  diff rather than a hand edit of the one file whose numbers decide which orders exist. The capture
  carries its `as_of` and will age; refreshing it belongs in the soak's periodic maintenance.
- The simulated venue lists thirty pairs, not Binance's several thousand. A symbol outside that set
  is refused exactly as a venue refuses one it does not list.
- Alpaca has no gateway, so an equity basket's rules cannot be verified and it will not publish.
  That is the honest state: accepting hand-typed rules for an equity is the defect this removes.
- Two callers were removed by the same change: `VenueMarketData.instruments` now delegates to a
  catalogue instead of holding a second opinion about what a venue lists, and `--broker binance`
  no longer builds a second Binance transport with its own rate budget, which
  [ADR 0010](0010-one-signed-transport-per-venue.md) forbids.
- Slice C of [PHASE_11](../PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md) turns the four free-text
  boxes into a **Look up** button and resolved read-only fields. That is convenience; this ADR is
  the guarantee, and it holds without it.
