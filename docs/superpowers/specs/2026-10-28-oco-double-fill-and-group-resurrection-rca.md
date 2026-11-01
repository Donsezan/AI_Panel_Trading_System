# RCA: the 2025 reference pass halts at cycle 377 with an oversell on ETH/USDT

Source: a copy of `decision_lab/workspace/c5ff6a2e289a3d2f60a014957b75e834/corpus.db`, saved in
this directory. Repo at `known-gaps-5-intra-cycle` @ ee60c33. Read-only: no repo files edited.
The repro script is `repro/oco_repro.py` in this directory.

> **Status (2026-10-29):** §4 items 1 and 2 are fixed on this branch (`b52a980`, `63f6105`, `81d752a`); item 3 and the fidelity rows are KNOWN_GAPS §6, §10, §11, §12. The corpus copy and `repro/oco_repro.py` were working files and are not committed.

## TL;DR

Two defects act together. Neither is the §5 race, and neither is a discretionary SELL.

1. **The sim lets both legs of one OCO fill (sim-only; this is what oversold).**
   `SimBroker.observe` (`tradebot/execution/brokers/sim.py:215-227`) takes a snapshot of the
   working orders before it matches any of them. `_match` (`:229-244`) never checks
   `resting.state.is_open` again. So the stop leg fills, and `_fill` → `_cancel_siblings`
   (`:320-338`) marks the take-profit CANCELLED and releases its lock. The loop then reaches the
   take-profit anyway and fills it. The sim ends up short: venue qty −0.1244, locked ETH −0.1244
   (reproduced, see §2). The ledger books the first fill and refuses the second. ADR 0030 poisons
   that group, and the scoped poll then halts the basket, which is the intended fail-closed path.
2. **The monitor brings a finished protective group back onto a later position, with stale
   prices (live-relevant).** Group `sim-KKGLVJTFKMFR7Z6I` belongs to an entry from 2025-03-28 at
   2004.86 (stop 1974.53, TP 2050.37). A discretionary SELL emptied that holding on 2025-04-21.
   The group was never closed or forgotten. When the panel bought again on 04-24 and 05-02, the
   monitor's `_targets` ranked this dead group first, because its stop is the highest and so the
   "tightest" for a long. It gave the group the new shares and re-placed its March legs. That
   put a SELL stop at 1974.53 against a market near 1757-1840, which is above the market. In the
   sim this "stop" arms on the first bar and then rests as a limit sell at 1964.66. That is a
   second take-profit, and it is what let both legs reach their limits in one rally bar.

## 1. Timeline (event seqs)

### How the group came back

| seq | ts | what |
|---|---|---|
| 5980 ff. | 03-28 00:00 | Entry `sim-KKGLVJTFKMFR7Z6I` BUY 0.1244 @ 2004.86, cycle `ccd80d50…`. Legs SP6Z (stop 1974.53/lim 1964.66) and T3KO (TP trig 2050.37/lim 2040.12) |
| — | 04-02 … 04-14 | Legs cancelled and re-placed at the same March prices for 0.0622, 0.0467, 0.1206, then 0.1244 (`orders` table, group KKGLV) |
| 7721-7724 | 04-21 08:00 | Panel SELL `sim-5HUM…` 0.1244 @ 1644.94 fills. POSITION_UPDATED qty **0** |
| 7726-7727 | 04-21 08:00 | KKGLV legs 55MD and LXK5 cancelled (`released_to_position`). The group is **not forgotten** |
| 7928-7931 | 04-24 08:00 | New entry `sim-UISF…` BUY 0.0609 @ 1757.30. Position 0.0609 |
| 7932-7938 | 04-24 08:00 | PROTECTIVE_PLACED for **group KKGLV**, not UISF: stop 1974.53/1964.66 and TP 2050.37/2040.12, qty 0.0609. The legs carry `cycle_id ccd80d50…`, the **03-28** cycle (confirmed in the `orders` table). UISF's own group gets nothing |
| — | 04-25 | Partial panel SELL 0.0152. KKGLV re-placed at 0.0457 (W3UG/IYEO), same prices |

