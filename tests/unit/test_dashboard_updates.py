"""The live-update transport: a pure mapping, a log tail, and a socket that carries no commands.

Tested to the money-path standard even though it moves no money, because it is what decides
whether an operator mid-incident is looking at the present or at a page from ten minutes ago
(PHASE_10 §Passes). Three properties carry the weight:

* the dispatch table is **data**, so it is asserted as data — every tailed type maps somewhere,
  and the noisy per-seat types are absent by assertion rather than by accident;
* the tail **advances and never rewinds**, so a burst is one refresh and a quiet log is silence;
* a socket that dies takes nothing with it, because the tail feeds every other watcher after it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from tests.conftest import DASHBOARD_TOKEN as TOKEN
from tests.conftest import ASGIWebSocket

from tradebot.app import Application
from tradebot.core.events import Event, EventType
from tradebot.dashboard.auth import SESSION_COOKIE, Session
from tradebot.dashboard.updates import (
    PANES_BY_EVENT,
    TAILED_TYPES,
    Pane,
    UpdateHub,
    panes_for,
)
from tradebot.persistence.store import EventStore

TS = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: Deliberately unmapped: the drill-down's material, reached by clicking a log row. Tailing these
#: would refresh six panes per seat per cycle to show nothing that changed on screen.
DRILL_DOWN_ONLY = (
    EventType.SEAT_RESPONDED,
    EventType.SNAPSHOT_FROZEN,
    EventType.RISK_CHECKED,
    EventType.SHADOW_EVALUATED,
)


#: What `_project_risk_event` reads. Carried because appending goes through the projectors, and
#: an event the read model cannot fold is not an event the log would ever hold.
RISK_PAYLOAD = {
    "tier": "tier_2",
    "rule": "max_daily_loss",
    "scope": "global",
    "action_taken": "veto",
    "detail": "",
}


def event(event_type: EventType, seq: int | None = None) -> Event:
    payload = RISK_PAYLOAD if event_type is EventType.RISK_EVENT else {}
    return Event(ts=TS, type=event_type, aggregate_id="demo", seq=seq, payload=payload)


#: Short enough that a loop test finishes promptly, long enough that the tail yields real time
#: between reads instead of spinning.
TICK = 0.005


def signed_cookie() -> str:
    return f"{SESSION_COOKIE}={Session(TOKEN).issue()}"


@asynccontextmanager
async def _ticking(hub: UpdateHub, socket: FakeSocket) -> AsyncIterator[None]:
    """Run the hub's loop for the body, and always stop it afterwards."""
    await hub.register(socket)
    try:
        yield
    finally:
        await hub.stop()


class FakeSocket:
    """Records what it was told to refresh. Optionally dies on send, like a closed peer."""

    def __init__(self, *, broken: bool = False) -> None:
        self.notices: list[Any] = []
        self.accepted = False
        #: Set on every notice, so a loop test waits for the tail rather than polling it.
        self.notified = asyncio.Event()
        self._broken = broken

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: Any) -> None:
        if self._broken:
            raise ConnectionResetError("peer is gone")
        self.notices.append(data)
        self.notified.set()

    async def wait(self, within: float = 2.0) -> None:
        await asyncio.wait_for(self.notified.wait(), within)


# ---------------------------------------------------------------- the dispatch table


def test_every_tailed_type_maps_to_at_least_one_pane() -> None:
    """A type read from the log but routed nowhere is a poll that costs and shows nothing."""
    for event_type in TAILED_TYPES:
        assert PANES_BY_EVENT[event_type], event_type


def test_the_tail_reads_exactly_what_it_routes() -> None:
    assert set(TAILED_TYPES) == set(PANES_BY_EVENT)


@pytest.mark.parametrize("event_type", DRILL_DOWN_ONLY)
def test_the_noisy_types_are_not_tailed(event_type: EventType) -> None:
    assert event_type not in PANES_BY_EVENT


def test_every_pane_is_reachable() -> None:
    """A pane no event can invalidate would never refresh, and would go quietly stale."""
    routed = frozenset().union(*PANES_BY_EVENT.values())
    assert routed == set(Pane)


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (EventType.FILL_RECEIVED, Pane.PORTFOLIO),
        (EventType.CYCLE_COMPLETED, Pane.LOG),
        (EventType.DECISION_MADE, Pane.CHART),
        (EventType.RISK_EVENT, Pane.RC),
        (EventType.KILL_SWITCH_CHANGED, Pane.CONTROLS),
        # Quarantine is versioned configuration (ADR 0022), so the safety pane has to watch
        # config changes and not only risk events.
        (EventType.CONFIG_CHANGED, Pane.RC),
    ],
)
def test_an_event_reaches_the_pane_that_shows_it(event_type: EventType, expected: Pane) -> None:
    assert expected in panes_for([event(event_type)])


