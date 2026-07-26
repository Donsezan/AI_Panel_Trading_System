# Review Findings — DESIGN.md & IMPLEMENTATION_PLAN.md

> Multi-perspective review (architecture, lead dev, financial expert, financial analytics, QC),
> two verification iterations: (1) external fact-check of every load-bearing claim via web
> research, (2) internal consistency pass across both documents. Date: 2026-07-25.
>
> **Verdict:** the architecture is sound and unusually well-grounded; nothing found invalidates
> the design. But there are 7 must-fix findings (one is a genuine money-loss vector), 9
> high-value corrections, and a set of minor cleanups needed before the documents can be
> called final.
>
> **Resolution status (2026-07-26): ALL FINDINGS APPLIED** to DESIGN.md and
> IMPLEMENTATION_PLAN.md. Owner decisions incorporated: **A1** = venue-native protective
> orders where `BrokerCapabilities` supports them; unsupported venues flagged
> `unprotected_position` (sizing haircut + surfaced to the panel as a decision factor).
> **A5** = per-venue Tier-2 hard limits + cross-venue `PortfolioAggregate` summary enforced
> by the watchdog. C2 (Python 3.11) kept with corrected rationale; C10 sources kept but
> labeled informal.

---

## A. Must-fix before finalization (blockers)

### A1. Stop-loss/take-profit has no execution mechanism — **money-loss vector**
*(financial expert + architecture)*

Tier-1 (DESIGN §6.6) declares a 2×ATR stop-loss / 3×ATR take-profit policy, but nothing in the
design can enforce it:

- The system is cycle-based (minutes to hours between decisions).
- Entry orders are limit-with-TTL; ExecutionMonitor only watches **open** orders.
- No component watches the price of **held positions** between cycles. The Tier-2 watchdog
  monitors portfolio aggregates, not per-position stop levels.

In a fast market, a 2×ATR stop that is only checked at the next cycle is fiction — the realized
loss is unbounded up to the Tier-2 drawdown trip, which is a portfolio-level 10%, far beyond
any per-position stop. **Decide one of:**

1. **(Recommended)** Place venue-native protective orders at entry: Binance spot
   `STOP_LOSS_LIMIT`/OCO, Alpaca stop / bracket orders. Extend `BrokerCapabilities` to declare
   support; extend the order state machine to handle linked orders (entry + protective pair).
2. A dedicated price-watchdog task in Tier-2 that polls quotes for held positions and submits
   exits through the normal path (weaker: gap risk, adds polling load).
3. Drop SL/TP from v1 Tier-1 entirely and say so explicitly (cycle-cadence exits + Tier-2 only).

Anything except an explicit choice leaves the doc claiming a control that doesn't exist.

### A2. Position-sizing formula is dimensionally wrong
*(financial analytics)*

DESIGN §6.6: `qty = (basket_budget × size_hint_fraction × risk_per_trade) / (ATR × price)`.

Units: `$ / ($·$) = 1/$` — not asset units. Implemented literally, sizes are off by a factor of
~price (tiny for BTC, absurd for penny-priced assets). Correct volatility-normalized sizing
(with ATR in quote currency per unit, i.e. absolute ATR):

```
risk_dollars  = basket_budget × risk_per_trade × size_hint_fraction
stop_distance = k × ATR                # k = the stop multiple from A1, e.g. 2
qty           = risk_dollars / stop_distance
```

then clamp by max-position %, basket allocation, and exchange minimums. The `÷ price` form is
only correct if ATR is expressed as a *fraction of price* — the doc must pin ATR's units either
way. Note the formula is coupled to A1: without an enforced stop at `k×ATR`, "risk_dollars" is
not actually the amount at risk.

### A3. Conviction scale is inconsistent across the two docs
*(QC)*

- DESIGN §4 `Decision.conviction` — **0–1**
- DESIGN §7.1 seat schema — **1–5**
- DESIGN §6.6 Tier-1 "Min conviction to act" — **0.6** (0–1 scale)
- DESIGN §2.4 [L8] calls it "confidence"

Define once: seats emit 1–5; the consensus rule maps to 0–1 with an explicit formula (e.g.
`(mean(agreeing) − 1)/4 × agreement_fraction`); all thresholds stated in 0–1; one term
("conviction") everywhere.

### A4. Drawdown / daily-loss limits break on external cash flows
*(financial expert)*

