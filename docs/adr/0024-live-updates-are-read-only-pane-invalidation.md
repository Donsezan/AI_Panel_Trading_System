# ADR 0024 — Live updates are read-only pane invalidation over a WebSocket

Status: **accepted** · 2026-08-02 · Phase 10, Pass 1

## Context

The dashboard never updates on its own. During the exact situations it exists for — a halt, a
kill-switch trip, an order working at the venue — the operator refreshes pages by hand. Phase 10
replaces the page-per-question layout with a single blotter workspace whose panes stay current
([docs/PHASE_10_BLOTTER_WORKSPACE.md](../PHASE_10_BLOTTER_WORKSPACE.md), decision 2).

A live transport into a process that can move real money is exactly the kind of addition that
grows a second command surface by accident: it is one small step from "the socket tells the page
what changed" to "the socket carries the change", and one more to "the socket accepts an action".
Each step is individually reasonable and the end state is an unauthenticated, unvalidated path to
the kill switch.

## Decision

**One endpoint, `WS /ws/updates`, carrying pane names outward and nothing in either direction
besides.** A notice is `{"panes": ["blotter", "rc"]}`. The refresh it triggers is an ordinary
authenticated `GET` through the full middleware stack.

Four properties, each closing a specific way this goes wrong:

- **No data on the wire.** The socket never carries a number, so there is no second rendering
  path: every figure is rendered once, by the same templates and filters, whether the request
  came from navigation or from a socket nudge. A pane that renders exact `Decimal` strings on
  navigation cannot render something else on a live update, because there is no other code that
  could.
- **No command surface.** Inbound frames are read *only* so a disconnect is noticed promptly, and
  are discarded unparsed. There is nothing to validate because there is nothing accepted. Every
  state change remains a plain authenticated POST with its typed phrase where it has one
  (ADR 0012, ADR 0021, ADR 0022 are untouched by this phase — PHASE_10 decision 5).
- **No cursor to resume.** The tail anchors at the log's end when a page connects and is
  discarded when it disconnects. A socket that missed ten notices is healed by the next full
  refresh, so there is nothing to resume and nothing to resume wrongly. This is the deliberate
  difference from `AlertDispatcher`, whose cursor *is* persisted because a missed alert is not
  recoverable by looking at the screen (ADR 0019).
- **Worst case is extra refreshes.** A hijacked or misbehaving socket can make a page reload its
  own panes. That is the whole blast radius — the same "never touches the money path" property
  ADR 0019 gives alerting.

### The auth middleware becomes pure ASGI

`SessionMiddleware` extended `BaseHTTPMiddleware`, which only ever sees `http` scopes. A
WebSocket route added behind it would have been **unauthenticated by construction** — precisely
the failure ADR 0014's middleware-not-a-dependency rule exists to prevent, arriving through the
one door that rule did not cover.

It is now pure ASGI and guards `http` and `websocket` alike. A WebSocket upgrade is an ordinary
HTTP request until the handshake completes, so it carries the same cookie and the check is
identical; refusal is a `websocket.close` with code 1008 before `websocket.accept`, which an ASGI
server reports to the browser as a refused handshake. `lifespan` is exempt and is the only exempt
scope: it is the server talking to the application about its own startup, with no client and no
principal to check.

`test_dashboard_auth.py` now walks WebSocket routes as well as HTTP ones, asserts an absent *and*
a forged cookie are both refused, asserts a valid session is admitted, and asserts every guarded
scope has a refusal — a scope the middleware guards but cannot refuse would fall through it.

**This landed before the socket route existed.** It is the one safety-critical piece of an
otherwise presentational phase.

### The tail paces on real time, not the injected clock

`UpdateHub` sleeps with `asyncio.sleep`, deliberately departing from the repo-wide injected-clock
rule. That rule protects the testability of code whose *behaviour* depends on time; nothing here
timestamps, ages or expires anything — the interval only paces a poll. Pacing it on simulated
time would be actively wrong in two ways: a backtest stepping its clock a month forward would
spin the poll a million times, and `ManualClock`, whose `sleep` returns immediately, turns it into
a busy loop reading the database as fast as the event loop allows.

The poll interval **is** the debounce window: one tick reads everything that arrived, unions the
panes those events touch, and sends at most one notice. A burst of a hundred fills is one refresh.

### The tail is lazy

