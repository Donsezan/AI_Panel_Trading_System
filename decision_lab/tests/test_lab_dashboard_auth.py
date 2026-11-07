"""The lab dashboard's only gate, tested as one (§12, ADR 0014 inherited).

Two properties this suite exists for. The route walk covers a route added by a later task the day
it lands — which is why auth is middleware here as it is in the bot. And the cookie *name*:
cookies are not port-scoped, so a lab app setting `tradebot_session` on 127.0.0.1 would overwrite
the bot dashboard's cookie on another port, signed with a different token, and log the operator
out of the surface holding the kill switch.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

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


#: Every router the app includes, by the module its endpoints live in. A router dropped from the
#: app, or one the walk stopped reaching, fails `test_the_walk_reaches_every_router`.
ROUTERS: Final = frozenset(
    {
        "decision_lab.dashboard.routes.runs",
        "decision_lab.dashboard.routes.matrices",
        "decision_lab.dashboard.routes.jobs",
        "decision_lab.dashboard.routes.periods",
    }
)
#: Values for every path parameter a route declares, so a parameterised route is walked too.
PARAMETERS: Final = {
    "run_id": "r",
    "candidate_id": "c",
    "name": "n",
    "label": "2024-01",
    "cycle_id": "c",
    "index": "0",
}


def served_routes(app: Any) -> Iterator[tuple[str, frozenset[str], str]]:
    """Every route the app serves, as `(path, methods, endpoint module)`, routers included.

    FastAPI 0.141 lists an included router in `app.routes` as one `_IncludedRouter` with no path,
    so walking `app.routes` alone reaches only the app's own few routes (fix round 1, item 1). This
    descends into each included router and applies its prefix.
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


def protected_routes(app: Any) -> list[tuple[str, frozenset[str], str]]:
    return [
        (path, methods, module)
        for path, methods, module in served_routes(app)
        if methods and not auth.is_public(path) and path != "/openapi.json"
    ]


def test_the_walk_reaches_every_router(client: TestClient) -> None:
    reached = {module for _, _, module in served_routes(client.app)}
    assert reached >= ROUTERS, f"routers the walk does not reach: {sorted(ROUTERS - reached)}"
    assert len(protected_routes(client.app)) >= 17, "the walk has gone nearly empty again"


def test_every_route_is_protected(client: TestClient) -> None:
    """GET is redirected to the login form; anything else is refused outright (401)."""
    checked = 0
    for path, methods, _ in protected_routes(client.app):
        target = filled(path)
        for method in sorted(methods - {"HEAD"}):
            if method == "GET":
                response = client.get(target, follow_redirects=False)
                assert response.status_code == 303, (method, path)
                assert response.headers["location"] == "/login", path
            else:
                response = client.request(method, target, data={}, follow_redirects=False)
                assert response.status_code == 401, (method, path)
            checked += 1
    assert checked >= 17, f"only {checked} route/method pairs were walked"


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
    over `data/`. Walked through every included router (fix round 1, item 1): a mount or a
    file-serving endpoint inside a router is exactly the one `app.routes` alone never shows.
    """
    import inspect

    from starlette.staticfiles import StaticFiles

    def mounts(app: Any) -> Iterator[Any]:
        for route in app.routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from mounts(nested)
            elif isinstance(getattr(route, "app", None), StaticFiles):
                yield route

    found = list(mounts(client.app))
    assert len(found) == 1
    assert found[0].path == "/static"

    def endpoints(app: Any) -> Iterator[tuple[str, Any]]:
        for route in app.routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from endpoints(nested)
            elif getattr(route, "endpoint", None) is not None:
                yield route.path, route.endpoint

    serving = [
        path
        for path, endpoint in endpoints(client.app)
        if "FileResponse" in inspect.getsource(endpoint)
    ]
    assert not serving, f"routes answering with a file: {serving}"
    assert len(list(served_routes(client.app))) >= 20, "the walk reached nothing"


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
