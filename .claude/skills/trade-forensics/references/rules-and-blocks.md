# Rules, Outcomes and Blocking Layers

A map for orientation. **`tradebot/risk/` is the authority** — verify a rule name here against
source before asserting it, because rules are added and renamed.

Each `RISK_CHECKED` check carries `rule`, `decision`, `limit`, `observed`, `max_qty`, `detail`.
`decision` is one of `pass` · `adjusted` · `veto` (`RiskDecision`, `core/enums.py`).

**`adjusted` is not a refusal.** It means a rule capped the size and the order proceeded smaller.
Only `veto` stops an order. Rules return caps composed with `min()`, so no ordering can widen a
limit an earlier rule imposed.

## Tier-1 — per instrument, before the order exists

Source: `risk/rules.py`, `risk/sizing.py`, `risk/tier1.py`. Config lever: the basket's
`risk_policy` (Settings → the basket), unless noted.

| Rule | Vetoes when | Lever |
|---|---|---|
| `sizing` | ATR not positive (no volatility estimate), or `SELL` while flat | indicators / `stop_loss_atr_multiple` |
| `min_conviction` | `conviction < floor`, on the 0–1 scale | `min_conviction` |
| `long_only` | `SELL` while flat would open a short. Caps (`adjusted`) a `SELL` larger than the holding | `long_only` (v1: leave true) |
| `quarantine` | the instrument or its basket is quarantined | Workspace quarantine toggle — **not** a form field |
| `max_consecutive_losses` | `losses >= cap`; also auto-pauses the basket | `max_consecutive_losses` |
| `cooldown` | traded fewer than `cooldown_cycles` cycles ago | `cooldown_cycles` |
| `max_trades_per_day` | `placed >= cap` today | `max_trades_per_day` |
| `max_position_size` | this instrument is at its share of basket budget | `max_position_pct_of_basket` |
| `max_basket_allocation` | basket exposure is at budget (`headroom <= 0`) | `max_basket_allocation_pct` |
| `venue_quantization` | after rounding to venue precision the order is unsubmittable | venue reference data — see below |

### `venue_quantization` — three distinct reasons

The `detail` reads `<reason> after quantizing <requested> to venue precision`. Reasons
(`SizingVeto`, `core/money.py`):

- `non_positive_qty` — rounded down to zero. The size is **smaller than one `lot_size` step**.
- `below_min_qty` — above zero but under the venue's `min_qty`.
- `below_min_notional` — qty × price is under the venue's `min_notional`.

Quantization only ever **shrinks**, and below a minimum is a veto, never a bump up — bumping
would silently oversize past the risk limit that produced the quantity.

**These are venue reference data, never operator input (ADR 0025).** The lever is not a setting:
either the position size must be larger (risk budget, `risk_per_trade_pct`) or the instrument is
not viable at that budget. In **sim**, suspect a lot-size/price mismatch first — see the
"instrument never trades" entry in `reading-the-log.md`.

## Tier-2 — portfolio-wide, at submit

Source: `risk/tier2.py`. Lever: the `global_risk` document, clamped in live by `LIVE_CEILING`
(`min(published, ceiling)` — the ceiling only tightens).

| Rule | Vetoes when | Lever |
|---|---|---|
| `price_collar` | order price deviates too far from last, **or there is no last price** | `marketable_cross_pct` |
| `max_orders_per_hour` | trailing-hour order count at cap | `max_orders_per_hour` |
| `max_order_notional` | order notional above the per-order cap, or no price to value it | `max_order_notional` |
| `max_gross_exposure` | portfolio gross exposure at ceiling | `max_gross_exposure` |
| `max_instrument_exposure` | this instrument at its portfolio ceiling | `max_instrument_exposure` |
| `max_cluster_exposure` | the correlation bucket is at its ceiling, **or the instrument belongs to no bucket** | cluster config |
| `tier2_shrink` | shrinking to fit portfolio headroom left nothing submittable | whichever ceiling bound |

Portfolio-wide limits read the **configured universe**, not one basket's instruments — only
`max_basket_allocation` is basket-scoped.

## Operator exits stand aside

A manual close is an `OrderIntent` through the same engines (ADR 0015). The **metering** rules —
`cooldown`, `max_trades_per_day`, `max_consecutive_losses`, `max_orders_per_hour`, and
`quarantine` — record a `pass` with `stood aside: an operator exit reduces exposure and is not
metered`. **Correctness and venue legality never stand aside:** `long_only`, `venue_quantization`,
`price_collar` and the minimums still refuse.

Seeing `STOOD_ASIDE` in a `detail` means a human initiated it, not that a limit failed.

## Cycle outcomes

`CycleOutcome`, `core/enums.py`. This is **basket-wide** — never read it as one instrument's result.

| Outcome | Means | Where to look next |
|---|---|---|
| `orders_placed` | at least one order reached the venue | per-instrument `RISK_CHECKED`; others may still have been vetoed |
| `no_action` | panel decided nothing tradable | `decisions.action` — `WAIT`/`HOLD` |
| `risk_vetoed` | a rule refused | `RISK_CHECKED` with `decision=veto` |
| `data_stale` | market data missing, holed, or too old | gap/staleness in the snapshot |
| `panel_degraded` | seats unreachable or unparseable | `SEAT_RESPONDED`; provider `secret_ref` env vars |
| `blocked` | kill switch tripped or basket halted — **before** the panel | `risk_state`, `basket_status` |
| `quarantined` | whole basket quarantined; snapshot built, panel skipped (ADR 0022) | quarantine toggle |
| `failed` | the cycle raised | the exception in the log |

`blocked` and `quarantined` still **record** a cycle. A basket that stops appearing in the log
is a different problem — gate 1, not gate 2.

## Blocking layers above the rules

Four different mechanisms, easy to confuse. They are cleared in four different ways.

| State | What it is | How it clears |
|---|---|---|
| **Pause** | configuration: `status` on the basket | publish `status: active` |
| **Halt** | the system protecting itself after failures — database state, per basket | `risk unhalt <basket> --confirm "RE-ARM TRADING"` |
| **Kill switch** | portfolio-wide trip (drawdown, reconciliation mismatch) | `risk rearm --confirm "RE-ARM TRADING"` |
| **Quarantine** | an operator excluding a scope from *automated* trading (ADR 0022) | one click in the workspace |

Publishing `status: active` **never** clears a halt — different mechanisms, deliberately kept apart.

### Valuation freeze — the one that looks like a trip but is not

Phase 12 / ADR 0027. If a position's mark is **absent or older than `mark_staleness_seconds`**,
`price_of` returns `None` and equity cannot be computed. Then:

- new orders are **blocked**
- the kill switch is **not** tripped, no baseline moves, no state is written
- it **clears itself** when marks return
- it never blocks a reduce-only operator exit

A freeze is ignorance, not a breach. Do not report it as a kill-switch trip, and do not
recommend `risk rearm` for it — there is nothing to re-arm.

### Supervision stopped

`serve --observe` comes up stopped, and Stop pauses cycling without cancelling anything. **No
order may be placed while stopped, manual close included**, and nothing polls open orders. If
cycles simply stop appearing, check this before looking for a veto.

### Live-only gates

`run --mode live` refuses on the spot; `serve --mode live` comes up unarmed and says so. Four
preconditions (ADR 0012) plus `control/readiness.py`: alerting configured, panel real and
reachable, data complete, configs build. A seat bound to the stub is refused outright in live.
