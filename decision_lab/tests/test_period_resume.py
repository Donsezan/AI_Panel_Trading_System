"""Checkpoints and Continue: an error continues from the last good point (period spec §5.7).

Every comparison here is against an uninterrupted run, and the panel is `Plan`, which answers from
the date and the symbol in the prompt, so a live call after the boundary answers as the unbroken run
did. That is forced: a seeded `varied-*` stub keeps its draws in one `Random`, so a resumed process
would start that sequence again and the test would measure the seed instead of the Continue.

A kill is a `BaseException` raised after a checkpoint. `run` catches `Exception`, so the meta stays
`RUNNING` exactly as a dead process leaves it, where an ordinary exception writes `INTERRUPTED`.

Fills, round trips and cycles are compared by time, price and quantity. Order and fill ids are
fresh uuids in every process, so they cannot match and say nothing if they do not.
"""

from __future__ import annotations

import gc
import json
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from decision_lab import cli
from decision_lab import period as pd
from decision_lab.candidates import Candidate
from decision_lab.longrun import ProfitEvidence
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.records import CycleRecord
from decision_lab.replay import Replayer
from decision_lab.tests import factories as f
from decision_lab.tests import test_period_pause as pause
from decision_lab.tests.test_period_pause import (
    BTC,
    ETH,
    PAUSED_ON,
    TWO_LOSSES,
    Ran,
    Votes,
    _oscillation,
    candidate_over,
    day,
    execute,
    limits_pinned,
    sells_between,
    utc,
)
from decision_lab.tests.test_period_run import cycle_stamps, fills_in, read_log
from tradebot.app import Application
from tradebot.control import startup as startup_module
from tradebot.control import supervisor as supervisor_module
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, ProviderBinding
from tradebot.core.decision import Decision
from tradebot.core.enums import Action
from tradebot.core.errors import ConfigError
from tradebot.core.events import Event, EventFactory, EventType
from tradebot.core.market import Candle
from tradebot.core.money import ZERO
from tradebot.core.snapshot import ContextSnapshot, NewsCoverage, NewsItemView
from tradebot.decision.providers.stub import DEFAULT_RESPONSE, FAIL
from tradebot.execution import monitor as monitor_module
from tradebot.interfaces.llm import CompletionRequest
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.store import EventStore
from tradebot.validation.evidence import Evidence

COST = Decimal("0.01")
QUARTER = "2024-Q1"
RISE_DAY = date(2024, 2, 10)
#: Past the entry's stop in March, so a position carried over the February boundary is closed by a
#: stop that only a faithfully re-executed venue can fire.
DROP_DAY = date(2024, 3, 10)
#: One trip closed in January, one entry carried over the February boundary, and an entry after it.
TRADES: Votes = {
    (BTC, day(1, 10)): "BUY",
    (BTC, day(1, 13)): "SELL",
    (BTC, day(2, 20)): "BUY",
    (BTC, day(3, 20)): "BUY",
}
#: The review's probe: a carried position sold after the boundary, and a fresh entry after that.
SOLD_THEN_BOUGHT: Votes = {
    (BTC, day(2, 20)): "BUY",
    (BTC, day(3, 5)): "SELL",
    (BTC, day(3, 20)): "BUY",
}
#: A carried position trimmed twice, each sale replacing legs the one before it placed.
TRIMMED: Votes = {
    (BTC, day(2, 20)): "BUY",
    (BTC, day(3, 5)): "SELL half",
    (BTC, day(3, 15)): "SELL half",
}
#: Held over the boundary at which the bar below has already armed the stop.
HELD_INTO_THE_GAP: Votes = {(BTC, day(2, 20)): "BUY"}
#: A win in February hands the limit back, and March can pause under the configured limit again.
WIN_AFTER_A_LIFT: Votes = {
    **TWO_LOSSES,
    (BTC, day(2, 5)): "BUY",
    (BTC, day(2, 14)): "SELL",
    (BTC, day(3, 1)): "BUY",
    (BTC, day(3, 4)): "SELL",
    (BTC, day(3, 7)): "BUY",
    (BTC, day(3, 10)): "SELL",
    (BTC, day(3, 13)): "BUY",
}


class Killed(BaseException):
    """A process dying. Not an `Exception`, so nothing in `run` can tidy up after it."""


Rewrite = Callable[[dict[str, Any]], dict[str, Any] | None]
Trip = tuple[str, datetime, Decimal]
Cycle = tuple[datetime, str, Decimal]


def _verified(root: Path, name: str, btc: tuple[Candle, ...]) -> Path:
    data = root / name
    f.write_dataset(data, {(f.instrument(), "1h"): btc, (f.instrument(ETH), "1h"): _oscillation()})
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DECISION_LAB_WORKSPACE", str(root / f"{name}-lock"))
        assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    return data


def _with_a_gap(hour: datetime, close: str) -> tuple[Candle, ...]:
    """The flat market, with one bar wholly below a stop's limit: it arms the stop, unfilled."""
    bars = list(_oscillation())
    index = int((hour - f.EPOCH) // timedelta(hours=1))
    price = Decimal(close)
    bars[index] = bars[index].model_copy(
        update={"open": price, "high": price, "low": price, "close": price}
    )
    return tuple(bars)


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("resume")
    # The last tick of February is 29 Feb 00:00, and it reads the bar that opened an hour before.
    gap_hour = datetime(2024, 2, 28, 23, tzinfo=utc(1).tzinfo)
    return {
        "flat": _verified(root, "flat", _oscillation()),
        "drop": _verified(root, "drop", _oscillation(drop_on=DROP_DAY)),
        "rise": _verified(root, "rise", _oscillation(rise_on=RISE_DAY)),
        "gap": _verified(root, "gap", _with_a_gap(gap_hour, "98")),
    }


@pytest.fixture(scope="module")
def whole_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Ran]:
    return {}


Uninterrupted = Callable[[Path, str, Votes], Awaitable[Ran]]


@pytest.fixture
def uninterrupted(
    whole_runs: dict[str, Ran], tmp_path_factory: pytest.TempPathFactory
) -> Uninterrupted:
    """The run each test is compared with, paid for once. Votes are not in a run's identity."""

    async def make(data_dir: Path, name: str, votes: Votes) -> Ran:
        if name not in whole_runs:
            workspace = tmp_path_factory.mktemp(f"whole-{name}")
            whole_runs[name] = await execute(data_dir, workspace, QUARTER, votes, cost_usd=COST)
        return whole_runs[name]

    return make


