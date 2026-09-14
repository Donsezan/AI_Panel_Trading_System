"""`python -m decision_lab dashboard` — the entry point every other dashboard test bypasses.

Important 4: every test under `decision_lab/tests` builds the surface through
`create_lab_dashboard` directly, so the command that actually serves it — its defaults, its bind
refusal, and its deliberate absence from `LOCKED` — had nothing pinning it at all.

The one that matters is the last. `("dashboard", "")` is **not** in `cli.LOCKED` on purpose
(§12.4): the command writes nothing a second writer could interleave with, and holding the
workspace lock for the life of the server would make every run started *from that page* exit 7 —
the page refusing every job it exists to launch, for as long as it is up.
"""

from __future__ import annotations

import argparse

import pytest

from decision_lab import cli
from decision_lab.dashboard import auth as lab_auth
from decision_lab.params import DEFAULT_DASHBOARD_PORT


def test_the_dashboard_defaults_to_loopback_and_its_own_port() -> None:
    """Loopback by default, and a port of its own — the bot's dashboard is already on 8787, and
    two surfaces fighting for one port is a start that fails for a reason nobody reads."""
    args = cli.parse_args(["dashboard"])

    assert args.command == "dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == DEFAULT_DASHBOARD_PORT
    assert args.allow_remote is False


def test_a_non_loopback_bind_is_refused_before_anything_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN §3.3's second lock, on a surface that spends money: auth is already mandatory, so this
    exists so a `--host 0.0.0.0` typo cannot put it on a LAN without anyone deciding to.

    The app factory is replaced with one that fails the test if it is reached, so this asserts the
    refusal happens *before* a server exists rather than merely that the exit code is non-zero —
    a refusal raised after `uvicorn.Server.serve()` had been awaited would look identical here.

    A token is set for the same reason the factory is stubbed: without one, `require_token` refuses
    on the very next line and the exit code would be identical whether the bind guard ran or not.
    With it, the *only* thing left that can refuse this call is the bind guard.
    """
    monkeypatch.setenv(lab_auth.TOKEN_ENV, "decision-lab-token-0123456789")

    def _must_not_be_built(**_: object) -> object:
        raise AssertionError("the surface was built despite a refused bind")

    monkeypatch.setattr(cli, "create_lab_dashboard", _must_not_be_built)

    # `assert_bind_allowed` raises `ConfigError`, which `main`'s boundary maps to its generic
    # `TradebotError` code — the tool has no separate code for a bad bind (§13).
    assert cli.main(["dashboard", "--host", "0.0.0.0"]) == cli.EXIT_DATASET


def test_serving_the_page_does_not_hold_the_workspace_lock() -> None:
    """§12.4: `dashboard` is deliberately absent from `LOCKED`.

    Added to it, `cli.main` would take the OS advisory lock for the life of the server and every
    run launched from the page — each a child process that takes the same lock — would exit 7.
    The second assertion is the other half of the same table: a `LOCKED` entry naming a key
    `COMMANDS` does not have could never be dispatched, so it would lock nothing at all.
    """
    assert ("dashboard", "") not in cli.LOCKED
    assert set(cli.LOCKED) <= set(cli.COMMANDS)


def test_the_bind_refusal_is_argparse_free() -> None:
    """`--allow-remote` is the operator stating they meant it, so it must reach the namespace as
    given rather than being silently normalised away by the parser."""
    args = cli.parse_args(["dashboard", "--host", "0.0.0.0", "--allow-remote", "--port", "9000"])

    assert isinstance(args, argparse.Namespace)
    assert (args.host, args.port, args.allow_remote) == ("0.0.0.0", 9000, True)
