# Known gaps

Defects found by audit, verified but **not** fixed. Each is written with what it costs, what was
actually observed, and what closing it would take — because each is a decision someone has to make
rather than a bug waiting for a free afternoon.

The house rule applies to reading this file too: **fail-closed is not the same as safe.** Most of
these resolve in the fail-closed direction and are still on this list, because a system that stops
doing something for a reason nobody can name is an incident.

Two audits so far:

- **§1–3, the instrument-universe seam** (2026-08-19). None is caused by the two fixes that shipped
  alongside them ([marketdata/synthetic.py](../tradebot/marketdata/synthetic.py), the live
  instrument universe); all three predate that work.
- **§4–7, the maintenance package** (2026-08-22), found while checking Phase 13 pieces A and B
  against their plans and spec. All four are in `tradebot/maintenance/`, which never touches the
  money path — none of them can cause or prevent a trade. What they cost is the record.

| # | Gap | Reaches | Direction |
|---|---|---|---|
| 1 | The mismatch kill threshold compares quantities to money, and sizes itself from explained lines | live · paper | **fails open and closed** |
| 2 | Retiring a basket that holds a position is unguarded | every mode | fails closed, and traps |
| 3 | The one-quote-currency rule is enforced only at boot | live · paper | fails open |
| 4 | One unverifiable archive stops all retention, permanently | every mode | fails closed, and wedges |
| 5 | `pending_days` scans every payload on the event loop | every mode | stalls, never wrong |
| 6 | An undeletable archive file is reported as a failed pass | every mode | over-alerts |
| 7 | `maintenance status` answers three of the six questions the spec gives it | every mode | silent omission |

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

## 4. One archive that will not verify stops every day behind it, for good

