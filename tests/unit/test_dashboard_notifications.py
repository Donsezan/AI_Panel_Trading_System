"""The notification bell: three counts, a dropdown, and a dismissal that is an audited act.

Two hazards this file exists to hold down. The `<details>` must never be the swap target — htmx
replacing it would snap the dropdown shut while someone is reading it, up to once a second
(spec §5.7). And the counts must render all three numbers including zeros, because a widget that
reflows on every socket tick moves the Log out link under the cursor of someone mid-incident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tradebot.app import Application
from tradebot.core.events import Event, EventType
from tradebot.dashboard.updates import PANES_BY_EVENT, Pane
from tradebot.interfaces.alerts import AlertKind

NOW = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)


async def raise_alert(
    application: Application,
    alert_id: str,
    *,
    kind: AlertKind = AlertKind.KILL_SWITCH,
    title: str = "Kill switch tripped",
    at: datetime = NOW,
) -> None:
    await application.store.append(
        Event(
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
                "event_seq": 10,
            },
        )
    )


def trigger_of(page: str, region: str) -> str:
    """The `hx-trigger` on one of the bell's two regions, as written into the page."""
    return page.split(f'id="{region}"')[1].split('hx-trigger="')[1].split('"')[0]


def events_of(trigger: str) -> set[str]:
    """The event names a trigger listens for, with their filters and modifiers dropped."""
    return {spec.strip().split("[")[0].split(" ")[0] for spec in trigger.split(",")}


@pytest.fixture
def head(client: httpx.AsyncClient) -> httpx.AsyncClient:
    return client


