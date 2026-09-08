"""The lab's own token, its own cookie, and the bot's refusal semantics (spec §12).

`tradebot.dashboard.auth` is imported one way — `Session` for the signing, `GUARDED_SCOPES` and
`REFUSALS` for how a request without a session is turned away — so ADR 0014's posture is
inherited rather than re-argued, and the separation contract is untouched.

Two things are deliberately *not* inherited:

* **The environment variable.** `DECISION_LAB_DASHBOARD_TOKEN`, so the tuning surface and the
  surface holding the kill switch are not one credential.
* **The cookie name.** Cookies are not port-scoped: a lab app on 127.0.0.1:8788 setting
  `tradebot_session` would overwrite the bot dashboard's cookie on :8787, which is signed with a
  different token — logging in here would silently log the operator out of the bot's dashboard at
  the moment they reached for it.

Failure semantics: identical to the bot's. An absent or unverifiable session is never anonymous
access; a navigation is redirected to the login form and anything else is refused with 401,
because silently redirecting a POST would swallow a change the operator believes they made.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS, get_logger
from tradebot.dashboard.auth import GUARDED_SCOPES, REFUSALS, Session

logger = get_logger("decision_lab.dashboard.auth")

TOKEN_ENV: Final = "DECISION_LAB_DASHBOARD_TOKEN"  # noqa: S105 — a variable name, not a secret
SESSION_COOKIE: Final = "decision_lab_session"
MIN_TOKEN_LENGTH: Final = 16
PUBLIC_PATHS: Final = frozenset({"/login", "/logout"})
STATIC_PREFIX: Final = "/static/"

__all__ = [
    "MIN_TOKEN_LENGTH",
    "PUBLIC_PATHS",
    "SESSION_COOKIE",
    "TOKEN_ENV",
    "LabSessionMiddleware",
    "clear_session",
    "is_public",
    "require_token",
    "set_session",
]


def require_token(environ: Mapping[str, str] | None = None) -> str:
    """The configured token, or a refusal to start.

    Registered with the log redactor on the way out, so a token that later reaches a log line is
    scrubbed rather than recorded.
    """
    token = (environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(
            f"the decision_lab dashboard refuses to start without {TOKEN_ENV}: it can spend real "
            "API credit and publish seat sets, so it is authenticated even on localhost. Set a "
            f"token of at least {MIN_TOKEN_LENGTH} characters and restart."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters; got {len(token)}. "
            "A short token is a guessable one."
        )
    SECRETS.register(token)
    return token


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX)


class LabSessionMiddleware:
    """Refuses every request without a valid session, except the public paths.

    Pure ASGI, and it reuses the bot's `REFUSALS` table, so a scope type added there is refused
    here by the same code rather than by a second copy of the rule.
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
        return is_public(scope["path"]) or self._session.verifies(
            HTTPConnection(scope).cookies.get(SESSION_COOKIE)
        )


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
