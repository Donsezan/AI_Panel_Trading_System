# Maintenance Piece C — Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the alerts `ops/rules.py` already produces visible on the dashboard — a three-count severity badge in the header, a dropdown listing every undismissed notice, and a per-line dismissal recorded in the event log.

**Architecture:** No new notion of "alert". The existing rules keep sole ownership of what a human should be told; what changes is that the dispatcher now *always* evaluates them (delivery, not the tail, is what a configured sink gates) and appends a `NOTIFICATION_RAISED` event for each. That event and `ALERT_DISMISSED` fold into a new `notifications` projection, which the dashboard reads. The widget is a `<details>` in the header whose two inner regions refresh independently, so a socket tick can never close a dropdown someone is reading.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 Core, Alembic, FastAPI, Jinja2, vendored HTMX, pytest.

**Spec:** [docs/superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md](../specs/2026-08-20-retention-backup-and-notifications-design.md) — §5 in full, plus §2 D5, D6, D7, D8.

**Depends on:** Piece B, for `EventType.MAINTENANCE_RAN` and the events the maintenance rule reads. Piece A only indirectly.

## Resolved before Task 3 (decided 2026-08-22, operator-approved)

**Task 3 as drafted corrupts the streak counters.** `_record` and `_drain` would both evaluate the
same events against `RuleState`, whose `degraded_streak` / `stale_streak` are two columns of the one
`alert_cursor` row. With the two cursors at different positions — recording at seq 500 while a dead
webhook holds delivery at seq 10 — delivery re-counts events on top of the recorder's streak, and a
`PROVIDER_FAILURE` notice fires at a different count on screen than in the webhook.

**The chosen fix: delivery reads the recorded notifications rather than re-evaluating the rules.**

- `_record` tails `ALERT_TYPES` from `recorded_seq`, evaluates the rules **once**, appends
  `NOTIFICATION_RAISED`, and owns the streak fields outright.
- `_drain` tails `NOTIFICATION_RAISED` from `last_seq` and delivers the `Alert` rebuilt from the
  payload (which already carries kind, at, scope, title, body). It evaluates nothing and touches no
  streak. At-least-once delivery is unchanged: the cursor still advances only after a sink lands.
- This is what spec §5.1's *"one evaluation, one persisted `RuleState`, no second opinion"* actually
  asks for.
- `last_seq` changes meaning — it now indexes `NOTIFICATION_RAISED` rather than source events. The
  upgrade is self-healing and needs no data migration for it: no `NOTIFICATION_RAISED` exists below
  the upgrade point, so `read_after(last_seq, NOTIFICATION_RAISED)` returns only new rows whatever
  the stored value is. `recorded_seq` still defaults to `last_seq` for existing rows, so the log is
  not re-recorded from the start.
- **The daily summary must be recorded too.** §5.3 gives `DAILY_SUMMARY` a severity and §5.8 says
  the dropdown lists every undismissed notification, but `_summary` currently runs only when
  `enabled`. Record it on the same unconditional path, or a sim/paper operator never sees one.

Two more things to state rather than discover:

- **Recording appends through `SingleWriter`.** ADR 0019's "alerting never touches the money path"
  becomes "never *reads* it, but does now queue one small append behind a cycle's". Acceptable —
  one row per alert, minutes apart — but ADR 0029 must say so rather than leave it implied.
- **The widget's `counts` must come from `views.render`**, the one place base-template context is
  supplied. Task 5's markup assumes `counts` exists on every page and does not say where from; one
  grouped `COUNT(*) ... WHERE dismissed_at IS NULL` per render.

## Global Constraints

