# 19. Ops alerts are a log tail with a persisted cursor, never a hook on the money path

Date: 2026-08-01
Status: accepted. Amended 2026-08-22 by
[ADR 0029](0029-notifications-are-a-projection-of-the-alert-rules.md): the **tail now runs
unconditionally** and records what the rules produce, while *delivery* stays configured-only. A
second cursor, `recorded_seq`, was added for it — everything below about `last_seq` and
at-least-once delivery is unchanged and still describes delivery.

## Context

PLAN Phase 7 lists five alert triggers: kill switch, basket halt, reconciliation mismatch,
repeated provider failure, daily summary. Four of them describe a system that has already stopped
doing something, and the alert exists because a paper soak runs for weeks and nobody watches a
dashboard at 03:00.

The obvious implementation is a hook in `EventStore.append`, and it is the wrong one. An append
happens inside the transaction that records an order intent, and PLAN §1.4 requires that record
committed *before* the network call to the venue. A sink on that path puts a third-party
webhook's latency — and its timeouts, its 500s, its DNS — between a decision and its order.
Alerting must never be able to delay or fail a trade.

The second question is what a restart does. Alerting that loses an alert across a restart is
alerting you cannot rely on during exactly the incident that caused the restart.

## Decision

**Alerting reads the log afterwards, like a report.** `AlertDispatcher` tails `events` by
sequence, narrowed to four types, on its own task beside the supervisor. The money path does not
know it exists, and turning it off changes nothing about how the system trades.

**The cursor advances only after delivery.** That makes the guarantee **at-least-once**: a crash
between sending and saving repeats an alert, and only the opposite ordering could lose one. A
repeated kill-switch alert is an annoyance; a missed one is the failure this whole component
exists to prevent. The cursor is saved per event rather than per batch, so a sink that fails
stops the drain where it is and everything behind it stays unread until the destination is back.

**A fresh database starts at the end of the log.** Switching alerting on after three weeks of
soak must alert on what happens *next*, not deliver three weeks of resolved incidents into
somebody's phone at once — which would teach the operator to mute the channel before the first
real alert arrives.

**Destinations come from the environment, never the database.** A webhook URL is a bearer
credential and a Telegram bot token is literally in the URL path, so both follow the venue-key
rule (PLAN §3.2): environment or keyring, registered with the log redactor at construction, and
never named in a log line. Every delivery error identifies the *sink*, not the endpoint. A
half-configured Telegram — a token with no chat id, or the reverse — **refuses to start** rather
than quietly alerting nobody.

**Alerting is on exactly when a destination is configured.** There is no flag. An operator
starting a six-week soak cannot forget to enable it, and a developer running the demo is never
asked for a webhook.

**Repeated provider failure is counted as consecutive `PANEL_DEGRADED` cycles**, at a threshold
of three, firing once per streak. The precise signal — a seat exhausting its fallback chain —
lives in `SEAT_RESPONDED`, which carries raw model text and is the largest payload in the log;
tailing it would make alerting the most expensive reader in the system. A degraded panel is what
a run of provider failures *does*, and it is the point at which cycles have started costing
decisions. The streak is **persisted** alongside the cursor, for the same reason the risk
baselines are persisted: a streak counted in memory is a streak a restart forgives. An outcome
this build cannot parse leaves the streak untouched — it is not evidence the providers recovered.

**The rules use the `Evidence` vocabulary the promotion gates use.** Four of the five alerts
correspond exactly to an `IncidentKind`, so "what needed a human" has one definition rather than
a reporting one and an alerting one that drift apart. In particular a **veto is not an alert**,
for the same reason it is not an incident: the system did what it was built to do.

**The daily summary rolls over on the venue session day**, via the same
`TradingCalendar.session_day` the Tier-2 watchdog uses for the daily-loss baseline. A UTC
rollover would cut a US equities session in half, and two disagreeing definitions of "day" in one
codebase is how a limit ends up measured against the wrong baseline.

## Consequences

- Alerts are up to one poll interval late (60s by default). Every trigger here is something a
  human responds to in minutes; none of them is a latency problem.
- A duplicate alert is possible and expected after an unclean shutdown. Sinks should be treated
  as at-least-once by whatever reads them.
- One broken destination does not silence a working one: an alert delivered to *any* sink counts
  as delivered. Re-sending to the sink that succeeded on every poll forever is the alternative,
  and it is worse.
- `alert_cursor` is not a projection and is excluded from rebuilds. Truncating it would
  re-deliver every alert the log has ever justified.
- The dispatcher swallows unclassified defects in its own loop and keeps tailing. A dispatcher
  that died in week one alerts on nothing in weeks two through six.