async def killed_after(
    data_dir: Path, workspace: Path, label: str, votes: Votes, **options: Any
) -> None:
    """Run until `label`'s checkpoint is on disk, then die."""
    real = pd.write_checkpoint

    def then_die(directory: Path, month_label: str, **options: Any) -> Path:
        written = real(directory, month_label, **options)
        if month_label == label:
            raise Killed
        return written

    with pytest.MonkeyPatch.context() as patch, pytest.raises(Killed):
        patch.setattr(pd, "write_checkpoint", then_die)
        await execute(data_dir, workspace, QUARTER, votes, cost_usd=COST, **options)


def explode_on(moment: datetime) -> Callable[[BasketWorker], Awaitable[None]]:
    async def explode(worker: BasketWorker) -> None:
        if worker._clock.now() == moment:
            raise RuntimeError("injected crash")

    return explode


def run_dir(workspace: Path) -> Path:
    return next(workspace.glob("period-*"))


def abandoned_files(workspace: Path) -> list[Path]:
    return sorted(run_dir(workspace).glob(f"{pd.PERIOD_DB}.abandoned-*"))


def stored_meta(workspace: Path) -> pd.PeriodMeta:
    text = (run_dir(workspace) / pd.PERIOD_META).read_text(encoding="utf-8")
    return pd.PeriodMeta.model_validate_json(text)


def cycles_of(database: Path) -> tuple[Cycle, ...]:
    def read(store: EventStore) -> tuple[Cycle, ...]:
        return tuple(
            (cycle.started_at, str(cycle.outcome), cycle.cost_usd)
            for cycle in Evidence.gather(store).cycles
        )

    return read_log(database, read)


def round_trips_of(database: Path) -> tuple[Trip, ...]:
    def read(store: EventStore) -> tuple[Trip, ...]:
        return tuple(
            (trip.instrument_key, trip.at, trip.realized_pnl)
            for trip in Evidence.gather(store).round_trips
        )

    return read_log(database, read)


def protective_orders(database: Path) -> tuple[tuple[datetime, str, Decimal], ...]:
    """When each leg was placed, its role and size. Ids are fresh uuids, so never compared."""

    def read(store: EventStore) -> tuple[tuple[datetime, str, Decimal], ...]:
        orders = ((e.ts, e.payload["order"]) for e in store.read_types(EventType.ORDER_SUBMITTED))
        return tuple(
            (at, order["role"], Decimal(order["qty"]))
            for at, order in orders
            if order["role"] != "entry"
        )

    return read_log(database, read)


def per_cycle(run: Ran) -> Decimal:
    """What one panel-answered cycle costs, off February's whole, unpaused month of 29."""
    return run.meta.months[1].cost_usd / 29


def assert_same_outcomes(resumed: Ran, whole: Ran) -> None:
    assert fills_in(resumed.database) == fills_in(whole.database)
    assert round_trips_of(resumed.database) == round_trips_of(whole.database)
    assert cycles_of(resumed.database) == cycles_of(whole.database)
    assert limits_pinned(resumed.database) == limits_pinned(whole.database)
    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.lifts == whole.meta.lifts
    assert resumed.meta.vetoes == whole.meta.vetoes
    assert resumed.meta.incidents == whole.meta.incidents