DESIGN §6.8 correctly absorbs deposits/withdrawals as `EXTERNAL_CHANGE`, but Tier-2 (§6.6)
computes max-drawdown-from-HWM and max-daily-loss from raw equity. Consequences:

- A user withdrawal ≥10% of equity **trips the kill switch** as a phantom drawdown.
- A deposit silently masks real losses against both limits.

Fix: HWM and daily-loss baselines must be flow-adjusted using the reconciler's
`EXTERNAL_CHANGE` events (adjust HWM and day-start equity by net external flow). One sentence
in §6.6 + a chaos-test row in §8.1.

### A5. "Global" Tier-2 risk is ambiguous with two venues in v1
*(architecture)*

DESIGN §4: Portfolio is "one per venue account"; `GlobalRiskPolicy` is "singleton per
portfolio". With Binance **and** Alpaca both in v1 (plan's confirmed scope), that reads as two
independent "global" policies — so no limit is actually global, and the correlated-cluster rule
(e.g. {BTC, ETH, **crypto basket**} vs tech equities) can't see across venues. Also
cross-venue equity needs a valuation convention (USDT/USD parity assumption, and what happens
if it breaks). Decide and state: either Tier-2 aggregates across all venues in USD (define the
FX/stablecoin convention and its staleness rule), or Tier-2 is per-venue in v1 and the docs
stop calling it portfolio-global.

### A6. Short selling is never excluded — accidental-short vector on equities
*(financial expert)*

`action=SELL` with no position: spot crypto venues reject it, but **Alpaca will open a short**.
Nothing in either doc states the system is long-only or that SELL is reduce-only. An LLM SELL
vote on a flat equities instrument becomes a margin short with borrow costs and unlimited-loss
semantics that Tier-1/Tier-2 rules (written for long exposure) don't model. Fix: state v1 is
**long-only; SELL is reduce-only**, enforced as a deterministic Tier-1 check (`qty ≤ current
position`), with shorting listed in §12 future work.

### A7. Binance testnet resets ~monthly, unannounced — breaks the Phase 7 soak and its gates
*(QC + lead dev; verified)*

Binance Spot testnet is periodically reset to a blank state (~monthly, no prior notice): all
orders cleared, balances reset, trade history deleted (API keys survive). Consequences for the
plan as written:

- A reset mid-soak is a massive **unexplained recon mismatch → kill switch** by design.
- The promotion gate "every reconciliation clean" over a multi-week soak is unattainable.
- Testnet fills are unrealistically good (thin fake book), weakening the "paper results
  predictive of live" claim.

Fix: make **live market data + SimBroker** the primary paper-soak mode (the architecture
already supports exactly this wiring); use Binance testnet only as an integration check for
adapter correctness. Give the reconciler a documented testnet-reset classification (balances
reset to defaults + open orders gone ⇒ `VENUE_RESET`, halt + notify, not kill), and exclude
venue-reset events from promotion-gate accounting.

---

## B. High-value corrections

### B1. NewsAPI free tier is unusable as a trading signal *(verified)*
Free "Developer" plan: articles **delayed 24 hours**, 100 req/day, commercial use forbidden,
CORS localhost-only. Phase 3 lists NewsAPI without caveat. Either budget for a paid plan or
drop it; RSS-first policy stands on its own.

### B2. Corporate actions are missing from the equities story
A stock split doubles reported qty at the venue ⇒ reconciler classifies it "unexplained
mismatch" ⇒ halt/kill switch. Dividends create unexplained cash. Add corporate-action handling
to §6.8 reconciler classification (Alpaca exposes corporate-announcement data) and a row to the
risk register. At minimum: classify, halt only the affected instrument, require human ack.

### B3. PDT references are obsolete — regulatory change June 2026 *(verified)*
FINRA retired the Pattern Day Trader rule; Alpaca's Intraday Margin Framework replaced it on
**2026-06-04**, and the old API fields (`pattern_day_trader`, `daytrade_count`,
`daytrading_buying_power`, …) were removed from the API by **2026-07-06**. The docs don't
mention PDT (fine), but Phase 5 should note: don't port any prototype/reference logic touching
those fields, and review the new framework's behavior for the chosen account type.

### B4. SUBMIT_UNKNOWN terminal handling contradicts between the docs
DESIGN §8.1: not found after T ⇒ "mark failed, *no auto-resubmit in this cycle*" (implies
trading continues next cycle). PLAN §2.3: "mark failed and **halt the basket for human
review**". The plan's version is the correct fail-closed behavior — an order that vanished is
not a routine event. Align DESIGN §8.1 to the plan.

