"""Every decision gets a verdict, and an unscorable one is counted with its reason (spec §9.4).

A run that quietly dropped them would report accuracy over a subset it chose after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from decision_lab import dataset as ds
from decision_lab import scoring as sc
from decision_lab.calibration_days import Pool
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, SizeHint
from tradebot.core.market import Quote
from tradebot.core.snapshot import IndicatorReading, InstrumentContext, PositionView
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def context(*, atr: str = "5", price: str = "100", holding: bool = False) -> InstrumentContext:
    inst = f.instrument()
    return InstrumentContext(
        instrument=inst,
        quote=Quote(
            instrument_key=inst.key,
            bid=Decimal(price),
            ask=Decimal(price),
            last=Decimal(price),
            observed_at=f.EPOCH,
        ),
        indicators=(IndicatorReading(name="ATR", timeframe="1h", value=Decimal(atr), text=""),),
        position=PositionView(qty=Decimal(1), unrealized_pnl_pct=Decimal(0), held_cycles=1)
        if holding
        else None,
    )


def decision(action: Action, *, conviction: str = "0.8") -> Decision:
    return Decision(
        instrument_key=f.instrument().key,
        action=action,
        conviction=Decimal(conviction),
        size_hint=SizeHint.HALF if action.is_tradable else SizeHint.NONE,
        votes_for=2,
        votes_total=3,
    )


def forward(move: str) -> sc.Forward:
    p0 = Decimal("100")
    return sc.Forward(
        p0=p0, p_h=p0 + Decimal(move), move=Decimal(move), mfe=Decimal(move), mae=Decimal(0)
    )


def score(
    ctx: InstrumentContext, dec: Decision, fwd: sc.Forward, regime: Pool = Pool.NORMAL
) -> sc.ScoredDecision:
    return sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=ctx,
        decision=dec,
        forward=fwd,
        band=Decimal("5"),
        regime=regime,
        window_name="",
    )


def test_a_buy_before_a_rally_is_correct() -> None:
    result = score(context(), decision(Action.BUY), forward("12"))
    assert result.verdict is sc.Verdict.CORRECT
    assert result.truth is sc.Truth.BUY


def test_a_buy_before_nothing_is_wrong() -> None:
    assert score(context(), decision(Action.BUY), forward("1")).verdict is sc.Verdict.WRONG


def test_waiting_through_a_crash_while_flat_is_correct() -> None:
    result = score(context(), decision(Action.WAIT), forward("-40"), regime=Pool.SHOCK_DOWN)
    assert result.verdict is sc.Verdict.CORRECT
    assert result.regime is Pool.SHOCK_DOWN


def test_holding_through_a_crash_is_wrong() -> None:
    result = score(context(holding=True), decision(Action.HOLD), forward("-40"))
    assert result.verdict is sc.Verdict.WRONG
    assert result.truth is sc.Truth.EXIT


def test_a_missing_atr_is_unscored_by_name() -> None:
    inst = f.instrument()
    bare = context().model_copy(update={"indicators": ()})
    result = sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=bare,
        decision=decision(Action.BUY),
        forward=forward("12"),
        band=None,
        regime=Pool.NORMAL,
        window_name="",
    )
    assert result.verdict is sc.Verdict.UNSCORED_NO_ATR
    assert inst.key


def test_a_missing_forward_window_is_unscored_by_name() -> None:
    result = sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=context(),
        decision=decision(Action.BUY),
        forward=None,
        band=Decimal("5"),
        regime=Pool.NORMAL,
        window_name="",
    )
    assert result.verdict is sc.Verdict.UNSCORED_HORIZON


def test_the_action_rate_flag_is_the_decisions_own() -> None:
    """§9.5's precision-on-action needs to know which decisions asked for an order."""
    assert score(context(), decision(Action.BUY), forward("12")).asked_for_an_order
    assert not score(context(), decision(Action.WAIT), forward("1")).asked_for_an_order


async def test_the_price_index_finds_the_bar_h_ahead(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(data, clock)
    params = sc.ScoringParams(timeframe="1h", horizon_bars=6)

    index = await sc.build_price_index(data, audit, params)
    found = index.forward(inst.key, f.EPOCH + timedelta(hours=10), horizon=6)

    assert found is not None
    assert found.move == Decimal(6)


async def test_a_decision_near_the_end_has_no_forward_window(tmp_path: Path) -> None:
    """§5.6: never silently dropped, which would flatter a run by discarding its most recent
    behaviour."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    index = await sc.build_price_index(
        data, await ds.audit(data, clock), sc.ScoringParams(timeframe="1h")
    )

    assert index.forward(inst.key, f.EPOCH + timedelta(hours=46), horizon=6) is None


async def test_a_decision_whose_window_crosses_a_hole_is_flagged(tmp_path: Path) -> None:
    """§4.4: scoring across a hole is a wrong answer wearing a right one's clothes."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(data, clock)
    holed = audit.model_copy(
        update={
            "series": {
                "binance:BTC/USDT|1h": audit.series["binance:BTC/USDT|1h"].model_copy(
                    update={
                        "known_holes": (
                            ds.KnownHole(
                                **{
                                    "from": f.EPOCH + timedelta(hours=12),
                                    "to": f.EPOCH + timedelta(hours=14),
                                    "reason": "test",
                                }
                            ),
                        )
                    }
                )
            }
        }
    )
    params = sc.ScoringParams(timeframe="1h", horizon_bars=6)
    index = await sc.build_price_index(data, holed, params)

    assert index.crosses_hole(inst.key, f.EPOCH + timedelta(hours=10), params)
    assert not index.crosses_hole(inst.key, f.EPOCH + timedelta(hours=40), params)