def test_panes_are_unioned_across_a_batch() -> None:
    panes = panes_for([event(EventType.FILL_RECEIVED), event(EventType.RISK_EVENT)])
    assert {Pane.PORTFOLIO, Pane.CHART, Pane.RC} <= panes


def test_an_unmapped_event_invalidates_nothing() -> None:
    assert panes_for([event(EventType.SEAT_RESPONDED)]) == frozenset()


def test_no_events_invalidate_nothing() -> None:
    """The quiet case, which the tail hits on almost every tick."""
    assert panes_for([]) == frozenset()


# ---------------------------------------------------------------- the tail
#
# Driven a tick at a time, with no socket connected and therefore no background task: the
# cursor's behaviour is what is under test, and a loop running beside the assertions would be
# draining the same log they are.


@pytest.fixture
async def hub(store: EventStore) -> AsyncIterator[UpdateHub]:
    """Always stopped afterwards, so a test that fails mid-way leaves no task polling behind it."""
    hub = UpdateHub(store)
    yield hub
    await hub.stop()


async def append(store: EventStore, *types: EventType) -> None:
    await store.append(*(event(event_type) for event_type in types))


async def test_a_fresh_tail_starts_at_the_end_of_the_log(hub: UpdateHub, store: EventStore) -> None:
    """The page that opened this tail has just rendered; its history is already on screen."""
    await append(store, EventType.EXTERNAL_CHANGE, EventType.RISK_EVENT)
    assert hub.drain() == frozenset()


async def test_the_tail_reports_what_arrived_after_it_started(
    hub: UpdateHub, store: EventStore
) -> None:
    hub.drain()
    await append(store, EventType.RISK_EVENT)
    assert Pane.RC in hub.drain()


async def test_a_drained_event_is_not_reported_twice(hub: UpdateHub, store: EventStore) -> None:
    """The cursor advances past what it read, or every tick would refresh the whole workspace."""
    hub.drain()
    await append(store, EventType.RISK_EVENT)
    assert hub.drain()
    assert hub.drain() == frozenset()


async def test_a_burst_larger_than_a_batch_drains_in_one_tick(store: EventStore) -> None:
    """Catching up within the tick is what keeps a long backlog from taking a hundred seconds."""
    small = UpdateHub(store, batch=2)
    small.drain()
    await append(store, *([EventType.EXTERNAL_CHANGE] * 5), EventType.RISK_EVENT)
    assert {Pane.PORTFOLIO, Pane.RC} <= small.drain()
    assert small.drain() == frozenset()


async def test_the_tail_ignores_types_it_does_not_route(hub: UpdateHub, store: EventStore) -> None:
    hub.drain()
    await append(store, EventType.SEAT_RESPONDED, EventType.SHADOW_EVALUATED)
    assert hub.drain() == frozenset()


async def test_a_restarted_tail_does_not_replay_what_it_slept_through(
    hub: UpdateHub, store: EventStore
) -> None:
    """A reconnecting page renders everything anyway; replaying the gap is work with no effect."""
    await hub.register(FakeSocket())
    await hub.stop()
    await append(store, EventType.RISK_EVENT)
    assert hub.drain() == frozenset()


# ---------------------------------------------------------------- broadcast


async def test_a_notice_names_panes_and_nothing_else(hub: UpdateHub) -> None:
    """The socket carries no data, so a payload key beyond `panes` is a contract break."""
    socket = FakeSocket()
    await hub.register(socket)
    await hub.broadcast(frozenset({Pane.RC, Pane.BLOTTER}))
    assert socket.notices == [{"panes": ["blotter", "rc"]}]


async def test_nothing_is_sent_when_nothing_changed(hub: UpdateHub) -> None:
    socket = FakeSocket()
    await hub.register(socket)
    await hub.broadcast(frozenset())
    assert socket.notices == []


async def test_a_dead_socket_is_dropped_and_the_others_are_still_told(hub: UpdateHub) -> None:
    """One closed tab must not cost another operator their live view."""
    broken, alive = FakeSocket(broken=True), FakeSocket()
    await hub.register(broken)
    await hub.register(alive)
    await hub.broadcast(frozenset({Pane.RC}))
    assert alive.notices == [{"panes": ["rc"]}]
    assert hub.watching == 1


