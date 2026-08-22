# Reading the Log

Exact shapes and working recipes. **Every schema below is verified against the code** — do not
guess a column or a JSON shape, because the obvious guesses here are wrong.

## 1. Take a consistent copy first

Never query `data/{mode}.db` in place while a process owns it. The backup API is atomic and
read-only, so it is safe against a running bot and cannot produce a torn copy the way a plain
file copy of a WAL database can:

```python
import sqlite3

src = sqlite3.connect("file:data/sim.db?mode=ro", uri=True)
dst = sqlite3.connect(r"<SCRATCHPAD>/sim_copy.db")
src.backup(dst)
dst.close()
src.close()
```

Work only on the copy. Never write to it and then present the result as production state.

To see what the operator sees, run the real dashboard against a **private copy** — it never
touches their data:

```bash
mkdir -p <SCRATCHPAD>/dd && cp <SCRATCHPAD>/sim_copy.db <SCRATCHPAD>/dd/sim.db
TRADEBOT_DASHBOARD_TOKEN="0123456789abcdefghij" .venv/Scripts/python.exe -m tradebot serve \
    --mode sim --observe --data-dir <SCRATCHPAD>/dd --port 8899
```

Log in with `POST /login` (field `token`, keep the cookie jar), then GET the page. Stop the
server when done.

## 2. Schemas — the ones that get guessed wrong

`config_versions` uses **`kind` / `config_id`**, *not* `scope` / `key`:

```
config_versions: kind, config_id, version, document_json, retired, actor, note, created_at
```

The rest:

```
events:        seq, event_id, ts, type, aggregate_id, basket_id, cycle_id, payload_json
cycles:        cycle_id, basket_id, started_at, completed_at, outcome, snapshot_id,
               snapshot_digest, cost_usd, config_versions_json
decisions:     cycle_id, instrument_key, action, conviction, size_hint, reasoning_summary,
               dissent_json, flags_json, decided_at
orders:        client_order_id, basket_id, cycle_id, instrument_key, side, order_type, qty,
               limit_price, state, venue_order_id, filled_qty, avg_fill_price, created_at,
               updated_at, role, group_id, stop_price, expires_at
fills:         fill_id, client_order_id, instrument_key, side, qty, price, fee, fee_currency,
               filled_at
positions:     instrument_key, qty, avg_entry, realized_pnl, updated_at, opened_at
basket_status: basket_id, status, reason, updated_at
risk_state:    scope, kill_switch, reason, high_water_mark, day_start_equity, day_started_on,
               updated_at
```

**Money columns are TEXT.** Never `SUM` one in SQL — SQLite's numeric affinity rounds through an
IEEE-754 double. Total in Python with `Decimal`.

## 3. The one query that answers most questions

Write it to a file and run it, rather than fighting shell quoting:

```python
import sqlite3, json

CID = "<cycle-id>"
db = sqlite3.connect(r"<SCRATCHPAD>/sim_copy.db")
db.row_factory = sqlite3.Row

row = db.execute(
    "SELECT basket_id, outcome, started_at FROM cycles WHERE cycle_id=?", (CID,)
).fetchone()
print("CYCLE:", dict(row) if row else "no such cycle")

for d in db.execute(
    "SELECT instrument_key, action, conviction, size_hint FROM decisions WHERE cycle_id=?", (CID,)
):
    print("DECISION:", dict(d))

for (payload,) in db.execute(
    "SELECT payload_json FROM events WHERE cycle_id=? AND type='RISK_CHECKED' ORDER BY seq", (CID,)
):
    p = json.loads(payload)
    print(f"RISK {p['instrument_key']} approved={p['approved']}")
    for c in p["checks"]:
        print(
            f"   {c['decision']:<9} {c['rule']:<24} "
            f"limit={c['limit']} observed={c['observed']} | {c['detail']}"
        )

for o in db.execute(
    "SELECT instrument_key, side, role, qty, state FROM orders WHERE cycle_id=?", (CID,)
):
    print("ORDER:", dict(o))
```

Two aggregates worth knowing — which rules actually stop a basket, and its outcome mix:

```python
# every veto rule across one basket
rows = db.execute(
    "SELECT e.payload_json FROM events e JOIN cycles c ON c.cycle_id = e.cycle_id "
    "WHERE c.basket_id = ? AND e.type = 'RISK_CHECKED'",
    (basket,),
)
# then count checks whose decision == "veto"

db.execute("SELECT outcome, COUNT(*) FROM cycles WHERE basket_id=? GROUP BY outcome", (basket,))
```

