"""Rows, the cache, and what folds back into a `CycleRecord` (spec §7.4, §7.6, §7.7).

Three things this file pins, each of which a later change could break silently:

* a cache key names the evidence and the panel and *nothing else*, so it is shared across
  matrices — scoping it by matrix would make adding one candidate re-pay for every other;
* a row survives a round trip, so resume picks up what a killed process had already bought;
* a substitute binding is detected from `fingerprint`, and it contaminates the whole cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import candidates as cd
from decision_lab import sweep
from decision_lab.tests.factories import corpus_with_entries
from decision_lab.tests.test_candidates import STUB_MATRIX, reference, write
from tradebot.core.config import PanelConfig, ProviderBinding, SeatConfig
from tradebot.core.decision import Decision, SeatResponse, SeatVote
from tradebot.core.enums import Action, SizeHint

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)


def panel() -> PanelConfig:
    return PanelConfig(
        panel_id="p",
        seats=(
            SeatConfig(
                seat_id="technical",
                role="t",
                provider_id="openrouter",
                model="primary-model",
                fallbacks=(ProviderBinding(provider_id="gemini", model="backup-model"),),
            ),
        ),
    )


def response(provider_id: str, model: str, *, seat_id: str = "technical") -> SeatResponse:
    return SeatResponse(
        seat_id=seat_id,
        role="t",
        provider_id=provider_id,
        model=model,
        round_index=0,
        instrument_key="binance:BTC/USDT",
        vote=SeatVote(action=Action.BUY, conviction=3, size_hint=SizeHint.HALF, thesis="t"),
        responded_at=AS_OF,
        cost_usd=Decimal("0.01"),
    )


def row(**overrides: object) -> sweep.SweepRow:
    base: dict[str, object] = {
        "cycle_id": "c0",
        "as_of": AS_OF,
        "decisions": (Decision(instrument_key="binance:BTC/USDT", action=Action.BUY),),
        "responses": (response("openrouter", "primary-model"),),
        "cost_usd": Decimal("0.01"),
    }
    base.update(overrides)
    return sweep.SweepRow(**base)


def test_the_cache_key_is_a_deterministic_hash_of_its_two_arguments() -> None:
    """This is all `cache_key` itself can be shown to do: it takes two already-computed digest
    strings and combines them deterministically. It does **not** prove the key "names the
    evidence and the panel only" — that property lives one layer up, in what feeds it
    `panel_digest` (see `test_two_candidates_differing_only_in_decision_mode_get_different_
    cache_keys` below, which is the test that used to carry this name and did not assert it)."""
    key = sweep.cache_key("snap-digest", "panel-digest")

    assert key == sweep.cache_key("snap-digest", "panel-digest")
    assert key != sweep.cache_key("snap-digest", "other-panel")
    assert key != sweep.cache_key("other-snap", "panel-digest")


def test_two_candidates_differing_only_in_decision_mode_get_different_cache_keys(
    tmp_path: Path,
) -> None:
    """CRITICAL finding: `decision_mode` is a `Basket` field (tradebot/core/config.py:604), not a
    `PanelConfig` one, but §7.1 lists it as a matrix axis. A cache key built from `panel_digest`
    alone would collide `X~decision_mode=per_asset` and `X~decision_mode=basket` on the same §7.4
    key, so the second candidate is served the first's rows verbatim and never actually runs."""
    text = STUB_MATRIX + '\n[expand]\ndecision_mode = ["per_asset", "basket"]\n'
    matrix = cd.load_matrix(write(tmp_path, text), reference=reference())
    per_asset, basket = matrix.candidates

    key_a = sweep.cache_key("snap-digest", per_asset.panel_digest)
    key_b = sweep.cache_key("snap-digest", basket.panel_digest)

    assert key_a != key_b


