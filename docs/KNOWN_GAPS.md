# Known gaps

Defects found while fixing the simulated feed and the frozen instrument universe (2026-08-19),
verified but **not** fixed. Each one is a decision someone has to make, not a bug waiting for a
free afternoon — so each is written with what it costs, what was actually observed, and what
closing it would take.

None of these is caused by the two fixes that shipped alongside them
([marketdata/synthetic.py](../tradebot/marketdata/synthetic.py), the live instrument universe).
All three predate that work and would have been found by any audit of the same seam.

The house rule applies to reading this file too: **fail-closed is not the same as safe.** Two of
these three resolve in the fail-closed direction, and are still on this list, because a system that
stops trading for a reason nobody can name is an incident.

| # | Gap | Reaches | Direction |
|---|---|---|---|
| 1 | The mismatch kill threshold compares quantities to money, and sizes itself from explained lines | live · paper | **fails open and closed** |
| 2 | Retiring a basket that holds a position is unguarded | every mode | fails closed, and traps |
| 3 | The one-quote-currency rule is enforced only at boot | live · paper | fails open |

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

## What was checked and found sound

So the boundary of this audit is legible rather than implied:

- **`StartupSequence` keeping a boot snapshot is correct**, not a fourth instance of the frozen-
  universe defect. It completes DESIGN §8.2 before anything can be published, so the set it reads
  is the set that exists.
- **`_instrument()` still refuses an unknown key** in both venue adapters after the universe was
  made live, with the message naming the adapter. Fail-closed did not become fail-open.
- **`ReplayMarketData` still refuses a series it was not given.** That refusal is what keeps a
  backtest honest, and it is why the simulated venue's feed is a separate class rather than a
  relaxation of this one.

## Related, already recorded

- **Soak evidence gathered before Phase 12 Piece 1 ran under a drawdown gate that could not see
  unrealized loss** (CLAUDE.md, ADR 0027). Whether those cycles still count is the operator's
  call, but it is a call.
- **The simulated venue's timeframes are independent walks**, so a 4h chart will not agree with
  fills priced off the 1h series. A stated limitation of a feed that has never claimed to be a
  market model, not a defect — recorded here only so a reader who notices it on the chart does not
  file it as one.
