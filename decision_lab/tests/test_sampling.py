"""The sample is stratified, seeded, and never crowds one shock direction out (spec §7.3).

Two properties matter more than the sizes. A re-run with the same seed draws the same entries, or
two sweeps are not comparable and the whole design collapses. And the rare strata — named windows
and the pinned days — are taken whole, because they are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from decision_lab import sampling
from decision_lab.calibration_days import Pool
from decision_lab.tests.factories import corpus_with_entries

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class FakeRegimes:
    """Labels by instant, so a test states its own distribution."""

    def __init__(self, labels: dict[datetime, Pool], windows: dict[datetime, str]) -> None:
        self._labels = labels
        self._windows = windows

    def label_at(self, instrument_key: str, as_of: datetime) -> Pool:
        return self._labels[as_of]

    def window_at(self, as_of: datetime) -> object | None:
        name = self._windows.get(as_of)
        return type("W", (), {"name": name})() if name else None


def fixture(labels: list[Pool], windows: dict[int, str] | None = None):  # type: ignore[no-untyped-def]
    corpus = corpus_with_entries(count=len(labels), as_of=EPOCH)
    by_time = {EPOCH + timedelta(hours=i): label for i, label in enumerate(labels)}
    named = {EPOCH + timedelta(hours=i): name for i, name in (windows or {}).items()}
    return corpus, FakeRegimes(by_time, named)


def test_the_same_seed_draws_the_same_entries() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 50)

    first = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=7, sizes={"NORMAL": 10}
    )
    second = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=7, sizes={"NORMAL": 10}
    )

    assert first.cycle_ids == second.cycle_ids
    assert len(first.cycle_ids) == 10


def test_a_different_seed_draws_a_different_sample() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 50)

    first = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=1, sizes={"NORMAL": 10}
    )
    second = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", seed=2, sizes={"NORMAL": 10}
    )

    assert first.cycle_ids != second.cycle_ids


def test_neither_shock_direction_can_crowd_the_other_out() -> None:
    corpus, regimes = fixture([Pool.SHOCK_UP] * 40 + [Pool.SHOCK_DOWN] * 4 + [Pool.NORMAL] * 10)

    sample = sampling.stratified(
        corpus,
        regimes=regimes,
        reference_instrument="k",
        sizes={"NORMAL": 5, "SHOCK_UP": 6, "SHOCK_DOWN": 6},
    )

    assert sample.selected["SHOCK_UP"] == 6
    assert sample.selected["SHOCK_DOWN"] == 4, "a short stratum contributes all it has"
    assert sample.available["SHOCK_DOWN"] == 4


def test_a_named_window_is_taken_whole_however_small_the_quota() -> None:
    corpus, regimes = fixture(
        [Pool.NORMAL] * 20, windows=dict.fromkeys(range(12, 20), "spot ETF approval")
    )

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", sizes={"NORMAL": 1}
    )

    assert sample.selected["spot ETF approval"] == 8


def test_a_pinned_day_is_taken_whole() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 30)

    sample = sampling.stratified(
        corpus,
        regimes=regimes,
        reference_instrument="k",
        pinned=(EPOCH.date(),),
        sizes={"NORMAL": 1},
    )

    assert sample.selected["pinned"] == 24, "every entry on a pinned day, not a quota of them"


def test_full_disables_sampling_entirely() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 30)

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", full=True, sizes={"NORMAL": 2}
    )

    assert len(sample.cycle_ids) == 30
    assert sample.full is True


def test_entries_come_back_in_corpus_order() -> None:
    corpus, regimes = fixture([Pool.NORMAL] * 40)

    sample = sampling.stratified(
        corpus, regimes=regimes, reference_instrument="k", sizes={"NORMAL": 12}
    )

    order = [entry.cycle_id for entry in corpus.entries if entry.cycle_id in set(sample.cycle_ids)]
    assert list(sample.cycle_ids) == order, "a sweep walks history forwards, whatever it drew in"
