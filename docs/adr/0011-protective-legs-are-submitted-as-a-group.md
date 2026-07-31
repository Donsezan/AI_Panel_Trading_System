# ADR 0011 — Linked protective legs are submitted as one group, or only a stop is placed

**Status:** accepted (2026-07-30) · **Phase:** 5 · **Amends:** [ADR 0004](0004-protective-orders-are-venue-held.md)

## Context

ADR 0004 established that protective exits are venue-held: a cycle-based system cannot babysit a
stop, so between cycles the venue must hold it. Phase 2 implemented that against `SimBroker`, which
holds both legs in one in-process book and cancels the sibling when either fills.

Real venues do not work that way. Binance spot creates a linked pair through **one** call
(`POST /api/v3/orderList/oco`); Alpaca through **one** order with `order_class=oco`. There is no way
to place two independent orders and have the venue treat them as a group.

`BrokerAdapter.submit` is one-intent-one-order. So on a real venue, the Phase 2 code would have
placed a stop and a take-profit as two free-standing orders over a single holding — and both can
fill. The second sells a position that is already gone, which in a long-only system is an
accidental short: R13, the risk register's other **severe (money)** entry.

## Decision

### `BrokerAdapter` gains `submit_group`

```python
async def submit_group(self, intents: Sequence[OrderIntent]) -> tuple[OrderAck, ...]: ...
```

Called only where `capabilities().oco_groups` is true, and only for the exit legs of one entry.
`SimBroker` places them in turn (its book already provides the linkage), Binance posts an OCO list,
Alpaca posts an `oco` order. A single-leg group falls through to the ordinary `submit`, so a venue
that cannot link legs takes exactly the same code path.

`ExecutionService.submit_group` writes **every** leg's `client_order_id` durably *before* the one
network call. A crash in between must leave a trace of every id that may now exist at the venue, or
recovery has nothing to query by (PLAN §1.4, §2.3). This changes the event order for a protective
pair — two `ORDER_SUBMITTED` events, then the acknowledgements — and the scenario suite asserts the
new shape literally.

### `oco_groups=false` means the take-profit is never placed

`plan_legs` already did this, and it is now load-bearing rather than defensive: where a venue cannot
link legs, only the stop is placed. A take-profit is an optimisation; a double sell is an incident.
Alpaca declares `oco_groups=false` when extended hours are enabled, because Alpaca cannot bracket an
extended-hours order — so that configuration silently gives up targets, and says so in
`capabilities()` rather than discovering it at the venue.

### A leg the venue does not report on fails closed

Both real adapters raise `DataStaleError` if the group was accepted but a leg is missing from the
response. The legs exist at the venue either way; inventing a state for one would leave the monitor
guarding a position with an order it has never seen. The cycle halts and startup recovery resolves
each leg by querying its `client_order_id`.

### Rejected: declaring `oco_groups=false` for every real venue

It would have avoided touching Phase 2 code, and left `take_profit_atr_multiple` as configuration
that no real venue reads — which `core/config.py`'s own doctrine calls worse than a missing field,
because an operator would believe a limit is in force when it is not. DESIGN §6.7 names Binance spot
OCO explicitly.

## Consequences

* The monitor's leg-replacement path is one call instead of N, so resizing legs after a further
  partial entry fill is atomic at the venue as well as in our records.
* `OrderStatus` now reports `side`, `order_type`, `limit_price` and `stop_price`. The group logic
  did not need them, but the self-trade check does — an untriggered stop is not a resting price, and
  without `order_type` every entry behind a stop would be vetoed (see `execution/selftrade.py`).
* The contract suite asserts the linkage behaviourally: fill one leg, and the sibling must be
  terminal at the venue. An adapter that places two unlinked orders fails CI.
