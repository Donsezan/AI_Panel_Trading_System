"""The lab dashboard's only gate, tested as one (§12, ADR 0014 inherited).

Two properties this suite exists for. The route walk covers a route added by a later task the day
it lands — which is why auth is middleware here as it is in the bot. And the cookie *name*:
cookies are not port-scoped, so a lab app setting `tradebot_session` on 127.0.0.1 would overwrite
the bot dashboard's cookie on another port, signed with a different token, and log the operator
out of the surface holding the kill switch.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

# `starlette.testclient.TestClient` (pulled in by `fastapi.testclient`) now prefers the `httpx2`
# package and falls back to `httpx` with a `StarletteDeprecationWarning` — which the root
# `filterwarnings = ["error"]` promotes into a collection failure of this module, unrelated to
# anything decision_lab does. `httpx2` is not one of this repo's pinned dependencies (the bot's
# own dashboard suite sidesteps the same gap with `httpx.ASGITransport`, see `tests/conftest.py`),
# so the import is scoped to its own filter rather than adding a repo-wide exemption for a warning
# nothing else in this file needs to see.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from decision_lab.dashboard import auth
from decision_lab.dashboard.app import create_lab_dashboard
from tradebot.core.errors import ConfigError
from tradebot.dashboard import auth as bot_auth

TOKEN = "decision-lab-token-0123456789"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_lab_dashboard(workspace=tmp_path, token=TOKEN))


def test_missing_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match=auth.TOKEN_ENV):
        auth.require_token({})


def test_short_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="at least 16 characters"):
        auth.require_token({auth.TOKEN_ENV: "short"})


def test_the_lab_reads_its_own_environment_variable() -> None:
    assert auth.TOKEN_ENV == "DECISION_LAB_DASHBOARD_TOKEN"
    assert auth.TOKEN_ENV != bot_auth.TOKEN_ENV


def test_the_cookie_name_differs_from_the_bots() -> None:
    """Cookies ignore ports. Sharing the name would log the operator out of the bot dashboard."""
    assert auth.SESSION_COOKIE != bot_auth.SESSION_COOKIE


def test_every_route_is_protected(client: TestClient) -> None:
    checked = 0
    for route in client.app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", "")
        if not path or auth.is_public(path) or "{" in path:
            continue
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path
        checked += 1
    assert checked, "no protected routes were found; the walk is not testing anything"


def test_unauthenticated_post_is_refused_not_redirected(client: TestClient) -> None:
    assert client.post("/matrices", data={}, follow_redirects=False).status_code == 401


def test_login_then_a_page_renders(client: TestClient) -> None:
    assert client.post("/login", data={"token": TOKEN}, follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200


def test_a_wrong_token_is_refused(client: TestClient) -> None:
    assert client.post("/login", data={"token": "nope"}, follow_redirects=False).status_code == 401


def test_every_guarded_scope_has_a_refusal() -> None:
    assert set(bot_auth.REFUSALS) == set(bot_auth.GUARDED_SCOPES)


def test_static_is_the_only_route_that_serves_a_file(client: TestClient) -> None:
    """§16's structural row: no route resolves a path out of a request into a file it serves.

    The run forms *pass* operator-typed paths to a child process, which is the CLI's own argument
    and is checked by the CLI. What must not exist is a route that reads a path from a request and
    answers with the file — that would turn an authenticated tuning surface into a file browser
    over `data/`.
    """
    from starlette.staticfiles import StaticFiles

    mounts = [
        route
        for route in client.app.routes  # type: ignore[attr-defined]
        if isinstance(getattr(route, "app", None), StaticFiles)
    ]
    assert len(mounts) == 1
    assert mounts[0].path == "/static"


def _lab_core_dashboard_imports(directory: Path) -> list[str]:
    """Every top-level `*.py` in `directory` that imports `decision_lab.dashboard`.

    `cli.py` is exempt: it is the one entry point permitted to import the dashboard (spec §12,
    the module docstring in `cli.py`). Factored out so `test_the_guard_can_actually_fail` can
    prove this detector actually detects something, rather than trusting the walk by inspection.
    """
    import ast

    offenders = []
    for path in sorted(directory.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any("decision_lab.dashboard" in name for name in names) and path.name != "cli.py":
                offenders.append(f"{path.name}:{node.lineno}")  # type: ignore[attr-defined]
    return offenders


def test_nothing_in_the_lab_core_imports_the_dashboard() -> None:
    """The dashboard is a front door, never a dependency — so the CLI stays importable headless.

    `parents[1]` of `decision_lab/dashboard/auth.py` is `decision_lab/` itself — the lab-core
    top level this guards. (`parents[2]` lands on the repo root, which has no loose `.py` files
    and so would pass vacuously whatever any lab-core module imported.)
    """
    root = Path(auth.__file__).resolve().parents[1]
    offenders = _lab_core_dashboard_imports(root)
    assert not offenders, f"lab core modules importing the dashboard: {offenders}"


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """A structural test that cannot fail is a comment (`test_discipline.py`'s own standard).

    A module that imports the dashboard is flagged; `cli.py`, imported identically, is not — so
    the one deliberate exception is proven to be *why* the real scan comes back clean, not an
    accident of the walk finding nothing at all.
    """
    (tmp_path / "matrices.py").write_text(
        "from decision_lab.dashboard import auth\n", encoding="utf-8"
    )
    (tmp_path / "cli.py").write_text("from decision_lab.dashboard import auth\n", encoding="utf-8")

    offenders = _lab_core_dashboard_imports(tmp_path)

    assert offenders == ["matrices.py:1"]