def test_a_cached_row_is_returned_without_a_second_call(tmp_path: Path) -> None:
    sweep.cache_write("corpus-1", "key-1", row(), workspace=tmp_path)

    found = sweep.cache_read("corpus-1", "key-1", workspace=tmp_path)

    assert found is not None
    assert found.cycle_id == "c0"
    assert found.cost_usd == Decimal("0.01")
    assert sweep.cache_read("corpus-1", "absent", workspace=tmp_path) is None


def test_the_cache_is_shared_across_matrices(tmp_path: Path) -> None:
    """§7.4: adding one candidate must not re-pay for every candidate already answered."""
    directory = sweep.cache_dir("corpus-1", workspace=tmp_path)

    assert directory.name == "cache"
    assert "sweep-" not in str(directory)


def test_rows_append_and_read_back_keyed_by_cycle(tmp_path: Path) -> None:
    path = sweep.rows_path("corpus-1", "matrix-a", "baseline", workspace=tmp_path)
    sweep.append_row(path, row())
    sweep.append_row(path, row(cycle_id="c1"))

    found = sweep.read_rows(path)

    assert sorted(found) == ["c0", "c1"]
    assert found["c1"].as_of == AS_OF


def test_reading_rows_from_a_path_that_does_not_exist_is_empty(tmp_path: Path) -> None:
    assert sweep.read_rows(tmp_path / "nothing.jsonl") == {}


def test_two_matrices_do_not_resume_into_each_others_files(tmp_path: Path) -> None:
    left = sweep.rows_path("corpus-1", "matrix-a", "baseline", workspace=tmp_path)
    right = sweep.rows_path("corpus-1", "matrix-b", "baseline", workspace=tmp_path)

    assert left != right
    sweep.append_row(left, row())
    assert sweep.read_rows(right) == {}


def test_a_primary_binding_is_not_a_substitute() -> None:
    assert sweep.substitutes_in((response("openrouter", "primary-model"),), panel()) == ()


def test_a_fallback_binding_is_a_substitute_and_names_both_ends() -> None:
    found = sweep.substitutes_in((response("gemini", "backup-model"),), panel())

    assert found == ("technical: openrouter:primary-model -> gemini:backup-model",)


def test_a_response_from_an_undeclared_seat_is_a_substitute_and_names_the_fault() -> None:
    """§7.7: unmatched to any configured seat, so it cannot be attributed to this panel."""
    found = sweep.substitutes_in(
        (response("openrouter", "primary-model", seat_id="ghost"),), panel()
    )

    assert found == ("ghost: not a seat this panel declares",)


def test_an_undeclared_seat_contaminates_the_row_and_drops_the_record() -> None:
    corpus = corpus_with_entries(count=1, as_of=AS_OF)
    substitutes = sweep.substitutes_in(
        (response("openrouter", "primary-model", seat_id="ghost"),), panel()
    )
    bad_row = row(substitutes=substitutes)

    assert bad_row.contaminated is True
    assert sweep.records_from_rows(corpus, {"c0": bad_row}) == ()


def test_a_row_with_any_substitute_is_contaminated() -> None:
    assert row().contaminated is False
    assert row(substitutes=("technical: a -> b",)).contaminated is True


def test_rows_fold_into_the_cycle_records_slice_b_already_scores() -> None:
    """The load-bearing reuse: slice C adds no scoring code at all."""
    corpus = corpus_with_entries(count=1, as_of=AS_OF)

    records = sweep.records_from_rows(corpus, {"c0": row()})

    assert len(records) == 1
    assert records[0].cycle_id == "c0"
    assert records[0].snapshot == corpus.entries[0].snapshot
    assert records[0].decisions[0].action is Action.BUY
    assert records[0].cost_usd == Decimal("0.01")


def test_a_contaminated_or_failed_row_never_becomes_a_scorable_record() -> None:
    """§7.7: a substitute answered, so no part of that cycle measures the configured panel."""
    corpus = corpus_with_entries(count=2, as_of=AS_OF)

    records = sweep.records_from_rows(
        corpus,
        {"c0": row(substitutes=("technical: a -> b",)), "c1": row(cycle_id="c1", error="boom")},
    )

    assert records == ()
