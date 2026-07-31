"""CLI entry point.

`--mode` is **required and has no default**. The classic way to lose real money is running live
while believing you are on testnet, so no environment variable, config default, or typo can
select a mode (PLAN §2.4).

The `risk` subcommands are the operator surface for the controls that only a human may clear —
a tripped kill switch and a halted basket. They require the same typed phrase the dashboard will:
an automatic re-arm would defeat the control entirely (DESIGN §6.6).

`config` reads the versioned ConfigStore. It deliberately only reads: editing a limit is the
dashboard's job, where the change can be reviewed against the same pydantic validators the engine
uses, and a Tier-2 loosening can demand its extra confirmation (DESIGN §6.10).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from tradebot.app import Application, BrokerChoice, build, database_path
from tradebot.control.arming import (
    LIVE_CONFIRMATION_PHRASE,
    ArmingStore,
    assert_live_confirmation,
)
from tradebot.control.basket_runner import CycleResult
from tradebot.control.config_store import ConfigStore
from tradebot.control.supervisor import Supervisor
from tradebot.core.clock import SystemClock
from tradebot.core.enums import ConfigKind, Mode
from tradebot.core.errors import TradebotError
from tradebot.core.logging import configure_logging, get_logger
from tradebot.dashboard.app import create_dashboard
from tradebot.dashboard.auth import assert_bind_allowed, require_token
from tradebot.decision.presets import PANELS
from tradebot.news.rss import FEEDS
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.risk.state import assert_rearm_phrase

logger = get_logger("tradebot.cli")

CLI_ACTOR = "cli"

#: Exit codes an operator (or a supervisor script) can act on.
EXIT_REFUSED = 1  # a `TradebotError`: the process would not start
EXIT_MISUSE = 2  # the command cannot be carried out as asked
EXIT_RECOVERY_HALTED = 3  # DESIGN §8.2 left the process up but not trading
EXIT_CYCLE_FAILED = 4  # a `--once` cycle failed; a supervised run would have retried it


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tradebot", description="AI Panel Trading System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run decision cycles")
    _add_common(run)
    _add_wiring(run)
    run.add_argument(
        "--once",
        action="store_true",
        help=(
            "run a single cycle per basket and exit. Without it, each basket cycles on its own "
            "schedule until interrupted"
        ),
    )

    serve = subparsers.add_parser(
        "serve", help="run the dashboard, and the baskets alongside it unless --observe"
    )
    _add_common(serve)
    _add_wiring(serve)
    serve.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "permit a non-loopback bind. Auth is mandatory either way; this exists so a --host "
            "typo cannot put the kill switch on a network without someone deciding to (PLAN §3.3)"
        ),
    )
    serve.add_argument(
        "--observe",
        action="store_true",
        help=(
            "serve the dashboard without the supervisor: the system is inspectable and nothing "
            "cycles. What you want to read the log after an incident without it trading on"
        ),
    )

    risk = subparsers.add_parser("risk", help="inspect and clear risk state")
    risk_actions = risk.add_subparsers(dest="action", required=True)
    _add_common(risk_actions.add_parser("status", help="show kill switch and halted baskets"))

    rearm = risk_actions.add_parser("rearm", help="re-arm the kill switch (human only)")
    _add_common(rearm)
    rearm.add_argument("--confirm", required=True, help="the exact re-arm phrase")

    unhalt = risk_actions.add_parser("unhalt", help="clear a halted basket (human only)")
    _add_common(unhalt)
    unhalt.add_argument("basket_id")
    unhalt.add_argument("--confirm", required=True, help="the exact re-arm phrase")

    arm = risk_actions.add_parser(
        "arm-live",
        help="record that a human armed live trading, with a per-order notional cap (human only)",
    )
    _add_common(arm)
    arm.add_argument(
        "--max-notional",
        required=True,
        type=Decimal,
        help="largest notional one live order may carry, in the account's quote currency",
    )
    arm.add_argument(
        "--confirm", required=True, help=f"the exact phrase {LIVE_CONFIRMATION_PHRASE!r}"
    )
    arm.add_argument("--note", default="", help="why live was armed; recorded on the row")

    disarm = risk_actions.add_parser("disarm-live", help="withdraw live arming")
    _add_common(disarm)
    disarm.add_argument("--reason", default="", help="why arming was withdrawn")

    config = subparsers.add_parser("config", help="inspect versioned configuration")
    config_actions = config.add_subparsers(dest="action", required=True)
    _add_common(config_actions.add_parser("list", help="baskets and limits currently in service"))
    history = config_actions.add_parser("history", help="every version of one document")
    _add_common(history)
    history.add_argument("kind", choices=[kind.value for kind in ConfigKind])
    history.add_argument("config_id", help="basket id, or 'global' for the Tier-2 policy")
    return parser.parse_args(argv)


def _add_wiring(parser: argparse.ArgumentParser) -> None:
    """Options that select what gets wired. Shared by `run` and `serve`, which wire the same
    stack — one of them additionally puts a web page in front of it."""
    parser.add_argument("--confirm", default=None, help="typed confirmation phrase for live mode")
    parser.add_argument(
        "--news",
        action="append",
        default=[],
        metavar="SOURCE_ID",
        help=(
            "enable an RSS news source (repeatable). Off by default: this reaches out to the "
            f"internet. Known sources: {', '.join(sorted(FEEDS))}"
        ),
    )
    parser.add_argument(
        "--panel",
        default="stub",
        choices=sorted(PANELS),
        help=(
            "which agent panel to run. Defaults to 'stub': the offline scripted panel, which "
            "costs nothing and needs no API key. 'free' uses hosted free slots with per-seat "
            "cross-vendor fallbacks; 'local' runs entirely on your own machine. Each panel "
            "declares its own providers and each seat its own fallback chain — see "
            "tradebot/decision/presets.py"
        ),
    )
    parser.add_argument(
        "--broker",
        default=BrokerChoice.SIM.value,
        choices=[choice.value for choice in BrokerChoice],
        help=(
            "which venue takes the orders. Defaults to 'sim': deterministic fills, which in paper "
            "mode means real market data with simulated execution — the soak's evidence base "
            "(DESIGN §9). 'binance' and 'alpaca' reach the venue's *test* endpoint and are "
            "adapter integration checks, not evidence; they need mode-specific API keys in the "
            "environment"
        ),
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in Mode],
        help="execution mode (required; there is no default)",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--verbose", action="store_true")


async def _open(args: argparse.Namespace) -> Application:
    mode = Mode(args.mode)
    configure_logging(mode=mode.value, level=logging.DEBUG if args.verbose else logging.INFO)
    return await build(
        mode,
        confirmation=getattr(args, "confirm", None),
        db_path=database_path(mode, args.data_dir),
        news_sources=tuple(getattr(args, "news", None) or ()),
        panel_id=getattr(args, "panel", None) or "stub",
        broker=BrokerChoice(getattr(args, "broker", None) or BrokerChoice.SIM.value),
    )


async def run_command(args: argparse.Namespace) -> int:
    """Recover, then cycle — once, or on each basket's schedule until interrupted."""
    application = await _open(args)
    try:
        recovery = await application.recover()
        if recovery.halted:
            logger.error(
                "startup recovery halted the process; nothing will trade",
                extra={"failures": list(recovery.failures), "reason": recovery.state.reason},
            )
            return EXIT_RECOVERY_HALTED
        if args.once:
            return _report(application, await application.supervisor.run_once())
        await _serve(application)
    finally:
        await application.shutdown()
    return 0