**Where** — [maintenance/service.py:156-167](../tradebot/maintenance/service.py#L156)

```python
try:
    for day in pending_days(self.store.engine, before=horizon):
        archived, compacted = await self._archive_then_compact(day, now, archived, compacted)
except (TradebotError, OSError) as exc:
    return MaintenanceReport(..., failure=f"archive: {exc}")
```

The whole day loop is inside one `try`, and the handler `return`s. So the first day whose archive
raises takes with it **every later day** *and* the `delete_aged` call on line 169, which never
runs. Spec §6.4 asks for something narrower: an archive that fails verification means "nothing
compacted **for that day**".

A day file is verified rather than rewritten when it already exists
([archive.py:84](../tradebot/maintenance/archive.py#L84)) — correct, and the reason the failure is
permanent. A truncated or corrupt file fails `_verify` on this pass, on the next, and on every
pass after, because nothing repairs it and nothing skips it.

**Observed.** A copy of `data/sim.db` with five pending days, a corrupt file planted for the
earliest, and one archive dated 2026-01-01 that is far past `archive_keep_days`:

```
pending days: ['2026-08-16', '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-21']
outcome : FAILED
archived: 0  compacted: 0  deleted: 0
later days archived despite the bad one: NONE
aged archive still present (should have been deleted): True
```

Two further forced passes: `ok=False archived=0 compacted=0 deleted=0`, twice, and the aged file
still there. **Retention stops entirely** — the database keeps growing, and the 90-day deletion
that makes OPERATIONS precondition 17 answerable silently stops happening.

**What it does *not* do.** Nothing is lost and nothing is wrongly deleted: the ordering that makes
compaction safe is intact, and a day that was not archived is a day that was not compacted. Since
Piece C it is also **not silent** — the pass records `outcome: failed` and the maintenance rule
raises a HIGH `MAINTENANCE_FAILED` notice naming the file
([ADR 0029](adr/0029-notifications-are-a-projection-of-the-alert-rules.md)). Before that it was one
`WARNING` in a log file. The operator now learns about it; the wedge is what remains.

**Why CI is green.** `test_an_unverifiable_archive_compacts_nothing_for_that_day`
([test_maintenance_service.py:220](../tests/unit/test_maintenance_service.py#L220)) has exactly one
pending day in its fixture, so per-day and per-pass containment are indistinguishable to it. The
test name asserts the narrower behaviour the spec describes; the code implements the wider one.

**To close it.** Move the `try` inside the loop, count the day as failed, and carry on — collecting
failures the way `delete_aged` already does rather than returning at the first. Then let the pass
reach `delete_aged` regardless, since deletion is scoped by file name and does not depend on
anything the archive step did. Add the second day to that test.

---

## 5. `pending_days` scans every heavy payload, on the event loop

**Where** — [maintenance/compaction.py:123-130](../tradebot/maintenance/compaction.py#L123), called
from [service.py:157](../tradebot/maintenance/service.py#L157)

```python
heavy = [
    and_(events.c.type == type_.value, events.c.payload_json.like(f'%"{c.heavy_key}"%'))
    for type_, c in COMPACTORS.items()
]
```

Two `LIKE '%...%'` predicates over `payload_json` — the largest column in the database, and the one
compaction exists because of. There is no index that can serve them, so every `SEAT_RESPONDED` and
`SNAPSHOT_FROZEN` payload is read, including the ones already compacted. Selecting on `heavy_key`
rather than on the event type is deliberate and correct
([ADR 0028](adr/0028-retention-is-archive-then-compact.md) — selecting by type recreates deleted
archives); the cost of that choice is this scan.

It is called **synchronously inside `async def _pass`**. Every other filesystem step in that method
was deliberately moved off the loop — `take_backup`, `archive_day` and `delete_aged` each go through
`asyncio.to_thread`, and the plan's own correction list says why (spec §6.3). This one was missed.

**Measured**, on the 8.3 MB sim database with 5.41 MB of payload: **26 ms**. Extrapolating linearly
on payload size, a year of continuously supervised sim at the observed ~23 MB/week is roughly 1.2 GB
and therefore **~6 seconds** — once a day, with the event loop held. Extrapolation, not measurement:
nobody has run this against a database that size.

**What that costs.** The maintenance task shares its loop with the supervisor, the execution monitor
and the dashboard's WebSocket. Six seconds is six seconds in which no open order is polled and no
pane refreshes. It cannot produce a wrong answer — but "the bot stopped responding for six seconds
every night at 04:00" is an incident report nobody will enjoy writing.

**To close it.** `await asyncio.to_thread(pending_days, self.store.engine, before=horizon)`. One
line, and it is the same hop the three calls around it already make. Worth considering separately:
the scan re-reads compacted payloads forever, which a marker column or a `payload_json NOT LIKE
'%"compacted"%'` prefilter would not fix cheaply — but it is a cost that grows with the log, so
it is worth knowing about before the log is large.

---

## 6. A file that will not delete is reported as a failed housekeeping pass

**Where** — [maintenance/service.py:169-181](../tradebot/maintenance/service.py#L169)

`delete_aged` returns `(removed, failures)` and does the right thing: a file it cannot unlink is
reported and skipped so one locked file does not stop the rest of the pass, and the next pass tries
again. Then `_pass` folds those strings straight into `MaintenanceReport.failure`, and `failure`
being non-empty is exactly what `ok` means:

```python
return MaintenanceReport(..., deleted_archives=len(removed), failure="; ".join(failures))
```

So a pass that backed up, archived and compacted everything correctly, and merely could not unlink
one file that a virus scanner had open, is recorded as `outcome: failed`.

Spec §6.4 puts an undeletable file in its own row — *"reported and skipped; the next pass retries;
counted in the daily line"* — and §5.4 lists what `MAINTENANCE_FAILED` is for: a failed backup, an
unverifiable archive, short disk headroom, an unclassified error. A locked file is none of them.

**Since Piece C this is visible rather than theoretical.** It now raises a HIGH
`MAINTENANCE_FAILED` notice, and suppresses the LOW `MAINTENANCE_OK` that would have carried the
count — so the operator gets an alarm instead of the line the spec wanted, and the day's real work
goes unreported. It also loses the supersession: HIGH notices deliberately do not supersede, so
each night's locked file stacks another red row.

**To close it.** Carry the deletion failures on the report as their own field, render them in the
`MAINTENANCE_OK` body next to the deleted count, and leave `failure` for the things §5.4 names.

---

## 7. `maintenance status` answers three of the six questions the spec gives it

**Where** — [__main__.py:934](../tradebot/__main__.py#L934)

Spec §7: *"`status` prints the windows in force and where they came from, the last backup, the last
compaction, the archive inventory and free disk."* It prints the database size, the backup count and
newest, free bytes, and what the next backup needs. Missing:

- **the windows in force, and whether they came from a published document or the defaults** — which
  is the setting that governs how long financial records are kept, and the substance of OPERATIONS
  precondition 17;
- **the last compaction** — that is, whether the daily pass is running at all. A dead tick and a
  healthy one look identical here;
- **the archive inventory** — how many day files exist and what they span, which is the only place
  to see what deletion has already taken.

Verified by running it against a copy of `data/sim.db`. Nothing about this is unimplementable: the
command deliberately builds no `Application` so it can be pointed at a database another process has
open, but `open_database` already gives it a read-only route to the `maintenance` config document
and the `MAINTENANCE_RAN` events, and the archive directory is a `glob`.

**What it costs.** The one command an operator runs to answer "is housekeeping healthy" cannot
answer it. Combined with §4, that is the sharp edge: retention can be wedged for weeks, and the
command whose name suggests it would say so does not look.

---

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

From the maintenance audit (§4–7):

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
  than a decision, which is why it is here and not in the table above — but §4 is exactly the kind
  of defect a multi-day scenario would have caught and unit tests did not.
