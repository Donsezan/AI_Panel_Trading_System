"""Dashboard authentication: a mandatory token, and a session cookie signed with it.

Stricter than DESIGN §6.10, which only demands auth off-loopback. Auth here is **always on**,
because anything that can reach localhost otherwise gets the kill switch and config CRUD for
free (ADR 0014).

Four properties, each defending against a specific way this goes wrong:

* **The server refuses to start without a token**, the same way live mode refuses without its
  preconditions (PLAN §2.4). There is no flag that disables auth: a control that can be turned
  off is worth whatever the least careful invocation of it is worth.
* **Enforcement is middleware, not a per-route dependency.** A dependency someone forgets to add
  to a new route is an unauthenticated route. `test_dashboard_auth.py` walks every registered
  route and asserts only the login, logout and static paths are exempt.
* **The middleware is pure ASGI, so it sees every scope a client can open** — `http` *and*
  `websocket`. `BaseHTTPMiddleware` only ever sees HTTP requests, which would have made the
  first WebSocket route unauthenticated by construction: the exact failure the
  middleware-not-a-dependency rule exists to prevent (ADR 0024).
* **The cookie's signing key is derived from the token.** The session does not expire, so
  rotating `TRADEBOT_DASHBOARD_TOKEN` and restarting is the revocation lever — it invalidates
  every live session at once.

Failure semantics: an absent or unverifiable session is never treated as anonymous access. A
navigation is redirected to the login form; anything else is refused with `401`, because
silently redirecting a POST would swallow a state change the operator believes they made. A
WebSocket is closed before it is accepted, which an ASGI server reports as a refused handshake.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable, Mapping

from itsdangerous import BadSignature, Signer
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS, get_logger

logger = get_logger(__name__)

#: Environment variable holding the dashboard token. The *name*, never a value (PLAN §3.2).
TOKEN_ENV = "TRADEBOT_DASHBOARD_TOKEN"  # noqa: S105 — an env var name, not a credential

SESSION_COOKIE = "tradebot_session"
SESSION_SALT = "tradebot.dashboard.session"

#: What the signed cookie asserts. Single-user tool: there is one principal, and the signature
#: is the whole claim.
PRINCIPAL = "operator"

#: Short enough to type, long enough not to be guessed by something hammering the login form.
MIN_TOKEN_LENGTH = 16

#: The only paths reachable without a session. Everything else is protected by construction.
PUBLIC_PATHS = frozenset({"/login", "/logout"})
STATIC_PREFIX = "/static/"


def require_token(environ: Mapping[str, str] | None = None) -> str:
    """The configured dashboard token, or a refusal to start (ADR 0014).

    Registered with the log redactor on the way out, so a token that later reaches a log line —
    through an exception, a traceback, a request echo — is scrubbed rather than recorded.
    """
    token = (environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(
            f"the dashboard refuses to start without {TOKEN_ENV}: it can trip the kill switch, "
            "publish a risk policy and close a position, so it is authenticated even on "
            "localhost (ADR 0014). Set a token of at least "
            f"{MIN_TOKEN_LENGTH} characters and restart."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters; "
            f"got {len(token)}. A short token is a guessable one."
        )
    SECRETS.register(token)
    return token


class Session:
    """Issues and verifies the signed session cookie, and checks a submitted token."""

    def __init__(self, token: str) -> None:
        self._token = token.encode()
        self._signer = Signer(token, salt=SESSION_SALT)

    def accepts(self, submitted: str | None) -> bool:
        """Whether `submitted` is the configured token, compared in constant time.

        Encoded first: `hmac.compare_digest` raises on non-ASCII `str`, and a token pasted with a
        stray non-ASCII character must be a failed login rather than a 500.
        """
        if not submitted:
            return False
        return hmac.compare_digest(submitted.encode(), self._token)

    def issue(self) -> str:
        return self._signer.sign(PRINCIPAL).decode()

    def verifies(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        try:
            self._signer.unsign(cookie)
        except BadSignature:
            return False
        return True


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX)


#: The ASGI scope types that carry a client, and therefore a principal to check. `lifespan` is
#: the server talking to the application about its own startup: no client, no cookie, nothing to
#: authenticate. Every type in here has an entry in `REFUSALS`, asserted by the auth suite — a
#: guarded scope with no way to refuse would fail open on the one path that must not.
GUARDED_SCOPES = frozenset({"http", "websocket"})

#: Sent when a socket is opened without a session. RFC 6455 "policy violation" — an ASGI server
#: turns a close sent before `websocket.accept` into a refused handshake.
WS_POLICY_VIOLATION = 1008


class SessionMiddleware:
    """Refuses every request without a valid session, except the public paths.

    Pure ASGI rather than `BaseHTTPMiddleware` so the `websocket` scope is covered by the same
    gate as `http`, without the socket route knowing it is being guarded (ADR 0024).
    """

    def __init__(self, app: ASGIApp, session: Session) -> None:
        self._app = app
        self._session = session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in GUARDED_SCOPES or self._admits(scope):
            await self._app(scope, receive, send)
            return
        await REFUSALS[scope["type"]](scope, receive, send)

    def _admits(self, scope: Scope) -> bool:
        """A public path, or a cookie this server signed. Identical for HTTP and WebSocket.

        An upgrade request is an ordinary HTTP request until the handshake completes, so it
        carries the same cookies and needs no second code path to check them.
        """
        return is_public(scope["path"]) or self._session.verifies(
            HTTPConnection(scope).cookies.get(SESSION_COOKIE)
        )


async def _refuse_http(scope: Scope, receive: Receive, send: Send) -> None:
    """Send a browser to the login form; refuse anything else outright.

    A GET is a navigation and a redirect is the helpful answer. A POST is an operator asking for
    a state change, and redirecting it would report success for something that never happened —
    on a surface where "something" includes the kill switch.
    """
    response: Response = (
        RedirectResponse("/login", status_code=303)
        if scope["method"] == "GET"
        else PlainTextResponse("authentication required", status_code=401)
    )
    await response(scope, receive, send)


async def _refuse_websocket(_scope: Scope, _receive: Receive, send: Send) -> None:
    """Close before accepting, which the server reports to the browser as a failed handshake.

    There is no redirect to offer a socket and no body it would render. Nothing is lost by the
    terse refusal: this transport only ever asks the page to refresh, and the page itself is one
    unauthenticated navigation away from the login form.
    """
    await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})


#: How a refusal is delivered, per scope type. Dispatch rather than a branch, so adding a scope
#: means adding its refusal (CLAUDE.md conventions).
REFUSALS: dict[str, Callable[[Scope, Receive, Send], Awaitable[None]]] = {
    "http": _refuse_http,
    "websocket": _refuse_websocket,
}


def set_session(response: Response, session: Session, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session.issue(),
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


#: Addresses only this machine can reach. Everything else is the network.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def assert_bind_allowed(host: str, *, allow_remote: bool) -> None:
    """Refuse a non-loopback bind unless the operator asked for one (PLAN §3.3).

    Auth is already mandatory, so this is the second lock rather than the first: it exists so a
    `--host 0.0.0.0` typo cannot put the kill switch on a LAN without anyone deciding to.
    """
    if host in LOOPBACK or allow_remote:
        return
    raise ConfigError(
        f"refusing to bind the dashboard to {host!r}: it is not a loopback address, and this "
        "surface holds the kill switch, config CRUD and manual close. Pass --allow-remote to "
        "state that you meant it (PLAN §3.3)."
    )