- **Alerting never touches the money path** (ADR 0019). Nothing in this piece may delay, block, or alter a cycle, an order, or a risk decision. A dead sink must not stop a notification being recorded, and a full notifications table must not stop a cycle.
- **The dispatcher must never read its own writes.** `NOTIFICATION_RAISED` and `ALERT_DISMISSED` are not alert types; adding them to `ALERT_TYPES` would be a feedback loop.
- **Dismissal is an audited act.** It goes through an event with `dashboard` as the actor, exactly like every other dashboard action (PLAN §6 exit criterion).
- **Auth is by middleware, not per route.** Adding a route must not add an auth decorator; `test_dashboard_auth.py` walks every route and will fail if a new one is unguarded.
- **Colour is never the only carrier of meaning** (spec §5.6).
- **Line length 100**, ruff format, full annotations.
- Verification: `.venv\Scripts\python.exe -m pytest tests/unit/test_ops_dispatcher.py tests/unit/test_dashboard_notifications.py -q`, then `.\check.ps1`.

---

### Task 1: Severity on the kind, and two maintenance kinds

**Files:**
- Modify: `tradebot/interfaces/alerts.py:25-66`
- Modify: `tradebot/ops/sinks.py` (wherever `is_urgent` chose an emoji)
- Test: `tests/unit/test_ops_rules.py` (or the existing file that covers `AlertKind`)

**Interfaces:**
- Produces:
  - `class Severity(StrEnum)` with `HIGH`, `MEDIUM`, `LOW`
  - `AlertKind.severity -> Severity`
  - `AlertKind.MAINTENANCE_FAILED`, `AlertKind.MAINTENANCE_OK`
  - `Alert.severity` (delegating to `kind`), replacing `AlertKind.is_urgent`

- [ ] **Step 1: Write the failing test**

```python
class TestSeverity:
    """Severity lives on the kind, because behaviour belongs on the enum (repo conventions)."""

    def test_the_things_that_stopped_trading_are_high(self) -> None:
        assert AlertKind.KILL_SWITCH.severity is Severity.HIGH
        assert AlertKind.BASKET_HALTED.severity is Severity.HIGH
        assert AlertKind.RECON_MISMATCH.severity is Severity.HIGH
        assert AlertKind.MAINTENANCE_FAILED.severity is Severity.HIGH

    def test_a_frozen_valuation_is_high_though_it_trips_nothing(self) -> None:
        """It is not a breach, but it stops every basket trading (ADR 0027)."""
        assert AlertKind.VALUATION_FROZEN.severity is Severity.HIGH

    def test_the_degradations_are_medium(self) -> None:
        assert AlertKind.PROVIDER_FAILURE.severity is Severity.MEDIUM
        assert AlertKind.DATA_STALE.severity is Severity.MEDIUM

    def test_the_routine_notices_are_low(self) -> None:
        assert AlertKind.DAILY_SUMMARY.severity is Severity.LOW
        assert AlertKind.MAINTENANCE_OK.severity is Severity.LOW

    def test_every_kind_has_one(self) -> None:
        """A kind added later without a severity must fail here, not render as blank."""
        assert all(isinstance(kind.severity, Severity) for kind in AlertKind)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -k Severity -q`
Expected: FAIL — `ImportError: cannot import name 'Severity'`

- [ ] **Step 3: Write the implementation**

In `tradebot/interfaces/alerts.py`:

```python
class Severity(StrEnum):
    """How much of a human's attention this needs. Three levels, because the dashboard shows
    three counts and a fourth would be a distinction nobody acts on differently."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertKind(StrEnum):
    ...
    MAINTENANCE_FAILED = "maintenance_failed"
    MAINTENANCE_OK = "maintenance_ok"

    @property
    def severity(self) -> Severity:
        """What this is worth interrupting someone for. Total by construction: a kind missing
        from the table raises rather than defaulting to quiet."""
        return _SEVERITY[self]


_SEVERITY: dict[AlertKind, Severity] = {
    AlertKind.KILL_SWITCH: Severity.HIGH,
    AlertKind.BASKET_HALTED: Severity.HIGH,
    AlertKind.RECON_MISMATCH: Severity.HIGH,
    AlertKind.VALUATION_FROZEN: Severity.HIGH,
    AlertKind.MAINTENANCE_FAILED: Severity.HIGH,
    AlertKind.PROVIDER_FAILURE: Severity.MEDIUM,
    AlertKind.DATA_STALE: Severity.MEDIUM,
    AlertKind.DAILY_SUMMARY: Severity.LOW,
    AlertKind.MAINTENANCE_OK: Severity.LOW,
}
```