### B5. DESIGN §6.7 client_order_id format is invalid for Binance *(verified)*
Binance spot `newClientOrderId`: regex `^[\.A-Z\:/a-z0-9_-]{1,36}$` (36-char cap). The plan
§2.2 already fixes this with the hashed scheme — but a "finalized" DESIGN.md must not carry the
known-broken `{basket}-{cycle}-{instrument}-{seq}` format. Adopt the §2.2 scheme in DESIGN §6.7
(keep the human-readable tuple as the *hash input*, which it already is). Also state the Alpaca
`client_order_id` limit explicitly in Phase 5 (the ≤36-char scheme fits, but assert it in the
contract tests).

### B6. Consensus / abstain arithmetic is underspecified; HOLD vs WAIT never defined
- 3 seats: one abstain = exactly ⅓, which is **not** ">⅓" ⇒ panel proceeds with 2 seats. Is the
  qualified majority then 2-of-3-original or 2-of-2-remaining? Different behaviors; pick one
  (recommend: majority of *original* seats, so one abstain + one BUY ≠ trade).
- HOLD vs WAIT semantics are never defined in either doc (suggested: HOLD = affirmative
  keep-current-position, counts as a vote *against* acting; WAIT = no-signal/no-consensus/
  degraded outcome). What the cycle does when the majority is HOLD is also unstated.
- Write the full decision table (per action × seat count × abstentions) in §6.5 — this is
  deterministic code per [L6]; it deserves a spec, and it's cheap.

### B7. TTL is client-side on Binance spot — say it
Binance spot supports only GTC/IOC/FOK — there is no venue-side "good till time" for the
`good_till = cycle_interval − buffer` policy. The design implies (§6.7 ExecutionMonitor cancels
at TTL) but never states that TTL is bot-enforced; make it explicit so nobody assumes venue GTD,
and note kill-switch cancel-all uses the bulk-cancel endpoints. (Alpaca: DAY/GTC/IOC/FOK fine;
fractional shares support limit + extended hours since 2024 — verified.)

### B8. Prompt-injection claim is slightly overstated
§8.3: "the worst injection can do is bias one vote among several." With 3 seats and a 2/3
majority, the News/Sentiment seat's vote is **pivotal whenever the other two split**, and that
seat's evidence slice is 100% attacker-visible text. The defense stack (schema-bound output,
no tools, deterministic sizing, risk gate) genuinely holds — but restate honestly: *injection
can flip a marginal decision; it cannot size, route, or exceed risk limits on an order.*

### B9. Fallback chains can silently collapse panel heterogeneity
[L5] mandates heterogeneous models per seat, but per-seat fallbacks (§6.5) can land two seats
on the same provider/model mid-cycle, recreating the homogeneous-panel failure mode the design
exists to avoid. The substitution is recorded — add: detect when the *effective* panel loses
heterogeneity, flag the cycle (dashboard + event), and optionally degrade to WAIT if all seats
converge on one model.

---

## C. Minor / hygiene