No socket connected means no task, no polling, no cost. A headless `tradebot run`, a closed
browser tab and the whole test suite pay nothing for this feature.

## Consequences

- `wsproto` joins the pinned dependencies. Plain `uvicorn` ships no WebSocket protocol
  implementation, and without one the socket fails its handshake at runtime while every test
  passes — the suite drives ASGI directly and never needs it.
- Ordering inside `UpdateHub.register` is load-bearing and is why the hub, not the route,
  completes the handshake: anchor the cursor *before* accepting, so nothing appended from the
  moment the client is told it is live can be missed; join the fan-out *after* accepting, because
  a notice sent to a socket mid-handshake raises and this hub drops a socket that raises. Either
  half alone is a page that quietly stops updating.
- The event-type → pane table is data, and is tested as data: every tailed type routes somewhere,
  every pane is reachable, and the high-volume drill-down types (`SEAT_RESPONDED`,
  `SNAPSHOT_FROZEN`, `RISK_CHECKED`, `SHADOW_EVALUATED`) are absent by assertion rather than by
  accident. Their absence also narrows the store read that runs every second.
- Losing the socket must be **loud**: the browser shows a reconnect pill and panes fall back to
  slow polling (Pass 2). Silent staleness is the failure this transport exists to prevent, so
  losing the transport may not itself be silent.

## The vendored chart library

TradingView `lightweight-charts` 5.2.0 (Apache-2.0, ~196 KB, canvas, zero dependencies) is
vendored into `dashboard/static/` and hash-pinned exactly as htmx is, for the reasons ADR 0014
already gives: no CDN, no build step, the file on disk *is* the supply chain, and the dashboard
works offline. Its licence header travels with the copy, as Apache-2.0 requires.

`test_dashboard_static.py` re-derives every vendored asset's SRI hash, asserts any template
serving an asset serves the recorded hash, and asserts the bundle still exports the API the
workspace calls (`createChart`, `CandlestickSeries`, `createSeriesMarkers`) — a minified bundle
that no longer exports these is a different library wearing the same filename, and the failure
would otherwise surface as a blank pane in front of an operator rather than as a red build.

Floats appear at this boundary and only here: the charting library consumes IEEE-754 doubles, and
that crossing is sanctioned for *rendering* the way `money.from_measurement` is (PHASE_10
decision 6). Nothing reads a number back out of the chart, and marker labels are the server's
exact `Decimal` strings.

## The dashboard reads venue truth, never the venue bridge

Added in pass 2, when the chart first exercised the price source pass 1 had exposed.

`VenueStack.prices` is not a plain provider in the simulated stack. `SimulatedMarket.get_candles`
forwards the bar to `SimBroker.observe`, which matches every resting order against it and stores
it as the reference price for the next market order — the mechanism by which a simulated venue
learns the market at all. That is correct for a cycle and unacceptable for an observer: a chart
refreshing once a second would match stops, and a chart left open on the 1d timeframe would set
the price a manual close executes at. `SimBroker` is the **primary paper venue** (DESIGN §9 rung
5), so this would have corrupted the soak that live promotion is decided on, in proportion to how
much the operator looked at the screen.

`VenueStack` therefore carries two price fields. `read_only_prices` is required rather than
defaulted, because the difference is invisible at the call site and getting it wrong is silent:
the sim stack sets it to the source *under* the bridge, and the venue stacks set it to the same
object they trade on, since reading a real venue changes nothing. `Application.market_data` is the
read-only one. Asserted as behaviour rather than as identity — a market order needs a reference
price, so submitting one is the public probe for whether the venue has been told anything.

## Alternatives rejected

- **Server-Sent Events.** Adequate — the transport is one-directional in practice — but a socket
  was the operator's choice and DESIGN §6.10 sketched the same. SSE would not have avoided the
  ASGI middleware rewrite, since the hazard is any long-lived authenticated connection.
- **Sending rendered HTML over the socket.** Fewer round trips, and a second rendering path with
  its own filters. Rejected: two ways to render a position size is two ways for one of them to be
  wrong.
- **Subscribing to the writer instead of tailing the log.** Zero latency and no polling, at the
  cost of coupling the presentation layer to the persistence writer. The log tail keeps
  `dashboard/` strictly read-only, which is worth more than a second of latency on a system whose
  cycles take minutes.
- **Persisting the tail's cursor.** Solves a problem this transport does not have: the page
  re-renders everything on reconnect anyway.