On `Alert`, replace the `is_urgent` use in `text` with `self.kind.severity is not Severity.LOW`,
and delete `AlertKind.is_urgent`. Update every call site the compiler and `grep -rn is_urgent`
find.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -k "Severity or sink or dispatcher" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tradebot/interfaces/alerts.py tradebot/ops/ tests/unit/
git commit -m "feat(ops): severity on AlertKind, and the two maintenance kinds"
```

---

### Task 2: The maintenance rule

**Files:**
- Modify: `tradebot/ops/rules.py` (`ALERT_TYPES` at :39-43, `RULES` at :245-252)
- Test: `tests/unit/test_ops_rules.py`

**Interfaces:**
- Consumes: `EventType.MAINTENANCE_RAN` (Piece B), `AlertKind.MAINTENANCE_*` (Task 1).
- Produces: `def maintenance(event: Event, state: RuleState) -> Alert | None`

- [ ] **Step 1: Write the failing test**

```python
class TestMaintenanceRule:
    def test_a_failed_pass_is_a_high_alert_naming_the_reason(self) -> None:
        alert = evaluate(maintenance_event(outcome="failed", detail="no room"), RuleState())

        assert alert is not None
        assert alert.kind is AlertKind.MAINTENANCE_FAILED
        assert "no room" in alert.body

    def test_a_successful_pass_is_a_low_alert_quoting_what_it_did(self) -> None:
        alert = evaluate(
            maintenance_event(outcome="ok", compacted_rows=42, deleted_archives=1), RuleState()
        )

        assert alert is not None
        assert alert.kind is AlertKind.MAINTENANCE_OK
        assert "42" in alert.body
        assert "1" in alert.body

    def test_the_daily_line_names_the_windows_in_force(self) -> None:
        """So "why did that get deleted" is answerable from the notice itself."""
        alert = evaluate(maintenance_event(outcome="ok"), RuleState())

        assert alert is not None
        assert "30" in alert.body and "90" in alert.body
```

with a helper building a `MAINTENANCE_RAN` event whose payload matches the one Piece B writes.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_ops_rules.py -k Maintenance -q`
Expected: FAIL — `evaluate` returns `None`, because the type is unrouted.

- [ ] **Step 3: Write the implementation**

```python
def maintenance(event: Event, _state: RuleState) -> Alert | None:
    """One pass, rendered for a human. Loud on failure, quiet and superseding on success.

    A dedicated event type rather than an overloaded `RISK_EVENT`: maintenance is not a risk rule,
    and `valuation_frozen` keeps sole ownership of that type (spec §5.4).
    """
    failed = text(event, "outcome") == "failed"
    windows = (
        f"windows in force: compact after {event.payload.get('compact_after_days')}d, "
        f"keep archives {event.payload.get('archive_keep_days')}d"
    )
    if failed:
        return Alert(
            kind=AlertKind.MAINTENANCE_FAILED,
            at=event.ts,
            scope="maintenance",
            title="Housekeeping failed — backups or retention did not complete",
            body=f"{text(event, 'detail') or 'no reason recorded'}. {windows}",
        )
    return Alert(
        kind=AlertKind.MAINTENANCE_OK,
        at=event.ts,
        scope="maintenance",
        title="Housekeeping ran",
        body=(
            f"backup {text(event, 'backup') or 'none'}; "
            f"{event.payload.get('compacted_rows', 0)} payloads compacted; "
            f"{event.payload.get('deleted_archives', 0)} archives deleted. {windows}"
        ),
    )
```