| # | Finding | Fix |
|---|---|---|
| C1 | Plan §4: hash-pinned lock "compiled from pyproject.toml" but no tool named — pip can't compile locks. "uv not installed" is a weak reason; it's a single-binary install and the 2026 default | Name the tool: `uv pip compile --generate-hashes` (or pip-tools if uv is vetoed) |
| C2 | Python 3.11 rationale "3.12 offers nothing we need" — 3.11 has been security-fixes-only since April 2024, EOL Oct 2027 | Keep 3.11 if you want, but state the real rationale (installed, EOL horizon covers the project); consider 3.12/3.13 for a fresh repo |
| C3 | Plan Phase 1 "all six protocols" vs DESIGN's interface surface which also includes `RelevanceFilter` (§6.4) and `VectorStore` (plan §4) | Reconcile the count/list |
| C4 | §2.1 "never accidentally cross the spread" vs §6.7 default "limit orders at a *marketable* price" (which cross deliberately) | Reword §2.1: quantization must never make a price *more aggressive than intended* |
| C5 | "Max daily loss" day boundary undefined (UTC? venue session?) | Define: UTC day for crypto, exchange session for equities — and which equity basis (realized only vs mark-to-market) |
| C6 | "Max consecutive losses before auto-pause" — "a loss" undefined under partial fills / partial TTL entries | Define a realized-loss event (closed round-trip PnL < 0) |
| C7 | Retention policy covers transcripts only; ContextSnapshots (full candles × timeframes × instruments) also grow unboundedly | Add snapshot retention/compaction to §6.9 |
| C8 | ChromaDB embedding is CPU-bound; in-process it can stall the asyncio loop (and the dashboard shares the loop) | Note: run embedding in a thread executor; acceptable for research, but write it down |
| C9 | freezegun does not affect `loop.time()`; scheduler tests can't rely on it | The injectable `core/clock.py` (already planned) must be the scheduler's only time source; freezegun for datetime-only tests |
| C10 | Sources list mixes peer-reviewed work with a BlackHatWorld forum thread and a Coin Bureau listicle | Label informal sources as practitioner anecdotes or drop them; all arXiv/ACL citations verified real (TradeTrap 2512.02261, TradingAgents 2412.20138, Agentic Trading 2605.19337, CONSENSAGENT ACL 2025, Summoning the Oracle 2605.24564) |
| C11 | SimBroker fill realism unstated — "touch = fill" limit logic overstates fill quality and undermines "paper predicts live" | Spec conservative fills: require trade-through, model partial fills and fees |
| C12 | Promotion gate (≥200 cycles) is operational, not statistical; comparing panel configs on weeks of forward PnL is underpowered | Promote the shadow A/B harness (DESIGN §12 future work) into the plan — same snapshots, two panels, doubles evidence per cycle; prefer decision-quality metrics over raw PnL |

---

## D. Scenarios that could ruin the whole idea (requested explicitly)

1. **Unenforced stops + gap move** (A1): one weekend gap on a leveraged-feeling position and
   the "two-tier risk" story loses credibility with real money on. Fixable in-doc now.
2. **Accidental short on equities** (A6): the cheapest-to-prevent catastrophic failure in the whole review.
3. **Kill-switch crying wolf** (A4, A7, B2): withdrawals, testnet resets, and stock splits all
   trip halts/kills as designed today. A kill switch that fires on non-events gets disabled by
   its operator — the classic path to having no kill switch on the day it matters.
4. **Evaluation self-deception** (C12 + [L12], already handled well): concluding the panel
   "works" from noise and scaling allocation. The docs' look-ahead discipline is excellent;
   extend the same honesty to forward-test sample sizes.
5. **Duplicate orders / ledger drift** — already covered thoroughly (idempotency,
   SUBMIT_UNKNOWN, reconciler). No action; this is the docs' strongest area.

---

## E. What was verified externally (iteration 1)

| Claim | Result |
|---|---|
| TradeTrap arXiv 2512.02261 exists, says what's claimed | ✅ confirmed |
| TradingAgents 2412.20138, Agentic Trading 2605.19337, CONSENSAGENT (ACL 2025 Findings), Summoning the Oracle 2605.24564 | ✅ all real |
| Binance `newClientOrderId` ≤36 chars, charset `[.A-Za-z0-9_:/-]` | ✅ confirmed (`^[\.A-Z\:/a-z0-9_-]{1,36}$`) |
| Binance `GET /sapi/v1/account/apiRestrictions` reports `enableWithdrawals` | ✅ confirmed |
| NewsAPI free tier: 24 h article delay, 100 req/day, non-commercial | ✅ confirmed → B1 |
| Binance spot testnet monthly unannounced resets to blank state | ✅ confirmed → A7 |
| Alpaca fractional shares support limit orders + extended hours | ✅ confirmed (since 2024) |
| PDT rule status | ⚠️ changed: retired 2026-06-04, API fields removed 2026-07-06 → B3 |

Sources: [TradeTrap](https://arxiv.org/abs/2512.02261) · [Binance spot REST docs](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md) · [Binance testnet general info](https://developers.binance.com/docs/binance-spot-api-docs/testnet/general-info) · [Binance API key permission endpoint](https://developers.binance.com/docs/wallet/account/api-key-permission) · [Alpaca: FINRA retires PDT](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/) · [Alpaca fractional limit/extended-hours](https://alpaca.markets/blog/fractional-shares-trading-supports-limit-orders-and-extended-hours/) · [CONSENSAGENT](https://aclanthology.org/2025.findings-acl.1141/) · [Summoning the Oracle](https://arxiv.org/html/2605.24564) · [Agentic Trading](https://arxiv.org/abs/2605.19337) · [NewsAPI free-tier limitations](https://apitube.io/blog/post/best-free-news-apis-honest-limitations)