async def serve_command(args: argparse.Namespace) -> int:
    """Run the dashboard, and the baskets alongside it unless `--observe`.

    The token and the bind address are checked **before anything is wired**, so a refusal costs
    nothing and reports the same way `run` does. Note what happens when startup recovery halts:
    the dashboard still serves and the supervisor does not start. That is DESIGN §8.2 step 5
    exactly — the process stays up, the dashboard shows why, and nothing trades. Refusing to
    serve would leave the operator with no way to see the reason.
    """
    token = require_token()
    assert_bind_allowed(args.host, allow_remote=args.allow_remote)
    application = await _open(args)
    try:
        recovery = await application.recover()
        if recovery.halted:
            logger.error(
                "startup recovery halted the process; serving the dashboard, nothing will trade",
                extra={"failures": list(recovery.failures), "reason": recovery.state.reason},
            )
        trading = not recovery.halted and not args.observe
        logger.warning(
            "dashboard listening",
            extra={"host": args.host, "port": args.port, "supervising": trading},
        )
        await _run_server(
            create_dashboard(application, token=token, observe_only=not trading),
            host=args.host,
            port=args.port,
            supervisor=application.supervisor if trading else None,
        )
    finally:
        await application.shutdown()
    return EXIT_RECOVERY_HALTED if recovery.halted else 0


