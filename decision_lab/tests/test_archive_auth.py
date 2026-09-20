"""Crawling as a logged-in subscriber, so the publisher serves the article text (spec §6.3.1).

CoinDesk meters the body at three articles per session. The archive's answer to that is **not** to
look like more than one visitor — cycling clients or identities is paywall circumvention and this
tool will not do it. The answer is to present the operator's own subscription, once, from one
honest client.

That constrains the design more than it might look:

* **A session cookie, never a password.** Logging in means POSTing to `/auth/`, which CoinDesk's
  own `robots.txt` disallows — so a crawl that logged itself in would have to ignore the very
  file `FeedFetcher` exists to honour, on its first act. A cookie lifted from a browser the
  operator already logged into needs no such request.
* **The `User-Agent` stays honest.** Adding a cookie makes us an authenticated subscriber; it must
  not also make us a fake browser. Asserted structurally here, because that is the line between
  using a subscription and defeating a meter.
* **Absent means refused, never anonymous.** An `--authenticated` pass that quietly fell back to
  an anonymous client would spend 5 678 requests and gain three bodies, and the report would say
  the subscription does not lift the meter. Fail closed (CLAUDE.md non-negotiable 1).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from decision_lab.archive.auth import (
    COOKIE_VARIABLE,
    authenticated_fetcher,
    cookie_from_environment,
    cookies_in,
)
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS
from tradebot.news.http import DEFAULT_USER_AGENT

NOW = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


def test_a_pasted_cookie_header_becomes_the_pairs_it_names() -> None:
    assert cookies_in("session=abc123; consent=yes") == {"session": "abc123", "consent": "yes"}


def test_a_cookie_value_containing_an_equals_sign_survives() -> None:
    """Session tokens are base64 and routinely carry `=` padding; splitting on every `=` eats it."""
    assert cookies_in("token=eyJhbGci=") == {"token": "eyJhbGci="}


def test_a_header_naming_no_pair_is_refused() -> None:
    """A truncated paste must not read as "no cookies" and then crawl anonymously for hours."""
    with pytest.raises(ConfigError, match="no name=value pair"):
        cookies_in("just-some-text")


def test_an_unset_variable_is_refused_by_name() -> None:
    with pytest.raises(ConfigError, match=COOKIE_VARIABLE):
        cookie_from_environment(environ={})


def test_a_blank_variable_is_refused_like_an_absent_one() -> None:
    with pytest.raises(ConfigError, match=COOKIE_VARIABLE):
        cookie_from_environment(environ={COOKIE_VARIABLE: "   "})


def test_the_cookie_is_registered_with_the_redactor() -> None:
    """Credentials are environment-only and redactor-registered (ADR 0019's rule, applied here)."""
    SECRETS.clear()
    try:
        cookie_from_environment(environ={COOKIE_VARIABLE: "session=super-secret-value"})
        assert "super-secret-value" not in SECRETS.scrub("cookie: session=super-secret-value")
    finally:
        SECRETS.clear()


async def test_the_session_travels_on_every_request() -> None:
    fetcher = authenticated_fetcher(ManualClock(NOW), cookie="session=abc123")
    try:
        assert fetcher.cookie_names == ("session",)
    finally:
        await fetcher.close()


async def test_an_authenticated_client_still_identifies_itself_honestly() -> None:
    """A subscription cookie is legitimate access; a spoofed browser string is not.

    This is the line between using a subscription and defeating a meter, so it is pinned rather
    than left to the reader of `auth.py`.
    """
    fetcher = authenticated_fetcher(ManualClock(NOW), cookie="session=abc123")
    try:
        assert fetcher.user_agent == DEFAULT_USER_AGENT
        assert "Mozilla" not in fetcher.user_agent
    finally:
        await fetcher.close()
