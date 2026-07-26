"""CLI entry point.

`--mode` is **required and has no default**. The classic way to lose real money is running live
while believing you are on testnet, so no environment variable, config default, or typo can
select a mode (PLAN §2.4).

The `risk` subcommands are the operator surface for the controls that only a human may clear —
a tripped kill switch and a halted basket. They exist here until the dashboard takes the job in
Phase 6, and they require the same typed phrase the GUI will: an automatic re-arm would defeat
the control entirely (DESIGN §6.6).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from tradebot.app import Application, build, database_path
from tradebot.core.enums import Mode
from tradebot.core.errors import TradebotError
from tradebot.core.logging import configure_logging, get_logger
from tradebot.risk.state import assert_rearm_phrase

logger = get_logger("tradebot.cli")

CLI_ACTOR = "cli"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tradebot", description="AI Panel Trading System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run decision cycles")
    _add_common(run)
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--confirm", default=None, help="typed confirmation phrase for live mode")

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
    return parser.parse_args(argv)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in Mode],
        help="execution mode (required; there is no default)",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--verbose", action="store_true")


def _open(args: argparse.Namespace) -> Application:
    mode = Mode(args.mode)
    configure_logging(mode=mode.value, level=logging.DEBUG if args.verbose else logging.INFO)
    return build(
        mode,
        confirmation=getattr(args, "confirm", None),
        db_path=database_path(mode, args.data_dir),
    )


async def run_command(args: argparse.Namespace) -> int:
    if not args.once:
        logger.error("continuous scheduling arrives with the Scheduler in Phase 6; use --once")
        return 2

    application = _open(args)
    try:
        recovery = await application.recover()
        if recovery.halted:
            logger.error(
                "startup recovery halted the process; nothing will trade",
                extra={"failures": list(recovery.failures), "reason": recovery.state.reason},
            )
            return 3

        for runner in application.runners:
            if not recovery.may_run(runner.basket):
                logger.warning("basket is halted; skipping", extra={"basket": runner.basket.name})
                continue
            result = await runner.run_once()
            logger.info(
                "cycle finished",
                extra={
                    "cycle_id": result.cycle_id,
                    "outcome": result.outcome.value,
                    "orders": len(result.orders),
                    "decisions": [d.action.value for d in result.decisions],
                },
            )
    finally:
        application.close()
    return 0


async def risk_command(args: argparse.Namespace) -> int:
    application = _open(args)
    try:
        return await _RISK_ACTIONS[args.action](application, args)
    finally:
        application.close()


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


_RISK_ACTIONS = {"status": _risk_status, "rearm": _risk_rearm, "unhalt": _risk_unhalt}
_COMMANDS = {"run": run_command, "risk": risk_command}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_COMMANDS[args.command](args))
    except TradebotError as exc:
        configure_logging(mode=getattr(args, "mode", "unknown"))
        logger.error("refusing to run", extra={"error": str(exc), "kind": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
