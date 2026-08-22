# 29. Notifications are a projection of the alert rules, dismissed by an audited act

Date: 2026-08-22

## Status

Accepted. Implements Piece C of
[docs/superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md](../superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md),
and amends [ADR 0019](0019-alerts-are-a-log-tail-with-a-persisted-cursor.md): the tail now runs
unconditionally, while delivery stays configured-only.

## Context

`ops/rules.py` already owned the answer to "what should a human be told about" — five triggers,
one function per event type, arrived at deliberately in Phase 7 pass 2. What it did not have was a
destination anyone could see. Alerts went to a webhook or Telegram, both of which are credentials
read from the environment, and neither of which a sim or paper machine has.

That was not merely a missing view. `AlertDispatcher.poll` and `.run` both returned immediately
when `enabled` — "any sink configured" — was false, so **on every machine without a webhook the
rules never evaluated at all**. Any bell fed by them would have been permanently empty, and the
first symptom would have been an operator concluding there was nothing to tell them.

There was also an operational gap the maintenance work had just widened. Piece B records one
`MAINTENANCE_RAN` event per pass and, on failure, records it and moves on: a refused backup or an
unverifiable archive is a line in a log file nobody is watching. The thing that makes retention
safe is a backup, and the thing that makes a failed backup safe is somebody finding out.

The temptation was a second notion of "alert" — a notifications table written directly by whatever
wanted to say something. That would leave two answers to the same question, which is how a system
ends up telling an operator different things in two places.

## Decision

**Notifications are a projection of the alert rules that already existed.** Nothing new decides
what an operator should be told. What changed is where the answer goes.

- **`enabled` gates delivery only.** The dispatcher always tails the log, evaluates the rules, and
  appends one `NOTIFICATION_RAISED` per alert. Webhook and Telegram remain configured-only.
- **Two cursors, because recording and delivering fail differently.** `recorded_seq` advances once
  the notification is appended; `last_seq` still advances only after a sink has taken it, which is
  what keeps delivery at-least-once. One cursor would also corrupt the streak counters: with
  delivery stalled behind a dead webhook, a second evaluation would re-count `PROVIDER_FAILURE`
  events on top of the recorder's total and the alert would fire at a different number on screen
  than in the webhook.
- **The rules run exactly once**, in `_record`, which owns `degraded_streak` and `stale_streak`
  outright. `_drain` rebuilds each `Alert` from the payload and evaluates nothing.
- **Dismissal is an event**, `ALERT_DISMISSED`, appended by the dashboard with `dashboard` as the
  actor, exactly like every other dashboard action. It is in the log rather than beside it, so a
  projection rebuild reproduces the notification history *and* its dismissals.
- **Severity lives on `AlertKind`**, as a property backed by a total table. It replaces
  `is_urgent`, which only ever chose a sink's emoji.
- **Maintenance joins as two kinds**, `MAINTENANCE_FAILED` (HIGH) and `MAINTENANCE_OK` (LOW), via
  one new row in the existing `RULES` table — a new row, not a new alerting path.

## Consequences

- A sim or paper operator has, for the first time, a place where the system says what needs
  attention — and the alert rules are now exercised on every machine rather than only on one with
  a webhook configured.
- **Recording appends through `SingleWriter`.** ADR 0019's "alerting never touches the money path"
  becomes: it never *reads* it, and it now queues one small append behind whatever a cycle is
  writing. One row per alert, minutes apart, never in the path of an order intent.
- The log gains two event types and the database one table and one column. `events.type` is a
  plain `String(48)` with no database-level enum, so the event types needed no migration;
  `notifications` and `alert_cursor.recorded_seq` are migrations 0009 and 0008.
- A `run`/`serve` process now polls the log every 60 seconds whatever is configured. Previously
  that task returned immediately with no destination.

## Rules that are easy to get backwards

- **The dispatcher must never read its own writes.** `NOTIFICATION_RAISED` and `ALERT_DISMISSED`
  are deliberately absent from `ALERT_TYPES`. Adding either would be a feedback loop that appends
  a notification about a notification, forever.
- **On conflict the row is left untouched, never overwritten.** Recording is at-least-once, so the
  same `alert_id` can arrive twice; an upsert would rewrite the payload columns and clear a
  `dismissed_at` set in between, so a re-record at 03:20 would resurrect a notice dismissed at
  03:12. `alert_id` is deterministic — `"{event_seq}:{kind}"` — which is what makes the repeat
  harmless in the first place.
- **Both cursors anchor at the log's end on a database alerting never ran against.** ADR 0019's
  rule, applied to the new one: without it the first poll after this upgrade opens the bell on
  every incident the log has ever held, and an operator who scrolls past a hundred resolved rows
  has learned to scroll past the one that matters.
- **Only quiet kinds may supersede.** `MAINTENANCE_OK` retires the previous day's so reassurance
  never stacks into thirty identical green rows. `MAINTENANCE_FAILED` never does — hiding
  yesterday's unread failure behind today's is the one thing that list must not do.
- **The `<details>` is never the swap target.** htmx replacing it would close the dropdown while
  someone is reading it, up to once a second. Its two inner regions carry their own `hx-get`
  against one route and listen on the `<details>` via `from:closest details`, because
  `workspace.js` dispatches `refresh` with `bubbles: false`.
- **`PANES_BY_EVENT` keys the bell on exactly the two events the dispatcher appends**, and not on
  the five the rules read. A kill-switch trip reaches the socket tail immediately, but the
  notification it produces is written later, when the dispatcher next polls — so keying on the
  trip would repaint the widget before there was anything new in it, and then never repaint it
  again.
- **The counts and the list both come from `views.render`.** They are rendered by the base
  template, so a page supplying only the counts shows "Nothing to report" under a counter reading
  two, and never corrects itself with scripting off or on a page that does not load
  `workspace.js`. This was found by rendering the page, not by reading it.
- **Dismissing something already gone writes nothing.** It is a 303 back to the workspace: an
  event that projects onto no row would read in the audit trail as a dismissal that never
  happened, and refusing a stale browser tab with an error would teach an operator that the ×
  is unreliable.
- **Dismissal acknowledges a message and changes nothing the bot does.** The kill switch, the
  halt and the quarantine that a notice was *about* each keep their own act, with their own typed
  phrase where they had one.

## Alternatives considered

- **A parallel notification entity**, written directly by anything that wanted to say something.
  Rejected: two answers to "what should the operator be told", which drift.
- **Every event, filtered by severity on read.** Roughly 1,300 rows a day, which trains dismissal
  without reading — the failure mode ADR 0019 already names for over-eager alerting.
- **A plain dismissals table.** Cheaper, and it loses the audit line for who cleared what; a
  rebuild would also silently undo every dismissal.
- **Session-only dismissal.** Re-floods on every restart, which is the same lesson as
  [ADR 0005](0005-risk-state-and-history-are-persisted.md): a limit a restart clears is not a
  limit, and a dismissal a restart forgets is not a dismissal.
- **A badge that appears only when non-empty.** Reflows the header during an incident, moving the
  Log out link under the cursor. All three counts are always shown, muted at zero.
