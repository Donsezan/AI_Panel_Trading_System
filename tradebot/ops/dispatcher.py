"""The alert dispatcher: a log tail with a persisted cursor (ADR 0019).

Alerting is deliberately **not** a hook in `EventStore.append`. An append happens inside the
transaction that records an order intent, and PLAN §1.4 requires that record to be committed
before the network call to the venue — a sink on that path would put a third-party webhook's
latency, and its failures, between a decision and its order. So this reads the log afterwards,
like every report does, and the money path never knows it exists.

The cursor makes that safe across a restart. It advances **only after** a batch has been
delivered, which makes the guarantee at-least-once: a crash between sending and saving repeats an
alert, and only the opposite ordering could lose one. On a database alerting has never run
against, the tail starts at the log's *end* — a soak switched on after three weeks must alert on
what happens next, not replay three weeks of history into somebody's phone at once.

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
from datetime import timedelta

from tradebot.core.clock import Clock
from tradebot.core.errors import TradebotError
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
        return await self._cursor.save(
            cursor.model_copy(
                update={
                    "last_seq": self._store.last_seq(),
                    "last_summary_day": await self._session_day(),
                }
            )
        )

    async def poll(self) -> tuple[Alert, ...]:
        """Read what is new, deliver what it justifies, and record how far we got."""
        if not self.enabled:
            return ()
        cursor = await self.start()
        delivered = await self._drain(cursor)
        summary = await self._summary(self._cursor.load())
        return delivered + summary

    async def run(self, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Poll until cancelled. What `run` and `serve` start alongside the supervisor."""
        if not self.enabled:
            logger.info("no alert destination configured; not tailing the log")
            return
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

    async def _drain(self, cursor: AlertCursor) -> tuple[Alert, ...]:
        """Deliver the next batch, one event at a time, saving after each one that lands.

        Per event rather than per batch, because the cursor may only ever describe what has
        actually been sent. A failure stops the drain where it is: the events behind it stay
        unread until the destination is back, and none of them is lost.
        """
        events = self._store.read_after(cursor.last_seq, *ALERT_TYPES, limit=self._batch)
        state = RuleState(cursor.degraded_streak, self._streak_limit, cursor.stale_streak)
        delivered: list[Alert] = []
        for event in events:
            alert = evaluate(event, state)
            if alert is not None and not await self._deliver(alert):
                break
            cursor = await self._cursor.save(
                cursor.model_copy(
                    update={
                        "last_seq": event.seq or 0,
                        "degraded_streak": state.degraded_streak,
                        "stale_streak": state.stale_streak,
                    }
                )
            )
            if alert is not None:
                delivered.append(alert)
        return tuple(delivered)

    async def _summary(self, cursor: AlertCursor) -> tuple[Alert, ...]:
        """One summary per session day, covering the day that just ended."""
        today = await self._session_day()
        if cursor.last_summary_day == today:
            return ()
        alert = self._daily_summary(cursor.last_summary_day)
        if not await self._deliver(alert):
            return ()
        await self._cursor.save(cursor.model_copy(update={"last_summary_day": today}))
        return (alert,)

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
