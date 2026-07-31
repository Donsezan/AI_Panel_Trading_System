"""Where venue keys come from, and why the *names* differ per mode.

Keys live in the environment only — never in the database, never in a log, never in a prompt
(PLAN §3.2). That much follows from the design. What is load-bearing here is subtler:

**Each mode reads differently-named variables.** A paper run looks for `BINANCE_TESTNET_API_KEY`;
only live reads `BINANCE_API_KEY`. So a live key sitting in the environment of a machine running
paper is not merely unused — it is *unreachable*. The classic way to lose real money is running
live while believing you are on testnet, and this removes one of the paths there: it is no longer
enough to get the sandbox flag wrong, you would also have to have renamed your keys (PLAN §2.4).

Failure semantics: a missing variable raises `ConfigError` naming the variable, at wiring time.
Refusing to start is the only correct response — a trading process that reaches its first order
before discovering it cannot sign has already committed an intent it cannot fulfil.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError

#: Environment variable *names* per venue and mode. Never values.
SECRET_REFS: Final[Mapping[str, Mapping[Mode, tuple[str, str]]]] = {
    "binance": {
        Mode.SIM: ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
        Mode.PAPER: ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
        Mode.LIVE: ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    },
    "alpaca": {
        Mode.SIM: ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY"),
        Mode.PAPER: ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY"),
        Mode.LIVE: ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY"),
    },
}


def secret_refs(venue_id: str, mode: Mode) -> tuple[str, str]:
    """The variable names this venue's keys are read from in this mode."""
    refs = SECRET_REFS.get(venue_id)
    if refs is None:
        raise ConfigError(f"no credential names are defined for venue {venue_id!r}")
    return refs[mode]


def credentials(
    venue_id: str, mode: Mode, environ: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """Read this venue's key pair, or refuse to start."""
    env = environ if environ is not None else os.environ
    names = secret_refs(venue_id, mode)
    values = tuple(env.get(name, "").strip() for name in names)
    missing = [name for name, value in zip(names, values, strict=True) if not value]
    if missing:
        raise ConfigError(
            f"{venue_id} in {mode.value} mode needs {' and '.join(missing)} in the environment. "
            f"Keys are read from mode-specific variables on purpose, so a live key cannot be "
            f"picked up by a {mode.value} run (PLAN §2.4)."
        )
    return values[0], values[1]


def has_credentials(venue_id: str, mode: Mode, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the keys are present, without raising — for the live precondition report."""
    try:
        credentials(venue_id, mode, environ)
    except ConfigError:
        return False
    return True
