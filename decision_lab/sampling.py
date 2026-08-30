"""Which corpus entries a sweep pays for (spec §7.3).

Evaluating every candidate on every entry is affordable only at coarse cadence, so the default is
a stratified, seeded draw. Two properties carry the design:

* **Seeded.** A re-run draws the same entries, so two sweeps are comparable. The seed is recorded
  on the report and on the §11 row; an unseeded sample would make every comparison a comparison of
  two different subsets of history.
* **Stratified by the reference instrument.** A cycle covers every instrument in the basket, and
  two of them can sit in two different regimes at one instant — so a cycle has no single regime of
  its own. The stratum is the regime of the instrument §4.5 already draws the day set from and
  every report already names, rather than a label a cycle cannot have one of.

Named windows and the pinned days are taken whole, never sampled: they are rare, and they are what
the shock questions are asked over.

Failure semantics: a stratum with fewer entries than its quota contributes all of them and says so
on `available`, rather than refusing — a corpus short of down-shocks is a fact about the window,
not a broken run. Nothing here performs I/O.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

from pydantic import Field

from decision_lab.corpus import Corpus, CorpusEntry
from decision_lab.params import DEFAULT_SEED, SAMPLE_SIZES
from decision_lab.regimes import RegimeIndex
from tradebot.core.schema import DomainModel

#: The stratum taken whole because §4.5 pinned it.
PINNED: Final = "pinned"


class Sample(DomainModel):
    """The entries one sweep will pay for, and how they were chosen."""

    cycle_ids: tuple[str, ...] = ()
    seed: int = DEFAULT_SEED
    full: bool = False
    #: Stratum -> entries drawn. Printed on the report, so a thin stratum is visible.
    selected: dict[str, int] = Field(default_factory=dict)
    #: Stratum -> entries the corpus held. Beside `selected`, this is what says "all there was".
    available: dict[str, int] = Field(default_factory=dict)


def stratified(
    corpus: Corpus,
    *,
    regimes: RegimeIndex,
    reference_instrument: str,
    pinned: Sequence[date] = (),
    seed: int = DEFAULT_SEED,
    full: bool = False,
    sizes: Mapping[str, int] = SAMPLE_SIZES,
) -> Sample:
    """Draw the sample. `full` disables it and returns every entry (§7.3)."""
    if full:
        return Sample(
            cycle_ids=tuple(entry.cycle_id for entry in corpus.entries),
            seed=seed,
            full=True,
            selected={"all": len(corpus.entries)},
            available={"all": len(corpus.entries)},
        )

    strata: dict[str, list[CorpusEntry]] = {}
    pinned_days = set(pinned)
    for entry in corpus.entries:
        name = _stratum(entry, regimes, reference_instrument, pinned_days)
        strata.setdefault(name, []).append(entry)

    # S311 is about cryptographic use: this draws which recorded cycles a sweep pays for, reaches
    # no venue, and guards nothing — a CSPRNG here would say otherwise, and would not be seedable.
    rng = random.Random(seed)  # noqa: S311
    chosen: set[str] = set()
    selected: dict[str, int] = {}
    for name in sorted(strata):
        pool = strata[name]
        quota = len(pool) if name == PINNED or name not in sizes else min(sizes[name], len(pool))
        drawn = pool if quota >= len(pool) else rng.sample(pool, quota)
        chosen.update(entry.cycle_id for entry in drawn)
        selected[name] = len(drawn)

    return Sample(
        cycle_ids=tuple(e.cycle_id for e in corpus.entries if e.cycle_id in chosen),
        seed=seed,
        full=False,
        selected=selected,
        available={name: len(pool) for name, pool in sorted(strata.items())},
    )


def _stratum(
    entry: CorpusEntry, regimes: RegimeIndex, instrument_key: str, pinned: set[date]
) -> str:
    """A named window outranks a pinned day outranks the automatic label.

    Windows first for the reason §8.2 gives — a named window is an operator's assertion about a
    period and overrides the labeller — and pinned days next, so a calibration day is never
    thinned by a `NORMAL` quota.
    """
    window = regimes.window_at(entry.as_of)
    if window is not None:
        return str(window.name)
    if entry.day in pinned:
        return PINNED
    return str(regimes.label_at(instrument_key, entry.as_of).value)
