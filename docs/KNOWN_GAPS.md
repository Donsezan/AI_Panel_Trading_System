# Known gaps

Defects found by audit and verified. The numbered ones are **open**: each is written with what it
costs, what was actually observed, and what closing it would take, because each is a decision
someone has to make rather than a bug waiting for a free afternoon. What has since been fixed moves
to *Closed*, with the same evidence, so nothing about an audit's boundary has to be inferred.

The house rule applies to reading this file too: **fail-closed is not the same as safe.** Most of
these resolve in the fail-closed direction and are still on this list, because a system that stops
doing something for a reason nobody can name is an incident.

Four passes so far:

- **§1–3, the instrument-universe seam** (2026-08-19). None is caused by the two fixes that shipped
  alongside them ([marketdata/synthetic.py](../tradebot/marketdata/synthetic.py), the live
  instrument universe); all three predate that work.
- **M1–M4, the maintenance package** (2026-08-22). All four have since been **fixed** — see
  *Closed* below. They are kept in this file's history rather than deleted from it, because the
  boundary of an audit is only legible if what it found and what became of it are both recorded.
- **§4, protective legs against a reduced position** (2026-08-25). Not an audit: found by the
  first long reference pass through `decision_lab`, over six months of recorded Binance data with
  a panel that takes partial exits. It predates that work — no bot file was changed by it.
- **§5–8, found while designing gap 4's fix** (2026-08-27). Not an audit either: four things
  the fix had to reason about and then leave alone. §5 and §6 are the parts of the same seam
  that monitor-side scope deliberately excludes; §7 and §8 are what an operator is actually
  told when the discrepancy §6 can cause is the one that gets caught.

| # | Gap | Reaches | Direction |
|---|---|---|---|
| 1 | The mismatch kill threshold compares quantities to money, and sizes itself from explained lines | live · paper | **fails open and closed** |
| 2 | Retiring a basket that holds a position is unguarded | every mode | fails closed, and traps |
| 3 | The one-quote-currency rule is enforced only at boot | live · paper | fails open |
| 4 | Protective legs are sized to the entry, so a partial exit leaves them oversized | live · paper · sim | fails closed, at the venue |
| 5 | The monitor polls only inside a cycle that placed orders | every mode | **fails open** |
| 6 | Nothing releases protective legs before a discretionary exit | live · paper on a venue | fails closed, and traps |
| 7 | The mismatch alert names no instrument and no quantity | live · paper | fires, but says too little |
| 8 | The kill-switch reason carries the explained lines and no absolute figures | every mode | fires, but says too little |

---

## 1. `exceeds_kill_tolerance` compares a base-asset quantity against equity