async def _run_server(
    dashboard: FastAPI, *, host: str, port: int, supervisor: Supervisor | None
) -> None:
    """Serve HTTP and — unless observing — cycle baskets, on one event loop.

    One loop rather than two processes because the dashboard reads the same in-memory ledger and
    supervisor the runners use; a second process would be reading a second, staler view of an
    account that only one of them is allowed to write (PLAN §2.6).

    Whichever task finishes first stops the other: a supervisor still cycling after the operator
    has stopped the server would trade with nobody watching.
    """
    server = uvicorn.Server(
        uvicorn.Config(dashboard, host=host, port=port, log_config=None, access_log=False)
    )
    tasks = [asyncio.create_task(server.serve(), name="dashboard")]
    if supervisor is not None:
        tasks.append(asyncio.create_task(supervisor.serve(), name="supervisor"))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("interrupted; stopping the dashboard and the runners")
    finally:
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _serve(application: Application) -> int:
    """Run every basket on its schedule until the operator interrupts.

    `KeyboardInterrupt` is a normal exit, not a failure: the supervisor's `stop` cancels each
    basket's task, and the orders those baskets left working are recovered by the startup sequence
    on the next start rather than being abandoned (DESIGN §8.2).
    """
    logger.info(
        "supervising baskets on their schedules; interrupt to stop",
        extra={"baskets": [basket.basket_id for basket in application.baskets]},
    )
    try:
        await application.supervisor.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("interrupted; stopping runners")
    return 0


def _report(application: Application, results: Sequence[CycleResult]) -> int:
    """Log what each cycle did, and exit non-zero if any of them failed.

    A supervised run absorbs a failed cycle by design — it backs off and tries again. A `--once`
    run has no next attempt, so the failure has to reach the shell instead of being reported as a
    clean exit to whatever launched it.
    """
    for result in results:
        logger.info(
            "cycle finished",
            extra={
                "cycle_id": result.cycle_id,
                "basket_id": result.basket_id,
                "outcome": result.outcome.value,
                "orders": len(result.orders),
                "decisions": [d.action.value for d in result.decisions],
            },
        )
    failed = [w.basket_id for w in application.supervisor.workers if w.failures]
    if failed:
        logger.error("cycles failed", extra={"baskets": failed})
        return EXIT_CYCLE_FAILED
    return 0


async def risk_command(args: argparse.Namespace) -> int:
    if args.action in _ARMING_ACTIONS:
        return await arming_command(args)
    application = await _open(args)
    try:
        return await _RISK_ACTIONS[args.action](application, args)
    finally:
        await application.shutdown()


async def config_command(args: argparse.Namespace) -> int:
    """Read the versioned ConfigStore. Editing is the dashboard's job (DESIGN §6.10)."""
    application = await _open(args)
    try:
        return _CONFIG_ACTIONS[args.action](application.configs, args)
    finally:
        await application.shutdown()


