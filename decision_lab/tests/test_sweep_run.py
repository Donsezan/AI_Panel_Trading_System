"""The run loop: cache, budget, and the rule that a substitute model stops the measurement.

§7.7 is the file's centre. A seat answering on its backup produced a *different panel's* answer in
a row labelled with the configured seat's name, so it can never be scored — and under the default
policy it stops the sweep, because the alternative is an operator reading a ranking built on a
panel that was never configured.

The engine is injected, so nothing here reaches a network: `engine_for` hands the loop a scripted
stand-in, exactly as `BacktestHarness` takes its dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import candidates as cd
from decision_lab import sweep
from decision_lab.corpus import Corpus
from decision_lab.sampling import Sample
from decision_lab.tests.factories import corpus_with_entries
from decision_lab.tests.test_candidates import reference, write
from decision_lab.tests.test_sweep_storage import response
from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision, Deliberation, PanelOutcome
from tradebot.core.enums import Action

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)

MATRIX = """
[[candidates]]
id = "baseline"
providers = ["stub"]

  [[candidates.seats]]
  seat_id = "technical"
  role = "t"
  provider_id = "stub"
  model = "varied-technical"
"""


class ScriptedEngine:
    """A `DecisionEngine` stand-in. Counts calls and answers from a list."""

    def __init__(self, outcomes: list[PanelOutcome | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def deliberate(self, snapshot: object, basket: object) -> PanelOutcome:
        self.calls += 1
        answer = self._outcomes.pop(0) if self._outcomes else _plain()
        if isinstance(answer, Exception):
            raise answer
        return answer


def _plain(provider_id: str = "stub", model: str = "varied-technical") -> PanelOutcome:
    seat = response(provider_id, model, seat_id="technical")
    return PanelOutcome(
        decisions=(Decision(instrument_key="binance:BTC/USDT", action=Action.BUY),),
        deliberations=(
            Deliberation(
                instrument_keys=("binance:BTC/USDT",),
                protocol_id="single_round",
                rounds=1,
                responses=(seat,),
            ),
        ),
    )


def matrix_of(tmp_path: Path, text: str = MATRIX) -> cd.Matrix:
    return cd.load_matrix(write(tmp_path / "m", text), reference=reference())


def sample_of(corpus: Corpus) -> Sample:
    return Sample(cycle_ids=tuple(e.cycle_id for e in corpus.entries))


async def _run(
    corpus: Corpus,
    matrix: cd.Matrix,
    tmp_path: Path,
    engine: sweep.DeliberatingEngine,
    budget: str = "1",
) -> sweep.SweepResult:
    return await sweep.run(
        corpus,
        matrix,
        sample=sample_of(corpus),
        clock=ManualClock(AS_OF),
        budget_usd=Decimal(budget),
        workspace=tmp_path,
        engine_for=lambda _: engine,
    )


def _rows(corpus: Corpus, matrix: cd.Matrix, tmp_path: Path) -> dict[str, sweep.SweepRow]:
    return sweep.read_rows(
        sweep.rows_path(corpus.meta.corpus_id, matrix.matrix_digest, "baseline", workspace=tmp_path)
    )


async def test_a_cached_answer_costs_no_provider_call(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=3, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    first = await _run(corpus, matrix, tmp_path, engine)
    second = await _run(corpus, matrix, tmp_path, engine)

    assert first.evaluated == 3
    assert engine.calls == 3, "the second run answered entirely from what was already bought"
    assert second.evaluated == 0
    assert second.status is sweep.SweepStatus.OK


async def test_a_substitute_binding_halts_the_sweep_by_default(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=4, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news"), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)

    assert result.status is sweep.SweepStatus.HALTED_FALLBACK
    assert "varied-news" in result.halted_on
    assert engine.calls == 2, "it stopped rather than buying the rest"


async def test_completed_work_survives_a_halt(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=4, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news")])

    await _run(corpus, matrix, tmp_path, engine)
    kept = _rows(corpus, matrix, tmp_path)

    assert len(kept) == 2, "§7.5/§7.6: a halt never discards what it already bought"
    assert kept["c1"].contaminated is True


async def test_exclude_carries_on_and_the_contaminated_cycle_never_scores(tmp_path: Path) -> None:
    corpus = corpus_with_entries(count=4, as_of=AS_OF)
    matrix = matrix_of(tmp_path, '[sweep]\non_fallback = "exclude"\n' + MATRIX)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news"), _plain(), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)
    rows = _rows(corpus, matrix, tmp_path)

    assert result.status is sweep.SweepStatus.OK
    assert result.contaminated == 1
    assert len(rows) == 4
    assert len(sweep.records_from_rows(corpus, rows)) == 3, "the contaminated cycle is not scored"


async def test_the_budget_halts_without_overspending_or_discarding(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=10, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    result = await _run(corpus, matrix, tmp_path, engine, budget="0.025")

    assert result.status is sweep.SweepStatus.HALTED_BUDGET
    assert result.spent_usd <= Decimal("0.03")
    assert result.evaluated >= 2
    assert len(_rows(corpus, matrix, tmp_path)) == result.evaluated


async def test_a_candidate_that_raises_is_recorded_and_counted(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=3, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), RuntimeError("provider exploded"), _plain()])

    result = await _run(corpus, matrix, tmp_path, engine)
    rows = _rows(corpus, matrix, tmp_path)

    assert result.failed == 1
    assert result.status is sweep.SweepStatus.OK, "one bad deliberation is not a failed sweep"
    assert any("provider exploded" in row.error for row in rows.values())
    assert len(sweep.records_from_rows(corpus, rows)) == 2


async def test_only_the_sampled_entries_are_paid_for(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=10, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([])

    result = await sweep.run(
        corpus,
        matrix,
        sample=Sample(cycle_ids=("c0", "c4")),
        clock=ManualClock(AS_OF),
        budget_usd=Decimal(1),
        workspace=tmp_path,
        engine_for=lambda _: engine,
    )

    assert result.evaluated == 2
    assert engine.calls == 2


async def test_the_meta_round_trips_and_records_the_run_kind(tmp_path: Path) -> None:
    corpus, matrix = corpus_with_entries(count=2, as_of=AS_OF), matrix_of(tmp_path)

    result = await _run(corpus, matrix, tmp_path, ScriptedEngine([]))
    sweep.write_meta(result, workspace=tmp_path)

    reread = sweep.read_meta(corpus.meta.corpus_id, matrix.matrix_digest, workspace=tmp_path)
    assert reread is not None
    assert reread.matrix_digest == result.matrix_digest
    assert reread.evaluation is False, "a stub matrix is a plumbing check (§7.2)"


async def test_a_contaminated_row_is_never_written_to_the_shared_cache(tmp_path: Path) -> None:
    """§7.4: the cache stores answers, and a failure is not one — a contaminated row cached would
    be served to every future matrix sharing this (snapshot, panel) key."""
    corpus, matrix = corpus_with_entries(count=2, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news")])

    await _run(corpus, matrix, tmp_path, engine)

    candidate = matrix.candidates[0]
    key = sweep.cache_key(corpus.entries[1].snapshot.digest, candidate.panel_digest)
    assert sweep.cache_read(corpus.meta.corpus_id, key, workspace=tmp_path) is None


async def test_an_errored_row_is_never_cached_and_is_re_attempted(tmp_path: Path) -> None:
    """§7.4/§7.6: an errored row is likewise not an answer, and resuming past it would leave a
    permanent hole rather than giving a since-fixed provider a chance to answer for real."""
    corpus, matrix = corpus_with_entries(count=2, as_of=AS_OF), matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), RuntimeError("boom")])

    first = await _run(corpus, matrix, tmp_path, engine)
    candidate = matrix.candidates[0]
    key = sweep.cache_key(corpus.entries[1].snapshot.digest, candidate.panel_digest)

    assert first.failed == 1
    assert sweep.cache_read(corpus.meta.corpus_id, key, workspace=tmp_path) is None

    second = await _run(corpus, matrix, tmp_path, engine)
    kept = _rows(corpus, matrix, tmp_path)

    assert engine.calls == 3, "the errored entry was retried, not skipped as already-done"
    assert second.failed == 0
    assert kept["c1"].error == "", "the retry succeeded and its answer is what is kept"


async def test_a_second_run_after_a_fallback_halt_re_evaluates_the_dirty_entry(
    tmp_path: Path,
) -> None:
    """§7.6/§7.7: a halted entry's contaminated row must not make a resume skip it — that would
    complete the sweep while permanently missing that entry's real answer (§3's even comparison)."""
    corpus = corpus_with_entries(count=3, as_of=AS_OF)
    matrix = matrix_of(tmp_path)
    engine = ScriptedEngine([_plain(), _plain("stub", "varied-news")])

    first = await _run(corpus, matrix, tmp_path, engine)
    assert first.status is sweep.SweepStatus.HALTED_FALLBACK
    assert engine.calls == 2, "halted after the second entry, the third never attempted"

    second = await _run(corpus, matrix, tmp_path, engine)
    kept = _rows(corpus, matrix, tmp_path)

    assert engine.calls == 4, "the dirty entry was retried and the third entry was reached too"
    assert second.status is sweep.SweepStatus.OK, "the panel now answers cleanly and it proceeds"
    assert kept["c1"].contaminated is False, "the retry's clean answer is what is kept"
