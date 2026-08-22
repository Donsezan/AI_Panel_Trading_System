"""Live updates: a log tail that tells connected pages which panes went stale.

The transport is **read-only by construction** (ADR 0024). It carries pane names and nothing
else — no data, no commands — so the refresh itself is an ordinary authenticated GET through the
full middleware stack, rendered by the same templates and filters a navigation would use. There
is no second rendering path, and a hijacked socket can cause extra page refreshes and nothing
more. That is the same property ADR 0019 gives alerting: it tails the log beside the money path
and can never reach it.

The tail is the pattern `AlertDispatcher.poll` already uses — read after a `seq`, act, advance —
minus the persisted cursor. There is deliberately nothing to resume: a socket that missed ten
notices is healed by the next full refresh, so a fresh tail starts at the log's end and a
reconnect starts there again.

**The poll interval is the debounce window.** One tick reads everything that arrived, unions the
panes those events touch, and sends at most one notice — so a burst of a hundred fills is one
refresh, not a hundred.

The tail runs only while a socket is connected. A headless `run`, a closed browser tab and the
whole test suite therefore cost nothing: no task, no polling, no clock.

Failure semantics: neither a failing store read nor a dead socket may stop the tail. A read that
raises is logged and retried on the next tick; a socket that raises on send is dropped, because a
peer that cannot be written to is already gone. The visible consequence of a stalled tail is a
page that stops refreshing, which the browser reports through the reconnect pill — silence is the
one thing this transport must not do quietly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Protocol

from tradebot.core.events import Event, EventType
from tradebot.core.logging import get_logger
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

__all__ = ["PANES_BY_EVENT", "Pane", "UpdateHub", "panes_for"]

#: How often the log is read, and therefore the most often any pane can be asked to refresh.
#: Seconds; a cycle takes minutes, so this is already far finer than the thing it reports on.
#:
#: Paced with `asyncio.sleep` rather than the injected `Clock`, and that is deliberate despite the
#: repo-wide rule. This is a **transport** interval, not domain time: nothing here timestamps,
#: ages or expires anything, so none of the testability the rule protects is at stake. Pacing it
#: on simulated time would instead be actively wrong — a backtest stepping its clock a month
#: forward would spin this poll a million times, and a `ManualClock`, whose `sleep` returns
#: immediately, turns it into a busy loop that reads the database as fast as the loop allows.
DEFAULT_INTERVAL = 1.0

#: Events read per store call. A catch-up drains in batches rather than one unbounded read, so a
#: log with a day of history behind it cannot allocate itself into trouble on the first tick.
DEFAULT_BATCH = 500


class Pane(StrEnum):
    """The workspace's independently refreshable regions (PHASE_10 §Pane contracts).

    The value is what crosses the socket and what the browser matches against `hx-trigger`, so
    these strings are a wire contract: renaming one renames it in the templates too.
    """

    PORTFOLIO = "portfolio"
    BLOTTER = "blotter"
    CHART = "chart"
    LOG = "log"
    CONTROLS = "controls"
    RC = "rc"
    #: The header's notification bell. Not one of the workspace's six panes — it lives above the
    #: grid and is on every page — but it refreshes by exactly the same mechanism.
    NOTIFICATIONS = "notifications"


#: Which panes an event invalidates. The whole routing decision of this module, as data.
#:
#: Only event types that change something *visible* appear. `SEAT_RESPONDED`, `SNAPSHOT_FROZEN`
#: and `RISK_CHECKED` are deliberately absent: they are the drill-down's material, reached by
#: clicking a log row, and tailing them would refresh six panes per seat per cycle to show
#: nothing new. Their absence also narrows the store read (`TAILED_TYPES`).
PANES_BY_EVENT: dict[EventType, frozenset[Pane]] = {
    # Money moved.
    EventType.FILL_RECEIVED: frozenset({Pane.PORTFOLIO, Pane.BLOTTER, Pane.CHART}),
    EventType.POSITION_UPDATED: frozenset({Pane.PORTFOLIO, Pane.BLOTTER, Pane.CONTROLS}),
    EventType.ROUND_TRIP_CLOSED: frozenset({Pane.PORTFOLIO, Pane.BLOTTER, Pane.LOG}),
    # The venue's view of ours.
    EventType.RECONCILED: frozenset({Pane.PORTFOLIO, Pane.RC}),
    EventType.EXTERNAL_CHANGE: frozenset({Pane.PORTFOLIO, Pane.RC}),
    EventType.CORPORATE_ACTION: frozenset({Pane.PORTFOLIO, Pane.RC}),
    # A basket cycling.
    EventType.CYCLE_STARTED: frozenset({Pane.BLOTTER}),
    EventType.CYCLE_COMPLETED: frozenset({Pane.BLOTTER, Pane.LOG}),
    EventType.DECISION_MADE: frozenset({Pane.BLOTTER, Pane.CHART, Pane.LOG}),
    # Orders working at the venue: what may still be closed by hand depends on them.
    EventType.ORDER_SUBMITTED: frozenset({Pane.CHART, Pane.CONTROLS}),
    EventType.ORDER_STATE_CHANGED: frozenset({Pane.CHART, Pane.CONTROLS}),
    EventType.PROTECTIVE_PLACED: frozenset({Pane.CHART}),
    # Safety states. Quarantine is versioned configuration (ADR 0022), which is why a config
    # change reaches the RC pane and not only the panes that show a limit.
    EventType.RISK_EVENT: frozenset({Pane.RC}),
    EventType.KILL_SWITCH_CHANGED: frozenset({Pane.RC, Pane.CONTROLS, Pane.BLOTTER}),
    EventType.BASKET_STATUS_CHANGED: frozenset({Pane.RC, Pane.CONTROLS, Pane.BLOTTER}),
    EventType.CONFIG_CHANGED: frozenset({Pane.RC, Pane.CONTROLS, Pane.BLOTTER}),
    # The bell, and *only* these two. Deliberately not the five types the alert rules read: a
    # kill-switch trip reaches this tail immediately, but the notification it produces is written
    # later, when the dispatcher next polls — so keying on the trip would repaint the widget
    # before there was anything new in it, and then never repaint it again (spec §5.7).
    EventType.NOTIFICATION_RAISED: frozenset({Pane.NOTIFICATIONS}),
    EventType.ALERT_DISMISSED: frozenset({Pane.NOTIFICATIONS}),
}

#: What the tail asks the store for. Narrowing here is what keeps a soak's transcripts and frozen
#: snapshots out of a poll that runs every second (ADR 0016's `read_types` rationale).
TAILED_TYPES: tuple[EventType, ...] = tuple(PANES_BY_EVENT)


def panes_for(events: Iterable[Event]) -> frozenset[Pane]:
    """Every pane the given events invalidate, as one set. Unmapped types contribute nothing."""
    return frozenset().union(*(PANES_BY_EVENT.get(event.type, frozenset()) for event in events))


class Notifiable(Protocol):
    """What the hub needs of a socket: a handshake to complete, and somewhere to put a notice."""

    async def accept(self) -> None: ...

    async def send_json(self, data: Any) -> None: ...


class UpdateHub:
    """Tails the event log while any page is watching, and nudges the panes that went stale."""

    def __init__(
        self,
        store: EventStore,
        *,
        interval: float = DEFAULT_INTERVAL,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._store = store
        self._interval = interval
        self._batch = batch
        self._sockets: set[Notifiable] = set()
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        #: Whether `_seq` means anything yet. Cleared by `stop`, so a tail that starts again
        #: anchors afresh at the log's end rather than replaying whatever it slept through.
        self._anchored = False

    @property
    def watching(self) -> int:
        """How many pages are connected. Zero means nothing is polling the log."""
        return len(self._sockets)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def register(self, socket: Notifiable) -> None:
        """Anchor the cursor, complete the handshake, then watch — strictly in that order.

        The order is the whole point, and it is why the handshake happens here rather than in the
        route. Anchor *before* accepting, and nothing appended from the moment the client is told
        it is live can be missed. Add to the fan-out only *after* accepting, because a notice sent
        to a socket that has not completed its handshake raises, and this hub drops a socket that
        raises. Either half on its own is a page that quietly stops updating.
        """
        self._anchor()
        await socket.accept()
        self._sockets.add(socket)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="dashboard-updates")

    async def unregister(self, socket: Notifiable) -> None:
        """Stop watching for this socket, stopping the tail when it was the last."""
        self._sockets.discard(socket)
        if not self._sockets:
            await self.stop()

    async def stop(self) -> None:
        """Cancel the tail and forget where it was. Idempotent; safe from the app's shutdown."""
        task, self._task = self._task, None
        self._anchored = False
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def drain(self) -> frozenset[Pane]:
        """Read everything appended since the last call; return the panes it invalidates.

        An unanchored tail anchors itself and reports nothing, so draining is safe before any
        socket has registered.

        Drains in batches until one comes back short, so a tick that opens against a long backlog
        catches up within that tick instead of over the next hundred.
        """
        if self._anchor():
            return frozenset()
        panes: frozenset[Pane] = frozenset()
        while events := self._store.read_after(self._seq, *TAILED_TYPES, limit=self._batch):
            panes |= panes_for(events)
            self._seq = max(event.seq or self._seq for event in events)
            if len(events) < self._batch:
                break
        return panes

    async def broadcast(self, panes: frozenset[Pane]) -> None:
        """Send one notice to every watcher, dropping those that cannot receive it."""
        if not panes:
            return
        notice = {"panes": sorted(panes)}
        for socket in tuple(self._sockets):
            try:
                await socket.send_json(notice)
            except Exception:
                # Broad on purpose: every way a socket can be gone — closed, reset, half-open —
                # arrives here as some exception, and none of them may stop the other watchers
                # being told. The peer is dropped rather than retried; it reconnects by itself.
                logger.debug("dropping a dashboard socket that could not be written to")
                self._sockets.discard(socket)

    def _anchor(self) -> bool:
        """Anchor an unanchored cursor at the log's end. True if this call did it.

        The end, not the beginning: the page that opened this tail has just rendered every pane,
        so the history behind it is already on the screen. That is also why there is no persisted
        cursor — a socket that missed ten notices is healed by the next full refresh, so there is
        nothing to resume and nothing to resume wrongly (ADR 0024).
        """
        if self._anchored:
            return False
        self._seq = self._store.last_seq()
        self._anchored = True
        return True

    async def _run(self) -> None:
        """Tick until cancelled. A failing read is logged and retried, never fatal."""
        while True:
            try:
                await self.broadcast(self.drain())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("the dashboard update tail failed; retrying on the next tick")
            await asyncio.sleep(self._interval)
