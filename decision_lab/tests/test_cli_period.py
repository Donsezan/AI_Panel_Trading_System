"""`evaluate period` through `cli.main`, as an operator (or the page's child process) runs it.

Offline on the stub matrix, so the file is free and deterministic. The refusals are asserted on
exit codes because the page renders exactly those codes (period spec §5.6). The cases that need a
particular `PeriodMeta` replace `period.run`: what they pin is the command's reading of an ending
and its report, and a real run to reach each ending would test the driver a second time.

Exit codes this command uses, all existing ones:

- 2  unreadable period, unknown candidate, missing `--budget` (argparse)
- 3  dataset or evidence problem, including a period the history does not cover, a run that never
     finished and was not continued, and a Continue that refuses (rule-ended, diverged, no log)
- 4  candidate, matrix or news archive that will not load, or an unreachable provider
- 5  budget: a projection over it, or a run it interrupted
- 6  the §10.6 gate is unsatisfied
- 7  the workspace is held
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import cli, gate, jobs, registry
from decision_lab import dataset as ds
from decision_lab import period as pd
from decision_lab import render as rd
from decision_lab.longrun import Profit
from tradebot.app import dataset_basket, select_panel
from tradebot.core.clock import SystemClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def lab(
    calibrated_corpus: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The dataset `calibrated_corpus` built, with `period`'s workspace redirected as well."""
    monkeypatch.setattr(pd, "workspace_root", lambda: tmp_path / "workspace")
    return calibrated_corpus[1]


def _matrix(data: Path) -> cd.Matrix:
    dataset = ReplayDataset.load(data, SystemClock())
    reference = dataset_basket(dataset, select_panel("sim"), basket_id="reference")
    return cd.load_matrix(cd.STUB_MATRIX, reference=reference)


def _first_candidate(data: Path) -> str:
    return _matrix(data).candidates[0].candidate_id


def argv(data: Path, out: Path, *extra: str, period: str = "2024-01") -> list[str]:
    return [
        "evaluate",
        "period",
        "--data",
        str(data),
        "--configs",
        str(cd.STUB_MATRIX),
        "--candidate",
        _first_candidate(data),
        "--start-equity",
        "1000",
        "--every",
        "24h",
        "--period",
        period,
        "--budget",
        "5",
        "--out",
        str(out),
        *extra,
    ]


def open_gate(data: Path, tmp_path: Path, *, cost_per_cycle: str = "0") -> None:
    """A satisfied gate record for the stub matrix, written directly: no model call is needed to
    prove what the command does with a record."""
    from decision_lab.gate import CandidateEvidence, GateRecord, ScenarioVerdict

    matrix = _matrix(data)
    digest = ds.require_verified(data).dataset_digest
    dayset = cday.require_pinned(data).dayset_digest
    key = gate.gate_key(
        dataset_digest=digest, matrix_digest=matrix.matrix_digest, dayset_digest=dayset
    )
    evidence = tuple(
        CandidateEvidence(candidate_id=one.candidate_id, cost_per_cycle=Decimal(cost_per_cycle))
        for one in matrix.candidates
    )
    gate.write(
        GateRecord(
            gate_key=key,
            dataset_digest=digest,
            matrix_digest=matrix.matrix_digest,
            dayset_digest=dayset,
            normal=ScenarioVerdict(scenario="normal", ran_at=NOW, candidates=evidence),
            shock=ScenarioVerdict(scenario="shock", ran_at=NOW, candidates=evidence),
        ),
        workspace=tmp_path / "workspace",
    )


def meta_of(**changes: Any) -> pd.PeriodMeta:
    period = pd.parse_period("2024-01")
    base: dict[str, Any] = {
        "run_id": "r" * 8,
        "built_at": NOW,
        "period": period,
        "dataset_directory": "d",
        "dataset_digest": "x",
        "candidate_id": "c",
        "basket_digest": "b",
        "panel_digest": "p",
        "archive_digest": "",
        "cadence_seconds": 86_400,
        "start_equity": Decimal(1000),
        "budget": Decimal(5),
        "first_cycle_at": period.start,
        "ending": pd.RunEnding.COMPLETED,
    }
    return pd.PeriodMeta(**{**base, **changes})


