# Phase 7 — the validation ladder

> Authoritative specs are [DESIGN.md](../DESIGN.md) §9 and
> [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) §5 Phase 7. This records what was decided,
> in what order it is being built, and what is still open. Conventions that outlive it move to
> [CLAUDE.md](../CLAUDE.md); decisions move to `docs/adr/`.

Four code deliverables, split into two passes (agreed 2026-07-31):

| Pass | Deliverable | State |
|---|---|---|
| 1 | Backtest harness + history recorder | ✅ delivered |
| 1 | Promotion gates + report | ✅ delivered |
| 2 | Shadow A/B harness | ✅ delivered |
| 2 | Ops alerts (webhook + Telegram) | ✅ delivered |

The fifth deliverable — the paper soak itself — is wall-clock time, not code. It runs
`tradebot run --mode paper` (live data, `SimBroker`) for weeks and is measured by `report
promotion`.

## Pass 1 — delivered

```
tradebot/validation/evidence.py    the log → the facts a report is made of
tradebot/validation/promotion.py   three automatic gates; the fourth is a signature
tradebot/validation/backtest.py    the harness, warm-up, and the banner
tradebot/validation/cutoffs.py     model knowledge cutoffs, with a source per entry
tradebot/validation/render.py      Markdown for both reports
tradebot/marketdata/recorder.py    venue history → a self-describing replay dataset
```

CLI: `backtest fetch`, `backtest run`, `report promotion`. Decisions recorded in
[ADR 0016](adr/0016-validation-reports-are-folded-from-the-event-log.md) and
[ADR 0017](adr/0017-a-backtest-declares-its-warm-up-and-its-contamination.md).

Three changes reached outside the new package, each for a stated reason:

- `CYCLE_STARTED` carries a `venue`, so the gates can tell the evidence base from an adapter
  integration check sharing one database. Payload-only; no migration.
- `CandleSeries.require_fresh` rejects a bar closing after the cycle's `now`. Staleness had only
  ever been checked in one direction, and the other one is a look-ahead leak.
- `RunnerBuilder` reads the ATR from the basket's **shortest configured timeframe** rather than a
  hardcoded `1h`. A basket on 4h/1d bars was asking for an ATR the snapshot never carried, and
  sizing vetoes without a volatility estimate — so it would have silently never traded.

## Pass 2 — delivered

```
tradebot/decision/shadow.py        the challenger, deliberated on the champion's snapshot
tradebot/validation/comparison.py  the log → the A/B facts a comparison is made of
tradebot/interfaces/alerts.py      Alert, AlertKind, AlertSink — the destination seam
tradebot/ops/sinks.py              webhook + Telegram, off unless configured
tradebot/ops/rules.py              the five PLAN triggers, as a dispatch table
tradebot/ops/dispatcher.py         the log tail
tradebot/ops/cursor.py             how far it has delivered
```

CLI: `report shadow`. Decisions recorded in
[ADR 0018](adr/0018-a-challenger-panel-is-evaluated-on-the-champions-snapshot.md) and
[ADR 0019](adr/0019-alerts-are-a-log-tail-with-a-persisted-cursor.md).

### Shadow A/B harness

Two `PanelConfig`s on the **same frozen snapshot** each cycle, so panels are compared on identical
evidence rather than on different weeks of market.

- **The challenger lives on the `Basket`** as `shadow_panel: PanelConfig | None` — versioned,
  pinned per cycle, editable in the dashboard, and off entirely when unset.
- **It never trades.** The runner deliberates the champion, acts on it, and *then* runs the
  challenger for the record. One `SHADOW_EVALUATED` event and nothing else — no decision, no risk
  check, no intent.
- **Its cost is its own**, from its own panel's `CycleBudget`, recorded on its own event, so
  `$/decision` for the panel that traded stays a true figure.
- **Comparison report:** agreement rate, the action matrix, the divergences that would have moved
  money, conviction spread, and each side's cost — read from the log like every other report.

Both open questions from pass-1 planning were resolved as they were leaning:

- **Log-only**, no projection, consistent with ADR 0016.
- One thing changed from the plan: the shadow panel is **GUI-editable now**, not later. The
  configure form round-trips a basket through `draft_of`/`parse`, so a form that rendered only the
  champion would have deleted a configured challenger on the first edit. The panel editor moved
  into a macro rendered twice; a blank challenger section is dropped before validation.

### Ops alerts

Five triggers: kill switch, basket halt, recon mismatch, repeated provider failure, daily summary.

- **A log tail with a persisted cursor**, not a hook in `EventStore.append` — alerting never sits
  on the money path, and the cursor advances only after delivery, so the guarantee is
  at-least-once. A fresh database starts at the log's *end*.
- **Sinks:** a generic webhook (JSON POST) and Telegram (`sendMessage`), both over `httpx`, both
  off unless their destination is configured. Destinations are environment variables, never
  database rows — they are credentials (PLAN §3.2), registered with the log redactor and never
  named in a log line. `test_ops_sinks.py` pushes a real-shaped bot token through the real sink
  and the real logger to prove it.
- **Repeated provider failure = three consecutive `PANEL_DEGRADED` cycles**, firing once per
  streak, with the streak persisted beside the cursor. Tailing `SEAT_RESPONDED` would be precise
  and would make alerting the most expensive reader in the system.
- **The daily summary rolls over on the venue session day**, reusing `TradingCalendar.session_day`
  — the same answer the watchdog already gives for the daily-loss baseline.

Environment:

```powershell
$env:TRADEBOT_ALERT_WEBHOOK_URL   = "https://hooks.example/..."
$env:TRADEBOT_TELEGRAM_BOT_TOKEN  = "..."   # both, or neither: half-configured refuses
$env:TRADEBOT_TELEGRAM_CHAT_ID    = "..."
```

## Remaining

The paper soak itself — wall-clock time, not code. Everything the exit criterion needs is now
built: run it, watch the alerts, and read `report promotion` when it has enough cycles.
