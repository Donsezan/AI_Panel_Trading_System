"""Dashboard authentication: a mandatory token, and a session cookie signed with it.

Stricter than DESIGN §6.10, which only demands auth off-loopback. Auth here is **always on**,
because anything that can reach localhost otherwise gets the kill switch and config CRUD for
free (ADR 0014).

Three properties, each defending against a specific way this goes wrong:

* **The server refuses to start without a token**, the same way live mode refuses without its
  preconditions (PLAN §2.4). There is no flag that disables auth: a control that can be turned
  off is worth whatever the least careful invocation of it is worth.
* **Enforcement is middleware, not a per-route dependency.** A dependency someone forgets to add
  to a new route is an unauthenticated route. `test_dashboard_auth.py` walks every registered
  route and asserts only the login, logout and static paths are exempt.
* **The cookie's signing key is derived from the token.** The session does not expire, so
  rotating `TRADEBOT_DASHBOARD_TOKEN` and restarting is the revocation lever — it invalidates
  every live session at once.

Failure semantics: an absent or unverifiable session is never treated as anonymous access. A
navigation is redirected to the login form; anything else is refused with `401`, because
silently redirecting a POST would swallow a state change the operator believes they made.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable, Mapping

from itsdangerous import BadSignature, Signer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

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


class SessionMiddleware(BaseHTTPMiddleware):
    """Refuses every request without a valid session, except the public paths."""

    def __init__(self, app: Callable[..., Awaitable[None]], session: Session) -> None:
        super().__init__(app)
        self._session = session

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_public(request.url.path) or self._session.verifies(
            request.cookies.get(SESSION_COOKIE)
        ):
            return await call_next(request)
        return _refuse(request)


def _refuse(request: Request) -> Response:
    """Send a browser to the login form; refuse anything else outright.

    A GET is a navigation and a redirect is the helpful answer. A POST is an operator asking for
    a state change, and redirecting it would report success for something that never happened —
    on a surface where "something" includes the kill switch.
    """
    if request.method == "GET":
        return RedirectResponse("/login", status_code=303)
    return Response("authentication required", status_code=401, media_type="text/plain")


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