async def test_continuing_equals_an_uninterrupted_run(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """Spec §5.7's deciding test: killed between months 2 and 3, holding a position with legs."""
    whole = await uninterrupted(data["drop"], "trades", TRADES)
    assert sells_between(whole.database, utc(2, 20), utc(3, 20)), "the carried stop never fired"

    await killed_after(data["drop"], tmp_path, "2024-02", TRADES)
    assert stored_meta(tmp_path).ending is pd.RunEnding.RUNNING
    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta == whole.meta


async def test_a_trade_with_a_triggered_unfilled_stop_survives_a_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """`SimBroker.restore` comes back un-armed: must equal the uninterrupted run, or the seam is
    needed (§5.7). February's last tick reads a bar below the stop's limit, which arms the stop and
    cannot fill it, and every later bar is back above the stop. Only an armed stop sells there."""
    whole = await uninterrupted(data["gap"], "gap", HELD_INTO_THE_GAP)
    assert sells_between(whole.database, utc(3), utc(3, 2)), "the armed stop never filled"

    await killed_after(data["gap"], tmp_path, "2024-02", HELD_INTO_THE_GAP)
    resumed = await execute(
        data["gap"], tmp_path, QUARTER, HELD_INTO_THE_GAP, cost_usd=COST, resume=True
    )

    assert_same_outcomes(resumed, whole)


async def test_a_sale_of_a_carried_position_after_a_continue_cancels_its_stop(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """The review's probe, and why restoring was abandoned. Recovery rebuilds a held position's
    group with no plan (KNOWN_GAPS §9), so on a restored run March's sale left the stop resting over
    a flat position; it then fired on the 10th and the basket halted refusing to settle it."""
    whole = await uninterrupted(data["drop"], "probe", SOLD_THEN_BOUGHT)
    await killed_after(data["drop"], tmp_path, "2024-02", SOLD_THEN_BOUGHT)

    resumed = await execute(
        data["drop"], tmp_path, QUARTER, SOLD_THEN_BOUGHT, cost_usd=COST, resume=True
    )

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert [fill[1] for fill in fills_in(resumed.database)] == ["buy", "sell", "buy"]
    assert_same_outcomes(resumed, whole)
    assert [m.end_equity for m in resumed.meta.months] == [m.end_equity for m in whole.meta.months]


async def test_replaced_legs_after_a_continue_match_the_unbroken_run(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """A trim in March resizes legs placed in February, and `_Tracked.revision`, which feeds the
    deterministic ids of the replacements, is memory that only a re-execution rebuilds."""
    whole = await uninterrupted(data["flat"], "trimmed", TRIMMED)
    legs = protective_orders(whole.database)
    replaced = {qty for at, _, qty in legs if at >= utc(3)}
    assert len(replaced) == 2, f"two trims should have placed two sizes of leg: {legs}"

    await killed_after(data["flat"], tmp_path, "2024-02", TRIMMED)
    resumed = await execute(data["flat"], tmp_path, QUARTER, TRIMMED, cost_usd=COST, resume=True)

    assert protective_orders(resumed.database) == legs
    assert_same_outcomes(resumed, whole)


async def test_continuing_twice_equals_an_uninterrupted_run(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["drop"], "trades", TRADES)
    await killed_after(data["drop"], tmp_path, "2024-01", TRADES)
    await killed_after(data["drop"], tmp_path, "2024-02", TRADES, resume=True)

    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta == whole.meta
    assert len(abandoned_files(tmp_path)) == 2
    assert not resumed.plan.calls_between(utc(1), utc(3))


async def test_a_crash_mid_replay_loses_nothing_and_bills_nothing(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """The shorter log it leaves cannot reach the boundary, so the second Continue must go back to
    the one the first was replaying, and neither replay was new spend."""
    whole = await uninterrupted(data["drop"], "trades", TRADES)
    await killed_after(data["drop"], tmp_path, "2024-02", TRADES)
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["drop"],
            tmp_path,
            QUARTER,
            TRADES,
            cost_usd=COST,
            resume=True,
            before_cycle=explode_on(utc(1, 15)),
        )

    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert resumed.meta.profit == whole.meta.profit
    assert not resumed.plan.calls_between(utc(1), utc(3))


async def test_replayed_spend_is_carried_and_not_billed_again(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})
    await killed_after(data["flat"], tmp_path, "2024-02", {})

    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    # The recorded cost is in the new log, nothing went to the provider for it, and nothing is
    # counted as abandoned, so the total is the unbroken run's to the cent.
    assert not resumed.plan.calls_between(utc(1), utc(3))
    assert resumed.plan.calls_between(utc(3), utc(4))
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert resumed.meta.profit.cost_usd == whole.meta.profit.cost_usd
    assert cycles_of(resumed.database) == cycles_of(whole.database)


def _rewrite_event(database: Path, cycle_at: datetime, event: EventType, change: Rewrite) -> None:
    """Alter one recorded event of the cycle at `cycle_at`, as a file on disk: the recorded run then
    differs from what a replay will do. `change` returning `None` deletes it."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT seq, ts, payload_json FROM events WHERE type = ? ORDER BY seq", (event.value,)
        ).fetchall()
        seq, payload = next(
            (seq, json.loads(body))
            for seq, ts, body in rows
            if datetime.fromisoformat(ts) == cycle_at
        )
        changed = change(payload)
        if changed is None:
            connection.execute("DELETE FROM events WHERE seq = ?", (seq,))
        else:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE seq = ?", (json.dumps(changed), seq)
            )
        connection.commit()
    finally:
        connection.close()


def _with_another_price(payload: dict[str, Any]) -> dict[str, Any]:
    payload["snapshot"]["instruments"][0]["quote"]["last"] = "1234.5"
    return payload


@pytest.mark.parametrize(
    ("event", "change", "named"),
    [
        (EventType.SNAPSHOT_FROZEN, _with_another_price, "quote differs"),
        (EventType.DECISION_MADE, lambda _: None, "asked the panel at"),
    ],
    ids=["market", "cycle"],
)
async def test_a_replay_that_differs_refuses_naming_the_cycle_and_keeps_the_old_log(
    data: dict[str, Path], tmp_path: Path, event: EventType, change: Rewrite, named: str
) -> None:
    await killed_after(data["flat"], tmp_path, "2024-02", {})
    gc.collect()
    _rewrite_event(run_dir(tmp_path) / pd.PERIOD_DB, utc(1, 20), event, change)

    with pytest.raises(ConfigError, match=rf"2024-01-20.*{named}"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    gc.collect()
    assert stored_meta(tmp_path).ending is pd.RunEnding.INTERRUPTED
    assert "2024-01-20" in stored_meta(tmp_path).ending_reason
    kept = abandoned_files(tmp_path)
    assert len(kept) == 1
    assert max(cycle_stamps(kept[0])) == utc(2, 29)
    # Refused at the first difference: no later cycle was run, and no live call was made.
    assert max(cycle_stamps(run_dir(tmp_path) / pd.PERIOD_DB)) <= utc(1, 22)


async def test_a_month_that_replays_differently_refuses(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """The month row is the last check: what the final poll did is in no later snapshot."""
    await killed_after(data["flat"], tmp_path, "2024-02", {})
    written = run_dir(tmp_path) / pd.CHECKPOINT_DIR / "2024-02.json"
    meta = pd.PeriodMeta.model_validate_json(written.read_text(encoding="utf-8"))
    row = meta.months[0].model_copy(update={"end_equity": meta.months[0].end_equity + 1})
    written.write_text(
        meta.model_copy(update={"months": (row, meta.months[1])}).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="end of 2024-01"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)


class _Feed:
    """Stands in for `ArchiveNewsFeed`; only `digest` and the protocol are read."""

    digest = "same-digest"

    def __init__(self, title: str) -> None:
        self._title = title

    async def snapshot_news(
        self, instruments: tuple[Any, ...], as_of: datetime, limit: int
    ) -> tuple[tuple[NewsItemView, ...], NewsCoverage]:
        item = NewsItemView(
            source="archive",
            title=self._title,
            summary="s",
            published_at=as_of,
            observed_at=as_of,
            relevance=Decimal(1),
        )
        return (item,), NewsCoverage(sources_ok=("archive",))


async def test_a_news_fed_run_continues_on_the_same_feed_and_refuses_a_different_one(
    data: dict[str, Path], tmp_path: Path
) -> None:
    feed = cast(ArchiveNewsFeed, _Feed("the same headline"))
    other = cast(ArchiveNewsFeed, _Feed("a different headline"))
    whole = await execute(data["flat"], tmp_path / "a", QUARTER, {}, cost_usd=COST, news_feed=feed)
    b, c = tmp_path / "b", tmp_path / "c"
    await killed_after(data["flat"], b, "2024-01", {}, news_feed=feed)
    await killed_after(data["flat"], c, "2024-01", {}, news_feed=feed)

    # In its own workspace: pytest's log capture keeps the refused wiring's frames, and with them
    # its open connections, and Windows will not then move that file. A real Continue is a new
    # process.
    with pytest.raises(ConfigError, match="news differs"):
        await execute(data["flat"], c, QUARTER, {}, cost_usd=COST, resume=True, news_feed=other)
    resumed = await execute(
        data["flat"], b, QUARTER, {}, cost_usd=COST, resume=True, news_feed=feed
    )

    assert resumed.meta.months == whole.meta.months
    assert not resumed.plan.calls_between(utc(1), utc(2))


def test_a_period_basket_carries_no_challenger(data: dict[str, Path]) -> None:
    """Its answers are not replayed, so a Continue would pay for every one of them again."""
    candidate = candidate_over(data["flat"], 2)
    panel = candidate.basket.panel.model_copy(update={"panel_id": "challenger"})
    shadowed = replace(
        candidate, basket=candidate.basket.model_copy(update={"shadow_panel": panel})
    )
    dataset = ReplayDataset.load(data["flat"], ManualClock(utc(1)))

    assert pd.period_basket(shadowed, dataset, 86_400).shadow_panel is None


def test_a_period_basket_is_validated_after_it_is_narrowed(data: dict[str, Path]) -> None:
    """A quarantine naming ETH matches nothing once the basket holds BTC alone, and the basket's
    own rule refuses that (ADR 0022). Without re-validation it surfaces mid-run instead, at the
    first `configs.put` of a pause, as a confusing error that interrupts the run."""
    candidate = candidate_over(data["flat"], 2)
    eth = next(one.key for one in candidate.basket.instruments if one.symbol == ETH)
    policy = candidate.basket.risk_policy.model_copy(update={"quarantined_instruments": (eth,)})
    quarantined = replace(
        candidate, basket=candidate.basket.model_copy(update={"risk_policy": policy})
    )
    dataset = ReplayDataset.load(data["flat"], ManualClock(utc(1)))

    with pytest.raises(ConfigError, match=re.escape(eth)):
        pd.period_basket(quarantined, dataset, 86_400)


def _record(at: datetime) -> CycleRecord:
    snapshot = ContextSnapshot(snapshot_id="old", basket_id="period", as_of=at, instruments=())
    decision = Decision(instrument_key="binance:BTC/USDT", action=Action.WAIT)
    return CycleRecord(
        cycle_id="c", basket_id="period", as_of=at, snapshot=snapshot, decisions=(decision,)
    )


async def test_a_recorded_answer_nobody_asked_for_refuses(data: dict[str, Path]) -> None:
    candidate = candidate_over(data["flat"], 2)
    replayer = Replayer(
        (_record(utc(1, 1)), _record(utc(1, 2))), boundary=utc(2), clock=ManualClock(utc(1))
    )

    answer = await replayer(_record(utc(1, 1)).snapshot.model_copy(), candidate.basket)
    assert answer.decisions == _record(utc(1, 1)).decisions
    with pytest.raises(ConfigError, match=r"2024-01-02.*did not"):
        replayer.require_consumed()


async def test_the_generated_id_is_all_a_replay_ignores(data: dict[str, Path]) -> None:
    candidate = candidate_over(data["flat"], 2)
    replayer = Replayer((_record(utc(1, 1)),), boundary=utc(2), clock=ManualClock(utc(1)))
    same = _record(utc(1, 1)).snapshot.model_copy(update={"snapshot_id": "new"})
    await replayer(same, candidate.basket)
    replayer.require_consumed()

    replayer = Replayer((_record(utc(1, 1)),), boundary=utc(2), clock=ManualClock(utc(1)))
    other = same.model_copy(update={"note": "something else"})
    with pytest.raises(ConfigError, match="note differs"):
        await replayer(other, candidate.basket)
    # The worker absorbs what a cycle raises, so the refusal has to be readable afterwards.
    with pytest.raises(ConfigError, match="note differs"):
        replayer.raise_if_diverged()


async def test_a_crash_mid_month_redoes_only_that_month(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["drop"], "trades", TRADES)

    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["drop"],
            tmp_path,
            QUARTER,
            TRADES,
            cost_usd=COST,
            before_cycle=explode_on(utc(2, 25)),
        )
    resumed = await execute(data["drop"], tmp_path, QUARTER, TRADES, cost_usd=COST, resume=True)

    assert [m.label for m in resumed.meta.months] == ["2024-01", "2024-02", "2024-03"]
    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    # No cycle from the abandoned work: its log is in another file, and every instant ran once.
    stamps = cycle_stamps(resumed.database)
    assert len(stamps) == len(set(stamps))
    assert_same_outcomes(resumed, whole)
    # January was not asked again: the only calls are from the checkpoint onward.
    assert not resumed.plan.calls_between(utc(1), utc(2))

    kept = abandoned_files(tmp_path)
    assert len(kept) == 1
    # The crash came before the cycle on the 25th, and the file kept everything up to it.
    assert max(cycle_stamps(kept[0])) == utc(2, 24)


async def test_spent_budget_still_counts_after_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})

    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, before_cycle=explode_on(utc(2, 11))
        )
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            QUARTER,
            {},
            cost_usd=COST,
            resume=True,
            before_cycle=explode_on(utc(2, 6)),
        )
    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    # Ten February cycles, then five: each crash's work is counted, and the second does not forget
    # the first even though both replayed the same January.
    abandoned = 15 * per_cycle(whole)
    assert resumed.meta.abandoned_cost_usd == abandoned
    assert len(abandoned_files(tmp_path)) == 2
    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.profit.net == whole.meta.profit.net - abandoned
    assert resumed.meta.profit.cost_usd == whole.meta.profit.cost_usd + abandoned


async def test_abandoned_spend_reaches_the_budget_check(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """Without the abandoned ten cycles, February's end would be inside this budget."""
    whole = await uninterrupted(data["flat"], "quiet", {})
    january, february, _ = whole.meta.months
    budget = january.cost_usd + february.cost_usd + 5 * per_cycle(whole)
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            QUARTER,
            {},
            cost_usd=COST,
            budget=budget,
            before_cycle=explode_on(utc(2, 11)),
        )

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=budget, resume=True
    )

    assert resumed.meta.ending is pd.RunEnding.INTERRUPTED
    assert "budget" in resumed.meta.ending_reason
    assert [m.label for m in resumed.meta.months] == ["2024-01", "2024-02"]


