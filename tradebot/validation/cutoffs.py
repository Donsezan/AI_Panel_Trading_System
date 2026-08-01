"""Model knowledge cutoffs, and what they say about a backtest window (DESIGN §2.6, [L12]).

LLMs memorize historical market data from pretraining. A backtest over a period *inside* a
model's knowledge window is contaminated: published studies show Sharpe decaying by more than
half out-of-window. Point-in-time correctness in our own code cannot rule this out, because the
leak is inside the model's weights rather than in our data path.

So every backtest report states, per model the panel would run, which part of the replayed window
predates that model's cutoff. The honest readings are:

* **contaminated** — the whole window predates the cutoff. The models may be recalling it.
* **partial** — the window straddles the cutoff. Only the later part is out-of-window.
* **clean** — the window is entirely after the cutoff. Still not alpha evidence: a clean window
  removes one known contaminant, it does not turn a plumbing test into a performance result.
* **unknown** — no cutoff on file. Read as contaminated; an unproven claim of freshness is worth
  less than an admitted gap.

> **These dates need verifying before a report is published.** Few vendors publish a cutoff, and
> the ones that do restate it between point releases. Each entry carries its `source` and the
> report prints it, so a reader can tell a vendor's statement from our estimate rather than
> having to trust the table. Same discipline as the model ids in `decision/presets.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from tradebot.core.money import ZERO, divide

#: How a date got into the table. Printed beside every verdict, because "the vendor says so" and
#: "we guessed from the release date" are different qualities of evidence.
VENDOR = "vendor-published"
ESTIMATE = "estimate from release date"


class Contaminated(StrEnum):
    """How much of a window a model may have memorized."""

    CLEAN = "clean"
    PARTIAL = "partial"
    CONTAMINATED = "contaminated"
    UNKNOWN = "unknown"

    @property
    def is_clean(self) -> bool:
        return self is Contaminated.CLEAN


@dataclass(frozen=True, slots=True)
class ModelCutoff:
    """One model's training cutoff, and where the date came from."""

    model: str
    cutoff: date
    source: str

    @property
    def moment(self) -> datetime:
        return datetime(self.cutoff.year, self.cutoff.month, self.cutoff.day, tzinfo=UTC)


#: Cutoffs for the models the seeded panels bind to (`decision/presets.py`). Matching is by
#: normalized id first and then by family prefix, so `qwen/qwen-2.5-72b-instruct:free` resolves
#: through the `qwen/qwen-2.5` entry without every point release needing a row.
CUTOFFS: tuple[ModelCutoff, ...] = (
    ModelCutoff("deepseek/deepseek-chat-v3", date(2024, 7, 1), ESTIMATE),
    ModelCutoff("meta-llama/llama-3.3", date(2023, 12, 1), VENDOR),
    ModelCutoff("meta-llama/llama-3.1", date(2023, 12, 1), VENDOR),
    ModelCutoff("qwen/qwen-2.5", date(2023, 10, 1), ESTIMATE),
    ModelCutoff("qwen2.5", date(2023, 10, 1), ESTIMATE),
    ModelCutoff("mistral-7b", date(2023, 9, 1), ESTIMATE),
    ModelCutoff("gemini-2.0", date(2024, 6, 1), VENDOR),
    ModelCutoff("gemini-1.5", date(2023, 11, 1), VENDOR),
)


@dataclass(frozen=True, slots=True)
class Contamination:
    """What one model's cutoff says about one replayed window."""

    model: str
    verdict: Contaminated
    #: Share of the window that falls after the cutoff, 0–1. Zero when nothing is known: an
    #: unproven claim of freshness is worth less than an admitted gap.
    post_cutoff_fraction: Decimal
    cutoff: date | None
    source: str

    @property
    def post_cutoff_pct(self) -> Decimal:
        return self.post_cutoff_fraction * Decimal(100)


def normalize(model: str) -> str:
    """`Qwen/Qwen-2.5-72B-Instruct:free` → `qwen/qwen-2.5-72b-instruct`.

    OpenRouter appends a routing suffix (`:free`, `:nitro`) that names a billing lane rather than
    a different set of weights, so it must not change which cutoff a model resolves to.
    """
    return model.strip().lower().split(":")[0]


def cutoff_for(model: str, table: tuple[ModelCutoff, ...] = CUTOFFS) -> ModelCutoff | None:
    """The longest family prefix matching `model`, or `None` when nothing is on file."""
    normalized = normalize(model)
    matches = [entry for entry in table if normalized.startswith(normalize(entry.model))]
    return max(matches, key=lambda entry: len(entry.model), default=None)


def classify(
    model: str,
    *,
    start: datetime,
    end: datetime,
    table: tuple[ModelCutoff, ...] = CUTOFFS,
) -> Contamination:
    """How much of `[start, end]` this model may have seen in training."""
    entry = cutoff_for(model, table)
    if entry is None:
        return Contamination(model, Contaminated.UNKNOWN, ZERO, None, "no cutoff on file")
    fraction = _post_cutoff_fraction(entry.moment, start, end)
    return Contamination(model, _verdict(fraction), fraction, entry.cutoff, entry.source)


def classify_all(
    models: tuple[str, ...],
    *,
    start: datetime,
    end: datetime,
    table: tuple[ModelCutoff, ...] = CUTOFFS,
) -> tuple[Contamination, ...]:
    """One verdict per distinct model, ordered by id so a report reads the same way twice."""
    distinct = sorted({normalize(model) for model in models})
    return tuple(classify(model, start=start, end=end, table=table) for model in distinct)


def _post_cutoff_fraction(cutoff: datetime, start: datetime, end: datetime) -> Decimal:
    if cutoff <= start:
        return Decimal(1)
    if cutoff >= end:
        return ZERO
    return divide(
        Decimal(int((end - cutoff).total_seconds())), Decimal(int((end - start).total_seconds()))
    )


def _verdict(fraction: Decimal) -> Contaminated:
    if fraction >= Decimal(1):
        return Contaminated.CLEAN
    if fraction <= ZERO:
        return Contaminated.CONTAMINATED
    return Contaminated.PARTIAL