### The failing group, 05-02 → 05-08

| seq | ts | what |
|---|---|---|
| 8470-8473 | 05-02 00:00 | Entry `sim-MT4B…` BUY 0.0896 @ 1839.21. Position 0.1353 |
| 8474-8475 | 05-02 00:00 | KKGLV legs W3UG and IYEO cancelled (resize) |
| 8476-8482 | 05-02 00:00 | KKGLV re-armed for **0.1244** (= its March `filled_qty` cap): stop **DMXTPS7EZQN3O7EF** 1974.53/1964.66 and TP **LPFF3WIHTHWPKT2W** 2050.37/2040.12. The market is ~1839, so the stop is placed **above the market** |
| 8483-8489 | 05-02 00:00 | MT4B's own group gets only the remainder, 0.0109: stop 6GT6 1808.84/1799.80 and TP RNIP 1884.78/1875.36 |
| 8536, 8629, 8753, 8847, 8894 | 05-02 … 05-07 | Every panel SELL is **vetoed** (`min_conviction`). No discretionary exit reaches the venue |
| 8684-8686 | 05-05 00:00 | MT4B stop 6GT6 fills 0.0109 @ 1799.80. Position 0.1244. Sibling RNIP cancelled. This is a correct OCO close |
| 8940 | 05-08 16:00 | **Stop DMXTPS fills 0.1244 @ 1964.66.** It is a limit *above* entry: the stop armed on the first bar after 05-02 (low ≤ 1974.53) and rested as a sell limit until the rally reached 1964.66 |
| 8941-8942 | 05-08 16:00 | POSITION_UPDATED qty **0E-8**. ROUND_TRIP_CLOSED |
| 8943 | 05-08 16:00 | RISK_EVENT `unbookable_fill` / `group_poisoned`: **LPFF (TP) fill 0.1244 @ 2040.12** exceeds holding 0 |
| 8944-8945 | 05-08 16:00 | BASKET_STATUS_CHANGED halted (`portfolio.py:148` message). Cycle `failed` |

There is no cancel event for LPFF after 8481, and the `orders` row stays `open` and unbooked.
**The holding was emptied by the group's own stop leg (DMXTPS).** The take-profit leg of the
same OCO (LPFF) then filled in the same venue tick. It was not a panel SELL, and it was not one
fill counted twice.

The bar that did it is `binance__ETH_USDT__1h.csv` 2025-05-08T15:00-16:00: H 2075.65, L 1987.81.
That high reaches the stop's limit (1964.66), the TP trigger (2050.37) and the TP limit (2040.12).

## 2. Mechanism

### Candidates

- **(a) A discretionary SELL did not cancel the legs: ruled out for this incident.** No SELL
  reached the venue between 05-02 and 05-08 (all were vetoed, seqs above). The memo's §2 issue
  is still real elsewhere. On 04-21, SELL 5HUM filled (7723) while legs 55MD and LXK5 still
  locked the whole 0.1244, and they were cancelled only afterwards (7726-7727). `_reserve`
  (`sim.py:435`) let free ETH go negative. Binance would have rejected that SELL for insufficient
  balance. That is fail-closed, but it is a fidelity gap (Option E).
- **(b) `ExecutionService.cancel` ignores `ack.cancelled=False`: ruled out for this incident.**
  Our code never cancelled LPFF. `service.py:197-218` does discard `ack.cancelled` and advances
  to CANCELLED regardless. That is a real adjacent defect, and it was not on this path.