async def test_continuing_past_a_budget_stop_needs_a_larger_budget(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})
    small, larger = COST, Decimal(5)
    first = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=small)
    assert first.meta.ending is pd.RunEnding.INTERRUPTED
    gc.collect()  # the finished wiring's connections, or its WAL files would come and go
    before = sorted(path.name for path in run_dir(tmp_path).iterdir())

    with pytest.raises(pd.BudgetReached, match="larger budget"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=small, resume=True)
    assert sorted(path.name for path in run_dir(tmp_path).iterdir()) == before

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=larger, resume=True
    )

    assert resumed.meta.budget == larger
    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    # Nothing ran past that checkpoint, so nothing was abandoned to count.
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert resumed.meta.months == whole.meta.months


async def test_continue_with_no_checkpoint_restarts_from_the_start_keeping_the_old_db(
    data: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="injected crash"):
        await execute(
            data["flat"],
            tmp_path,
            "2024-02",
            {},
            cost_usd=COST,
            before_cycle=explode_on(utc(2, 11)),
        )
    assert not (run_dir(tmp_path) / pd.CHECKPOINT_DIR).exists()

    resumed = await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST, resume=True)

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert min(cycle_stamps(resumed.database)) == utc(2)
    assert resumed.meta.abandoned_cost_usd == 10 * resumed.meta.months[0].cost_usd / 29
    assert max(cycle_stamps(abandoned_files(tmp_path)[0])) == utc(2, 10)