class TestCounts:
    async def test_all_three_counts_render_including_zeros(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """`1 | 0 | 3`, never `1 | 3` — a widget that reflows moves Log out under the cursor."""
        await raise_alert(sim_application, "1:kill_switch")
        for index in range(3):
            await raise_alert(
                sim_application,
                f"{index}:daily_summary",
                kind=AlertKind.DAILY_SUMMARY,
                title="Daily summary",
            )

        page = (await client.get("/")).text

        counts = page.split('id="notification-counts"')[1].split("</span>\n  </summary>")[0]
        assert ">1<" in counts and ">0<" in counts and ">3<" in counts

    async def test_the_counts_are_readable_without_colour(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """Position is fixed and the label spells it out, for a greyscale screenshot."""
        await raise_alert(sim_application, "1:kill_switch")

        page = (await client.get("/")).text

        assert "1 high, 0 medium, 0 low" in page

    async def test_an_empty_bell_is_muted_rather_than_hidden(
        self, client: httpx.AsyncClient
    ) -> None:
        """ "Nothing to tell you" and "the widget is broken" must not look identical."""
        page = (await client.get("/")).text

        assert 'id="notifications"' in page
        assert "0 high, 0 medium, 0 low" in page

    async def test_a_dismissed_notification_stops_counting(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        await raise_alert(sim_application, "1:kill_switch")

        await client.post("/control/notifications/1:kill_switch/dismiss", data={"scope": ""})

        assert "0 high, 0 medium, 0 low" in (await client.get("/")).text

    async def test_the_counts_are_on_every_page_not_only_the_workspace(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """It lives in the header, and an operator reading Analytics is still on duty."""
        await raise_alert(sim_application, "1:kill_switch")

        assert "1 high, 0 medium, 0 low" in (await client.get("/cycles")).text


class TestStructure:
    async def test_the_details_element_is_not_the_swap_target(
        self, client: httpx.AsyncClient
    ) -> None:
        """Swapping it would snap the dropdown shut mid-read, once a second (spec §5.7)."""
        page = (await client.get("/")).text

        opening = page.split('<details class="menu notifications" id="notifications"')[1]
        assert "hx-get" not in opening.split(">")[0]

    async def test_the_list_only_fetches_while_open(self, client: httpx.AsyncClient) -> None:
        assert "refresh[this.closest('details').open]" in (await client.get("/")).text

    async def test_opening_the_bell_refetches_both_regions(self, client: httpx.AsyncClient) -> None:
        """Found on a rendered page: the counts said `0 | 0 | 1` over "Nothing to report."

        The same contradiction as `test_the_list_is_filled_on_first_paint...`, one level on. The
        counts refresh on every socket nudge; the list refreshes only *while the dropdown is
        open*. So a notice raised while the bell was shut moved the counter and skipped the list,
        and opening the bell fetched nothing — leaving the two halves in disagreement until the
        *next* notification happened to arrive with the dropdown already open, which on a quiet
        system is never.

        `toggle` is what a `<details>` fires when it opens, and both regions take it, not just the
        list: while the socket is down the fallback poll is 30s apart, so a list that refetched
        alone would be newer than the counter above it — the same contradiction mirrored. Opening
        the bell is a read, and a read of an alert widget has to be current.
        """
        page = (await client.get("/")).text

        assert events_of(trigger_of(page, "notification-counts")) == {"refresh", "toggle"}
        assert events_of(trigger_of(page, "notification-list")) == {"refresh", "toggle"}

    async def test_a_shut_bell_still_costs_no_list_markup(self, client: httpx.AsyncClient) -> None:
        """`toggle` is filtered on `.open` too, so closing the bell fetches nothing either."""
        page = (await client.get("/")).text
        open_filter = "[this.closest('details').open]"

        # Both of the list's specs: it is fetched when opened, and refreshed only while open.
        assert trigger_of(page, "notification-list").count(open_filter) == 2
        # The counts refresh unconditionally; only their `toggle` spec is filtered.
        assert trigger_of(page, "notification-counts").count(open_filter) == 1

    async def test_both_regions_refresh_through_one_route(self, client: httpx.AsyncClient) -> None:
        """One rendering path however the request arrived — the Phase 10 rule."""
        page = (await client.get("/")).text

        assert page.count('hx-get="/workspace/notifications"') == 2

    async def test_the_fragment_renders_the_same_regions_as_first_paint(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        await raise_alert(sim_application, "1:kill_switch")

        fragment = (await client.get("/workspace/notifications")).text

        assert 'id="notification-counts"' in fragment
        assert 'id="notification-list"' in fragment
        assert "Kill switch tripped" in fragment


class TestTheList:
    async def test_it_lists_every_undismissed_notification_newest_first(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """Never a time window: an alert that scrolls out of existence by itself is the one
        behaviour this must not have (spec §5.8)."""
        await raise_alert(sim_application, "1:kill_switch", title="the older one")
        await raise_alert(
            sim_application,
            "2:recon_mismatch",
            kind=AlertKind.RECON_MISMATCH,
            title="the newer one",
            at=NOW + timedelta(hours=1),
        )

        page = (await client.get("/workspace/notifications")).text

        assert page.index("the newer one") < page.index("the older one")

    async def test_a_dismissed_row_leaves_the_list(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        await raise_alert(sim_application, "1:kill_switch")
        await client.post("/control/notifications/1:kill_switch/dismiss", data={"scope": ""})

        assert "Kill switch tripped" not in (await client.get("/workspace/notifications")).text

    async def test_an_empty_list_says_so(self, client: httpx.AsyncClient) -> None:
        assert "Nothing to report" in (await client.get("/workspace/notifications")).text

    async def test_the_list_is_filled_on_first_paint_not_only_after_a_refresh(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """Found on a rendered page: the counts said `2 | 1 | 1` over "Nothing to report."

        The context every page gets has to carry the list as well as the counts, or the dropdown
        is empty until htmx first refreshes it — which never happens with scripting off, or on
        an Analytics page, where the socket script is not even loaded. A widget saying nothing is
        wrong beside a counter saying two is the exact confusion spec §5.6 forbids.
        """
        await raise_alert(sim_application, "1:kill_switch", title="a notice with a title")

        page = (await client.get("/")).text

        assert "a notice with a title" in page
        assert "Nothing to report" not in page

    async def test_the_list_is_filled_on_pages_outside_the_workspace_too(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        await raise_alert(sim_application, "1:kill_switch", title="a notice with a title")

        assert "a notice with a title" in (await client.get("/cycles")).text


class TestDismissal:
    async def test_dismissing_appends_the_event_with_the_dashboard_as_actor(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        await raise_alert(sim_application, "1:kill_switch")

        await client.post("/control/notifications/1:kill_switch/dismiss", data={"scope": ""})

        (event,) = sim_application.store.read_types(EventType.ALERT_DISMISSED)
        assert event.payload["alert_id"] == "1:kill_switch"
        assert event.payload["actor"] == "dashboard"

    async def test_it_returns_to_the_selection_it_was_posted_from(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """An operator mid-incident must not lose the screen they were acting from."""
        await raise_alert(sim_application, "1:kill_switch")

        response = await client.post(
            "/control/notifications/1:kill_switch/dismiss",
            data={"scope": "basket:demo"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/?scope=basket%3Ademo"

    async def test_dismissing_something_already_gone_is_not_an_error(
        self, client: httpx.AsyncClient, sim_application: Application
    ) -> None:
        """Two tabs, one notice — and a stale tab must not answer with a 500."""
        response = await client.post(
            "/control/notifications/999:kill_switch/dismiss",
            data={"scope": ""},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert sim_application.store.read_types(EventType.ALERT_DISMISSED) == ()


class TestPanes:
    def test_only_the_two_notification_events_invalidate_the_widget(self) -> None:
        """Keying on a kill-switch trip would repaint before the row exists (spec §5.7).

        The trip is seen by the socket tail immediately; the notification it produces is written
        later, when the dispatcher next polls. Refreshing on the trip would repaint an unchanged
        widget and then never repaint it again.
        """
        keyed = {type_ for type_, panes in PANES_BY_EVENT.items() if Pane.NOTIFICATIONS in panes}

        assert keyed == {EventType.NOTIFICATION_RAISED, EventType.ALERT_DISMISSED}