## 4. Event types

`SEAT_RESPONDED` · `DECISION_MADE` · `RISK_CHECKED` · `CYCLE_STARTED` · `CYCLE_COMPLETED` ·
`SNAPSHOT_FROZEN` · `ORDER_SUBMITTED` · `ORDER_STATE_CHANGED` · `FILL_RECEIVED` ·
`PROTECTIVE_PLACED` · `POSITION_UPDATED` · `RECONCILED` · `RISK_EVENT` · `KILL_SWITCH_CHANGED` ·
`CONFIG_CHANGED` · `ROUND_TRIP_CLOSED` · `SHADOW_EVALUATED`

A `SEAT_RESPONDED` keeps its instrument at `payload["response"]["instrument_key"]`;
`RISK_CHECKED` keeps its own at the payload top level.

## 5. Metric glossary

| Term | Meaning |
|---|---|
| `conviction` | panel consensus strength, **0–1**. Compared against `min_conviction` |
| `size_hint` | `none`/`quarter`/`half`/`full` — a fraction of the *risk-allowed* maximum, never an absolute size |
| `risk_amount` | currency at risk on the trade: equity × `risk_per_trade_pct` |
| `stop_distance` | ATR × `stop_loss_atr_multiple`. Quantity ≈ `risk_amount / stop_distance` |
| `headroom` | budget remaining before a cap binds. `<= 0` is the veto condition |
| `limit` / `observed` | the rule's threshold, and the actual value it compared |
| `max_qty` | the cap that rule imposes; the engine composes all of them with `min()` |
| `high_water_mark` | peak equity the drawdown switch measures from |
| `day_start_equity` | baseline for the daily loss limit; a valuation freeze leaves it at yesterday's |
| `PANEL_HOMOGENEOUS` | seats agreed too closely to count as independent opinions |
| `PANEL_DEGRADED` | seats unreachable or unparseable; the cycle resolves `WAIT` |

**Equity is mark-to-market in the notional currency** (ADR 0027), read from `ledger.marks.Marks`
— never cost basis. `Ledger.equity` does not exist. A stale mark yields no mark at all, and the
fallback is a freeze rather than cost.

## 6. Order states

`pending_submit` · `submitted` · `submit_unknown` · `open` · `partially_filled` · `filled` ·
`cancelled` · `expired` · `rejected` · `failed`

- **`submit_unknown` is ambiguous placement, not failure** — the only state meaning "we do not
  know whether the venue got it". A rejection, a 429 and a ban are each a definite *no*.
- **`open` protective legs are correct, not stuck.** Stop-loss and take-profit rest at the venue
  until triggered (ADR 0004). An entry `filled` with two `open` legs beside it is a healthy
  position, not an incomplete one.

## 7. Sim-specific: "this instrument never trades"

`sim_markets.json` is a **real Binance capture**, so each symbol carries the lot size calibrated
to its *real-world* price, while `SyntheticMarketData` prices everything on one similar scale.
Cheap-in-reality assets are then untradable in sim: `XRP/USDT` has `lot_size` `0.1` (real XRP
≈ 0.50 USD) but is quoted around 1500 in sim, so ATR sizing rounds to zero and
`venue_quantization` vetoes it every cycle. `LTC` (`0.001`) and `BTC` (`0.00001`) round fine.

The file's shape — **`markets` is a list of dicts**, not a dict keyed by symbol:

```python
import json

d = json.load(open("tradebot/marketdata/sim_markets.json"))
for e in d["markets"]:
    if e["symbol"] in ("XRP/USDT", "LTC/USDT"):
        print(e["symbol"], "lot=", e["lot_size"], "min_notional=", e["min_notional"])
```

This is sim realism, not a defect — the veto is correct, since a zero-quantity order must never
be sent. But it narrows a soak's real coverage below what its configuration implies, which is
worth saying when reporting it.

## 8. Extracting a rendered dashboard section

Match the **heading tag**, not the bare words — the page's own prose names the sections, so
searching for the words lands on explanatory text rather than the table:

```python
i = page.find("<h2>Risk checks</h2>")  # not page.find("Risk checks")
j = page.find("<h2>Frozen snapshot</h2>")
```