# ---------------------------------------------------------------- lifecycle


async def test_nothing_polls_the_log_until_a_page_is_watching(hub: UpdateHub) -> None:
    """The whole reason a headless run and the test suite cost nothing for this feature."""
    assert not hub.running
    assert hub.watching == 0


async def test_the_tail_starts_with_the_first_socket_and_stops_with_the_last(
    hub: UpdateHub,
) -> None:
    first, second = FakeSocket(), FakeSocket()
    await hub.register(first)
    await hub.register(second)
    assert hub.running

    await hub.unregister(first)
    assert hub.running, "one watcher left; the tail is still needed"

    await hub.unregister(second)
    assert not hub.running


async def test_stopping_twice_is_harmless(hub: UpdateHub) -> None:
    await hub.register(FakeSocket())
    await hub.stop()
    await hub.stop()
    assert not hub.running


async def test_the_loop_delivers_what_a_tick_finds(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick is drain-then-broadcast, asserted without racing a writer (see `_ticking`)."""
    hub = UpdateHub(store, interval=TICK)
    socket = FakeSocket()
    monkeypatch.setattr(hub, "drain", lambda: frozenset({Pane.RC}))
    async with _ticking(hub, socket):
        await socket.wait()
    assert socket.notices[0] == {"panes": ["rc"]}


async def test_a_failing_tick_does_not_kill_the_tail(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that raises is retried on the next tick; a dead tail is a silently stale page."""
    hub = UpdateHub(store, interval=TICK)
    socket = FakeSocket()
    tried_three_times = asyncio.Event()
    attempts = 0

    def explode() -> frozenset[Pane]:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            tried_three_times.set()
        raise RuntimeError("the database went away")

    monkeypatch.setattr(hub, "drain", explode)
    async with _ticking(hub, socket):
        await asyncio.wait_for(tried_three_times.wait(), 2.0)
        assert hub.running, "three failed reads in a row must not have ended the tail"

        monkeypatch.setattr(hub, "drain", lambda: frozenset({Pane.RC}))
        await socket.wait()
    assert socket.notices[0] == {"panes": ["rc"]}


# ---------------------------------------------------------------- the socket route
#
# The fan-out is driven explicitly rather than by waiting on a tick. Every route test would
# otherwise have to write to the database *while* the tail reads it, and under the in-memory
# `StaticPool` the suite uses those share one connection: a reader returning it to the pool
# rolls back the writer's open transaction, and the append succeeds having written nothing.
# Production is a file database with a connection per checkout and is unaffected — but a test
# that races it is testing the harness, not the transport.


async def test_the_socket_carries_a_notice_to_a_live_page(dashboard: FastAPI) -> None:
    """End to end: auth, handshake, fan-out and wire format, over a real ASGI socket."""
    hub = dashboard.state.dashboard.updates
    async with ASGIWebSocket(dashboard, "/ws/updates", cookie=signed_cookie()) as socket:
        assert socket.accepted
        await hub.broadcast(frozenset({Pane.RC, Pane.BLOTTER}))
        assert await socket.receive_json() == {"panes": ["blotter", "rc"]}


async def test_an_open_page_is_registered_with_the_hub(dashboard: FastAPI) -> None:
    hub = dashboard.state.dashboard.updates
    async with ASGIWebSocket(dashboard, "/ws/updates", cookie=signed_cookie()):
        assert hub.watching == 1
        assert hub.running


async def test_an_inbound_frame_is_discarded_unread(
    dashboard: FastAPI, sim_application: Application
) -> None:
    """There is no command surface here: a frame the browser sends must do exactly nothing."""
    hub = dashboard.state.dashboard.updates
    before = sim_application.states.load()
    async with ASGIWebSocket(dashboard, "/ws/updates", cookie=signed_cookie()) as socket:
        await socket.send_text('{"action": "kill", "confirm": "RE-ARM TRADING"}')
        await hub.broadcast(frozenset({Pane.RC}))
        assert await socket.receive_json() == {"panes": ["rc"]}
        assert hub.watching == 1, "the frame must not have closed the socket either"
    assert sim_application.states.load().kill_switch == before.kill_switch


async def test_a_closed_page_stops_the_tail(dashboard: FastAPI) -> None:
    """A browser tab closing must leave nothing polling the database behind it."""
    hub = dashboard.state.dashboard.updates
    async with ASGIWebSocket(dashboard, "/ws/updates", cookie=signed_cookie()):
        assert hub.running
    assert not hub.running
    assert hub.watching == 0
