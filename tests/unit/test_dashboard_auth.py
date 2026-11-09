"""Authentication is the dashboard's only gate, so it is tested as one (ADR 0014).

The load-bearing tests here are the two route walks — `test_every_route_is_protected` and
`test_every_websocket_route_is_protected`. They walk the registered routes rather than asserting a
hand-written list, so a route added in a later pass is covered the day it lands. That is the whole
reason auth is middleware and not a dependency, and the reason the middleware is pure ASGI: an
HTTP-only middleware would have left the WebSocket walk with nothing to find and the socket
unauthenticated (ADR 0024).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import WebSocketRoute
from tests.conftest import DASHBOARD_TOKEN as TOKEN
from tests.conftest import ASGIWebSocket

from tradebot.app import Application
from tradebot.core.errors import ConfigError
from tradebot.dashboard.app import create_dashboard
from tradebot.dashboard.auth import (
    GUARDED_SCOPES,
    LOOPBACK,
    REFUSALS,
    SESSION_COOKIE,
    TOKEN_ENV,
    WS_POLICY_VIOLATION,
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


#: Every router the app includes, by the module its endpoints live in. A router dropped from the
#: app, or one the walk stopped reaching, fails `test_the_walk_reaches_every_router`.
ROUTERS: Final = frozenset(
    {
        "tradebot.dashboard.routes.workspace",
        "tradebot.dashboard.routes.monitor",
        "tradebot.dashboard.routes.configure",
        "tradebot.dashboard.routes.control",
    }
)
#: A value for every path parameter a route declares, so a parameterised route is walked too.
PARAMETERS: Final = {
    "basket_id": "demo",
    "alert_id": "a",
    "kind": "basket",
    "config_id": "demo",
    "cycle_id": "c",
}
#: Route/method pairs the app serves behind the gate today, less a margin for a route retired.
#: The walk falling below it means it has stopped reaching a router, as it once did silently.
MIN_PROTECTED: Final = 30


def served_routes(app: Any) -> Iterator[tuple[str, frozenset[str], str]]:
    """Every route the app serves, as `(path, methods, endpoint module)`, routers included.

    FastAPI 0.141 lists an included router in `app.routes` as one `_IncludedRouter` with no path,
    so walking `app.routes` alone reached only `/login`, `/logout` and the static mount: the
    workspace, monitor, configure and control routes were never proven protected. This descends
    into each included router and applies its prefix.
    """
    for route in app.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            prefix = route.include_context.prefix
            for inner, methods, module in served_routes(nested):
                yield prefix + inner, methods, module
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        endpoint = getattr(route, "endpoint", None)
        yield (
            path,
            frozenset(getattr(route, "methods", None) or ()),
            getattr(endpoint, "__module__", ""),
        )


def filled(path: str) -> str:
    for name, value in PARAMETERS.items():
        path = path.replace("{" + name + "}", value)
    assert "{" not in path, f"no value for a path parameter in {path}; add it to PARAMETERS"
    return path


#: FastAPI's own schema and docs routes, which carry no data of ours.
PUBLIC_DOCS: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def protected_routes(app: FastAPI) -> list[tuple[str, frozenset[str]]]:
    return [
        (path, methods)
        for path, methods, _ in served_routes(app)
        if methods and not is_public(path) and path not in PUBLIC_DOCS
    ]


def test_the_walk_reaches_every_router(dashboard: FastAPI) -> None:
    reached = {module for _, _, module in served_routes(dashboard)}
    assert reached >= ROUTERS, f"routers the walk does not reach: {sorted(ROUTERS - reached)}"


async def test_every_route_is_protected(dashboard: FastAPI, http: httpx.AsyncClient) -> None:
    """No route outside the public set may be reachable without a session.

    Walks the app rather than a hand-written list on purpose: a route added by a later pass is
    covered by this assertion without anyone remembering to extend it. GET is redirected to the
    login form; anything else is refused outright, because redirecting a POST would report success
    for a state change that never happened.
    """
    checked = 0
    for path, methods in protected_routes(dashboard):
        target = filled(path)
        for method in sorted(methods - {"HEAD"}):
            if method == "GET":
                response = await http.get(target)
                assert response.status_code == 303, (method, path)
                assert response.headers["location"] == "/login", path
            else:
                response = await http.request(method, target, data={})
                assert response.status_code == 401, (method, path)
            checked += 1
    assert checked >= MIN_PROTECTED, f"only {checked} route/method pairs were walked"


async def test_unauthenticated_post_is_refused_not_redirected(http: httpx.AsyncClient) -> None:
    """Redirecting a POST would report success for a state change that never happened."""
    assert (await http.post("/")).status_code == 401


# ---------------------------------------------------------------- the websocket gate


def test_every_guarded_scope_has_a_refusal() -> None:
    """A scope the middleware guards but cannot refuse would fall through it, unauthenticated."""
    assert set(REFUSALS) == set(GUARDED_SCOPES)


def websocket_paths(dashboard: FastAPI) -> list[str]:
    return [route.path for route in dashboard.routes if isinstance(route, WebSocketRoute)]


def signed_cookie() -> str:
    return f"{SESSION_COOKIE}={Session(TOKEN).issue()}"


def test_the_app_registers_a_websocket_route_to_protect(dashboard: FastAPI) -> None:
    """Guards the walks below: an empty walk asserts nothing, and would pass silently."""
    assert websocket_paths(dashboard)


@pytest.mark.parametrize("cookie", ["", f"{SESSION_COOKIE}=operator.forged-signature"])
async def test_every_websocket_route_is_protected(dashboard: FastAPI, cookie: str) -> None:
    """An upgrade without a valid session is closed before the route function is entered.

    Walked rather than listed, exactly like the HTTP routes: this is the assertion that makes the
    ASGI rewrite load-bearing rather than incidental. A forged cookie is walked beside an absent
    one because presence and validity are different questions.
    """
    for path in websocket_paths(dashboard):
        async with ASGIWebSocket(dashboard, path, cookie=cookie) as socket:
            assert not socket.accepted, path
            assert socket.close_code == WS_POLICY_VIOLATION, path


async def test_a_signed_in_websocket_is_accepted(dashboard: FastAPI) -> None:
    """The gate must admit as well as refuse, or it is untested in the direction that matters."""
    for path in websocket_paths(dashboard):
        async with ASGIWebSocket(dashboard, path, cookie=signed_cookie()) as socket:
            assert socket.accepted, path


async def test_rotating_the_token_shuts_the_socket_too(sim_application: Application) -> None:
    """The revocation lever reaches every transport, not only the pages."""
    rotated = create_dashboard(sim_application, token="a-different-token-entirely")
    for path in websocket_paths(rotated):
        async with ASGIWebSocket(rotated, path, cookie=signed_cookie()) as socket:
            assert not socket.accepted, path


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