async def test_a_database_killed_before_any_meta_can_continue(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Nothing but the file: a process killed in its first month, or before the schema existed."""
    meta = (await execute(data["flat"], tmp_path / "a", "2024-02", {})).meta
    directory = pd.period_dir(meta.run_id, workspace=tmp_path / "b")
    directory.mkdir(parents=True)
    (directory / pd.PERIOD_DB).touch()

    resumed = await execute(data["flat"], tmp_path / "b", "2024-02", {}, resume=True)

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert len(abandoned_files(tmp_path / "b")) == 1


def test_a_dead_process_leaves_its_sidecars_with_the_abandoned_file(tmp_path: Path) -> None:
    """A stale `-wal` beside the restored database would be read as belonging to it, and a second
    Continue in the same second must not overwrite the first's record."""
    database = tmp_path / pd.PERIOD_DB
    clock = ManualClock(utc(1))
    for generation in (b"first", b"second"):
        database.write_bytes(generation)
        (tmp_path / f"{pd.PERIOD_DB}-wal").write_bytes(generation)
        pd._abandon(database, clock)

    assert not database.exists()
    assert not (tmp_path / f"{pd.PERIOD_DB}-wal").exists()
    first = tmp_path / f"{pd.PERIOD_DB}.abandoned-20240101T000000Z"
    second = tmp_path / f"{first.name}-2"
    assert first.read_bytes() == b"first"
    assert Path(f"{first}-wal").read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert Path(f"{second}-wal").read_bytes() == b"second"


async def test_a_meta_with_no_database_is_a_continue_that_died_between_its_moves(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "quiet", {})
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    gc.collect()  # the dead wiring's connections: see `execute`
    pd._abandon(run_dir(tmp_path) / pd.PERIOD_DB, ManualClock(utc(1)))

    with pytest.raises(ConfigError, match="Continue"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST)
    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    assert resumed.meta.months == whole.meta.months
    assert not resumed.plan.calls_between(utc(1), utc(2))


async def test_a_checkpoint_whose_log_is_gone_refuses(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Its spend cannot be known and its answers cannot be replayed, so nothing is guessed."""
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    gc.collect()
    (run_dir(tmp_path) / pd.PERIOD_DB).unlink()

    with pytest.raises(ConfigError, match=r"2024-01.*no database"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)


async def test_killed_after_the_last_month_finishes_without_redoing_anything(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """No month is left to spend on, so a spent budget refuses nothing here (§5.5)."""
    whole = await uninterrupted(data["flat"], "quiet", {})
    await killed_after(data["flat"], tmp_path, "2024-03", {})

    resumed = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=COST, resume=True
    )

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert resumed.meta.months == whole.meta.months
    assert not resumed.plan.calls


async def test_a_rule_ended_run_refuses_continue(data: dict[str, Path], tmp_path: Path) -> None:
    async def trip(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.trip("drawdown", "injected")

    first = await execute(data["flat"], tmp_path, QUARTER, {}, before_cycle=trip)
    assert first.meta.ending is pd.RunEnding.RULE_ENDED

    with pytest.raises(ConfigError, match=r"rule.*kill switch.*2024-01-25"):
        await execute(data["flat"], tmp_path, QUARTER, {}, resume=True)

    assert not abandoned_files(tmp_path)


async def test_a_rule_verdict_survives_a_shutdown_that_raises(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def trip(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 25):
            await worker._watchdog.trip("drawdown", "injected")

    real = Application.shutdown

    async def shutdown(self: Application) -> None:
        await real(self)
        raise RuntimeError("injected shutdown failure")

    monkeypatch.setattr(Application, "shutdown", shutdown)

    with pytest.raises(RuntimeError, match="injected shutdown failure"):
        await execute(data["flat"], tmp_path, QUARTER, {}, before_cycle=trip)

    assert stored_meta(tmp_path).ending is pd.RunEnding.RULE_ENDED


async def test_continuing_a_completed_run_reuses_it(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await execute(data["flat"], tmp_path, "2024-02", {})

    async def refuse(**_: object) -> None:
        raise AssertionError("a finished run was wired again")

    monkeypatch.setattr(pd, "build_sim", refuse)

    assert (await execute(data["flat"], tmp_path, "2024-02", {}, resume=True)).meta == first.meta


def with_changed_seat(candidate: Candidate) -> Candidate:
    panel = candidate.basket.panel
    seat = panel.seats[0].model_copy(update={"instruction": "Trust nothing."})
    changed: Basket = candidate.basket.model_copy(
        update={"panel": panel.model_copy(update={"seats": (seat, *panel.seats[1:])})}
    )
    return replace(candidate, basket=changed)


async def test_continue_after_a_seat_change_refuses_and_leaves_the_old_run_alone(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Identity differs, so there is nothing to continue at the new one: Continue never starts a
    run, and the old directory is untouched."""
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    old = run_dir(tmp_path)
    gc.collect()  # the dead wiring's connections, which touch the file as they close
    before = {path.name: path.stat().st_mtime_ns for path in old.iterdir()}

    with pytest.raises(ConfigError, match="no unfinished run to continue"):
        await execute(
            data["flat"],
            tmp_path,
            QUARTER,
            {},
            resume=True,
            candidate=with_changed_seat(candidate_over(data["flat"], 2)),
        )

    assert _runs_in(tmp_path) == [old], "a refused Continue started a run"
    assert {path.name: path.stat().st_mtime_ns for path in old.iterdir()} == before


async def test_an_interrupted_run_refuses_without_continue_and_names_it(
    data: dict[str, Path], tmp_path: Path
) -> None:
    await killed_after(data["flat"], tmp_path, "2024-01", {})
    before = sorted(path.name for path in run_dir(tmp_path).iterdir())

    with pytest.raises(ConfigError, match="Continue"):
        await execute(data["flat"], tmp_path, QUARTER, {})

    assert sorted(path.name for path in run_dir(tmp_path).iterdir()) == before


async def test_continue_across_a_boundary_after_a_paused_month_still_lifts(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "pause", TWO_LOSSES)
    assert whole.meta.months[0].paused_at == PAUSED_ON
    assert whole.meta.lifts

    await killed_after(data["flat"], tmp_path, "2024-01", TWO_LOSSES)
    resumed = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)
    assert resumed.meta.lifts == whole.meta.lifts


async def test_a_budget_stop_on_a_paused_month_still_lifts_on_continue(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    whole = await uninterrupted(data["flat"], "pause", TWO_LOSSES)

    stopped = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, budget=COST)
    assert stopped.meta.ending is pd.RunEnding.INTERRUPTED
    assert stopped.meta.months[-1].paused_at == PAUSED_ON
    assert stopped.meta.lifts == ()

    resumed = await execute(data["flat"], tmp_path, QUARTER, TWO_LOSSES, cost_usd=COST, resume=True)

    assert_same_outcomes(resumed, whole)


@pytest.mark.parametrize("kill_after", ["2024-01", "2024-02"])
async def test_a_restored_run_lifts_and_restores_the_limit_as_the_unbroken_one_did(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted, kill_after: str
) -> None:
    """Before the lift, and after the win handed the limit back: the stored version is what runs,
    so a Continue that republished the candidate's basket would disagree on both."""
    whole = await uninterrupted(data["rise"], "win", WIN_AFTER_A_LIFT)
    pinned = limits_pinned(whole.database)
    assert (pinned[utc(2, 5)], pinned[utc(2, 15)]) == (4, 2)

    await killed_after(data["rise"], tmp_path, kill_after, WIN_AFTER_A_LIFT)
    resumed = await execute(
        data["rise"], tmp_path, QUARTER, WIN_AFTER_A_LIFT, cost_usd=COST, resume=True
    )

    assert_same_outcomes(resumed, whole)


async def test_a_halted_month_is_redone_from_the_month_before(
    data: dict[str, Path], tmp_path: Path, uninterrupted: Uninterrupted
) -> None:
    """A month cut short is no last good point: it writes no checkpoint and its row is dropped."""
    whole = await uninterrupted(data["flat"], "quiet", {})

    async def halt(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(2, 10):
            await worker._watchdog.halt_basket(worker.basket_id, "injected: not a loss streak")

    stopped = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, before_cycle=halt)
    assert stopped.meta.ending is pd.RunEnding.INTERRUPTED
    assert [m.label for m in stopped.meta.months] == ["2024-01", "2024-02"]
    written = run_dir(tmp_path) / pd.CHECKPOINT_DIR
    assert [path.name for path in written.iterdir()] == ["2024-01.json"]

    resumed = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)

    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.ending is pd.RunEnding.COMPLETED