Add `EventType.MAINTENANCE_RAN` to `ALERT_TYPES` and `EventType.MAINTENANCE_RAN: maintenance` to
`RULES`.

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_ops_rules.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tradebot/ops/rules.py tests/unit/test_ops_rules.py
git commit -m "feat(ops): a maintenance alert rule"
```

---

### Task 3: The tail runs unconditionally; delivery stays configured-only

**Files:**
- Modify: `tradebot/ops/dispatcher.py:76-160`
- Modify: `tradebot/persistence/schema.py:308-319` (`alert_cursor` gains `recorded_seq`)
- Create: a migration via `alembic revision --autogenerate -m "recorded_seq on alert_cursor"`
- Test: `tests/unit/test_ops_dispatcher.py`

**Interfaces:**
- Produces:
  - `AlertCursor.recorded_seq: int`
  - `AlertDispatcher.record()` — evaluates and appends, independent of sinks
  - `EventType.NOTIFICATION_RAISED`

- [ ] **Step 1: Write the failing test**

```python
class TestRecordingWithoutSinks:
    """The blocking defect: with no webhook, the rules never ran at all (spec §5.1)."""

    async def test_a_dispatcher_with_no_sinks_still_records(self, store: EventStore) -> None:
        dispatcher = AlertDispatcher(store, cursor_store(store), (), ManualClock(NOW))
        await store.append(kill_switch_event())

        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1

    async def test_recording_advances_its_own_cursor_while_delivery_stalls(
        self, store: EventStore
    ) -> None:
        """A dead webhook must not withhold what the operator could see on screen."""
        sink = RecordingSink(fail=True)
        dispatcher = AlertDispatcher(store, cursor_store(store), (sink,), ManualClock(NOW))
        await store.append(kill_switch_event())

        await dispatcher.poll()
        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1
        assert cursor_store(store).load().last_seq == 0

    async def test_the_dispatcher_never_reads_its_own_writes(self, store: EventStore) -> None:
        dispatcher = AlertDispatcher(store, cursor_store(store), (), ManualClock(NOW))
        await store.append(kill_switch_event())

        await dispatcher.poll()
        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_ops_dispatcher.py -k Recording -q`
Expected: FAIL — `poll` returns early with no sinks, so nothing is recorded.

- [ ] **Step 3: Write the implementation**

- Add `NOTIFICATION_RAISED` to `EventType` with a comment saying it is deliberately absent from
  `ALERT_TYPES`.
- Add `Column("recorded_seq", Integer, nullable=False, default=0)` to `alert_cursor`, add the field
  to the `AlertCursor` model, and generate the migration. Review the generated file: autogenerate
  does not see data migrations, and existing rows must default to `last_seq` rather than 0 so an
  upgrade does not re-record the whole log.
- Split `poll`:

```python
    async def poll(self) -> tuple[Alert, ...]:
        """Record what the rules produce, then deliver what a configured sink justifies.

        Recording is unconditional: a notification an operator could see on screen must not be
        withheld because a webhook is down, and the dashboard is the only destination a sim or
        paper run has (spec §5.1).
        """
        cursor = await self.start()
        await self._record(cursor)
        if not self.enabled:
            return ()
        delivered = await self._drain(await self._cursor.load())
        summary = await self._summary(self._cursor.load())
        return delivered + summary
```

with `_record` reading `read_after(cursor.recorded_seq, *ALERT_TYPES)`, evaluating each event
against a `RuleState` seeded from the cursor, appending one `NOTIFICATION_RAISED` per alert, and
saving `recorded_seq` after each — the mirror image of `_drain`, but advancing on the append rather
than on a delivery.

The `NOTIFICATION_RAISED` payload carries the deterministic identity the projection keys on:

```python
{
    "alert_id": f"{event.seq}:{alert.kind.value}",
    "kind": alert.kind.value,
    "severity": alert.kind.severity.value,
    "at": alert.at.isoformat(),
    "scope": alert.scope,
    "title": alert.title,
    "body": alert.body,
    "event_seq": event.seq,
}
```

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_ops_dispatcher.py -q`
Expected: PASS, including every existing at-least-once delivery test unchanged.