- **(c) Both OCO legs matched in one bar: confirmed.** `oco_groups=True` (`sim.py:208`), and
  `_cancel_siblings` exists. `observe` defeats it:

  ```python
  # sim.py:221-227
  working = [resting for resting in self._orders.values()
             if resting.intent.instrument_key == tick.instrument_key and resting.state.is_open]
  return tuple(fill for resting in working for fill in self._match(resting, tick))
  # _match (229-244) never re-checks resting.state.is_open
  ```

  Repro (`repro/oco_repro.py`, real `SimBroker`): legs as above, one bar at ~1840 (arms the
  stop, no fill), then one bar H 2110:

  ```
  bar2 [('STOP', 0.1244, 1964.66), ('TP', 0.1244, 2040.12)]
  STOP filled  TP filled
  venue qty -0.1244 free 0.0000 locked -0.1244
  ```

  The existing tests do not catch it. `test_sim_broker.py:200` uses a bar that triggers only the
  TP, and `test_broker_contract.py:421` fills one leg through the harness.
  Only *stop-armed-earlier* (or a bar wide enough to span both triggers) meets *same tick*.
- **(d) The monitor resurrects a group: confirmed. This is why the stop was above the market.**
  - `ExecutionMonitor.prune` (`monitor.py:569-581`) is called only with **the current cycle's**
    group ids (`basket_runner.py:273`). KKGLV was not settled at the end of its own 03-28 cycle,
    and no later cycle names it, so it stays in `_tracked` for good.
  - `_maintain` / `_replace_legs` (`monitor.py:336-444`) handle a target of 0 by cancelling the
    legs (`released_to_position`). They do not close the group.
  - `_targets` (`monitor.py:474-493`) gives each group `min(group.order.filled_qty, budget)` from
    the instrument's current holding. `_protectable` (`:495-528`) ranks the groups tightest stop
    first (`_RANK_SIGN`, `:84`), and it excludes only groups with a FILLED leg. A group whose
    holding was sold away keeps its full entitlement. The next BUY's shares go to it first, at
    the old entry's stop and TP (`plan_legs` from `group.order.protective`). The new entry's own
    plan gets only what is left over.

The arming and gap behaviour also matters. A SELL stop placed above the market satisfies
`low <= stop` on the very next observed bar (`_TRIGGERS`, `sim.py:67-72`), so it becomes armed.
`_TRADE_THROUGH` then waits for `high >= limit` (`sim.py:75-78`). The stop is now a resting limit
sell above the market. The sim also observes only `series.latest` per read (`sim.py:559`), so on
an 8h cadence most hourly bars are never seen. Example: the 13:00 bar on 05-08 already reached
1964.66, but the sim did not observe it.

## 3. Live or sim-only?

- **Defect 1 (double OCO fill) is sim-only.** Binance spot OCO is one order list, and the venue
  expires the other leg atomically. Both legs cannot fill, and the venue will not sell an asset
  the account does not hold. It is a sim-fidelity gap, but a severe one: it creates a short at
  the venue and corrupts the evidence a paper soak (venue `sim`) reports.
- **Defect 2 (group resurrection with stale prices) is live-relevant and on the money path.** It
  sits in `ExecutionMonitor`, which live shares (ADR 0020). On Binance, a SELL OCO needs
  `TP limit > last > stop`. A stale group whose stop is now above the market is rejected
  (-2010). `_replace_legs` records `unprotected_position` and re-raises, retries every poll, and
  the budget never reaches the new entry's own group. **The new position stays bare (no
  venue-held stop) for as long as the dead group exists.** If the market has instead risen past
  the old levels, the TP is below the market and the OCO is rejected the same way. If the stale
  levels happen to straddle the market, the OCO is accepted, and the new position is guarded by
  an unrelated entry's stop distance, not the one Tier-1 sized it against. Every outcome breaks
  "the stop that sized the position is the stop at the venue". None of them oversells in live.
- **Adjacent and live-relevant, but not on this path:** the exit path sells without cancelling
  the resting legs first (04-21, seq 7723 before 7726). Binance would reject that SELL for
  insufficient free balance, so a discretionary exit silently never happens. This fails closed.
  It is the memo's Option E.

