"""Authentication is the dashboard's only gate, so it is tested as one (ADR 0014).

The load-bearing test here is `test_every_route_is_protected`: it walks the registered routes
rather than asserting a hand-written list, so a route added in a later pass is covered by this
test the day it lands. That is the whole reason auth is middleware and not a dependency.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import DASHBOARD_TOKEN as TOKEN

from tradebot.core.errors import ConfigError
from tradebot.dashboard.auth import (
    LOOPBACK,
    SESSION_COOKIE,
    TOKEN_ENV,
    Session,
    assert_bind_allowed,
    is_public,
    require_token,
)

# ---------------------------------------------------------------- refuse to start


def test_missing_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match=TOKEN_ENV):
        require_token({})


def test_blank_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="refuses to start"):
        require_token({TOKEN_ENV: "   "})


def test_short_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="at least 16 characters"):
        require_token({TOKEN_ENV: "short"})


def test_token_is_registered_for_redaction() -> None:
    """A token that later reaches a log line must be scrubbed, not recorded (PLAN §3.2)."""
    from tradebot.core.logging import REDACTED, SECRETS

    require_token({TOKEN_ENV: TOKEN})
    try:
        assert SECRETS.scrub(f"authorization: {TOKEN}") == f"authorization: {REDACTED}"
    finally:
        SECRETS.clear()


# ---------------------------------------------------------------- cookie signing


def test_issued_cookie_verifies() -> None:
    session = Session(TOKEN)
    assert session.verifies(session.issue())


def test_tampered_cookie_is_rejected() -> None:
    session = Session(TOKEN)
    assert not session.verifies(session.issue() + "x")


def test_absent_cookie_is_rejected() -> None:
    assert not Session(TOKEN).verifies(None)


def test_rotating_the_token_invalidates_the_session() -> None:
    """The revocation lever a session with no expiry otherwise lacks (ADR 0014)."""
    issued = Session(TOKEN).issue()
    assert not Session("a-different-token-entirely").verifies(issued)


def test_forged_cookie_without_the_key_is_rejected() -> None:
    assert not Session(TOKEN).verifies("operator.forged-signature")


@pytest.mark.parametrize("submitted", ["", None, "wrong-token-but-long", TOKEN + " "])
def test_wrong_token_is_not_accepted(submitted: str | None) -> None:
    assert not Session(TOKEN).accepts(submitted)


def test_non_ascii_token_is_a_failed_login_not_a_crash() -> None:
    """`hmac.compare_digest` raises on non-ASCII `str`; a pasted odd character must not 500."""
    assert not Session(TOKEN).accepts("pässwörd-not-the-token")


def test_correct_token_is_accepted() -> None:
    assert Session(TOKEN).accepts(TOKEN)


# ---------------------------------------------------------------- bind guard


@pytest.mark.parametrize("host", sorted(LOOPBACK))
def test_loopback_binds_are_allowed(host: str) -> None:
    assert_bind_allowed(host, allow_remote=False)


def test_remote_bind_is_refused_without_the_flag() -> None:
    with pytest.raises(ConfigError, match="allow-remote"):
        assert_bind_allowed("0.0.0.0", allow_remote=False)


def test_remote_bind_is_allowed_when_asked_for() -> None:
    assert_bind_allowed("0.0.0.0", allow_remote=True)


# ---------------------------------------------------------------- the gate itself


@pytest.mark.parametrize(
    ("path", "public"),
    [
        ("/login", True),
        ("/logout", True),
        ("/static/app.css", True),
        ("/", False),
        ("/risk", False),
    ],
)
def test_is_public(path: str, public: bool) -> None:
    assert is_public(path) is public


async def test_every_route_is_protected(dashboard: FastAPI, http: httpx.AsyncClient) -> None:
    """No route outside the public set may be reachable without a session.

    Walks the app rather than a hand-written list on purpose: a route added by a later pass is
    covered by this assertion without anyone remembering to extend it.
    """
    checked = 0
    for route in dashboard.routes:
        path = getattr(route, "path", "")
        if not path or is_public(path) or "{" in path:
            continue
        response = await http.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path
        checked += 1
    assert checked, "no protected routes were found; the walk is not testing anything"


async def test_unauthenticated_post_is_refused_not_redirected(http: httpx.AsyncClient) -> None:
    """Redirecting a POST would report success for a state change that never happened."""
    assert (await http.post("/")).status_code == 401


async def test_login_with_the_wrong_token_is_401(http: httpx.AsyncClient) -> None:
    response = await http.post("/login", data={"token": "not-the-token"})
    assert response.status_code == 401
    assert "not accepted" in response.text
    assert SESSION_COOKIE not in response.cookies


async def test_login_then_logout(http: httpx.AsyncClient) -> None:
    assert (await http.post("/login", data={"token": TOKEN})).status_code == 303
    assert (await http.get("/")).status_code == 200

    await http.get("/logout")
    assert (await http.get("/")).status_code == 303


async def test_login_page_redirects_an_authenticated_operator(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_static_assets_need_no_session(http: httpx.AsyncClient) -> None:
    assert (await http.get("/static/app.css")).status_code == 200
    assert (await http.get("/static/htmx.min.js")).status_code == 200
