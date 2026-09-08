"""The Runs page: every row `registry.py` has recorded (spec §12.1). Filled in by Task 5.

For now, the empty state alone: a `GET /` that renders `runs.html` with no rows, which is what an
empty registry looks like and what `test_login_then_a_page_renders` needs to exist at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from decision_lab.dashboard.views import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def runs(request: Request) -> HTMLResponse:
    return render(request, "runs.html", rows=())