## 4. Fix (minimal; nothing is implemented here)

1. **SimBroker: a cancelled sibling cannot fill** (`sim.py`). In `_match`, return `()` when
   `not resting.state.is_open`, or re-filter inside the loop in `observe`. This is what Binance
   OCO guarantees and what `oco_groups=True` claims.
   - Defence in depth, matching the venue: `_fill`/`_settle` should never settle a SELL beyond
     the venue's own held qty. That should be an assertion-grade error, not a silent negative
     balance.
   - Fidelity, matching Binance:
     - Reject a SELL STOP_LOSS_LIMIT whose stop is at or above the touch ("would immediately
       trigger").
     - Reject an OCO group whose prices are not `tp_limit > last > stop`.
   - **Option E** (reject a submit when free base < qty, as Binance rejects for insufficient
     balance) is worth doing, but **it would not have prevented this halt**. LPFF was reserved
     legitimately at submit. The oversell happened at match time, after `_cancel_siblings` had
     released its lock.
2. **ExecutionMonitor: a group whose holding is gone does not come back** (`monitor.py`).
   - A group's entitlement must ratchet down with *filled* reductions and never grow back.
     Specifically: hold a per-group `capacity`, start it at `filled_qty`, and after each
     allocation set it to `min(capacity, allocation-from-held)`, computed on `held` alone, not on
     `held - committed`. A resting SELL that later expires must still let the group re-arm.
   - When capacity reaches 0, close the group and `forget` it, so a later BUY's shares are
     guarded only by that BUY's own plan.
   - At minimum: when the instrument's holding is flat, close every tracked group for it.
   - This does not violate fail-closed. The new entry carries its own `ProtectivePlan`, which
     will then guard it.
   - A second, independent gap: `prune` only names the current cycle's groups, so settled groups
     from earlier cycles live forever. The monitor should prune any settled group regardless of
     which cycle created it (or `_maintain` should forget a group it releases to zero).
3. **The exit path must cancel the legs before selling.** This is not needed for this incident.
   It is needed for live once Option E makes the sim reject what Binance rejects. It is the
   memo's adjacent item: cancel the group's legs, confirm with `ack.cancelled`, re-read the
   holding, and record before submitting the SELL. Also fix `service.cancel` to treat
   `ack.cancelled=False` as "ask the venue" (`fetch_order` → sync), not as CANCELLED.

### Tests that would prove it

- `tests/unit/test_sim_broker.py`: stop leg armed by a bar below its trigger with no fill. A
  later single bar reaches both the stop's limit and the TP's trigger and limit. Assert exactly
  one fill, the sibling CANCELLED, venue qty ≥ 0 and locked ≥ 0. This is `repro/oco_repro.py`
  turned into a test, and it fails today. Add the mirror case with the TP before the stop in
  insertion order.
- `tests/contract/test_broker_contract.py` (`TestProtectiveGroups`): "both legs reached in one
  observation; at most one fills". For the wire fakes, this asserts that the adapter reports the
  venue's single fill.
- `tests/unit/test_execution_monitor.py` (or wherever `_targets` and `KNOWN_GAPS §4` are tested):
  1. Entry A filled with legs.
  2. A discretionary SELL empties the holding, so A's legs are released.
  3. Entry B BUYs.
  4. Assert the PROTECTIVE_PLACED is for **B's group at B's stop and TP**, and A is forgotten.

  Also: a partial SELL then a new BUY must not grow A's guarded qty back.
- A scenario in `tests/scenario/` (replay through `BacktestHarness`): buy → full panel exit →
  re-entry at a lower price → rally through the *old* group's levels. Assert no `unbookable_fill`,
  no halt, and the stop resting at the venue equals the re-entry's planned stop. This mirrors the
  03-28 / 04-21 / 04-24 / 05-08 sequence above.
