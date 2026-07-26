"""CLI entry point.

`--mode` is **required and has no default**. The classic way to lose real money is running live
while believing you are on testnet, so no environment variable, config default, or typo can
select a mode (PLAN §2.4).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from tradebot.app import build, database_path
from tradebot.core.enums import Mode
from tradebot.core.errors import TradebotError
from tradebot.core.logging import configure_logging, get_logger

logger = get_logger("tradebot.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tradebot", description="AI Panel Trading System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run decision cycles")
    run.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in Mode],
        help="execution mode (required; there is no default)",
    )
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--confirm", default=None, help="typed confirmation phrase for live mode")
    run.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


async def run_command(args: argparse.Namespace) -> int:
    mode = Mode(args.mode)
    configure_logging(mode=mode.value, level=logging.DEBUG if args.verbose else logging.INFO)

    if not args.once:
        logger.error("continuous scheduling arrives with the Scheduler in Phase 6; use --once")
        return 2

    application = build(mode, confirmation=args.confirm, db_path=database_path(mode, args.data_dir))
    try:
        for runner in application.runners:
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run_command(args))
    except TradebotError as exc:
        configure_logging(mode=getattr(args, "mode", "unknown"))
        logger.error("refusing to run", extra={"error": str(exc), "kind": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
