# Operations — running the bot, and running it against real money

> **§§1–3 are for the person who arms live trading.** That person is **you**, never me. Every
> command below is one a human types deliberately; nothing in this repository arms itself, and no
> default, env var, or typo reaches a live venue ([ADR 0012](adr/0012-live-is-four-independent-preconditions.md),
> [ADR 0020](adr/0020-live-is-the-paper-wiring-minus-headroom.md)).

Four sections, in the order you will need them: [running the bot](#0-running-the-bot) in any mode,
[before you arm](#1-before-you-arm), [the arming procedure](#2-the-arming-procedure), and
[the incident runbook](#3-incident-runbook). **§0 applies to sim and paper as much as to live**;
everything from §1 onward is live only.

Live mode is **Binance spot only** in v1. There is no equity market-data provider, so an Alpaca
basket refuses to wire — in live exactly as in paper, with the same message.

---

## 0. Running the bot

### 0.1 The three modes

`--mode` is required and has no default — no environment variable, config default or typo selects
one. Each mode writes its **own** database (`data\sim.db`, `data\paper.db`, `data\live.db`) and
shares nothing with the others: not the ledger, not the risk state, not the live arming row.

| Mode | Prices | Fills | Money at risk | Needs a key |
|---|---|---|---|---|
| `sim` | synthetic, generated locally | `SimBroker` | none | no |
| `paper` | **real** Binance spot, public REST | `SimBroker` | none | no |
| `live` | real Binance spot | **the venue** | **real** | yes — live-only variable names |

`sim` refuses `--broker binance` and `--broker alpaca`: a simulation that reaches a venue is
neither offline nor reproducible. `live` refuses `--broker sim` for the mirror reason — an order
not sent to a venue is not a live order. Paper's default `--broker sim` is the soak's evidence
base; `--broker binance` there points the real adapter at the venue's **testnet** and is an
adapter integration check, reported but never counted as evidence (R15).

### 0.2 Starting it — two commands

```powershell
# one cycle per basket, then exit — what you use to check a change works
.venv\Scripts\python.exe -m tradebot run --mode sim --once

# every basket on its own schedule, until Ctrl-C
.venv\Scripts\python.exe -m tradebot run --mode sim

# the dashboard, with the same supervisor behind it — the normal way to operate
$env:TRADEBOT_DASHBOARD_TOKEN = "at-least-sixteen-characters"
.venv\Scripts\python.exe -m tradebot serve --mode sim
```

`serve` **refuses to start without `TRADEBOT_DASHBOARD_TOKEN`**, in every mode, including sim on
localhost ([ADR 0014](adr/0014-the-dashboard-is-vendored-and-always-authenticated.md)). It binds
`127.0.0.1:8000`; `--host` and `--port` move it, and a non-loopback address needs `--allow-remote`
on top of the token. Open <http://127.0.0.1:8000>, paste the token on the login page — that is the
whole login. Rotating the token and restarting invalidates every session.

Ctrl-C is the normal way out of both. It cancels nothing at the venue: orders left working are
recovered by the next start's startup sequence, not abandoned.

### 0.3 A first run, in order

1. **Sim, one cycle.** `run --mode sim --once`. Free, offline, and the scripted `stub` panel — no
   key, no egress, no cost. A fresh database seeds a two-instrument demo basket so there is
   something to cycle.
2. **Sim, with the dashboard.** `serve --mode sim`. Configure a basket, watch a cycle land in the
   blotter, try Stop and Start. Everything you will do in live, you do here first.
3. **Paper, one cycle.** `run --mode paper --once`. Same code, real Binance prices, simulated
   fills. Still no key and still no money.
4. **The soak.** `serve --mode paper`, left running for weeks, on the default `--broker sim`.
   Those are the cycles `report promotion` counts — it counts by the venue stamped on each cycle,
   so a testnet run mixed into the same database is reported and then excluded.
5. **Then, and only then, §1.** `report promotion --mode paper` writes a report to `reports\`;
   read it, then work through [before you arm](#1-before-you-arm).

Live is not step 6 of this list — it is [§1](#1-before-you-arm) and [§2](#2-the-arming-procedure),
and it starts with a signature on a promotion report.

### 0.4 The flags you will actually type

| Flag | On | What it does |
|---|---|---|
| `--mode sim\|paper\|live` | every command | required; there is no default |
| `--once` | `run` | one cycle per basket, then exit. Non-zero if a cycle failed, or if a halted basket never cycled at all |
| `--observe` | `serve` | dashboard up, **nothing cycling**. The state it starts in, not a lock |
| `--broker sim\|binance\|alpaca` | `run`, `serve` | which venue takes the orders; defaults to `sim` |
| `--panel stub\|free\|local` | `run`, `serve` | seeds a **fresh** database only. A stored basket carries its own panel and this flag is ignored — edit it in Settings |
| `--news <source>` | `run`, `serve` | repeatable: `cointelegraph`, `coindesk`, `sec_press` |
| `--host` `--port` `--allow-remote` | `serve` | where the dashboard binds |
| `--data-dir` | every command | where `{mode}.db` lives; defaults to `data\` |
| `--confirm "<phrase>"` | `run`, `serve` | the live phrase; live only, and typed on every invocation |
| `--verbose` | every command | debug logging |

Three things are **off unless asked for**, and all three are deliberate:

* **News** reaches the public internet, so it is off until `--news` names a source.
* **The panel** is the offline `stub`. `--panel free` needs `OPENROUTER_API_KEY`; `--panel local`
  needs a local runtime (LM Studio) and no key at all.
* **Alerting** is on exactly when a destination is configured — there is no flag to forget. Optional
  in sim and paper, mandatory in live ([§1.3](#13-the-environment)).

### 0.5 The dashboard, page by page

| Page | Path | What it is for |
|---|---|---|
| **View** | `/` | the workspace: portfolio, blotter, chart, log, the control dock and the risk pane. The only screen the bot is run from |
| **Parameters** | `/configure/risk` | the Tier-2 global limits |
| **Settings** | `/configure` | baskets, instruments, panels, schedules, quarantine |
| **Analytics** | `/cycles`, `/analytics/portfolio`, `/risk`, `/costs` | the cycle drill-down, round trips and the realized equity curve, risk history, model spend |

Selecting a row is a navigation (`/?scope=basket:demo`), so a reload or a bookmark lands on the
same view. Every act — Start, Stop, pause a basket, quarantine, un-halt, kill switch, manual close
— is on the dock, and every one of them appears in the event log with `dashboard` as the actor.

**Editing configuration is the dashboard's job.** The CLI's `config` subcommands only read: an edit
belongs where it is validated against the engine's own models and recorded with an actor.

### 0.6 Looking without trading

```powershell
.venv\Scripts\python.exe -m tradebot serve --mode sim --observe      # dashboard up, nothing cycles
.venv\Scripts\python.exe -m tradebot risk status --mode sim          # state, arming, limits in force
.venv\Scripts\python.exe -m tradebot config list --mode sim          # what is in service, at which version
.venv\Scripts\python.exe -m tradebot config history basket demo --mode sim
```

`--observe` is what you want after an incident: the log, the ledger and the reason are all readable
and nothing trades until someone clicks Start.

### 0.7 Stopping — four different things

| Act | Where | What it does | Cleared by |
|---|---|---|---|
| **Ctrl-C** | terminal | ends the process. Cancels nothing at the venue | starting again |
| **Stop** | the dock | pauses cycling. Cancels nothing, needs no phrase, never refused. It *does* end the polling of open orders, so **no order may be placed while stopped** — manual close included | **Start** (the phrase again, in live) |
| **Kill switch** | the dock | halts every basket and cancels working orders. Positions are **not** flattened | `risk rearm --confirm "RE-ARM TRADING"` |
| **Disarm** | the dock, or `risk disarm-live` | prevents the next live start. From the dashboard it also stops supervision | arming again ([§2.1](#21-record-the-arming-decision)) |

Stop is not the kill switch, and neither is a substitute for the other. If you need to close a
position from here, do **not** reach for Stop first — see [§3.8](#38-getting-out-entirely).

---

## 1. Before you arm

Nothing here is checked for you by the system unless the "asserted" column says so. The items that
are asserted are still listed, because a control you have not personally confirmed is a control you
are trusting a checkbox for.

### 1.1 The evidence

| # | Requirement | Asserted? |
|---|---|---|
| 1 | A promotion report over the paper soak that **passes**: ≥ 200 cycles, zero unhandled incidents, every reconciliation clean | you read it |
| 2 | The soak ran on live market data with `SimBroker` — the evidence base, not testnet fills | report states the venue |
| 3 | You have signed off on the report and filed it | you |

```powershell
.venv\Scripts\python.exe -m tradebot report promotion --mode paper   # exit 5 if a gate fails
```

The report is written to `reports/`, not printed, so it can be attached to the decision it
justified. A backtest is **not** evidence here — it never was; see the banner it prints and
[ADR 0017](adr/0017-a-backtest-declares-its-warm-up-and-its-contamination.md).

### 1.2 The exchange account

| # | Requirement | Asserted? |
|---|---|---|
| 4 | API key is **trade-only**; withdrawals **disabled at the exchange** | **yes** — startup refuses if Binance reports withdrawals enabled (PLAN §3.2) |
| 5 | API key is IP-allowlisted to the machine that will run this | no — do it at the venue |
| 6 | The account holds only what you are prepared to lose | no |
| 7 | Spot only. No margin, no futures, no derivatives | partially — the adapter only speaks spot |
| 8 | System clock is synchronised (NTP) | **yes** — warns past 2 s, refuses past 30 s |

Withdrawal permission is re-asserted **every boot**, not once at setup. Trusting a checkbox you set
months ago is not a control.

### 1.3 The environment

Live reads *differently named* variables from every other mode. This is deliberate: a live key on a
machine running paper is not merely unused, it is unreachable ([credentials.py](../tradebot/venues/credentials.py)).

```powershell
$env:BINANCE_API_KEY    = "..."          # live only — paper reads BINANCE_TESTNET_API_KEY
$env:BINANCE_API_SECRET = "..."

# Alerting is mandatory in live. Configure at least one destination, or the process refuses.
$env:TRADEBOT_ALERT_WEBHOOK_URL  = "https://hooks.example/..."
$env:TRADEBOT_TELEGRAM_BOT_TOKEN = "..."   # both of these, or neither
$env:TRADEBOT_TELEGRAM_CHAT_ID   = "..."

$env:TRADEBOT_DASHBOARD_TOKEN = "at-least-sixteen-characters"   # if you will serve the dashboard
```

Keys live in the environment or an OS keyring. Never in the database, never in a log, never in a
prompt — enforced by the redaction filter and its test.

### 1.4 The configuration

| # | Requirement | Asserted? |
|---|---|---|
| 9 | The basket's panel uses **real** providers — no stub binding, not even as a fallback | **yes** — live readiness refuses |
| 9a | Every provider the panel declares has its `secret_ref` key set in the environment | **yes** — live readiness refuses, and so does every Start ([ADR 0023](adr/0023-a-missing-provider-key-degrades-the-panel.md)) |
| 10 | Every seat can actually reach a model right now | **yes** — a real 16-token probe per seat at startup |
| 11 | Market data arrives fresh, deep enough, and **without gaps** | **yes** — live readiness refuses |
| 12 | Every stored basket builds: secrets present, indicators known, Tier-2 policy published | **yes** — built through the real factory at startup |
| 13 | Every instrument belongs to the venue this process is wired to | **yes** — live readiness refuses |
| 14 | Tier-1 limits reviewed for a real account (position size, cooldown, daily trade cap, SL/TP) | no — your call, in the dashboard |

A **fresh** live database seeds a two-instrument demo basket on the `sim` venue. That is a
placeholder, not a suggestion: publish your real basket in the dashboard first, or readiness will
refuse it by name. Item 9 has the same shape — the default panel is the offline stub, which must
never be what decides a live order.

Item 9a is the one gate that behaves *differently* by mode, and deliberately. In sim and paper a
missing key leaves that endpoint unwired: the seats bound to it fall back, a seat with nothing left
abstains, and the cycle resolves `WAIT` — the process runs, warns on every dashboard page, and can
be fixed from the GUI. Live never does that, because a panel deciding with fewer voices than it was
configured with would be deciding about real positions. Note what the fix *is*: set the named
variable and restart, or edit the panel so no seat binds that provider. The dashboard cannot take
an API key — keys are environment-only, and a field that accepted one would put a credential in the
database.

### 1.5 Jurisdiction — yours alone

| # | Requirement | Asserted? |
|---|---|---|
| 15 | Automated trading of these instruments is permitted for you, in your jurisdiction | **never** — the bot cannot infer this |
| 16 | You accept each venue's terms for automated access | never |
| 17 | Your tax and record-keeping requirements are known, and the event log's retention is set to match | never |

The append-only event log is the compliance artifact: for any order it can show the data seen, the
deliberation, the risk decision, and the venue's response (PLAN §3.3).

---

## 2. The arming procedure

Five things must be true at once. Four are yours; the fifth the system checks for itself.

### 2.1 Record the arming decision

The arming row lives in the **live database** (`data/live.db`), which paper and sim never share. It
survives a reboot, records who set it, and carries the per-order cap. Arm it from the CLI, or from
the dashboard's Control page (§2.5) — the two write the same row and differ only in the recorded
actor.

```powershell
.venv\Scripts\python.exe -m tradebot risk arm-live --mode live `
    --max-notional 50 `
    --confirm "I ACCEPT REAL MONEY RISK" `
    --note "first live week, BTC only, signed off against reports/promotion-....md"
```

`--max-notional` is the largest notional **one order** may carry, in the account's quote currency.
Start small enough that a total loss of one position is uninteresting. There is no "unlimited": a
missing or non-positive cap refuses.

Confirm it took:

```powershell
.venv\Scripts\python.exe -m tradebot risk status --mode live
```

That prints three things — the safety state, the arming row, and **the Tier-2 limits actually in
force**, including anything the live ceiling clamped.

### 2.2 Start it

```powershell
.venv\Scripts\python.exe -m tradebot run --mode live --broker binance --panel free `
    --confirm "I ACCEPT REAL MONEY RISK" --once
```

Then, once a single cycle has behaved:

```powershell
.venv\Scripts\python.exe -m tradebot serve --mode live --broker binance `
    --confirm "I ACCEPT REAL MONEY RISK"
```

The confirmation phrase is typed on **every** invocation. It is transient by design — an armed
database alone must not be enough to start.

`--broker sim` refuses in live: an order not sent to a venue is not a live order.

`serve --mode live` **no longer refuses when unarmed**. It comes up showing "not armed / not
trading", so it can be armed and started from the dashboard (§2.5). `run --mode live` is
unchanged and still refuses immediately: there is no GUI for an unarmed headless process to be
armed from, and an idle unusable process is worse than a refusal.

The one precondition that must still hold before `serve` starts at all is **credentials**: a venue
transport cannot be constructed without a key, and no dashboard could supply one, since keys are
environment-only.

### 2.3 What live enforces that paper does not

Tier-2 limits are clamped to a ceiling that can only tighten — `min(published, ceiling)`, so a
policy you have already tightened keeps its own number ([live.py](../tradebot/control/live.py)):

| Limit | Seed default | Live ceiling |
|---|---|---|
| Max gross exposure | 80% | **20%** |
| Max single-instrument exposure | 20% | **5%** |
| Max correlated-cluster exposure | 40% | **10%** |
| Price collar | ±5% | **±2%** |
| Orders per hour | 20 | **5** |
| Max daily loss | 3% | **1%** |
| Max drawdown (kill switch) | 10% | **5%** |
| Stablecoin peg tolerance | 2% | **1%** |
| Max order notional | uncapped | **your arming row** |

Widening past the ceiling is a source change, reviewed and released — not something a dashboard
edit can do at 03:00. Every clamp is logged and written to the event log as a `RISK_EVENT`
(`rule="live_ceiling"`).

### 2.4 Disarming

```powershell
.venv\Scripts\python.exe -m tradebot risk disarm-live --mode live --reason "week one over"
```

Disarming **from the CLI** does not stop a running process — stop that yourself. It prevents the
**next** start. Disarming **from the dashboard** also stops supervision, because it is issued from
inside the process that might be trading; leaving a basket cycling against a cap that was just
revoked is the one silent state this must never produce. The divergence is deliberate.

### 2.5 The same procedure from the dashboard

Everything above is on the Control page, and nothing about it is weaker there. The phrase is typed
into the Arm form *and* again into the Start form, never remembered between them and never held in
the session.

1. `serve --mode live --broker binance` — the dashboard comes up, "not armed", nothing cycling.
   Any unmet precondition is listed under **Supervision** before you click anything.
2. **Arm live trading** — a per-order cap and the phrase. Writes the same row `risk arm-live`
   writes, with `dashboard` as the actor.
3. **Start trading** — the phrase again. All four facts are re-checked at this exact moment;
   runners are built here, so the cap they enforce is the one on the row right now.
4. **Stop trading** — pauses cycling, the equivalent of `--observe`. It cancels nothing at the
   venue and needs no phrase. What it *does* stop is the polling of open orders, so any order still
   working is listed on the page while stopped, and no new order may be placed — including a manual
   close. Start again first, or use the kill switch if the orders must be cancelled.

Start is refused, with the reason on the page, while the kill switch is tripped or startup recovery
halted the process. Neither is cleared by starting; both are cleared the way they always were.

**Permission is not readiness.** The readiness gates — alerting configured, panel reachable, market
data complete, every configuration building — run once at startup, before the dashboard listens,
and are not re-run from the GUI. An operator who fixes one of them restarts the process.

---

## 3. Incident runbook

### 3.0 The three facts to establish first

1. **What does the venue say?** The venue is the source of truth; the local ledger is a projection.
   Open the exchange UI before believing anything here.
2. **Is the process still up?** A halted process is the *designed* outcome of most failures. It
   stays up, does not trade, and can be asked why.
3. **Are positions protected?** Entries carry venue-held stop legs, which survive this process
   dying. Check them at the venue, not here.

Read the system without letting it trade:

```powershell
.venv\Scripts\python.exe -m tradebot serve --mode live --observe
.venv\Scripts\python.exe -m tradebot risk status --mode live
```

`--observe` is the state it *starts* in, not a lock: it can be started from the Control page (with
the phrase, in live) once you have decided it should trade again. If it must stay stopped, do not
click Start — nothing else can.

Exit codes: `1` refused to start · `2` misuse · `3` recovery halted, nothing trades · `4` a `--once`
cycle failed · `5` a promotion gate failed.

### 3.1 Kill switch tripped

**Alert:** `🚨 KILL SWITCH TRIPPED`. **Meaning:** drawdown breach, a reconciliation mismatch above
tolerance, or a manual click. All runners halted, working orders cancelled. Positions are **not**
flattened — `flatten_on_kill` is false by default, because flattening into a broken market is
often worse, and that call is yours.

1. Read the reason: `risk status --mode live`, and the `KILL_SWITCH_CHANGED` event in the log.
2. Decide about the open positions **at the venue**. Their stop legs are still live.
3. Only when you understand the cause:

```powershell
.venv\Scripts\python.exe -m tradebot risk rearm --mode live --confirm "RE-ARM TRADING"
```

Re-arming resets the high-water mark to current equity. Do not re-arm to make an alert stop.

### 3.2 Basket halted

**Alert:** `🚨 Basket … halted`. **Meaning:** three consecutive cycle failures, an unresolved
`SUBMIT_UNKNOWN`, or a reconciliation problem scoped to that basket.

```powershell
.venv\Scripts\python.exe -m tradebot risk unhalt <basket_id> --mode live --confirm "RE-ARM TRADING"
```

Find the cause first: the halt reason is on the `BASKET_STATUS_CHANGED` event, and the failing
cycles are in the dashboard's drill-down. A halt cleared without a diagnosis re-halts.

### 3.3 Reconciliation mismatch

**Alert:** `🚨 Reconciliation mismatch`. **Meaning:** the ledger and the venue disagree and nothing
— fees, dust, funding, a corporate action, an external deposit — explains it.

**Do not resume.** Above tolerance this has already tripped the kill switch. Reconcile by hand
against the venue's trade history, and treat the venue's numbers as correct. A manual trade you
made yourself in the exchange UI shows up as `EXTERNAL_CHANGE` and is absorbed automatically; if
you see a mismatch instead, something else happened.

### 3.4 An order vanished (`SUBMIT_UNKNOWN`)

The system **never** blindly resubmits — there is no code path that can. It queries the venue by
our own `client_order_id` and adopts what it finds. If the window closes with nothing found, the
basket halts for human review. Search the venue for the id from the `ORDER_SUBMITTED` event before
doing anything by hand.

### 3.5 Repeated provider failure

**Alert:** `🚨 Panel degraded for 3 cycles running`. Nothing traded on those cycles — a degraded
panel resolves to `WAIT`. Not urgent for open positions; their stops are at the venue. Usual cause
is a free model slot that disappeared (R11). Check each seat's fallback chain in the dashboard's
panel editor.

### 3.6 Market data stale or holed

**Alert:** `🚨 Market data stale or holed for 3 cycles running`. Cycles are aborting before the
panel. The distinction that matters: open positions remain protected by their venue-held legs, but
nothing will be entered **or exited** until data flows again. If you want out while the feed is
down, close the position yourself — through the dashboard's manual close, which goes through the
same risk and execution path, or at the venue.

### 3.7 IP banned (HTTP 418)

Trips the kill switch immediately and latches the limiter. **Stop the process.** Continuing to call
a banned IP extends the ban. Wait it out; do not restart into it.

### 3.8 Getting out entirely

1. Trip the kill switch from the dashboard — halts everything, cancels working orders.
2. Close positions through the dashboard's manual close (same Tier-1/Tier-2 path, no side doors),
   or at the venue if this process cannot reach it.
3. Disarm — from the dashboard, which also stops supervision, or
   `risk disarm-live --mode live --reason "..."` so nothing starts again by habit.
4. Revoke the API key at the exchange if the cause is not understood.

Do **not** reach for Stop first if you intend to close a position from here: stopping ends the
polling of open orders, so a manual close is refused while stopped. Kill switch, close, *then*
disarm.

---

## 4. Things that are true and easy to forget

* **Each mode has its own database.** `data/live.db` is not `data/paper.db`. A paper ledger can
  never be read as a live one, and the arming row belongs to live's database alone.
* **A restart never un-halts anything.** Kill switch, halted baskets, high-water mark and
  day-start equity are all persisted and restored ([ADR 0005](adr/0005-risk-state-and-history-are-persisted.md)).
* **Rotating `TRADEBOT_DASHBOARD_TOKEN` invalidates every session** — the cookie's signing key is
  derived from it.
* **The panel can never size, route, or exceed a limit on an order.** It emits a proposal;
  deterministic, unit-tested code decides whether anything happens and at what size. A prompt
  injection via a news headline can flip a marginal decision and nothing more (R7).
* **Alerting is at-least-once.** A repeated alert is an annoyance; a missed one is what the design
  spends a database row to prevent.
* **`--observe` is the safe way to look.** Dashboard up, supervision stopped, nothing cycles — and
  it stays that way until someone clicks Start.
* **Stop is not the kill switch.** Stop pauses cycling and cancels nothing; the kill switch halts
  everything, cancels working orders, and needs the typed re-arm phrase to clear.
* **Nothing polls open orders while stopped.** That is why a manual close is refused there, and why
  the Control page lists whatever is still working at the venue.
