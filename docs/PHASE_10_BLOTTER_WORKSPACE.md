# Phase 10 — the blotter workspace: one screen that runs the bot

> Authoritative specs remain [DESIGN.md](../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
> This records what was decided, why, and what it will take to build. Conventions that outlive it
> move to [CLAUDE.md](../CLAUDE.md); decisions move to `docs/adr/`. Written and reviewed before any
> code changes, per the standing rule that a change touching operator control gets a design pass
> first. The reference the operator pointed at is the Charles River trading blotter: a dense
> master–detail workspace where selection drives every dependent pane.

**Status: all three passes shipped (2026-08-02).**

## Why now

The dashboard is seven separate pages. Answering the operator's most common question — *"what is
the bot doing with this instrument right now, and why?"* — takes four navigations: Overview for
the position, Cycles filtered by basket for the history, a drill-down for the reasoning, Control
to act on what was found. Nothing updates without a manual reload, so during the exact situations
the dashboard exists for (a halt, a kill-switch trip, an order working at the venue) the operator
is refreshing pages by hand. The ask is a single blotter-style workspace: portfolio status,
a selectable blotter of baskets and instruments, a chart with the panel's decisions overlaid,
a per-scope action log, and a control dock with the risk-control state beside it — with the whole
screen staying current on its own and never freezing during a long operation.

Nothing in this phase touches the money path. Every action the workspace offers already exists as
a tested POST route; every number it shows already has a projection or a query. This phase is a
*presentation and transport* change, and the plan's job is to keep it exactly that.

## Decisions

Confirmed with the operator before this document was written, because each changes an
architectural boundary:

1. **Server-rendered Jinja2 + htmx partial swaps + one vendored chart library.** No build step,
   no CDN, no SPA framework — the workspace is a CSS-grid page whose panes are ordinary template
   fragments swapped by htmx. The one new client-side dependency is a charting library, vendored
   and integrity-hashed exactly as htmx is (ADR 0014). The dashboard's existing auth, template
   filters, and route tests all carry forward.
2. **Live updates arrive over a WebSocket** (operator's choice; DESIGN §6.10 sketched the same).
   One endpoint, and it is **read-only by construction**: the socket carries pane-invalidation
   notices derived from tailing the event log, never data and never commands. Every state change
   remains a plain, authenticated POST. A hijacked or misbehaving socket can cause extra page
   refreshes and nothing else — the same "never touches the money path" property ADR 0019 gives
   alerting.
3. **The workspace replaces Overview, Portfolio and Control as the landing screen.** Configure
   survives as the Parameters/Settings menus; the cycle drill-down, Risk history and Costs pages
   survive under an Analytics menu, reached *from* the workspace (a log row click opens the
   drill-down). Replaced pages become redirects, not copies — two places showing positions is two
   places for them to disagree.

Three further decisions this plan makes, to be challenged in review rather than discovered in
code:

4. **The chart reads through the shared `CachingMarketData`, and candles are never persisted for
   the UI.** The venue is the source of truth and the cache is already single-flight with one
   venue call per bar interval (Phase 3), so a chart request costs at most what a cycle costs and
   usually nothing. A candle projection would be a second copy of venue truth that a replay must
   reproduce. Decision markers come from the `decisions` projection; fills from `fills`. In sim,
   the chart renders the synthetic/replay series — the workspace must be fully demonstrable
   offline, like everything else.
5. **Typed phrases survive the redesign untouched.** The sketch's one-click "Untrip RC" is two
   different acts in this system — re-arming the kill switch and un-halting a basket — and both
   keep their typed phrase, entered in a modal. Quarantine release stays one click (it is
   reversible configuration, ADR 0022); arming live keeps cap + phrase; Start in live re-checks
   all four facts (ADR 0021). A redesign that made a safety phrase more convenient would be a
   redesign of the safety property, and that is out of scope by declaration.
6. **Floats appear exactly once, at the chart boundary, display-only.** The charting library
   consumes IEEE-754 doubles; that is sanctioned the way `money.from_measurement` is — a one-way
   crossing for *rendering*, feeding nothing. No form, no POST, no template reads a number back
   out of the chart. Tooltip values are the server's exact `Decimal` strings, not the library's
   floats.

## The screen

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ header: MODE badge · View · Parameters · Settings · Analytics · [not trading] │
│ banners: kill switch · halted baskets · panel unreachable   (unchanged, all)  │
├──────────────────────────────┬────────────────────────────────────────────────┤
│ ① Portfolio status           │ ③ Chart — selected scope                       │
│   equity · today's PnL       │   candles (shared cache, venue truth)          │
│   per-venue lines (one, v1)  │   markers: BUY ▲ green · SELL ▼ red ·          │
├──────────────────────────────┤   HOLD/WAIT tick grey · fills ● · TTL cancels  │
│ ② Blotter                    │                                                │
│   ▸ basket rows: status pill │                                                │
│     cycles today · next fire ├────────────────────────────────────────────────┤
│     trades today / day cap   │ ⑤ Control dock                                 │
│   ▸ instrument rows: qty ·   │   Start · Stop · Kill switch │ live: Arm/Disarm│
│     avg entry · UPL · last   │   selected: Close position · Quarantine/Release│
│     decision · quarantine    │   basket: Pause/Resume                         │
│   (selection drives ③ ④ ⑤)   ├────────────────────────────────────────────────┤
├──────────────────────────────┤ ⑥ Risk controls (RC)                           │
│ ④ Operation log — selection  │   kill switch state + reason [Re-arm…]         │
│   one row per cycle: time ·  │   halted baskets + reason    [Un-halt…]        │
│   outcome · decision · qty · │   quarantines in force       [Release]         │
│   cost · → drill-down        │   live ceiling clamps · recent risk events     │
└──────────────────────────────┴────────────────────────────────────────────────┘
```

Mapping to the sketch's vocabulary: *Parameters* = Tier-1/Tier-2 risk forms (existing
`/configure/risk`), *Settings* = basket + panel configuration (existing `/configure`),
*Analytics* = Costs, Risk history, and the cycle drill-downs. "RC" = the safety states: kill
switch, halts, quarantines, and the Tier-2 clamps in force. "Untrip" = re-arm / un-halt, phrase
and all.

### Pane contracts

Every pane is a template fragment with its own GET partial route, so htmx can refresh one pane
without touching the rest, and a pane that fails renders *as a failed pane* — an error card with
the reason and a retry — never as a hung page or a silently stale one.

| Pane | Partial route | Reads | Refreshes on |
|---|---|---|---|
| ① Portfolio status | `GET /workspace/portfolio` | `application.equity()`, day-start equity from `RiskStateStore`, positions | fills, reconciliation, external change |
| ② Blotter | `GET /workspace/blotter` | `configs.baskets()`, positions, `decisions` (latest per instrument), halts, quarantine flags, schedule next-fire | cycle events, config published, halt/resume |
| ③ Chart | `GET /workspace/chart?scope=…` (shell) + `GET /workspace/chart/data?scope=…&tf=…` (JSON) | `CachingMarketData` candles; `decisions`, `orders`, `fills` projections for markers | decision recorded, fill booked; bar interval tick |
| ④ Operation log | `GET /workspace/log?scope=…` | `cycles` + `decisions` scoped to selection | cycle finished |
| ⑤ Control dock | `GET /workspace/controls` | `controller.running`, `controller.blockers()`, arming row, closable positions | start/stop, arm/disarm, config published |
| ⑥ Risk controls | `GET /workspace/rc` | `RiskStateStore` (switch, halts), quarantines from configs, `application.policy` clamps, `risk_events` (recent) | risk events, kill switch, halt/resume |

Selection is a URL query (`/?scope=basket:demo` or `/?scope=demo:BTC/USDT`), pushed with
`hx-push-url`, so a reload, a bookmark, or a WS-triggered refresh lands on the same selection —
selection state lives in the URL, never in JavaScript.

**Today's PnL** is `equity() − day_start_equity` from the risk state store — the same
flow-adjusted baseline the daily-loss rule uses (DESIGN §6.6), so the number the operator watches
is the number the limit is measured against. It is labelled with its day boundary (UTC).

**Per-venue breakdown**: one process wires one venue portfolio (Phase 6), so v1 renders one
line. The pane is a list, not a scalar, so the PortfolioAggregate (DESIGN §4, future) drops in
without a layout change. The sketch's "Binance 50,000 / Alpaca 40,000" is that future state.

### The chart

- **Library: TradingView `lightweight-charts`** (Apache-2.0, single file ~45 KB, canvas,
  purpose-built candlestick + marker API, zero dependencies). Vendored into
  `dashboard/static/`, integrity-hashed in the template, hash recorded in the ADR — the same
  treatment as htmx. Fallback candidate if review rejects it: uPlot.
- The JSON data route serializes candles and markers in one response. Candle values cross to
  float here (decision 6); each marker carries the exact decimal strings for its tooltip.
- Timeframe selector (1h default, 4h/1d), limited to what the provider's capabilities declare.
- Marker semantics honour the sketch: grey tick = a cycle that decided HOLD/WAIT (qty 0),
  green ▲ = BUY with the filled qty, red ▼ = SELL with qty; fill dots sit at fill price, not
  decision price, because that is what actually happened. A `QUARANTINED` short-circuit cycle
  renders its own mark — a basket under quarantine still cycles and must still be seen to
  (ADR 0022).
- A gap in the tape renders *as a gap*. Interpolating a hole in the chart would paint data the
  venue never published on the operator's primary screen.
- A basket-scope selection charts the basket's instruments as a small-multiple stack (one row
  per instrument, shared time axis), not an overlay — mixed quote currencies make a shared price
  axis a lie.

### The WebSocket

One endpoint, `WS /ws/updates`. One background task per process tails the event log by `seq` —
the same pattern `AlertDispatcher.poll` already uses — maps event types to pane names through a
dispatch table (convention: dispatch over branching), debounces to at most one notice per pane
per second, and fans out to connected sockets. The browser side is ~30 lines of vanilla JS: on
notice, trigger the named panes' htmx refresh; on close, show a visible **"live updates lost —
reconnecting"** pill and retry with backoff; while disconnected, panes fall back to slow htmx
polling (30 s). Silent staleness is the failure this transport exists to prevent, so losing the
transport must itself be loud.

The socket never carries payloads — only `{"panes": ["blotter", "rc"]}`. The refresh itself is
an ordinary authenticated GET through the full middleware stack. Consequences, in order of
importance:

- **No second rendering path.** The server renders every number exactly once, through the same
  filters, whether the request came from navigation or from a socket nudge.
- **No command surface.** There is nothing to validate on inbound frames because inbound frames
  are ignored entirely.
- **Reconnect needs no state.** A socket that missed ten notices is healed by the next full
  refresh; there is no cursor to resume, so there is nothing to resume wrongly.

**Auth must move before the socket exists.** Today's `SessionMiddleware` extends
`BaseHTTPMiddleware`, which sees only HTTP requests — a WebSocket route added behind it would be
**unauthenticated by construction**, the exact failure ADR 0014's middleware-not-dependency rule
exists to prevent. The middleware is rewritten as pure ASGI, refusing both `http` and
`websocket` scopes without a valid session cookie (a WS upgrade carries cookies, so the check is
identical). `test_dashboard_auth.py` extends its route walk to WebSocket routes and asserts an
unauthenticated upgrade is refused. This lands in Pass 1, before any socket route, and is the
one piece of this phase that is safety-critical rather than presentational.

Uvicorn needs a WebSocket protocol implementation: add `wsproto` (or `websockets`) to
`pyproject.toml` and regenerate both hash-pinned lockfiles.

### New backend surface

- **`Application.market_data: MarketDataProvider | None`** — the shared, cache-wrapped provider,
  exposed read-only. Wired in `_assemble` from the stack that already builds it; `None` renders
  the chart pane's "no data source in this wiring" card. The dashboard keeps taking a wired
  `Application` and building nothing (Phase 6 rule).
- **`Queries` additions**, same read-only projection discipline: `latest_decisions()` (most
  recent decision per instrument, for blotter rows), `activity(scope, limit)` (cycles joined
  with their decisions for the operation log), `day_realized(day_start)` (round trips closed
  since the boundary — totalled in Python, never `SUM` over a money column), plus chart-window
  variants of `orders`/`fills`.
- **`dashboard/updates.py`** — the tail task and the event-type → pane dispatch table, unit-tested
  as a pure mapping.
- **Scheduler next-fire exposure** for blotter rows, read from the existing scheduler state
  rather than recomputed in the view.

### Reliability rules (the "never freezes" section, made concrete)

1. **No request handler awaits the venue except the chart data route**, and that one reads
   through the cache (single-flight, one venue call per bar interval, shared rate budget) under
   an explicit timeout. On timeout or provider error the route returns the error card with the
   last good bar's timestamp — a failed chart is information; a spinner that never resolves is
   not.
2. **Long operations stay out of the request path**, which they already are: readiness probes run
   at boot (Phase 9 decision 9), Start is a DB read plus `create_task`. Nothing in this phase
   adds a slow handler, and review holds that line.
3. **Every mutation is an idempotent POST with `hx-disabled-elt`** on its button — a double-click
   during a slow redirect must not double-submit. (The money path has its own idempotency via
   `client_order_id`; this is UX hygiene on top, not a safety control.)
4. **A refused action re-renders inline with the reason and preserves selection** — the current
   Control page's refusal contract, kept: the error card names the rule that refused, because an
   operator mid-incident acts on what they can see.
5. **SQLite reads stay millisecond-scale** and run exactly as today. If a workspace query ever
   grows past that, it becomes a projection, not a slower query.

## Passes

Staged with a full gate (`check.ps1`, review, working system) between each, per repo convention.

**Pass 1 — transport and groundwork (no visible change). ✅ Shipped.** ASGI rewrite of
`SessionMiddleware` with the extended auth walk test; `wsproto` pinned; `/ws/updates` + tail task
+ dispatch table; `Application.market_data`; new `Queries` methods with unit tests; vendored chart
library in `static/` with [ADR 0024](adr/0024-live-updates-are-read-only-pane-invalidation.md).
Exit criteria all met: every existing page works unchanged; an unauthenticated WS upgrade is
refused (and so is a forged cookie); the tail survives a store with no new events and a burst of
many.

Three things the plan did not anticipate, decided in code and recorded in ADR 0024:

- **Selection is `<kind>:<rest>` for every scope** — `instrument:demo:binance:BTC/USDT`, not the
  un-prefixed form sketched under *Pane contracts*. One parsing rule, no shape-guessing, and an
  instrument literally named `basket` is unambiguous. The instrument key keeps its own colons.
- **The tail paces on `asyncio.sleep`, not the injected `Clock`.** A transport interval is not
  domain time, and a simulated clock makes it either a busy loop (`ManualClock`) or a million
  wasted ticks (a backtest stepping a month).
- **`UpdateHub.register` completes the handshake**, so anchoring the cursor and joining the
  fan-out cannot drift apart into a window where notices are lost. Found as a real race, not in
  review.

**Pass 2 — the read side of the workspace. ✅ Shipped.** The grid page at `/`, panes ①–④ as
partials, selection in the URL, WS-driven refresh with the reconnect pill and polling fallback,
chart with markers, small-multiple basket view. Overview is replaced at its own URL; Portfolio
303s to `/analytics/portfolio`. Exit criteria met: in sim mode with the stub panel an operator
watches a supervised run happen without pressing reload, and a lost socket raises the pill and
drops the panes to 30-second polling rather than going quietly stale.

Four things this pass decided that the plan above did not anticipate:

- **A chart read must not move the venue, so the composition root grew a second price field.**
  In the simulated stack `VenueStack.prices` is a *bridge*: `SimulatedMarket.get_candles` feeds
  the tick to `SimBroker`, which matches resting orders and becomes the reference price for the
  next market order. Pass 1 had exposed exactly that object as `Application.market_data`, so a
  chart left open on the 1d timeframe would have decided what a manual close filled at — on the
  primary paper venue. `VenueStack.read_only_prices` is now a required field, the venue stacks
  set it to the same provider they trade on, and the sim stack sets it to the source *under* the
  bridge. Two tests state the hazard and the fix as behaviour, through `SimBroker`'s public API.
- **Selection is a plain navigation, not an htmx swap.** The plan sketched `hx-push-url`; a
  `<a href="/?scope=…">` is simpler, cannot desynchronise from the URL, and cannot tear down a
  live chart mid-swap. htmx is used for what it is good at here — refreshing one pane in place —
  and for nothing else. The reload costs one templated page against a local SQLite database.
- **The chart pane is JS-owned, deliberately unlike the others.** It carries no `hx-get`: an
  htmx swap on every notice would destroy and rebuild the canvas once a second. It listens for
  the same `refresh` event and re-fetches its own JSON, one request per instrument in the stack,
  so one instrument failing fails one figure.
- **The blotter shows realized PnL, not unrealized.** Reliability rule 1 says no request handler
  but the chart's may await the venue, and no mark is persisted (`queries.py`) — so a UPL column
  would either cost N quotes per pane refresh or invent a mark. Realized is projection-true and
  free. The current mark-to-market figure stays where it already was: the equity line and the
  chart.

**Pass 3 — the control dock and RC pane. ✅ Shipped.** Panes ⑤–⑥; typed-phrase confirmations for
kill, re-arm, un-halt, arm and start-in-live; the quarantine second-click flow carried over; manual
close scoped to selection; the Control page a 303 to `/`. `tests/scenario/test_dashboard_lifecycle.py`
now drives the whole lifecycle *through the workspace*: create → configure → stop/start → observe
cycles → pause → quarantine → close → kill → re-arm, with every action in the event log under
`dashboard`. Exit criterion met: the Phase 6 §6 criterion holds with the workspace as the only
surface used.

Four things this pass decided that the plan above did not anticipate:

- **A typed phrase is a `<details>` drawer, not a modal.** The plan said modal; a modal needs
  JavaScript to open, and the screen an incident is read from is the worst place to discover a
  script did not load — the header menu had already made the same call against a scripted dropdown.
  The phrase field lives *inside* the drawer, so the only way to submit is to have opened it and
  typed, and the whole dock works with scripting off. The phrases themselves are unchanged
  (decision 5), which was the property under protection.
- **The Control *page* retired; the `/control/*` POSTs did not.** They are control actions, not
  view fragments, and moving them would have rewritten every URL in the one suite that covers the
  money-adjacent surface for a cosmetic gain. What changed is what they *render*: a refusal comes
  back as `workspace.page` — the same screen, the same selection, with the reason on it — so an
  operator mid-incident never loses the blotter, chart and log they were reading.
- **Selection travels in a hidden field on every form**, which is what makes that possible in both
  directions: a refusal re-renders the selection, and a success 303s back to `/?scope=…`. The dock
  is the only pane whose contents are *acts*, so it is also the only one where "where was I" has
  consequences beyond scrolling.
- **The panes are resizable**, asked for once the dock was on screen and the left column had to
  hold six blotter rows and a log at once. The grid became two columns of stacked panes with a
  splitter between every neighbour — a grid shares its row tracks across both columns, so dragging
  the blotter taller would have moved the chart's bottom edge with it. Sizes are the *only*
  client-side state here: `--size-*` on the container (not on a pane — htmx swaps those), pixel
  extents used as flex ratios, persisted in `localStorage`, defaulted in the stylesheet so a
  scriptless browser still gets this document's layout. Out of scope by the same logic as the rest:
  it changes nothing the server renders.
- **The phrases moved to `dashboard/dock.py`**, with the rendering ones reaching templates as
  globals. `routes/control.py` has to import the page builder from `routes/workspace.py`, so the
  phrases could not stay in `control.py` without a cycle — and a phrase passed per route is a
  phrase some route eventually forgets, rendering an empty `<code>` beside a field nobody can then
  fill. `dock.py` is otherwise pure assembly, like `blotter.py`: what a control offers and what its
  label means, testable without a browser.

Coverage gates apply as everywhere (`dashboard/` ≥ 80%); the dispatch table and the auth
middleware are tested to the money-path standard because one gates refreshes and the other gates
everything.

## Risks, named

| Risk | Standing answer |
|---|---|
| WS route bypasses auth | ASGI middleware covers the `websocket` scope; the auth test walks WS routes; lands before the route exists (Pass 1) |
| Floats leak from the chart toward money | Floats exist only in `dashboard/chart.py`; nothing reads chart data back; every marker label is a server `Decimal` string. Asserted, not remembered: `test_dashboard_chart.py` walks `dashboard/` for `float(` calls and requires the set of offending modules to be exactly `{chart.py}` — `dashboard/` sits outside the packages `test_money_discipline.py` covers |
| An observer's read moves the simulated venue | `Application.market_data` is `VenueStack.read_only_prices`, never the `SimulatedMarket` bridge; asserted through `SimBroker`'s own API (Pass 2) |
| Chart requests spend the venue rate budget | Cache read-through is single-flight per bar interval; the budget is shared and weight-aware (ADR 0008); a starved chart degrades before a cycle does |
| Two surfaces drift during migration | Replaced pages are 303 redirects from Pass 2/3 on, never parallel copies |
| Socket loss = silent staleness | Loud reconnect pill + polling fallback; staleness is always visible |
| One-click convenience erodes typed phrases | Decision 5: phrases are in-scope-frozen; any review comment proposing to soften one is answered by this line |
| Sketch's multi-venue totals read as v1 scope | Pane is venue-list-shaped from day one; aggregate is explicitly future (PortfolioAggregate, DESIGN §4) |

## Out of scope

- Multi-venue aggregation and the cross-venue equity view (needs `PortfolioAggregate`, unbuilt).
- Re-running readiness probes from the GUI (Phase 9 decision 9 stands).
- Streaming a deliberation's seat responses live during a cycle (research-grade nicety; the
  drill-down after the cycle is the audit artifact).
- Historical mark-to-market equity curve (no historical marks are persisted; the realized curve
  plus the current mark stays the honest display — `queries.py` rationale).
- Mobile layout. Dense blotter grids and phone screens are enemies; the workspace targets a
  desktop, like the tool it is modelled on.