async def test_a_stop_inside_a_replayed_month_refuses(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """A checkpointed month ran whole, so a stop in its replay is no verdict: it is a replay that
    went somewhere the recorded run did not, and the run stays continuable."""
    await killed_after(data["flat"], tmp_path, "2024-02", {})

    async def halt(worker: BasketWorker) -> None:
        if worker._clock.now() == utc(1, 20):
            await worker._watchdog.halt_basket(worker.basket_id, "injected: a replayed halt")

    with pytest.raises(ConfigError, match=r"2024-01.*which the recorded run finished"):
        await execute(
            data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True, before_cycle=halt
        )

    gc.collect()
    meta = stored_meta(tmp_path)
    assert meta.ending is pd.RunEnding.INTERRUPTED, "an error, never a rule's verdict"
    assert "which the recorded run finished" in meta.ending_reason


def fail_after_paying(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """The cycle at `moment` pays for its panel, then raises before it decides anything.

    `BasketWorker` absorbs it, as it absorbs any defect a cycle raises, so the cycle has seat
    responses (money spent) and no `CYCLE_COMPLETED`, which is where `Evidence` reads cost from.
    """
    real = EventFactory.decision_made

    def raising(self: EventFactory, decision: Decision) -> Event:
        if self._clock.now() == moment:
            raise RuntimeError("injected: a defect after the panel was paid")
        return real(self, decision)

    monkeypatch.setattr(EventFactory, "decision_made", raising)
    # Logged without its traceback: pytest's log capture keeps a record's frames, and with them the
    # wiring's open connections, and Windows will not then move the database a Continue abandons.
    monkeypatch.setattr(
        supervisor_module.logger,
        "exception",
        lambda message, **kwargs: supervisor_module.logger.error(message, **kwargs),
    )


async def test_a_failed_cycles_spend_reaches_the_budget_check(
    data: dict[str, Path],
    tmp_path: Path,
    uninterrupted: Uninterrupted,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted from the responses, as Continue counts it, so the two never disagree (§5.5)."""
    whole = await uninterrupted(data["flat"], "quiet", {})
    january = whole.meta.months[0].cost_usd
    # Inside the budget only if the failed cycle's spend is forgotten.
    budget = january - per_cycle(whole) / 2
    fail_after_paying(monkeypatch, utc(1, 15))

    ran = await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=budget)

    assert ran.meta.budget_stopped
    assert [m.label for m in ran.meta.months] == ["2024-01"]
    assert "end of 2024-01" in ran.meta.ending_reason


async def test_a_failed_cycle_in_a_checkpointed_month_names_itself(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its answers were never recorded as a decision, so there is nothing to replay into it. The
    refusal must say that, rather than that the market differed, which it did not."""
    fail_after_paying(monkeypatch, utc(1, 15))
    await killed_after(data["flat"], tmp_path, "2024-02", {})
    monkeypatch.undo()

    with pytest.raises(ConfigError, match=r"FAILED cycle at 2024-01-15.*cannot be replayed"):
        await execute(data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, resume=True)


async def test_a_checkpoint_that_cannot_be_written_interrupts_the_run(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_: int) -> None:
        raise OSError("injected: no room")

    monkeypatch.setattr(os, "fsync", refuse)

    with pytest.raises(OSError, match="no room"):
        await execute(data["flat"], tmp_path, QUARTER, {})

    assert stored_meta(tmp_path).ending is pd.RunEnding.INTERRUPTED
    assert not (run_dir(tmp_path) / pd.CHECKPOINT_DIR / "2024-01.json").exists()


async def test_each_month_end_writes_its_meta_and_no_database_copy(
    data: dict[str, Path], tmp_path: Path
) -> None:
    ran = await execute(data["flat"], tmp_path, QUARTER, {})
    root = run_dir(tmp_path) / pd.CHECKPOINT_DIR

    assert sorted(path.name for path in root.iterdir()) == [
        "2024-01.json",
        "2024-02.json",
        "2024-03.json",
    ]
    meta = pd.PeriodMeta.model_validate_json((root / "2024-01.json").read_text(encoding="utf-8"))
    assert [m.label for m in meta.months] == ["2024-01"]
    assert ran.meta.ending is pd.RunEnding.COMPLETED


def test_a_later_abandoned_file_is_read_as_later_whatever_its_digits(tmp_path: Path) -> None:
    """Ten Continues in one second: compared as text, `-10` would sort before `-9`."""
    stem = f"{pd.PERIOD_DB}.abandoned-20240101T000000Z"
    for name in (stem, f"{stem}-9", f"{stem}-10", f"{stem}-10-wal"):
        (tmp_path / name).touch()

    assert [path.name for path in pd._abandoned_newest_first(tmp_path)] == [
        f"{stem}-10",
        f"{stem}-9",
        stem,
    ]


def _vetoes_of(risk_events: dict[str, int]) -> set[str]:
    evidence = cast(ProfitEvidence, SimpleNamespace(risk_events=risk_events))
    return {f"{row.rule}/{row.action_taken}" for row in pd._vetoes(evidence)}


def test_only_what_refuses_nothing_leaves_the_veto_table() -> None:
    assert _vetoes_of(
        {
            "valuation_basis/recorded": 1,
            "tracked_group/group_retired": 1,
            "tracked_group/group_closed": 1,
            "tracked_group/group_lapsed": 1,
            "max_consecutive_losses/veto": 2,
        }
    ) == {"max_consecutive_losses/veto"}


def test_a_new_action_under_either_rule_stays_on_the_veto_table() -> None:
    """The exclusion is by what the row did, so a refusal could not hide under the rule's name."""
    rows = {"tracked_group/vetoed": 1, "valuation_basis/vetoed": 1}

    assert _vetoes_of(rows) == set(rows)


def test_the_excluded_actions_are_the_ones_the_bot_emits() -> None:
    """Read from the source: the table above is a claim about these two emitters on every run."""
    monitor = Path(monitor_module.__file__).read_text(encoding="utf-8")
    startup = Path(startup_module.__file__).read_text(encoding="utf-8")

    assert (
        set(re.findall(r'_release\(\s*group,\s*action="(\w+)"', monitor))
        == (pd._NOT_REFUSALS[monitor_module.TRACKED_GROUP_RULE])
    )
    around = startup[startup.index('rule="valuation_basis"') :]
    action = re.search(r'action="(\w+)"', around)
    assert action is not None
    assert action[1] in pd._NOT_REFUSALS["valuation_basis"]


class _Messy(pause.Plan):
    """What a real panel does that a clean script never does: a primary that is down so the
    fallback answers, a day on which every model is down, a reply that needs one repair, and one
    that does not parse after it. Answers vary with the date and the model, never with call order,
    so a resumed process asks the same question the unbroken one did."""

    def __init__(self, votes: pause.Votes, *, cost_usd: Decimal = ZERO) -> None:
        super().__init__(votes, cost_usd=cost_usd)
        self._broken_once: set[tuple[date, str]] = set()
        #: Each time the same seat was asked again after a reply that did not parse: the repair.
        self.repairs: list[tuple[date, str]] = []

    def _text_for(self, request: CompletionRequest) -> str:
        at = pause.as_of(request).date()
        model = request.model
        phase = at.day % 7
        if phase == 1 and "technical" in model and "backup" not in model:
            return FAIL
        if phase == 2:
            return FAIL
        if phase == 3 and "news" in model:
            if (at, model) not in self._broken_once:
                self._broken_once.add((at, model))
                return "not json at all"
            self.repairs.append((at, model))
        if phase == 4 and "skeptic" in model:
            return "still not json"
        vote = json.loads(DEFAULT_RESPONSE)
        buys = (phase == 5 and at.month != 3 and at.day % 14 == 5) or (
            phase == 6 and "technical" in model
        )
        action = "BUY" if buys else "WAIT"
        if phase == 0 and "skeptic" not in model:
            action = "SELL"
        vote["action"] = action
        vote["size_hint"] = "half" if action in ("BUY", "SELL") else "none"
        return json.dumps(vote)


def _messy_candidate(data_dir: Path, ceiling: Decimal) -> Candidate:
    """Three seats, debating, with a backup behind one of them and a cost ceiling per cycle."""
    base = candidate_over(data_dir, 50)
    seats = tuple(
        seat.model_copy(
            update={
                "fallbacks": (ProviderBinding(provider_id="stub", model=f"{seat.model}-backup"),)
                if "technical" in seat.model
                else ()
            }
        )
        for seat in base.basket.panel.seats
    )
    panel = base.basket.panel.model_copy(
        update={
            "seats": seats,
            "max_cost_usd_per_cycle": ceiling,
            "max_rounds": 3,
            "protocol": "blind_then_debate",
        }
    )
    return replace(base, basket=base.basket.model_copy(update={"panel": panel}))


def _logged(database: Path, kind: EventType, key: str) -> list[tuple[datetime, dict[str, Any]]]:
    def read(store: EventStore) -> list[tuple[datetime, dict[str, Any]]]:
        return [(event.ts, event.payload[key]) for event in store.read_types(kind)]

    return read_log(database, read)


@pytest.mark.parametrize(
    "ceiling", [Decimal(10), Decimal("0.05")], ids=["roomy-ceiling", "ceiling-truncates"]
)
async def test_a_continue_replays_a_panel_that_disagrees_repairs_and_falls_back(
    data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: Decimal
) -> None:
    """The replay is exercised with a real panel's shapes, not a script that always agrees.

    Debate rounds, a fallback answering, an abstention, a repaired reply and a cost ceiling that
    cuts a debate short are all in the old log, and the new one must hold the same answers, cost
    and fingerprints byte for byte up to the boundary, then live calls after it."""
    monkeypatch.setattr(pause, "Plan", _Messy)
    source = data["drop"]
    candidate = _messy_candidate(source, ceiling)
    options: dict[str, Any] = {"candidate": candidate, "budget": Decimal(100)}

    whole = await execute(source, tmp_path / "a", QUARTER, {}, cost_usd=COST, **options)
    assert whole.meta.ending is pd.RunEnding.COMPLETED, whole.meta.ending_reason
    interrupted = tmp_path / "b"
    await killed_after(source, interrupted, "2024-02", {}, **options)
    resumed = await execute(source, interrupted, QUARTER, {}, cost_usd=COST, resume=True, **options)
    assert resumed.meta.ending is pd.RunEnding.COMPLETED, resumed.meta.ending_reason

    answers = _logged(whole.database, EventType.SEAT_RESPONDED, "response")
    # Day 6 of each week splits the panel (one BUY, two WAIT), so it debates and it costs: the
    # all-abstain days debate for nothing and could not show a ceiling at work.
    split_rounds = {a["round_index"] for at, a in answers if at.day % 7 == 6}
    assert split_rounds >= {0}, "no split day was deliberated"
    if ceiling > COST * 10:
        assert 1 in split_rounds, "a split panel never debated"
    else:
        assert split_rounds == {0}, "the ceiling did not cut the debate short"
    assert isinstance(whole.plan, _Messy) and whole.plan.repairs, "no reply was repaired"
    assert any(
        a["vote"] is not None and "news" in a["model"] and at.day % 7 == 3 for at, a in answers
    ), "the repaired reply did not become a vote"
    assert any(answer["vote"] is None for _, answer in answers), "no seat abstained"
    assert any("backup" in answer["model"] for _, answer in answers), "no fallback answered"

    boundary = utc(3)
    abandoned = abandoned_files(interrupted)[0]
    for kind, key in (
        (EventType.SEAT_RESPONDED, "response"),
        (EventType.DECISION_MADE, "decision"),
    ):
        before = [one for one in _logged(abandoned, kind, key) if one[0] < boundary]
        after = [one for one in _logged(resumed.database, kind, key) if one[0] < boundary]
        assert after == before, f"{kind} differs across the replayed months"
    assert resumed.meta.months == whole.meta.months
    assert resumed.meta.profit == whole.meta.profit
    assert resumed.meta.abandoned_cost_usd == ZERO
    assert fills_in(resumed.database) == fills_in(whole.database)
    assert not resumed.plan.calls_between(utc(1), utc(3)), "a replayed month asked the model"


def _runs_in(workspace: Path) -> list[Path]:
    return list(workspace.glob("period-*"))


async def test_continue_with_nothing_to_continue_refuses_and_writes_nothing(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Continue never starts a run: a typo in the cadence or the period would mint a new identity
    and spend with the projection check waived (§5.7)."""
    with pytest.raises(ConfigError, match="no unfinished run to continue"):
        await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST, resume=True)

    assert not _runs_in(tmp_path), "a refused Continue left a run behind"


async def test_continue_on_a_finished_run_still_reuses_it(
    data: dict[str, Path], tmp_path: Path
) -> None:
    first = await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST)

    again = await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST, resume=True)

    assert again.meta == first.meta
    assert not again.plan.calls, "a reused run asked the model"


async def test_finished_run_reads_a_result_without_touching_it(
    data: dict[str, Path], tmp_path: Path
) -> None:
    ran = await execute(data["flat"], tmp_path, "2024-02", {}, cost_usd=COST)

    def look(start_equity: Decimal) -> pd.PeriodMeta | None:
        return pd.finished_run(
            data_dir=data["flat"],
            candidate=candidate_over(data["flat"], 2),
            period=pd.parse_period("2024-02"),
            cadence_seconds=pause.CADENCE,
            start_equity=start_equity,
            workspace=tmp_path,
        )

    assert look(pause.START_EQUITY) == ran.meta
    assert look(Decimal(5)) is None


async def test_a_continue_keeps_the_seat_set_file_the_run_began_from(
    data: dict[str, Path], tmp_path: Path
) -> None:
    """Fix round 1, item 3: `matrix_source` is first-build provenance, as `matrix_digest` is."""
    first = await execute(
        data["flat"], tmp_path, QUARTER, {}, cost_usd=COST, budget=COST, matrix_source="first.toml"
    )
    assert first.meta.ending is pd.RunEnding.INTERRUPTED
    assert first.meta.matrix_source == "first.toml"
    gc.collect()

    resumed = await execute(
        data["flat"],
        tmp_path,
        QUARTER,
        {},
        cost_usd=COST,
        budget=Decimal(5),
        resume=True,
        matrix_source="moved.toml",
    )

    assert resumed.meta.ending is pd.RunEnding.COMPLETED
    assert resumed.meta.matrix_source == "first.toml"