- [ ] **Step 5: Commit**

```bash
git add tradebot/ops/dispatcher.py tradebot/persistence/ migrations/ tests/unit/test_ops_dispatcher.py
git commit -m "feat(ops): record notifications whether or not a sink is configured"
```

---

### Task 4: The `notifications` projection and the dismissal event

**Files:**
- Modify: `tradebot/persistence/schema.py` (new `notifications` table; add it to `PROJECTION_TABLES`)
- Modify: `tradebot/persistence/projections.py` (two projectors)
- Modify: `tradebot/core/events.py` (`ALERT_DISMISSED`)
- Create: a migration
- Test: `tests/unit/test_notifications_projection.py`

**Interfaces:**
- Produces: `notifications` table; `_project_notification_raised`; `_project_alert_dismissed`.

- [ ] **Step 1: Write the failing test**

```python
class TestProjection:
    async def test_a_raised_notification_becomes_a_row(self, store: EventStore) -> None: ...

    async def test_the_same_alert_raised_twice_stays_one_row(self, store: EventStore) -> None:
        """Deterministic identity is what makes a delivery retry harmless (spec §5.5)."""

    async def test_a_retry_does_not_resurrect_a_dismissed_row(self, store: EventStore) -> None:
        """The 03:20 re-evaluation must not undo the 03:12 dismissal."""

    async def test_dismissal_records_who_and_when(self, store: EventStore) -> None: ...

    async def test_a_rebuild_reproduces_dismissals(self, store: EventStore) -> None:
        """Dismissal is in the log, not beside it, so a replay lands on the same state."""

    async def test_a_new_maintenance_ok_supersedes_the_previous_one(
        self, store: EventStore
    ) -> None:
        """Marked dismissed_by 'system', distinct from an operator's click (spec §5.4)."""
```

Fill each body following the style of `tests/unit/test_projections.py`: append events through the
store, then assert on `select(notifications)`.

- [ ] **Step 2-5: implement, run, commit** as in the previous tasks.

The `_project_notification_raised` projector must use the "insert, ignore on conflict" form — never
an upsert that writes the payload columns again, or a retry would clear `dismissed_at`. The
`MAINTENANCE_OK` supersession is a second statement in the same projector: mark earlier
undismissed rows of that kind dismissed with `dismissed_by = "system"`.

```bash
git commit -m "feat(persistence): the notifications projection and its dismissal"
```

---

### Task 5: The header widget

**Files:**
- Modify: `tradebot/dashboard/templates/base.html:37-42`
- Create: `tradebot/dashboard/templates/workspace/_notifications.html`
- Modify: `tradebot/dashboard/routes/monitor.py` (the fragment route) and `routes/control.py` (dismiss)
- Modify: `tradebot/dashboard/updates.py` (`Pane.NOTIFICATIONS`, two `PANES_BY_EVENT` keys)
- Modify: `tradebot/dashboard/static/app.css`
- Test: `tests/unit/test_dashboard_notifications.py`

**Interfaces:**
- Consumes: the `notifications` table (Task 4).
- Produces: `GET /workspace/notifications`, `POST /control/notifications/{alert_id}/dismiss`.

- [ ] **Step 1: Write the failing test**

