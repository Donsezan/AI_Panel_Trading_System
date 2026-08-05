"""The FastAPI factory. Takes a wired `Application`; never builds one.

`tradebot/app.py` stays the only module that names a concrete adapter (CLAUDE.md layering), so
the dashboard receives everything it can reach rather than constructing it. That is also what
makes it testable: the suite hands `create_dashboard` a sim application over an in-memory
database and drives real HTTP through `TestClient` — no socket, no venue, no model.

Authentication is installed as **middleware**, not as a dependency on each router, so a route
added later is protected by construction (`auth.py`, ADR 0014).

Failure semantics: the factory raises `ConfigError` before serving anything if the token is
missing or too short. An unauthenticated navigation is redirected to the login form and any
other unauthenticated request is refused outright.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from tradebot.app import Application
from tradebot.control.supervision import SupervisionController
from tradebot.core.logging import get_logger
from tradebot.dashboard.auth import (
    SESSION_COOKIE,
    Session,
    SessionMiddleware,
    clear_session,
    require_token,
    set_session,
)
from tradebot.dashboard.queries import Queries
from tradebot.dashboard.routes import configure, control, monitor, workspace
from tradebot.dashboard.updates import UpdateHub
from tradebot.dashboard.views import PACKAGE, DashboardState, build_templates, render, state_of

logger = get_logger(__name__)

__all__ = ["create_dashboard"]


def create_dashboard(
    application: Application,
    *,
    token: str | None = None,
    controller: SupervisionController | None = None,
) -> FastAPI:
    """Build the dashboard over an already-wired application.

    `token` is read from the environment when not supplied, and its absence is a refusal to
    start — the dashboard has no anonymous mode (ADR 0014).

    `controller` is the process's one supervision owner, so Start and Stop on the Control page act
    on the same task the CLI started. A dashboard given none gets its own, **stopped**: a page
    without a controller cannot be trading, and claiming otherwise would be the one lie an
    operator would act on.
    """
    session = Session(token if token is not None else require_token())
    app = FastAPI(
        title=f"tradebot — {application.mode.value}",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.dashboard = DashboardState(
        application=application,
        queries=Queries(application.store),
        templates=build_templates(application),
        session=session,
        controller=controller or SupervisionController(application),
        updates=UpdateHub(application.store),
    )
    app.add_middleware(SessionMiddleware, session=session)
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.include_router(workspace.router)
    app.include_router(monitor.router)
    app.include_router(configure.router)
    app.include_router(control.router)
    _add_session_routes(app)
    _add_update_socket(app)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Nothing to start — the tail is lazy — and one thing to stop.

    A tail left running past shutdown would hold the event loop open behind a process the
    operator has already asked to exit.
    """
    yield
    await app.state.dashboard.updates.stop()


def _add_update_socket(app: FastAPI) -> None:
    @app.websocket("/ws/updates")
    async def updates(socket: WebSocket) -> None:
        """Tells this page which panes went stale. Carries nothing else, in either direction.

        Authenticated by the same middleware as every other route — a socket opened without a
        session never reaches this function (ADR 0024). Inbound frames are read only so that a
        disconnect is noticed promptly, and are discarded unparsed: there is no command surface
        here to validate, because there are no commands.

        The hub completes the handshake rather than this route, so that accepting and starting to
        watch cannot drift apart into a window where notices are lost.
        """
        hub = state_of(socket).updates
        await hub.register(socket)
        try:
            while (await socket.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            await hub.unregister(socket)


def _add_session_routes(app: FastAPI) -> None:
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if state_of(request).session.verifies(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error="")

    @app.post("/login")
    async def login(request: Request, token: str = Form(default="")) -> Response:
        state = state_of(request)
        if not state.session.accepts(token):
            # The submitted value is never logged, not even truncated: a near-miss token in a log
            # file is a token in a log file (PLAN §3.2).
            logger.warning("dashboard login refused", extra={"client": _client(request)})
            refused = render(request, "login.html", error="That token was not accepted.")
            refused.status_code = 401
            return refused
        accepted = RedirectResponse("/", status_code=303)
        set_session(accepted, state.session, secure=request.url.scheme == "https")
        logger.info("dashboard login accepted", extra={"client": _client(request)})
        return accepted

    @app.get("/logout")
    async def logout() -> Response:
        """Ends the session. With no expiry, this is the only thing that does (ADR 0014)."""
        response = RedirectResponse("/login", status_code=303)
        clear_session(response)
        return response


def _client(request: Request) -> str:
    return request.client.host if request.client else ""