**Where** — [ledger/reconciler.py:377](../tradebot/ledger/reconciler.py#L377)

```python
worst = max((abs(d.delta) for d in report.differences), default=ZERO)
return _pct_of(worst, equity) > self._mismatch_kill_pct
```

A `Difference` is scoped either to an instrument or to a currency. For an instrument its `ours`
and `theirs` are **position quantities** — 0.5 BTC, 1 200 XRP — while `equity` is in the notional
currency. The ratio is therefore only meaningful when a difference happens to be denominated in
the quote currency; for every instrument-scoped difference it compares a count of coins to a sum
of dollars.

**Observed.** A ledger holding 0.5 BTC against a venue reporting none, equity 10 000 USDT. At the
simulated venue's own opening price that is 25 000 USDT missing — 250 % of equity:

```
CLASS mismatch
  DIFF sim:BTC/USDT ours 0.5 theirs 0.0 delta -0.5
KILL? False
```

`_pct_of(0.5, 10000)` is 0.005 %, so the switch is not tripped. The method's own docstring states
the property it fails to hold: *"The one thing that must not happen is a large discrepancy being
waved through as small."*

**It cuts both ways.** A cheap asset over-triggers on the same arithmetic: 600 XRP missing is
roughly 300 USDT, 3 % of the same equity and below the 5 % threshold, but `_pct_of(600, 10000)` is
6 % and trips. So the guard is scale-blind, not merely lenient.

**`worst` also ranges over *explained* lines.** `_report` filters only `MATCH`, so `DRIFT`,
`EXTERNAL_CHANGE` and `CORPORATE_ACTION` differences stay in the tuple `max` runs over. A report
classified `MISMATCH` because of one small genuine shortfall takes its magnitude from whatever
line happens to be largest — including a deposit the reconciler has already explained. Reproduced:

```
CLASSIFICATION mismatch
  sim:BTC/USDT   delta         -0.1  mismatch          ← the genuine discrepancy
  USDT           delta        41000  external_change   ← a legitimate deposit
KILL SWITCH TRIPS? True
```

The switch trips on the deposit. And because it is `max` rather than a sum, the mirror case also
holds: several shortfalls each just under tolerance never aggregate into one that clears it.

**What it does *not* do.** Every unclean report still halts the process —
[startup.py](../tradebot/control/startup.py) raises `_RecoveryHaltError` regardless of this
branch, which populates `Recovery.failures` → `Recovery.halted` → `SupervisionController.blockers`,
and the dashboard's Start cannot override it. **The bot does not trade through a discrepancy this
fails to catch.** Anyone reading the arithmetic alone will reach for that conclusion; it is wrong,
and the correct severity is narrower and more specific.

**What is lost is the escalation, and the two layers are not interchangeable.**

| | Kill switch | Recovery halt |
|---|---|---|
| Stored | `risk_state`, persisted | `Recovery.failures`, in memory |
| Survives a restart | yes | **no** |
| Cleared by | a human typing `RE-ARM TRADING` | restarting the process |
| Re-baselines drawdown | yes | no |

Nothing writes `basket_status` on a reconciliation halt — only `watchdog.halt_basket` does — so a
boot-time halt leaves no durable trace. The exposure is therefore an **intermittent** venue
discrepancy, which is a real failure mode: start #1 sees most of the portfolio missing, halts, and
persists nothing; the operator's reflex during an incident is to restart; start #2 gets a good
snapshot, reconciles clean, adopts it, and trades with no human ever having acknowledged that a
discrepancy above tolerance occurred. For a position-scoped discrepancy — which is to say, for
every instrument this system actually trades — the durable layer is simply absent.

**Why CI is green.** All four kill-tolerance assertions in `TestSeverity`
([test_reconciler.py:304-346](../tests/unit/test_reconciler.py#L304)) drive the discrepancy through
`usdt=`, the quote currency — the one scope where the units coincide with equity. The single test
that varies `qty=` asserts the *classification* and never calls `exceeds_kill_tolerance`. The
position branch of this check has never been exercised, so the suite cannot fail on it.

**One more caller than the docstring claims — or rather, one fewer.** `reconciler.py`'s module
docstring says reconciliation runs "at startup, after any connectivity gap, and periodically
(DESIGN §6.8, [L10])". `reconcile()` has exactly one production caller, in `StartupSequence`. The
supervisor's `serve` loop runs `_check_drift` and `_sweep_portfolio` and never reconciles. Whether
the missing periodic reconciliation is the gap, or the docstring is, is a separate decision — but
they cannot both be right.

**To close it.** Value each difference before comparing: `Marks.price_of` already answers this and
ADR 0027's rule applies unchanged — a difference that cannot be valued because the mark is missing
or stale must count as severe, exactly as `equity <= ZERO` already does. Scope `worst` to `MISMATCH`
lines. Then add the test that does not exist: a mismatch driven through a **position quantity**.

---

## 2. A basket holding a position can be retired with one click

**Where** — [dashboard/routes/configure.py:282](../tradebot/dashboard/routes/configure.py#L282) and
[control/config_store.py:119](../tradebot/control/config_store.py#L119)

Neither checks whether the basket holds anything. `ConfigStore.retire` verifies only that the
document has versions; the route posts straight through. Retirement removes the basket from
`configs.baskets()`, and therefore its instruments from `configured_instruments`.

The position does not go anywhere. Positions belong to the venue portfolio, not to a basket
(DESIGN §4), and the ledger keeps it across restarts. So the holding survives while every path
that needs an `Instrument` for it stops finding one:

- **It can no longer be marked.** [valuation.py:178](../tradebot/control/valuation.py#L178)
  resolves held positions through the universe. The mark ages out, the portfolio freezes, and
  **no basket in the process may send a new order** — the correct response to not knowing what
  the portfolio is worth, arrived at for a reason nobody asked for.
- **It can no longer be closed by hand.** `closable()`
  ([manual_close.py:146](../tradebot/control/manual_close.py#L146)) enumerates positions through
  baskets in service, so the retired basket's holding is not offered. That is deliberate and
  right — without a basket there is no Tier-1 policy to evaluate the close against — but it means
  the obvious remedy is closed off at the same moment the problem is created.
- **The next restart may refuse to complete recovery.** If the position had a resting protective
  leg, `_resolve_open_orders` ([startup.py:238](../tradebot/control/startup.py#L238)) raises
  `references unknown instrument` and the process comes up halted.

Both the freeze and the halt are the fail-closed direction, and `_to_mark`'s docstring already
names the situation. What is missing is that **nothing warns at the moment of the act**. The
operator retires a basket, and some minutes later the whole system stops trading with a message
about marks.

**Recoverable, and only one way.** Re-publish a basket containing that instrument, then close the
position by hand. An operator who does not know that is looking at a frozen portfolio with no
route out on the screen.

**To close it.** Refuse the retire, or gate it behind a second deliberate click, when the basket
holds a position — the pattern quarantine already uses for exactly this class of decision
(ADR 0022). The information is one `ledger.positions()` call away, and `closable()` already
computes the same join.

---

## 3. "One quote currency per process" is checked at boot and never again

**Where** — [app.py:782](../tradebot/app.py#L782), whose only caller is `_assemble` at
[app.py:868](../tradebot/app.py#L868)

```python
quotes = {instrument.quote_currency for instrument in instruments}
if len(quotes) != 1:
    raise ConfigError("every basket in one process must share a quote currency ...")
```

The rule is right and the reason is in its docstring: equity is one number per venue portfolio and
every Tier-2 limit is a percentage of it, so two quote currencies mean two different equities
sharing one account. But it runs once, over the baskets configured at start-up, and nothing
re-asks it.

Publishing goes through `store_basket` → `verify_publish`, which compares each instrument against
the venue's published rules. `quote_currency` is in `VERIFIED_FIELDS`, so it is checked — **against
what the venue says that symbol is quoted in**, never against what this process is denominated in.
Binance genuinely lists `BTC/EUR`, so a basket naming it verifies cleanly and is stored.

From then on the process holds EUR-quoted positions while `PortfolioWatch`, the drawdown baselines
and every Tier-2 percentage are denominated in USDT. `value_cash`'s rungs will try to price EUR
through a `EUR/USDT` market if the catalogue lists one and freeze if it does not — so the outcome
is either a silently wrong denomination or an unexplained freeze, depending on the venue.

**Reach.** Not sim: `sim_markets.json` is 30 markets and all 30 are USDT-quoted, so the simulated
venue cannot express this. Paper reaches it — the primary paper wiring is `SimBroker` fed by live
Binance data, and it takes *Binance's* catalogue (ADR 0025) — and so does live.

**To close it.** One check in `store_basket`, beside the exclusivity refusal it already performs
(ADR 0026): refuse a basket whose instruments do not all quote in the process's notional currency.
`Application.quote_currency` is already the single answer to what that is.

---

## 4. Protective legs track the entry order, not the position

**Where** — [execution/monitor.py:146](../tradebot/execution/monitor.py#L146)

```python
async def _maintain(self, group: _Tracked) -> None:
    """Keep the protective legs matched to what the entry has actually filled."""
    entry = group.order
    if entry.filled_qty > ZERO and entry.filled_qty != group.protected_qty:
        await self._replace_legs(group)
```

`_Tracked.protected_qty` is, by its own comment, the *entry* quantity the current legs guard. The
monitor has no view of the position. So a SELL taken by any other path — another cycle's exit
decision, an ADR 0015 operator close — reduces the holding while the legs already resting at the
venue keep their original size. Nothing resizes them and nothing cancels them: `_replace_legs`
fires only when the *entry* fills further, and `_close_group` only when one of that group's own
legs fills.

The `self._polling` lock two screens up names this exact hazard — *"twice the exit quantity against
one position, which in a long-only system is an accidental short (R13)"* — and guards only its
concurrent-poll variant. This is the same arithmetic reached by a partial exit instead.

**Observed**, in the event log of a reference pass over recorded BTC/ETH 1h data,
2024-01-01 → 2024-07-01, on `--reference-panel sim`:

```
2024-01-03 16:00  BUY  0.0351 fills          position 0.0351
2024-01-03 16:00  legs armed: stop 0.0351 @ trigger 2170.66, take-profit 0.0351 @ 2348.37
2024-01-04 12:00  SELL 0.0087 fills          position 0.0264   ← legs untouched at 0.0351
2024-01-07 12:00  BUY  0.0852 fills          position 0.1116   ← a second group, its own 0.0852
2024-01-08 00:00  that group's stop fills    position 0.0264
2024-01-08 ~03:00 ETH low 2166.38 trips the stale 2170.66 stop
```

```
ReconciliationMismatchError: sell of 0.03510000 exceeds holding 0.02640000 on binance:ETH/USDT;
v1 is long-only, so this is ledger corruption rather than a short
```

`Ledger._apply_sell` refused, which is the guard working. But it fires at the wrong end: **the
oversized order is already resting at the venue** by then, placed hours or days earlier.

**What it costs.** On Binance spot the venue rejects the stop for insufficient balance at the
moment it triggers — so the position is unguarded during exactly the move the stop existed for,
and the local ledger and the venue disagree about what is protected. On a venue that permits the
sell, it oversells into a short the risk engine believes cannot exist. The sim/backtest case is
milder and still fatal to the run: the pass raises and stops, which is how this was found.

**Reachable from ordinary automated trading.** The reducing order here was a plain cycle SELL
(`sim-R7GB2OIBDAQWPVTG`, its own group), not a manual close. Any basket whose panel takes partial
exits reaches it; the demo's `stub` panel never does, which is why hundreds of prior backtests and
the whole scenario suite have not.

**Closing it** means giving the monitor the position rather than the entry: resize or cancel a
group's legs whenever the holding it guards falls below `protected_qty`, from wherever the
reduction came. `Ledger` already emits `POSITION_UPDATED` on every fill, so the signal exists. The
awkward part is ownership — the monitor is deliberately venue-scoped and basket-agnostic, while
positions belong to the portfolio, so the resize has to be driven by the position rather than
polled per group. A rung-3 scenario driving an entry, a partial discretionary exit, and then a
bar through the original stop is the test this needs, and its absence is why the defect survived.


## 5. The monitor polls only inside a cycle that placed orders

**Where** — the module docstring of [execution/monitor.py](../tradebot/execution/monitor.py),
against [basket_runner.py:256](../tradebot/control/basket_runner.py#L256)

The docstring says the monitor polls the venue "**only while orders are actually open**, because a
polling storm against a venue is a rate-limit ban waiting to happen". That describes a loop which
does not exist. `poll()` has exactly two production callers:

- `BasketRunner._settle`, which returns *before* polling when the cycle placed no orders
- `BacktestHarness.run`

`settle()` — whose own docstring says "`--once` and the scenario tests use this instead of a
background task" — has **no** production caller at all. `run --once` goes through
`Supervisor.run_once` → `BasketWorker.cycle` → `BasketRunner._settle` like every other cycle, and
the only two callers of `settle()` are in `tests/unit/test_monitor.py`. `manual_close` submits and
tracks its order and never polls. `Supervisor.serve` runs `_check_drift` and `_sweep_portfolio` and
never touches the monitor; `PortfolioWatch` refreshes *marks*, not orders; `reconcile()` runs at
startup only (§1).

So everything the monitor owns advances only when some basket places an order. In `data/sim.db`
that is 41 orders across 196 cycles — the great majority of cycles poll nothing.

**What it costs.** A venue-held stop exists precisely because it fires *between* cycles
(ADR 0004). When it does, nothing books the fill until the next order-placing cycle, and until
then `Ledger.position` reports a holding that is gone:

- `risk.aggregate.aggregate` values a position that no longer exists, so equity and the drawdown
  baseline are wrong in whichever direction the market moved — ADR 0027's own argument, arriving
  through a different door.
- `_size_sell` sizes reduce-only from it and `LongOnlyRule` caps against it, so a SELL can be
  approved for quantity the venue no longer holds, and `Ledger._apply_sell` then refuses it as
  ledger corruption. That is §4's failure reached from the other side.
- TTL is bot-enforced, because Binance spot has no venue-side good-till-time. An order past its
  deadline keeps resting until something polls.

Gap 4's fix inherits this latency exactly: legs are resized at the next poll, so a manual close's
reduction is corrected promptly only if a basket happens to trade soon afterwards.

**To close it.** A monitor tick in `Supervisor.serve` beside `_check_drift` and `_sweep_portfolio`
— the loop that already exists for this class of between-cycle work — and a poll after
`manual_close` submits. Whether `settle()` is then deleted or wired is the same "the docstring or
the code" decision §1 ends on.

---

## 6. Nothing releases protective legs before a discretionary exit

**Where** — [manual_close.py:216](../tradebot/control/manual_close.py#L216) and
[basket_runner.py:293](../tradebot/control/basket_runner.py#L293)

Both submit a reducing SELL and then `monitor.track` it. Neither cancels the protective legs
already resting on that instrument, and nothing else does either.

On Binance spot a resting protective SELL **reserves the base asset** for as long as it rests.
`closable()` offers an operator the whole `position.qty`, so closing a fully protected position
asks the venue to sell coins its own stop is already holding, and the venue refuses for
insufficient balance. The better protected the position, the more certainly the exit is refused —
which inverts the intent of ADR 0015, where an operator exit is the one act the metering rules
stand aside for.

**Why no test and no soak has seen it.** `SimBroker` models the reservation
([sim.py:428](../tradebot/execution/brokers/sim.py#L428)) but never refuses an order for
insufficient free funds: `submit` rejects exactly one thing, a duplicate `client_order_id`, and
free balance is allowed to go negative. So the simulated venue cannot express this, and paper's
primary venue **is** `SimBroker` (ADR 0020). Derived from the venue's documented semantics rather
than observed — live has never run.

**Reach.** Live, and paper only when `--broker binance` puts orders at the testnet. Not sim.

**To close it.** Cancel the instrument's resting protective legs before submitting a discretionary
exit, then let the monitor re-arm whatever remains. The unprotected window between the two is real
and has to be argued for rather than discovered — though it is the same window `_replace_legs`
already opens on every resize, which is a precedent rather than an excuse.

---

## 7. The mismatch alert names no instrument and no quantity

**Where** — [ops/rules.py:112](../tradebot/ops/rules.py#L112)

```python
Alert(
    kind=AlertKind.RECON_MISMATCH,
    at=event.ts,
    scope=venue,
    title=f"Reconciliation mismatch on {venue or 'the venue'}",
    body=(
        "The ledger and the venue disagree and nothing explains the difference. Affected "
        "baskets are halted; above tolerance this trips the kill switch. The venue is the "
        "source of truth — do not resume until the difference is understood."
    ),
)
```

The body is a fixed string. The `RECONCILED` event written beside it carries the whole diff —
every line's `scope`, `ours`, `theirs`, derived `delta` and classification — and it is appended
unconditionally, clean or not ([reconciler.py:340](../tradebot/ledger/reconciler.py#L340)). None of
it reaches the message.

**What it costs.** `RECON_MISMATCH` is `Severity.HIGH`: webhook, Telegram, the dashboard bell. It
is the thing that wakes someone. It tells them the books are wrong and that baskets are halted, and
nothing about *which instrument* or *how much* — so the first act after being woken is always to
open a dashboard and find out whether this is dust or the whole portfolio. That is the distance
between an alert and a page.

**To close it.** Render the differences into the body, and **bound it**. The maintenance package
learned this one the expensive way (closed M1 above): a body reaches the event payload *and* the
notification, so an unbounded summary fails both paths at once. The full list belongs in a
`WARNING`.

---

## 8. The kill-switch reason carries the explained lines and no absolute figures

**Where** — [startup.py:225](../tradebot/control/startup.py#L225) →
[watchdog.py:190](../tradebot/risk/watchdog.py#L190), over
[reconciler.py:81](../tradebot/ledger/reconciler.py#L81)

```python
await self._watchdog.trip(report.classification.value, report.detail)
```

```python
@property
def detail(self) -> str:
    return "; ".join(f"{d.scope}: {d.detail}" for d in self.differences if d.detail)
```

`risk_state.reason` becomes `f"{rule}: {detail}"`. It is the durable record of why trading stopped,
and the sentence an operator reads before typing `RE-ARM TRADING`. Two things are wrong with it:

- **It joins the explained lines too.** The classifier chain is total — every branch returns a
  detail, including `DRIFT`'s "within tolerance" and `EXTERNAL_CHANGE`'s "unexplained increase" —
  and `_report` filters out only `MATCH`. So a trip caused by one shortfall reads as
  `mismatch: sim:BTC/USDT: unexplained difference of -0.1; USDT: 41000 within 0.5% drift
  tolerance`. That is §1's "`worst` ranges over explained lines" fault one layer up, in the
  sentence a human acts on rather than in the arithmetic behind it.
- **No line carries `ours` or `theirs`.** `unexplained difference of -0.5` does not say whether
  that is half a coin out of fifty or the entire holding, and which of those it is decides
  everything about the next hour.

**To close it.** Put `ours`, `theirs` and the delta on each rendered line, and narrow the join to
the lines that caused the classification — the same scoping §1 asks for in `worst`. Both are one
change in `Difference` / `ReconcileReport.detail`, and both want asserting rather than eyeballing:
there is currently no test over the text of the reason a human is asked to act on.

---

## Closed — the maintenance audit's four (2026-08-23)

All four were in `tradebot/maintenance/`, which never touches the money path; none could cause or
prevent a trade, and what they cost was the record. Each is now covered by a test that fails
against the old code, and the whole set was re-verified on a copy of the operator's `data/sim.db`.

**M1 — one archive that would not verify stopped every day behind it, permanently.** The day loop
sat inside one `try` whose handler returned, so the first bad day took every later day *and* the
`delete_aged` call with it. A day file that exists is verified rather than rewritten, so a corrupt
one failed on every pass forever: retention stopped entirely while the database kept growing.
Containment is now per day, as spec §6.4 always described it, and deletion runs regardless because
it is scoped by file name and depends on nothing the archive step did. Reproduced and re-checked on
the real database: with a corrupt file planted for the earliest of five pending days, the pass went
from `archived 0, compacted 0, deleted 0` to `archived 3, compacted 1351, deleted 1`, with the
corrupt day's payloads still in the database and its archive still not recreated on the next pass.
The failure summary on the report is bounded now, because it reaches the event payload *and* the
notification body and one permissions fault fails every pending day at once.

**M2 — `pending_days` scanned every heavy payload on the event loop.** Two unindexable
`LIKE '%...%'` predicates over `payload_json`, measured at 26 ms on the 8.3 MB sim database and
growing with the log, run synchronously inside `async def _pass` while the three filesystem steps
around it already hopped to a thread. It now takes the same hop (spec §6.3).

**M3 — a file that would not delete was reported as a failed pass.** `delete_aged`'s failure
strings were folded into `MaintenanceReport.failure`, which is exactly what `ok` means, so one
locked file turned a good night into a HIGH `MAINTENANCE_FAILED` — none of the four things spec
§5.4 names — and suppressed the LOW line carrying the day's real work. HIGH notices deliberately do
not supersede, so each night stacked another red row. They now ride on their own `undeletable`
field, are counted in the daily line as §6.4 asks, and appear on the failed body too so the fact is
not lost when a pass failed for an unrelated reason.

**M4 — `maintenance status` answered three of its six questions.** It now prints the windows in
force *and whether they came from a published document or the defaults*, the last pass with what it
did, and the archive inventory with its span, beside the backup and disk figures it already had. It
opens the database to do so, which `maintenance backup` beside it already did: `open_database` never
migrates, the reads write nothing, and the schema is WAL — so the command stays pointable at a file
another process has open, which is its whole premise.

**One observation from M2 was deliberately not acted on.** The scan re-reads already-compacted
payloads forever, because selecting on `heavy_key` rather than on the event type is what stops a
deleted archive being recreated (ADR 0028). A marker-column prefilter would narrow it, and the cost
grows with the log — but it is now off the event loop, which is what made it urgent. Worth knowing
about before the log is large.

## What was checked and found sound

So the boundary of these audits is legible rather than implied.

From the instrument-universe audit (§1-3):

- **`StartupSequence` keeping a boot snapshot is correct**, not a fourth instance of the frozen-
  universe defect. It completes DESIGN §8.2 before anything can be published, so the set it reads
  is the set that exists.
- **`_instrument()` still refuses an unknown key** in both venue adapters after the universe was
  made live, with the message naming the adapter. Fail-closed did not become fail-open.
- **`ReplayMarketData` still refuses a series it was not given.** That refusal is what keeps a
  backtest honest, and it is why the simulated venue's feed is a separate class rather than a
  relaxation of this one.

From the maintenance audit (whose four findings are now closed above as M1–M4):

- **The archive-then-compact ordering holds.** Nothing is compacted without a verified archive, on
  a real pass over `data/sim.db`: 4 days archived, 1,354 rows rewritten, payload 5.41 MB → 2.18 MB,
  and 2,403 → 2,405 events — no row deleted, the two added being the pass's own `MAINTENANCE_RAN`.
- **Compaction is idempotent and does not resurrect deleted archives.** A second pass over the same
  database reported 0/0/0 and did **not** recreate the three archive files the first pass had
  deleted — the failure mode `pending_days` selecting on `heavy_key` exists to prevent (ADR 0028).
- **The archive round trip is exact.** All 1,849 payloads in one day file re-serialise byte-identical
  to the canonical JSON in the pre-compaction copy, including the 96 rows holding non-ASCII text.
- **The pre-migration backup fires, and the data step is right.** A copy of `data/sim.db` at
  revision 0007 upgraded to 0009 wrote `sim-pre-0007-<stamp>.db` **before** touching the schema —
  named for the revision it was leaving — and carried `recorded_seq` up to the existing
  `last_seq` of 1234 rather than 0, so no installation re-records its whole log on the first poll
  after the upgrade.

## Related, already recorded

- **Soak evidence gathered before Phase 12 Piece 1 ran under a drawdown gate that could not see
  unrealized loss** (CLAUDE.md, ADR 0027). Whether those cycles still count is the operator's
  call, but it is a call.
- **The simulated venue's timeframes are independent walks**, so a 4h chart will not agree with
  fills priced off the 1h series. A stated limitation of a feed that has never claimed to be a
  market model, not a defect — recorded here only so a reader who notices it on the chart does not
  file it as one.
- **The maintenance package has no rung-3 scenario test.** Spec §8.9 asks for a simulated month —
  cycles running, the clock crossing midnight, a backup appearing, and past day 31 the drill-down
  showing the archive pointer instead of "No snapshot was frozen". Everything in
  `tradebot/maintenance/` is covered at rung 1, at 100%. A bug waiting for a free afternoon rather
  than a decision, which is why it is here and not in the table above — but the archive-containment
  defect closed above is exactly the kind a multi-day scenario would have caught and unit tests did
  not: the test asserting it had one pending day in its fixture, which cannot tell per-day
  containment from a per-pass give-up.