```python
class TestCounts:
    async def test_all_three_counts_render_including_zeros(self, client: AsyncClient) -> None:
        """`1 | 0 | 3`, never `1 | 3` — a widget that reflows moves Log out under the cursor."""

    async def test_the_counts_are_readable_without_colour(self, client: AsyncClient) -> None:
        """Position is fixed and the label spells it out, for a greyscale screenshot."""
        page = (await client.get("/")).text
        assert "high" in page and "medium" in page and "low" in page


class TestStructure:
    async def test_the_details_element_is_not_the_swap_target(self, client: AsyncClient) -> None:
        """Swapping it would snap the dropdown shut mid-read, once a second (spec §5.7)."""
        page = (await client.get("/")).text
        details = _extract(page, 'id="notifications"')
        assert "hx-get" not in details.split(">")[0]

    async def test_the_list_only_fetches_while_open(self, client: AsyncClient) -> None:
        assert "refresh[this.closest('details').open]" in (await client.get("/")).text


class TestDismissal:
    async def test_dismissing_appends_the_event_with_the_dashboard_as_actor(...) -> None: ...

    async def test_dismissing_something_already_gone_is_not_an_error(...) -> None: ...


class TestPanes:
    def test_only_the_two_notification_events_invalidate_the_widget(self) -> None:
        """Keying on a kill-switch trip would repaint before the row exists (spec §5.7)."""
        keyed = {t for t, panes in PANES_BY_EVENT.items() if Pane.NOTIFICATIONS in panes}
        assert keyed == {EventType.NOTIFICATION_RAISED, EventType.ALERT_DISMISSED}
```

- [ ] **Step 2-5: implement, run, commit.**

The markup, in `base.html` immediately before `#live-pill`:

```jinja
<details class="menu notifications" id="notifications">
  <summary title="{{ counts.high }} high, {{ counts.medium }} medium, {{ counts.low }} low"
           aria-label="{{ counts.high }} high, {{ counts.medium }} medium, {{ counts.low }} low">
    <span id="notification-counts" hx-get="/workspace/notifications" hx-trigger="refresh"
          hx-select="#notification-counts" hx-swap="outerHTML">
      <b class="danger">{{ counts.high }}</b><span class="muted">|</span>
      <b class="warn">{{ counts.medium }}</b><span class="muted">|</span>
      <b class="ok">{{ counts.low }}</b>
    </span>
  </summary>
  <div id="notification-list" class="menu-items"
       hx-get="/workspace/notifications" hx-trigger="refresh[this.closest('details').open]"
       hx-select="#notification-list" hx-swap="outerHTML">
    {% include "workspace/_notifications.html" %}
  </div>
</details>
```

CSS additions: `tabular-nums` and a fixed `min-width` on `#notification-counts b`, and a muted grey
rule when all three are zero. No new colour variables — `--danger`, `--warn`, `--ok` already exist.

Each row in `_notifications.html` carries an ordinary POST form with a hidden `scope`, following
`workspace/_actions.html`'s `action` macro, whose button is the `×`.

```bash
git commit -m "feat(dashboard): a notifications dropdown with severity counts"
```

---

### Task 6: ADR 0029 and the documentation

**Files:**
- Create: `docs/adr/0029-notifications-are-a-projection-of-the-alert-rules.md`
- Modify: `docs/adr/0019-alerts-are-a-log-tail-with-a-persisted-cursor.md`
- Modify: `CLAUDE.md` (the Phase 7 layering sentence about the dispatcher writing only its cursor)

- [ ] **Step 1-4:** write ADR 0029 in the house format — one entity, two cursors and why, dismissal
  as an audited act, and the `<details>` swap hazard. Note in ADR 0019 that the tail now runs
  unconditionally while delivery stays configured-only. Correct the CLAUDE.md sentence to "its
  cursors, and the notifications it recorded".

```bash
git commit -m "docs: ADR 0029, notifications are a projection of the alert rules"
```

---

## Notes for the executor

Tasks 4, 5 and 6 are written at lower resolution than Tasks 1-3 on purpose: their exact shape
depends on the `notifications` columns settled in Task 4 and on the fragment route conventions in
`routes/monitor.py`, both of which are easier to read than to predict. **Before starting Task 4,
re-read spec §5.5-5.8 and the existing `workspace/_rc.html` + `_actions.html`, then expand the task
into concrete steps in this file** — do not improvise past a checkbox that lacks code.
