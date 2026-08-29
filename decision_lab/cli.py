"""`python -m decision_lab …` — the tool's own entry point (spec §13).

Its own, not a subcommand of `tradebot`: the separation contract says the bot's CLI is untouched
(§2.1), and a tuning tool that appears in `tradebot --help` is a tuning tool an operator can
reach from a live process by accident.

Nothing here prints. `T20` bans `print` repo-wide and the reason holds here too — a result that
matters is written to a file under `reports/` (§14), and progress belongs in the log where a long
sweep's output can be filtered. Exit codes carry the verdict.

Failure semantics: every `TradebotError` is caught at the boundary and becomes the exit code its
kind implies, with the message logged. An unexpected exception is not caught — a stack trace is
the right answer to a defect in the tool.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from decision_lab import calibration_days as cd
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import records as rc
from decision_lab import regimes as rg
from decision_lab import render as rd
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.params import (
    CADENCE_SECONDS,
    DAYSET_FILE,
    DEFAULT_SEED,
    DEFAULT_SHOCK_PERCENTILE,
    reports_dir,
)
from tradebot.core.clock import SystemClock, ensure_utc
from tradebot.core.errors import ConfigError, MoneyError, TradebotError
from tradebot.core.logging import configure_logging, get_logger
from tradebot.core.money import to_decimal
from tradebot.interfaces.exchange import VenueTransport
from tradebot.marketdata.recorder import MANIFEST, ReplayDataset

logger = get_logger("decision_lab.cli")

#: Exit codes, following the bot's convention of a distinct code per distinct refusal (§13).
EXIT_OK = 0
EXIT_MISUSE = 2  # argparse's own code for bad arguments
EXIT_DATASET = 3  # unverified, holed beyond repair, or no pinned day set
EXIT_CANDIDATE = 4  # a candidate failed `Basket` validation            (slice C)
EXIT_BUDGET = 5  # budget ceiling reached, partial results written      (slice C)
EXIT_GATE = 6  # the §10.6 calibration gate is unsatisfied              (slice D)

#: `mode` is a static field on every log line the bot emits. This is not a bot mode and never
#: opens a bot database, so it says what it is rather than borrowing `sim`.
LOG_MODE = "decision_lab"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="decision_lab",
        description="score and compare the panel's decision logic over recorded history",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("dataset", help="audit recorded history and pin calibration days")
    dataset_actions = dataset.add_subparsers(dest="action", required=True)

    verify = dataset_actions.add_parser(
        "verify", help="find every hole in a recorded dataset, and optionally repair it"
    )
    verify.add_argument("--data", type=Path, required=True, help="dataset directory")
    verify.add_argument(
        "--repair",
        action="store_true",
        help=(
            "re-ask the venue for each hole over public, read-only REST. Off by default: a "
            "verification pass must not reach the network unless it was asked to"
        ),
    )
    verify.add_argument("--verbose", action="store_true")

    days = dataset_actions.add_parser(
        "days", help="select and pin the nine calibration days, or show the pinned set"
    )
    days.add_argument("--data", type=Path, required=True, help="dataset directory")
    days.add_argument("--seed", type=int, default=DEFAULT_SEED)
    days.add_argument(
        "--reference-instrument",
        default="",
        help="whose volatility distribution the days are drawn from; defaults to the first in "
        "the manifest. A day violent for one instrument and calm for another is a legitimate "
        "test and a different one, so it is recorded and printed on every report",
    )
    days.add_argument("--scoring-timeframe", default="")
    days.add_argument(
        "--shock-percentile",
        type=_decimal_arg,
        default=DEFAULT_SHOCK_PERCENTILE,
        help="at or above this percentile of the reference instrument's own distribution is a "
        "shock. Loosen it when a dataset's shock pools come up thin",
    )
    days.add_argument(
        "--reselect",
        action="store_true",
        help="replace an existing pinned set. An explicit act: it moves dayset_digest and "
        "therefore every recorded run identity derived from it",
    )
    days.add_argument(
        "--pin",
        action="append",
        default=[],
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="add a day by hand",
    )
    days.add_argument("--verbose", action="store_true")

    corpus = commands.add_parser("corpus", help="build the frozen decision contexts a sweep reads")
    corpus_actions = corpus.add_subparsers(dest="action", required=True)

    corpus_build_parser = corpus_actions.add_parser(
        "build", help="run one reference pass and index it"
    )
    corpus_build_parser.add_argument("--data", type=Path, required=True, help="dataset directory")
    corpus_build_parser.add_argument(
        "--every",
        default="4h",
        choices=tuple(CADENCE_SECONDS),
        help="cycle cadence. A corpus property, not a sweep one: every candidate in one sweep "
        "sees one cadence, so a cadence comparison is N corpora (§5.5)",
    )
    corpus_build_parser.add_argument(
        "--reference-panel",
        default="sim",
        help="whose deliberation supplies the positions in the snapshots. `sim` and `stub` are "
        "offline and free; a real panel is available when the positions themselves need to be "
        "the ones a real panel would have held (§5.2)",
    )
    corpus_build_parser.add_argument("--start-equity", type=_decimal_arg, default=Decimal(10_000))
    corpus_build_parser.add_argument(
        "--since", default=None, help="window start; defaults to the data's"
    )
    corpus_build_parser.add_argument(
        "--until", default=None, help="window end; defaults to the data's"
    )
    corpus_build_parser.add_argument("--verbose", action="store_true")

    report_ = commands.add_parser(
        "report", help="score a built corpus and file the result under decision_lab/reports/"
    )
    report_.add_argument("--corpus", required=True, help="corpus id from `corpus build`")
    report_.add_argument(
        "--data", type=Path, default=None, help="override the recorded dataset path"
    )
    report_.add_argument("--regimes", type=Path, default=None, help="named event windows TOML")
    report_.add_argument("--scoring-timeframe", default="", help="defaults to the shortest")
    report_.add_argument(
        "--band-k", type=_decimal_arg, default=None, help="the ATR multiple, default 1.0"
    )
    report_.add_argument("--horizon", type=int, default=None, help="forward bars, default 6")
    report_.add_argument("--out", type=Path, default=None, help="report path (.md)")
    report_.add_argument("--verbose", action="store_true")

    return parser.parse_args(argv)


def _decimal_arg(value: str) -> Decimal:
    """A `Decimal` command-line value, refused by argparse rather than by a traceback.

    `to_decimal` raises `MoneyError`, which argparse does not recognise as bad input — it would
    escape as an unhandled `ArithmeticError` and lose the usage message.
    """
    try:
        return to_decimal(value)
    except MoneyError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _load(directory: Path, clock: SystemClock) -> ReplayDataset:
    """The dataset, or a refusal naming the command that records one."""
    if not (directory / MANIFEST).is_file():
        raise ConfigError(
            f"{directory} holds no {MANIFEST}. Record one with `tradebot backtest fetch "
            f"--symbol BTC/USDT --timeframe 1h --since … --until … --out {directory}`"
        )
    return ReplayDataset.load(directory, clock)


async def dataset_verify(args: argparse.Namespace) -> int:
    """Audit the dataset, write the sidecar, and answer with whether it is fit to build on."""
    clock = SystemClock()
    dataset = _load(args.data, clock)

    if args.repair:
        provider, transport = _history_provider(clock)
        try:
            audit = await ds.repair(dataset, provider, clock)
        finally:
            await transport.close()
        # Re-load: repair rewrote the CSVs, so the counts and the digest on the audit that is
        # written must describe the files on disk *after* the correction, read back through the
        # bot's own reader rather than trusted from the patch loop.
        audit = await ds.audit(_load(args.data, clock), clock, carry=audit)
    else:
        audit = await ds.audit(dataset, clock)

    ds.write_audit(args.data, audit)
    holed = sorted(key for key, coverage in audit.series.items() if not coverage.is_clean)
    logger.info(
        "dataset audited",
        extra={
            "series": len(audit.series),
            "repaired": sum(coverage.repaired for coverage in audit.series.values()),
            "holed": holed,
        },
    )
    if holed:
        logger.error("dataset holds unrepairable holes", extra={"series": holed})
        return EXIT_DATASET
    return EXIT_OK


def _history_provider(clock: SystemClock) -> tuple[ds.HistoryProvider, VenueTransport]:
    """The public Binance read layer. Imported lazily so an offline run never constructs one.

    The import is what costs: `marketdata.factory` pulls in `ccxt`, and a `dataset verify` with no
    `--repair` has no business loading an exchange library at all.
    """
    from tradebot.marketdata.factory import binance_spot_history

    return binance_spot_history(clock)


async def dataset_days(args: argparse.Namespace) -> int:
    """Select and pin the nine calibration days, or report the set already pinned.

    Replacing a pinned set is `--reselect` and nothing else, `--pin` included: the digest it moves
    is the identity every §11 run is recorded under, so a command that quietly replaced it would
    invalidate results nobody was told about.
    """
    clock = SystemClock()
    audit = ds.require_verified(args.data)

    if (args.data / DAYSET_FILE).is_file() and not args.reselect:
        if args.pin:
            raise ConfigError(
                f"{args.data} already holds a pinned day set, and --pin would replace it. Pass "
                "--reselect as well to say so: the new set has a different dayset_digest, and "
                "every run recorded under the old one stops being comparable"
            )
        pinned = cd.require_pinned(args.data)
        logger.info("calibration days already pinned", extra=_days_fields(pinned))
        return EXIT_OK

    dataset = _load(args.data, clock)
    days = await cd.select(
        dataset,
        audit,
        clock,
        seed=args.seed,
        reference_instrument=args.reference_instrument or dataset.instruments[0].key,
        scoring_timeframe=args.scoring_timeframe or dataset.timeframes[0],
        thresholds=cd.Thresholds(shock_percentile=args.shock_percentile),
        pinned=tuple(args.pin),
    )
    cd.write(args.data, days)
    logger.info("calibration days pinned", extra=_days_fields(days))
    return EXIT_OK


def _days_fields(days: cd.CalibrationDays) -> dict[str, Any]:
    return {
        "digest": days.dayset_digest,
        "reference": days.reference_instrument,
        "seed": days.seed,
        "days": {
            pool: [day.isoformat() for day in dates] for pool, dates in sorted(days.days.items())
        },
    }


async def corpus_build(args: argparse.Namespace) -> int:
    """Build the corpus. Refuses an unverified dataset before doing any work."""
    built = await cp.build(
        data_dir=args.data,
        reference_panel=args.reference_panel,
        cadence_seconds=CADENCE_SECONDS[args.every],
        start_equity=args.start_equity,
        since=_moment(args.since),
        until=_moment(args.until),
    )
    logger.info(
        "corpus ready",
        extra={
            "corpus_id": built.meta.corpus_id,
            "entries": len(built.entries),
            "cadence": args.every,
            "panel": args.reference_panel,
            "news": "blind" if built.meta.news_blind else built.meta.archive_digest,
        },
    )
    return EXIT_OK


def _moment(value: str | None) -> datetime | None:
    """An optional ISO window edge, UTC-aware. A bare date is midnight UTC, never local time.

    Four lines rather than an import: the bot's equivalent is `tradebot.__main__._moment`, and
    reaching into another module's private for a date parse is the dependency `csv_path` avoids
    for a filename.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)