def _config_list(configs: ConfigStore, _args: argparse.Namespace) -> int:
    for record in configs.baskets():
        logger.info(
            "basket",
            extra={
                "basket_id": record.ref.config_id,
                "version": record.ref.version,
                "status": record.document.status.value,
                "instruments": [i.key for i in record.document.instruments],
                "every_seconds": record.document.schedule.every_seconds,
            },
        )
    policy = configs.global_risk()
    if policy is not None:
        logger.info(
            "global risk policy",
            extra={
                "version": policy.ref.version,
                "max_drawdown_pct": str(policy.document.max_drawdown_pct),
                "max_daily_loss_pct": str(policy.document.max_daily_loss_pct),
            },
        )
    return 0


def _config_history(configs: ConfigStore, args: argparse.Namespace) -> int:
    for record in configs.history(ConfigKind(args.kind), args.config_id):
        logger.info(
            "config version",
            extra={
                "config": record.ref.key,
                "version": record.ref.version,
                "actor": record.actor,
                "note": record.note,
                "retired": record.retired,
            },
        )
    return 0


async def _risk_status(application: Application, _args: argparse.Namespace) -> int:
    state = application.states.load()
    halted = application.states.halted_baskets()
    logger.info(
        "risk state",
        extra={
            "kill_switch": state.kill_switch.value,
            "reason": state.reason,
            "high_water_mark": str(state.high_water_mark),
            "day_start_equity": str(state.day_start_equity),
            "halted_baskets": halted,
        },
    )
    return 0


async def _risk_rearm(application: Application, args: argparse.Namespace) -> int:
    assert_rearm_phrase(args.confirm)
    await application.recover()
    state = await application.watchdog.rearm(application.equity(), actor=CLI_ACTOR)
    logger.warning("kill switch re-armed", extra={"high_water_mark": str(state.high_water_mark)})
    return 0


async def _risk_unhalt(application: Application, args: argparse.Namespace) -> int:
    assert_rearm_phrase(args.confirm)
    await application.watchdog.resume_basket(args.basket_id, actor=CLI_ACTOR)
    logger.warning("basket un-halted", extra={"basket_id": args.basket_id})
    return 0


async def arming_command(args: argparse.Namespace) -> int:
    """Arm or disarm live trading. Touches the database only, and never builds an application.

    Deliberately separate from every other path: arming is a decision recorded about a mode that
    cannot yet be run, so it must not depend on that mode being wireable (PLAN §2.4, Phase 8).
    """
    mode = Mode(args.mode)
    configure_logging(mode=mode.value, level=logging.DEBUG if args.verbose else logging.INFO)
    if not mode.is_live:
        logger.error(
            "live arming applies to the live database only; each mode has its own",
            extra={"mode": mode.value},
        )
        return EXIT_MISUSE

    engine = create_database(database_path(mode, args.data_dir))
    writer = SingleWriter(engine)
    store = ArmingStore(engine, writer, SystemClock())
    try:
        return await _ARMING_ACTIONS[args.action](store, args)
    finally:
        writer.close()


async def _arm_live(store: ArmingStore, args: argparse.Namespace) -> int:
    assert_live_confirmation(args.confirm)
    arming = await store.arm(actor=CLI_ACTOR, max_live_notional=args.max_notional, note=args.note)
    logger.warning(
        "LIVE TRADING ARMED — real money is now reachable once live wiring exists",
        extra={"max_live_notional": str(arming.max_live_notional), "note": arming.note},
    )
    return 0


async def _disarm_live(store: ArmingStore, args: argparse.Namespace) -> int:
    await store.disarm(actor=CLI_ACTOR, reason=args.reason)
    logger.warning("live trading disarmed", extra={"reason": args.reason})
    return 0


_ARMING_ACTIONS = {"arm-live": _arm_live, "disarm-live": _disarm_live}
_RISK_ACTIONS = {"status": _risk_status, "rearm": _risk_rearm, "unhalt": _risk_unhalt}
_CONFIG_ACTIONS = {"list": _config_list, "history": _config_history}
_COMMANDS = {
    "run": run_command,
    "serve": serve_command,
    "risk": risk_command,
    "config": config_command,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_COMMANDS[args.command](args))
    except TradebotError as exc:
        configure_logging(mode=getattr(args, "mode", "unknown"))
        logger.error("refusing to run", extra={"error": str(exc), "kind": type(exc).__name__})
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
