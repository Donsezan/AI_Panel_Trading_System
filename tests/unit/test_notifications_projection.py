"""The notifications read model: folded from two events, and only from those two.

Dismissal is in the **log**, not beside it, so "who cleared the reconciliation-mismatch notice,
and when" is answerable from the audit trail and survives a rebuild unchanged (spec §5.5, D6).
The two properties worth being pedantic about are both here: a re-recorded notification must
never resurrect a dismissed row, and a replay must land on exactly the state incremental
application produced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select

from tradebot.core.events import Event, EventType
from tradebot.interfaces.alerts import AlertKind, Severity
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.schema import notifications
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)


@pytest.fixture
async def store() -> AsyncIterator[EventStore]:
    engine = create_database(None)
    writer = SingleWriter(engine)
    yield EventStore(engine, writer)
    writer.close()


def raised(
    alert_id: str = "10:kill_switch",
    *,
    kind: AlertKind = AlertKind.KILL_SWITCH,
    at: datetime = NOW,
    event_seq: int = 10,
    title: str = "Kill switch tripped",
) -> Event:
    """A `NOTIFICATION_RAISED` shaped exactly as `AlertDispatcher._raise` writes one."""
    return Event(
        ts=at,
        type=EventType.NOTIFICATION_RAISED,
        aggregate_id="notifications",
        payload={
            "alert_id": alert_id,
            "kind": kind.value,
            "severity": kind.severity.value,
            "at": at.isoformat(),
            "scope": "portfolio",
            "title": title,
            "body": "drawdown 12% below the mark",
            "event_seq": event_seq,
        },
    )


def dismissed(alert_id: str = "10:kill_switch", *, at: datetime = NOW) -> Event:
    return Event(
        ts=at,
        type=EventType.ALERT_DISMISSED,
        aggregate_id="notifications",
        payload={"alert_id": alert_id, "actor": "dashboard"},
    )


def rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [row._asdict() for row in connection.execute(select(notifications))]


class TestProjection:
    async def test_a_raised_notification_becomes_a_row(self, store: EventStore) -> None:
        await store.append(raised())

        (row,) = rows(store.engine)
        assert row["alert_id"] == "10:kill_switch"
        assert row["kind"] == AlertKind.KILL_SWITCH.value
        assert row["severity"] == Severity.HIGH.value
        assert row["event_seq"] == 10
        assert row["dismissed_at"] is None

    async def test_the_alerts_own_instant_is_kept_not_the_moment_it_was_recorded(
        self, store: EventStore
    ) -> None:
        """A kill switch that tripped at 03:12 reads as 03:12, not as the poll that noticed."""
        tripped = NOW - timedelta(minutes=7)
        await store.append(raised(at=tripped))

        (row,) = rows(store.engine)
        assert row["at"] == tripped

    async def test_the_same_alert_raised_twice_stays_one_row(self, store: EventStore) -> None:
        """Deterministic identity is what makes a recording retry harmless (spec §5.5)."""
        await store.append(raised())
        await store.append(raised())

        assert len(rows(store.engine)) == 1

    async def test_a_retry_does_not_resurrect_a_dismissed_row(self, store: EventStore) -> None:
        """The 03:20 re-record must not undo the 03:12 dismissal.

        Which is why the insert ignores a conflict rather than upserting: writing the payload
        columns again would clear `dismissed_at` with it.
        """
        await store.append(raised())
        await store.append(dismissed())

        await store.append(raised())

        (row,) = rows(store.engine)
        assert row["dismissed_at"] is not None

    async def test_dismissal_records_who_and_when(self, store: EventStore) -> None:
        await store.append(raised())

        await store.append(dismissed(at=NOW + timedelta(minutes=3)))

        (row,) = rows(store.engine)
        assert row["dismissed_at"] == NOW + timedelta(minutes=3)
        assert row["dismissed_by"] == "dashboard"

    async def test_a_second_dismissal_keeps_the_first_ones_provenance(
        self, store: EventStore
    ) -> None:
        """Two tabs, one notice. The audit line is who cleared it, not who clicked last."""
        await store.append(raised())
        await store.append(dismissed(at=NOW))

        await store.append(dismissed(at=NOW + timedelta(hours=1)))

        (row,) = rows(store.engine)
        assert row["dismissed_at"] == NOW

    async def test_dismissing_something_that_was_never_raised_changes_nothing(
        self, store: EventStore
    ) -> None:
        """A stale browser tab posting a dead id must not be an error, or a row from nowhere."""
        await store.append(dismissed("999:kill_switch"))

        assert rows(store.engine) == []


class TestSupersession:
    """One "housekeeping ran" line at a time, so reassurance never stacks (spec §5.4, D7)."""

    async def test_a_new_maintenance_ok_supersedes_the_previous_one(
        self, store: EventStore
    ) -> None:
        await store.append(raised("1:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=1))

        await store.append(
            raised(
                "2:maintenance_ok",
                kind=AlertKind.MAINTENANCE_OK,
                at=NOW + timedelta(days=1),
                event_seq=2,
            )
        )

        by_id = {row["alert_id"]: row for row in rows(store.engine)}
        assert by_id["1:maintenance_ok"]["dismissed_by"] == "system"
        assert by_id["2:maintenance_ok"]["dismissed_at"] is None

    async def test_it_supersedes_only_its_own_kind(self, store: EventStore) -> None:
        """A green housekeeping line must never clear a kill-switch notice sitting beside it."""
        await store.append(raised())

        await store.append(raised("2:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=2))

        by_id = {row["alert_id"]: row for row in rows(store.engine)}
        assert by_id["10:kill_switch"]["dismissed_at"] is None

    async def test_a_failure_notice_is_never_superseded(self, store: EventStore) -> None:
        """Yesterday's failure stays until a human has seen it, whatever happened since."""
        await store.append(
            raised("1:maintenance_failed", kind=AlertKind.MAINTENANCE_FAILED, event_seq=1)
        )

        await store.append(
            raised(
                "2:maintenance_failed",
                kind=AlertKind.MAINTENANCE_FAILED,
                at=NOW + timedelta(days=1),
                event_seq=2,
            )
        )

        assert all(row["dismissed_at"] is None for row in rows(store.engine))

    async def test_supersession_leaves_an_operators_own_dismissal_alone(
        self, store: EventStore
    ) -> None:
        """It marks *undismissed* rows, so the log still says who cleared what."""
        await store.append(raised("1:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=1))
        await store.append(dismissed("1:maintenance_ok"))

        await store.append(raised("2:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=2))

        by_id = {row["alert_id"]: row for row in rows(store.engine)}
        assert by_id["1:maintenance_ok"]["dismissed_by"] == "dashboard"


class TestRebuild:
    async def test_a_rebuild_reproduces_the_notifications_and_their_dismissals(
        self, store: EventStore
    ) -> None:
        """Dismissal is in the log, not beside it, so a replay lands on the same state."""
        await store.append(raised())
        await store.append(raised("11:data_stale", kind=AlertKind.DATA_STALE, event_seq=11))
        await store.append(dismissed())
        before = rows(store.engine)

        await store.rebuild()

        assert rows(store.engine) == before

    async def test_a_rebuild_reproduces_supersession(self, store: EventStore) -> None:
        """It is derived from the log, exactly like an operator's own dismissal."""
        await store.append(raised("1:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=1))
        await store.append(raised("2:maintenance_ok", kind=AlertKind.MAINTENANCE_OK, event_seq=2))
        before = rows(store.engine)

        await store.rebuild()

        assert rows(store.engine) == before