async def report(args: argparse.Namespace) -> int:
    """Score the reference pass in a built corpus and write the Markdown report."""
    meta, cycles = rc.load(args.corpus)
    data_dir = args.data or Path(meta.dataset_directory)
    audit = ds.require_verified(data_dir)
    dataset = ReplayDataset.load(data_dir, SystemClock())

    params = sc.ScoringParams(
        timeframe=args.scoring_timeframe or dataset.timeframes[0],
        **({"band_k": args.band_k} if args.band_k is not None else {}),
        **({"horizon_bars": args.horizon} if args.horizon is not None else {}),
    )
    index = await sc.build_price_index(dataset, audit, params)
    regime_index = (await rg.index_dataset(dataset, params.timeframe)).with_windows(
        rg.load_windows(args.regimes or rg.DEFAULT_REGIMES_TOML)
    )

    scored = sc.score_records(cycles, index=index, regimes=regime_index, params=params)
    panel = meta.reference_basket.panel
    built = rd.LabReport(
        generated_at=SystemClock().now(),
        corpus_id=meta.corpus_id,
        dataset_directory=str(data_dir),
        dataset_digest=meta.dataset_digest,
        dayset_digest=_dayset_digest(data_dir),
        reference_instrument=dataset.instruments[0].key,
        reference_panel_id=meta.reference_panel_id,
        reference_config_digest=meta.reference_config_digest,
        cadence_seconds=meta.cadence_seconds,
        scoring=params,
        vol_window_bars=regime_index.window_bars,
        shock_percentile=regime_index.shock_percentile,
        named_windows=tuple(w.name for w in regime_index.windows),
        start_equity=meta.start_equity,
        news_blind=meta.news_blind,
        panel_models=tuple(dict.fromkeys(f"{s.provider_id}:{s.model}" for s in panel.seats)),
        cycles=len(cycles),
        regimes=sc.by_regime(scored),
        seats=st.score_seats(cycles, scored, panel=panel),
    )
    out = args.out or reports_dir() / f"decision-lab-{meta.corpus_id}.md"
    rd.write_report(built, out)
    logger.info("report written", extra={"path": str(out), "decisions": len(scored)})
    return EXIT_OK