def month(label: str = "2024-01", **changes: Any) -> pd.MonthResult:
    base: dict[str, Any] = {
        "label": label,
        "start_equity": Decimal(1000),
        "end_equity": Decimal(1010),
        "planned_cycles": 31,
        "ran_cycles": 31,
        "cost_usd": Decimal("0.5"),
    }
    return pd.MonthResult(**{**base, **changes})


class FakeRun:
    """Stands in for `period.run`, recording what the command passed it."""

    def __init__(self, outcome: pd.PeriodMeta | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> pd.PeriodMeta:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def fake(monkeypatch: pytest.MonkeyPatch, outcome: pd.PeriodMeta | BaseException) -> FakeRun:
    run = FakeRun(outcome)
    monkeypatch.setattr(pd, "run", run)
    return run


def rows(tmp_path: Path) -> list[registry.RunRow]:
    return [
        row
        for row in registry.read_all(workspace=tmp_path / "workspace")
        if row.scenario == "evaluate-period"
    ]


# --- refusals


def test_an_unreadable_period_exits_2(lab: Path, tmp_path: Path) -> None:
    assert (
        cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", period="Q2-2024")) == cli.EXIT_MISUSE
    )


def test_a_period_the_history_does_not_cover_exits_3(lab: Path, tmp_path: Path) -> None:
    out = tmp_path / "p.md"

    assert cli.main(argv(lab, out, "--skip-gate", period="2030")) == cli.EXIT_DATASET
    assert not out.exists(), "a refusal writes no page"


def test_an_unknown_candidate_exits_2_and_names_the_ones_that_exist(
    lab: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    args = cli.parse_args(argv(lab, tmp_path / "p.md", "--skip-gate"))
    args.candidate = "no-such-candidate"

    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        code = asyncio.run(cli.evaluate_period(args))

    assert code == cli.EXIT_MISUSE
    named = [record for record in caplog.records if hasattr(record, "available")]
    assert named and _first_candidate(lab) in named[0].available


def test_a_missing_budget_exits_2(lab: Path, tmp_path: Path) -> None:
    without = argv(lab, tmp_path / "p.md", "--skip-gate")
    at = without.index("--budget")
    del without[at : at + 2]

    with pytest.raises(SystemExit) as raised:
        cli.main(without)

    assert raised.value.code == cli.EXIT_MISUSE


def test_an_unsatisfied_gate_exits_6_before_any_provider_check(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reachability(_matrix: object) -> None:
        raise AssertionError("the provider was checked before the gate")

    monkeypatch.setattr(cd, "require_reachable", reachability)
    run = fake(monkeypatch, meta_of())

    assert cli.main(argv(lab, tmp_path / "p.md")) == cli.EXIT_GATE
    assert not run.calls


def test_a_held_workspace_exits_7(lab: Path, tmp_path: Path) -> None:
    holder = jobs.WorkspaceLock()
    holder.acquire(["held"], clock=SystemClock())
    try:
        assert cli.main(argv(lab, tmp_path / "p.md", "--skip-gate")) == cli.EXIT_BUSY
    finally:
        holder.release()


def test_a_projection_over_budget_exits_5_with_nothing_spent(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 31 cycles at 1 each against a budget of 5.
    open_gate(lab, tmp_path, cost_per_cycle="1")
    run = fake(monkeypatch, meta_of())

    assert cli.main(argv(lab, tmp_path / "p.md")) == cli.EXIT_BUDGET
    assert not run.calls, "nothing may be spent on a run its own projection refuses"
    assert not (tmp_path / "p.md").exists()


def test_a_projection_within_budget_runs(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_gate(lab, tmp_path, cost_per_cycle="0.1")
    run = fake(monkeypatch, meta_of(months=(month(),), projected_usd=Decimal("3.1")))

    assert cli.main(argv(lab, tmp_path / "p.md")) == cli.EXIT_OK

    assert len(run.calls) == 1
    assert run.calls[0]["projected_usd"] == Decimal("3.1")
    assert "projected 3.1" in (tmp_path / "p.md").read_text(encoding="utf-8")


def test_a_bad_news_archive_exits_4(lab: Path, tmp_path: Path) -> None:
    assert (
        cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", "--news-source", "nowhere"))
        == cli.EXIT_CANDIDATE
    )


def test_a_repeated_news_source_exits_4(lab: Path, tmp_path: Path) -> None:
    code = cli.main(
        argv(lab, tmp_path / "p.md", "--skip-gate", "--news-source", "a", "--news-source", "a")
    )

    assert code == cli.EXIT_CANDIDATE


def test_the_loaded_feed_reaches_run_and_a_continue(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Feed:
        digest = "feed-digest"

    monkeypatch.setattr(cli, "load_feed", lambda *_: Feed())
    run = fake(monkeypatch, meta_of(archive_digest="feed-digest"))

    assert cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", "--news-source", "s")) == 0
    assert (
        cli.main(argv(lab, tmp_path / "q.md", "--skip-gate", "--news-source", "s", "--continue"))
        == 0
    )

    assert [type(call["news_feed"]) for call in run.calls] == [Feed, Feed]
    assert [call["resume"] for call in run.calls] == [False, True]
    assert "feed-digest" in (tmp_path / "p.md").read_text(encoding="utf-8")


# --- endings and exit codes


def test_a_budget_interrupted_run_exits_5_and_still_files_its_report(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(
        monkeypatch,
        meta_of(
            months=(month(),),
            ending=pd.RunEnding.INTERRUPTED,
            ending_reason="budget of 5 passed at the end of 2024-01: 6 spent",
            budget_stopped=True,
        ),
    )
    out = tmp_path / "p.md"

    assert cli.main(argv(lab, out, "--skip-gate")) == cli.EXIT_BUDGET

    text = out.read_text(encoding="utf-8")
    assert "Interrupted: budget of 5 passed" in text
    assert "Last month end kept: 2024-01" in text
    assert "needs a larger `--budget`" in text
    assert rows(tmp_path)[0].ending == "INTERRUPTED"


def test_a_continue_with_its_budget_already_spent_exits_5(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(monkeypatch, pd.BudgetReached("budget of 5 is spent"))

    assert cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", "--continue")) == cli.EXIT_BUDGET


def test_an_interruption_for_another_reason_exits_non_zero_naming_it(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(
        monkeypatch,
        meta_of(ending=pd.RunEnding.INTERRUPTED, ending_reason="a FAILED cycle halted the basket"),
    )
    out = tmp_path / "p.md"

    code = cli.main(argv(lab, out, "--skip-gate"))

    assert code == cli.EXIT_DATASET
    assert "a FAILED cycle halted the basket" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "reason",
    [
        "holds a run that never finished",
        "this run was ended by a rule",
        "the replay differs from the recorded run",
        "its log is gone",
    ],
)
def test_a_refusal_to_run_or_to_continue_exits_3(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """An interrupted run without `--continue`, and every refusal of `--continue` itself."""
    run = fake(monkeypatch, ConfigError(reason))

    assert cli.main(argv(lab, tmp_path / "p.md", "--skip-gate")) == cli.EXIT_DATASET
    assert cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", "--continue")) == cli.EXIT_DATASET
    assert [call["resume"] for call in run.calls] == [False, True]


def test_a_rule_ended_run_is_a_result_and_exits_0(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = pd.parse_period("2024-Q1")
    fake(
        monkeypatch,
        meta_of(
            # A quarter's meta behind a command for one month: the dataset holds two, and the
            # command refuses what it does not cover. The page reads the run's own period.
            period=period,
            first_cycle_at=period.start,
            months=(month(),),
            ending=pd.RunEnding.RULE_ENDED,
            ending_reason="out of money on 2024-01-20",
        ),
    )
    out = tmp_path / "p.md"

    assert cli.main(argv(lab, out, "--skip-gate")) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "Model failed: out of money on 2024-01-20" in text
    assert "| 2024-02 | — | — | — | not run | — |" in text
    assert "| 2024-03 | — | — | — | not run | — |" in text
    assert "| not run | 2024-02, 2024-03 |" in text


# --- the page and the row


def test_the_report_and_the_row_carry_the_lifts_the_ending_and_what_was_not_run(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = pd.parse_period("2024-Q1")
    lift = pd.PauseLift(at=datetime(2024, 2, 1, tzinfo=UTC), streak=2, new_limit=4)
    fake(
        monkeypatch,
        meta_of(
            period=period,
            first_cycle_at=period.start,
            months=(month("2024-01", paused_at=datetime(2024, 1, 22, tzinfo=UTC)),),
            lifts=(lift,),
            abandoned_cost_usd=Decimal("0.25"),
            ending=pd.RunEnding.RULE_ENDED,
            ending_reason="kill switch tripped on 2024-02-03",
            profit=Profit(
                equity=Decimal(1010),
                start_equity=Decimal(1000),
                total=Decimal(10),
                realized=Decimal(4),
                unrealized=Decimal(6),
                cost_usd=Decimal("0.75"),
                net=Decimal("9.25"),
            ),
        ),
    )
    out = tmp_path / "p.md"

    # The dataset holds two months, so the command asks for one; the fake run answers for a quarter.
    assert cli.main(argv(lab, out, "--skip-gate")) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert rd.PAUSE_LIFTED in text and "| 2 | 4 |" in text
    assert "Model failed: kill switch tripped on 2024-02-03" in text
    assert "2024-02" in text and "2024-03" in text and "not run" in text
    assert "0.25" in text, "what Continue abandoned is on the page"
    assert "9.25" in text
    row = rows(tmp_path)[0]
    assert (row.pauses_lifted, row.ending, row.not_run) == (
        1,
        "RULE_ENDED",
        ("2024-02", "2024-03"),
    )
    assert row.ending_reason == "kill switch tripped on 2024-02-03"
    assert row.abandoned_cost_usd == Decimal("0.25")
    assert row.net_profit == Decimal("9.25")


def test_a_frozen_run_shows_unvaluable_and_no_figure(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(
        monkeypatch,
        meta_of(
            months=(month(unvaluable=True, start_equity=None, end_equity=Decimal(0)),),
            profit=Profit(unvaluable=True, freeze_reason="no mark for BTC/USDT"),
        ),
    )
    out = tmp_path / "p.md"

    assert cli.main(argv(lab, out, "--skip-gate")) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "UNVALUABLE" in text and "no mark for BTC/USDT" in text
    assert "| net profit |" not in text, "a frozen portfolio has no figure, not even a zero"
    assert "| spent | 0.5 |" in text, "the model was paid for whatever the valuation"
    row = rows(tmp_path)[0]
    assert row.unvaluable and row.cost_usd == Decimal("0.5")


def test_the_stamps_reach_the_report_and_the_row(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub matrix, no archive, no gate, no calibrated cost: all four, said twice."""
    # The stamps are read off the run, not the command line: a reused run keeps how it was made.
    fake(
        monkeypatch,
        meta_of(months=(month(),), plumbing_check=True, gate_skipped=True, news_blind=True),
    )
    out = tmp_path / "p.md"

    assert cli.main(argv(lab, out, "--skip-gate")) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert rd.PLUMBING_CHECK in text and rd.NEWS_BLIND in text and rd.GATE_SKIPPED in text
    assert "no projection was available" in text
    row = rows(tmp_path)[0]
    assert not row.evaluation and row.news_blind and row.gate_skipped and row.no_projection


def test_skipping_the_gate_runs_end_to_end_and_reuses_the_run_without_a_second_row(
    lab: Path, tmp_path: Path
) -> None:
    """The real driver, once; then the same command again at the same identity."""
    first = tmp_path / "a.md"

    assert cli.main(argv(lab, first, "--skip-gate")) == cli.EXIT_OK

    text = first.read_text(encoding="utf-8")
    assert rd.GATE_SKIPPED in text and rd.PLUMBING_CHECK in text and rd.NEWS_BLIND in text
    assert "no projection was available" in text
    assert "COMPLETED" in text.upper()
    directories = sorted((tmp_path / "workspace").glob("period-*"))
    assert len(directories) == 1
    stored = (directories[0] / pd.PERIOD_META).read_text(encoding="utf-8")

    assert cli.main(argv(lab, tmp_path / "b.md", "--skip-gate")) == cli.EXIT_OK

    assert (directories[0] / pd.PERIOD_META).read_text(encoding="utf-8") == stored
    assert len(rows(tmp_path)) == 1, "a reused run is one registry row, not two"


# --- reuse, continue, and provenance


def test_a_finished_run_is_reused_past_a_shut_gate_a_missing_key_and_a_small_budget(
    lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.1: the gate, the provider and the projection are consulted before spend, never before
    reuse. One real run, then three invocations each of which would refuse a new one."""
    assert cli.main(argv(lab, tmp_path / "a.md", "--skip-gate")) == cli.EXIT_OK
    before = {path: path.read_bytes() for path in (tmp_path / "workspace").glob("period-*/*.json")}

    def no_spend(**_: Any) -> None:
        raise AssertionError("a finished run was run again")

    monkeypatch.setattr(pd, "run", no_spend)

    # No gate record exists, and --skip-gate is absent: a new run would exit 6.
    assert cli.main(argv(lab, tmp_path / "b.md")) == cli.EXIT_OK
    # A provider that cannot be reached: a new run would exit 4.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            cd, "require_reachable", lambda _m: (_ for _ in ()).throw(ConfigError("no key"))
        )
        assert cli.main(argv(lab, tmp_path / "c.md", "--skip-gate")) == cli.EXIT_OK
    # A projection of 31 against a budget of 0.01: a new run would exit 5.
    open_gate(lab, tmp_path, cost_per_cycle="1")
    over = argv(lab, tmp_path / "d.md")
    over[over.index("--budget") + 1] = "0.01"
    assert cli.main(over) == cli.EXIT_OK

    after = {path: path.read_bytes() for path in (tmp_path / "workspace").glob("period-*/*.json")}
    assert after == before, "reuse rewrote the run"


def test_the_row_of_a_reused_run_keeps_its_provenance_and_stays_one_row(
    lab: Path, tmp_path: Path
) -> None:
    """A run made with --skip-gate is described that way however it is found again (§5.7)."""
    first = tmp_path / "a.md"
    assert cli.main(argv(lab, first, "--skip-gate")) == cli.EXIT_OK
    open_gate(lab, tmp_path)

    second = tmp_path / "b.md"
    assert cli.main(argv(lab, second)) == cli.EXIT_OK

    [row] = rows(tmp_path)
    assert row.gate_skipped, "the invocation that found the run overwrote how it was made"
    assert row.no_projection and row.news_blind
    assert rd.GATE_SKIPPED in second.read_text(encoding="utf-8")


def test_continue_with_nothing_to_continue_exits_3_and_writes_no_run(
    lab: Path, tmp_path: Path
) -> None:
    """A typo in --every, --start-equity or --period must not start a run with the projection
    check waived."""
    code = cli.main(argv(lab, tmp_path / "p.md", "--skip-gate", "--continue"))

    assert code == cli.EXIT_DATASET
    assert not list((tmp_path / "workspace").glob("period-*"))
    assert not (tmp_path / "p.md").exists()
    assert not rows(tmp_path)
