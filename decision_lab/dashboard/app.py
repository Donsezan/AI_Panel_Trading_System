"""The FastAPI factory for the tuning surface (spec §12).

Takes a workspace and a token; builds no `Application`, opens no bot database and constructs no
venue adapter. Everything it shows is read from `decision_lab/workspace/` and everything it runs
is a child process of this tool's own CLI (§12.4).

Failure semantics: the factory raises `ConfigError` before serving anything if the token is
missing or too short. An unauthenticated navigation is redirected to the login form; anything else
is refused outright.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from decision_lab.dashboard.auth import (
    SESSION_COOKIE,
    LabSessionMiddleware,
    clear_session,
    require_token,
    set_session,
)
from decision_lab.dashboard.cache import AnalysisCache
from decision_lab.dashboard.routes import jobs as jobs_routes
from decision_lab.dashboard.routes import matrices as matrices_routes
from decision_lab.dashboard.routes import runs as runs_routes
from decision_lab.dashboard.views import PACKAGE, LabState, build_templates, render, state_of
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.logging import get_logger
from tradebot.dashboard.auth import Session

logger = get_logger("decision_lab.dashboard")

__all__ = ["create_lab_dashboard"]


def create_lab_dashboard(
    *,
    workspace: Path | None = None,
    token: str | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the tuning dashboard. `workspace` of `None` means the tool's own (`params`)."""
    session = Session(token if token is not None else require_token())
    used_clock = clock or SystemClock()
    app = FastAPI(title="decision_lab", docs_url=None, redoc_url=None)
    app.state.lab = LabState(
        workspace=workspace,
        templates=build_templates(),
        session=session,
        clock=used_clock,
        cache=AnalysisCache(clock=used_clock),
    )
    app.add_middleware(LabSessionMiddleware, session=session)
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.include_router(runs_routes.router)
    app.include_router(matrices_routes.router)
    app.include_router(jobs_routes.router)
    _add_session_routes(app)
    return app


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
            # The submitted value is never logged, not even truncated.
            logger.warning("lab dashboard login refused")
            refused = render(request, "login.html", error="That token was not accepted.")
            refused.status_code = 401
            return refused
        accepted = RedirectResponse("/", status_code=303)
        set_session(accepted, state.session, secure=request.url.scheme == "https")
        return accepted

    @app.get("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        clear_session(response)
        return response