def _dayset_digest(data_dir: Path) -> str:
    """The pinned day set is not required to score a corpus — it is required to *calibrate* one
    (slice D). Recorded when present so a report can be tied to the set in force, absent
    otherwise rather than refusing a scoring run for want of a §10 artifact."""
    try:
        return cd.require_pinned(data_dir).dayset_digest
    except ConfigError:
        return ""


#: Command → coroutine. Dispatch over a table rather than a chain of `if`s, per the repo's own
#: convention (CLAUDE.md, "prefer dispatch over branching").
COMMANDS: dict[tuple[str, str], Callable[[argparse.Namespace], Coroutine[Any, Any, int]]] = {
    ("dataset", "verify"): dataset_verify,
    ("dataset", "days"): dataset_days,
    ("corpus", "build"): corpus_build,
    # `report` has no sub-action, so `getattr(args, "action", "")` yields "".
    ("report", ""): report,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(
        mode=LOG_MODE, level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    )
    handler = COMMANDS[(args.command, getattr(args, "action", ""))]
    try:
        return asyncio.run(handler(args))
    except TradebotError as error:
        # Every refusal this tool can raise is about the evidence it was pointed at: an absent
        # dataset, an unverified one, a venue that would not answer for it. `ConfigError` is the
        # common case and is named so the taxonomy stays visible at the boundary.
        logger.error(str(error), extra={"kind": type(error).__name__})
        return EXIT_DATASET
