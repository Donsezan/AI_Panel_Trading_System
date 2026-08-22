"""The alert dispatcher: a log tail with a persisted cursor (ADR 0019).

Alerting is deliberately **not** a hook in `EventStore.append`. An append happens inside the
transaction that records an order intent, and PLAN §1.4 requires that record to be committed
before the network call to the venue — a sink on that path would put a third-party webhook's
latency, and its failures, between a decision and its order. So this reads the log afterwards,
like every report does, and the money path never knows it exists.

**Recording and delivering are two passes over two cursors.** The rules are evaluated exactly
once, by `_record`, which appends one `NOTIFICATION_RAISED` per alert and owns the streak
counters outright. `_drain` then tails *those* events and delivers them; it evaluates nothing.
Before this split, a machine with no webhook never ran the rules at all, so the dashboard — the
only destination a sim or paper run has — could never be fed by them (spec §5.1). `enabled`
therefore gates delivery only.

Two cursors rather than one, because the two fail differently. `last_seq` still advances **only
after** a sink has taken the alert, which is what makes delivery at-least-once: a crash between
sending and saving repeats an alert, and only the opposite ordering could lose one.
`recorded_seq` advances once the notification is appended, so a dead webhook stalls delivery
without withholding what an operator could already see on screen. Sharing one cursor would also
corrupt the streaks — delivery would re-count events on top of the recorder's total and
`PROVIDER_FAILURE` would fire at a different number on screen than in the webhook.

On a database alerting has never run against, **both** cursors start at the log's *end* — a soak
switched on after three weeks must alert on what happens next, not replay three weeks of history
into somebody's phone, or into a bell that then opens full of resolved incidents.

Recording appends through `SingleWriter`, so ADR 0019's "alerting never touches the money path"
becomes: it never *reads* it, and it queues one small append behind whatever a cycle is writing.
One row per alert, minutes apart, and never in the path of an order intent.

The daily summary is the one time-driven rule. Its day is the **venue session day**, the same one
the Tier-2 watchdog rolls the daily-loss baseline on, so "today" means one thing in this codebase
(DESIGN §6.6). For crypto that is the UTC date; for equities it is the exchange session, which is
why a UTC rollover would cut a US session in half.

Failure semantics: `poll` never raises. A sink that fails leaves the cursor unmoved and the batch
is retried on the next poll; a rule that cannot read its event yields no alert and the tail
continues. Alerting that could stop the process would be worse than alerting that is late.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.errors import TradebotError
from tradebot.core.events import Event, EventType
from tradebot.core.logging import get_logger
from tradebot.interfaces.alerts import Alert, AlertKind, AlertSink
from tradebot.interfaces.broker import TradingCalendar
from tradebot.ops.cursor import AlertCursor, AlertCursorStore
from tradebot.ops.rules import ALERT_TYPES, DEFAULT_DEGRADED_STREAK, RuleState, evaluate
from tradebot.persistence.store import EventStore
from tradebot.risk.state import start_of_day
from tradebot.validation.evidence import Evidence

logger = get_logger(__name__)

#: How often the tail is read when it is running as a task. Alerting is minutes-scale by nature —
#: every trigger here is something a human responds to, and no human responds in ten seconds.
DEFAULT_POLL_SECONDS = 60.0

#: Events read per poll. Bounds a first poll on a long-running database.
DEFAULT_BATCH = 500

#: What a recorded notification is *about*. One aggregate, so `read_types` finds them all and no
#: notification is filed under a basket whose drill-down would then show alerting's own writes.
NOTIFICATION_AGGREGATE = "notifications"


def _alert_of(event: Event) -> Alert | None:
    """The alert a `NOTIFICATION_RAISED` payload describes, or `None` if it cannot be read.

    The dispatcher wrote this payload itself, so unreadable means corrupted — and a corrupted row
    must cost the notice it holds, never the delivery of everything behind it.
    """
    payload = event.payload
    try:
        return Alert(
            kind=AlertKind(payload["kind"]),
            at=ensure_utc(datetime.fromisoformat(str(payload["at"]))),
            title=str(payload.get("title", "")),
            body=str(payload.get("body", "")),
            scope=str(payload.get("scope", "")),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "a recorded notification could not be read back; skipping it",
            extra={"seq": event.seq, "event_id": event.event_id},
        )
        return None


class AlertDispatcher:
    """Tails the log, turns it into alerts, and delivers them to every configured sink."""

    def __init__(
        self,
        store: EventStore,
        cursor: AlertCursorStore,
        sinks: Sequence[AlertSink],
        clock: Clock,
        *,
        calendar: TradingCalendar | None = None,
        batch: int = DEFAULT_BATCH,
        degraded_streak: int = DEFAULT_DEGRADED_STREAK,
    ) -> None:
        self._store = store
        self._cursor = cursor
        self._sinks = tuple(sinks)
        self._clock = clock
        #: Whose day the summary covers. Absent means the UTC date, which is right for crypto and
        #: wrong for equities — the same distinction the watchdog draws for the daily-loss limit.
        self._calendar = calendar
        self._batch = batch
        self._streak_limit = degraded_streak

    @property
    def enabled(self) -> bool:
        """Whether any destination is configured. Nothing is delivered — or read — if not."""
        return bool(self._sinks)

    async def start(self) -> AlertCursor:
        """Position the tail before the first poll.

        A database alerting has never run against starts at the log's end. The alternative — a
        first poll that replays weeks of a soak — would deliver hundreds of alerts about incidents
        that were resolved long ago, and would train the operator to mute the channel.
        """
        cursor = self._cursor.load()
        if cursor.started:
            return cursor
        end = self._store.last_seq()
        return await self._cursor.save(
            cursor.model_copy(
                update={
                    "last_seq": end,
                    # Anchored too, and for the same reason: a first poll that recorded the whole
                    # log would open the dashboard's bell on every incident of the last month.
                    "recorded_seq": end,
                    "last_summary_day": await self._session_day(),
                }
            )
        )

    async def poll(self) -> tuple[Alert, ...]:
        """Record what the rules produce, then deliver what a configured sink justifies.

        Recording is unconditional: a notification an operator could see on screen must not be
        withheld because a webhook is down, and the dashboard is the only destination a sim or
        paper run has (spec §5.1). The two run in this order so that an alert raised by this poll
        is delivered by this poll, rather than waiting a minute for the next one.
        """
        cursor = await self.start()
        await self._record(cursor)
        if not self.enabled:
            return ()
        return await self._drain(self._cursor.load())

    async def run(self, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Poll until cancelled. What `run` and `serve` start alongside the supervisor.

        It loops whatever is configured. Returning early with no sink is what left the rules
        unevaluated, and with them the notification bell empty, on every sim and paper machine.
        """
        if not self.enabled:
            logger.info("no alert destination configured; recording notifications only")
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            # The tail outlives every incident it reports, so an unclassified defect here must
            # not end it: a dispatcher that died in week one alerts on nothing in weeks two to six.
            except Exception:
                logger.exception("alert poll failed; the tail continues")
            await self._clock.sleep(poll_seconds)

    # ------------------------------------------------------------------ internals

    async def _record(self, cursor: AlertCursor) -> None:
        """Evaluate the rules over what is new and append one notification per alert.

        The **only** place the rules run, and the only owner of the streak counters. It advances
        `recorded_seq` after every event — including one that justified no alert — so a quiet
        stretch is not re-read on every poll.

        Recording is at-least-once, like delivery: a crash between the append and the cursor save
        repeats the notification on the next poll. That is harmless by construction — `alert_id`
        is derived from the source event's `seq`, so the projection folds a repeat onto the row it
        already has rather than adding a second (spec §5.5).
        """
        events = self._store.read_after(cursor.recorded_seq, *ALERT_TYPES, limit=self._batch)
        state = RuleState(cursor.degraded_streak, self._streak_limit, cursor.stale_streak)
        for event in events:
            source = event.seq or 0
            alert = evaluate(event, state)
            if alert is not None:
                await self._raise(alert, alert_id=f"{source}:{alert.kind.value}", source=source)
            cursor = await self._cursor.save(
                cursor.model_copy(
                    update={
                        "recorded_seq": source,
                        "degraded_streak": state.degraded_streak,
                        "stale_streak": state.stale_streak,
                    }
                )
            )
        await self._record_summary(cursor)

    async def _record_summary(self, cursor: AlertCursor) -> None:
        """One summary per session day, covering the day that just ended.

        Recorded rather than delivered directly, so it reaches the dashboard on a machine with no
        sink and then rides the ordinary delivery path to any that exist — one notification
        pipeline, not two. It has no source event, so its identity is its day.
        """
        today = await self._session_day()
        if cursor.last_summary_day == today:
            return
        await self._raise(
            self._daily_summary(cursor.last_summary_day),
            alert_id=f"summary:{today}",
            source=0,
        )
        await self._cursor.save(cursor.model_copy(update={"last_summary_day": today}))

    async def _raise(self, alert: Alert, *, alert_id: str, source: int) -> None:
        """Append the notification the projection folds into the dashboard's list.

        The payload carries the whole rendered alert, not a reference to it: delivery rebuilds
        from this rather than re-evaluating, so what a webhook receives and what the screen shows
        are the same words, decided once.
        """
        await self._store.append(
            Event(
                ts=self._clock.now(),
                type=EventType.NOTIFICATION_RAISED,
                aggregate_id=NOTIFICATION_AGGREGATE,
                payload={
                    "alert_id": alert_id,
                    "kind": alert.kind.value,
                    "severity": alert.kind.severity.value,
                    "at": alert.at.isoformat(),
                    "scope": alert.scope,
                    "title": alert.title,
                    "body": alert.body,
                    #: The event that caused it, for the drill-down. Zero for the daily summary,
                    #: which is produced by the clock rather than by anything in the log.
                    "event_seq": source,
                },
            )
        )

    async def _drain(self, cursor: AlertCursor) -> tuple[Alert, ...]:
        """Deliver recorded notifications, one at a time, saving after each one that lands.

        Per event rather than per batch, because the cursor may only ever describe what has
        actually been sent. A failure stops the drain where it is: the notifications behind it
        stay undelivered until the destination is back, and none of them is lost.

        It evaluates nothing and touches no streak — `_record` already decided. A notification
        whose payload cannot be read is skipped *and* passed, because one malformed row must not
        stand in front of every later notice forever.
        """
        events = self._store.read_after(
            cursor.last_seq, EventType.NOTIFICATION_RAISED, limit=self._batch
        )
        delivered: list[Alert] = []
        for event in events:
            alert = _alert_of(event)
            if alert is not None and not await self._deliver(alert):
                break
            cursor = await self._cursor.save(cursor.model_copy(update={"last_seq": event.seq or 0}))
            if alert is not None:
                delivered.append(alert)
        return tuple(delivered)

    def _daily_summary(self, day: str) -> Alert:
        """What the day's log says, counted by exactly the vocabulary the gates count."""
        now = self._clock.now()
        evidence = Evidence.gather(self._store, since=now - timedelta(days=1), until=now)
        completed = [cycle for cycle in evidence.cycles if cycle.completed]
        outcomes = ", ".join(f"{name}={count}" for name, count in sorted(evidence.outcomes.items()))
        return Alert(
            kind=AlertKind.DAILY_SUMMARY,
            at=now,
            scope=day or "since start",
            title=f"Daily summary — {len(completed)} cycles, {len(evidence.incidents)} incidents",
            body="\n".join(
                (
                    f"cycles: {len(completed)} completed ({outcomes or '—'})",
                    f"orders filled: {evidence.fills}",
                    f"round trips: {len(evidence.round_trips)} closed, "
                    f"{evidence.losing_trips} losing, realized {evidence.realized_pnl}",
                    f"incidents: {len(evidence.incidents)}",
                    f"deliberation cost: ${evidence.cost_usd}",
                )
            ),
        )

    async def _deliver(self, alert: Alert) -> bool:
        """Send to every sink. False means the cursor must not advance past this alert.

        A partial delivery still counts as delivered: re-sending to the sink that succeeded, on
        every poll, forever, is how one broken destination silences the working one.
        """
        failures = []
        for sink in self._sinks:
            try:
                await sink.send(alert)
            except TradebotError as exc:
                failures.append(f"{sink.sink_id}: {exc}")
        if failures:
            logger.warning(
                "alert delivery failed",
                extra={"kind": alert.kind.value, "failures": failures, "sinks": len(self._sinks)},
            )
        return len(failures) < len(self._sinks)

    async def _session_day(self) -> str:
        now = self._clock.now()
        if self._calendar is None:
            return start_of_day(now)
        return await self._calendar.session_day(now)
