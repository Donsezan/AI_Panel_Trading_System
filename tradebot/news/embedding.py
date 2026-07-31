"""Deterministic local text embeddings, for near-duplicate detection and retrieval.

Why not a neural embedding model: this is the *dedup* half of the news pipeline, and its whole
job is to notice that two headlines are the same story. A hashed bag of unigrams and bigrams
does that well, and it buys three properties a downloaded transformer cannot:

* **Determinism.** The same text always produces the same vector, in this process and in one
  running a year from now, so a dedup test asserts a value rather than approximating one and a
  replayed cycle reproduces the pipeline exactly.
* **No runtime network.** Nothing is downloaded on first use, so the suite is offline and a
  news fetch cannot stall behind a model load.
* **No supply-chain surface.** No onnxruntime, no tokenizers, no model weights (PLAN §4).

`blake2s` does the hashing rather than `hash()`, which is salted per process and would make
yesterday's stored vectors incomparable with today's.

The trade-off is stated honestly: this captures lexical overlap, not meaning. Two reports of the
same event in different words score lower than a transformer would give them. That is acceptable
for dedup and weak for semantic retrieval; the `VectorStore` seam exists so a real embedding
model can replace this without touching the pipeline (DESIGN §6.4).

Failure semantics: pure and total. Empty text yields an empty vector, whose similarity with
anything is zero — so an item with no usable text can never be mistaken for a duplicate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from hashlib import blake2s
from itertools import pairwise
from typing import Final

from tradebot.core.money import MONEY_CONTEXT, ZERO, divide, to_decimal
from tradebot.core.schema import canonical_json
from tradebot.news.normalize import strip_html

#: Vector width. Large enough that unrelated headlines rarely collide, small enough that a
#: cosine over a few thousand stored items is a millisecond of Decimal arithmetic.
DIMENSIONS: Final = 256

#: Similarity at or above which two items are the same story. Tuned for unigram+bigram overlap:
#: syndicated copies of one headline sit well above it, two stories on one topic well below.
DEFAULT_DUPLICATE_THRESHOLD: Final = Decimal("0.85")

Vector = Mapping[int, Decimal]

_WORD_SPLIT: Final = str.maketrans(dict.fromkeys("\"'`.,;:!?()[]{}<>/\\|—–-", " "))


def _features(text: str) -> list[str]:
    """Unigrams plus adjacent bigrams.

    Bigrams are what make the difference for headlines: "Bitcoin falls" and "Bitcoin rises"
    share every unigram, and a dedup that cannot tell them apart is worse than none.
    """
    words = strip_html(text).lower().translate(_WORD_SPLIT).split()
    return words + [f"{first}_{second}" for first, second in pairwise(words)]


def _bucket(feature: str) -> int:
    return int.from_bytes(blake2s(feature.encode(), digest_size=4).digest(), "big") % DIMENSIONS


def embed(text: str) -> dict[int, Decimal]:
    """L2-normalised sparse vector. Cosine similarity is then a plain dot product."""
    counts: dict[int, Decimal] = {}
    for feature in _features(text):
        bucket = _bucket(feature)
        counts[bucket] = counts.get(bucket, ZERO) + Decimal(1)
    if not counts:
        return {}
    norm = MONEY_CONTEXT.sqrt(sum((value * value for value in counts.values()), start=ZERO))
    return {bucket: divide(value, norm) for bucket, value in counts.items()}


def similarity(left: Vector, right: Vector) -> Decimal:
    """Cosine similarity of two normalised vectors, clamped to `[0, 1]`.

    Clamped because accumulated rounding can push a dot product a hair past 1, and a similarity
    above 1 would silently break any threshold comparison downstream.
    """
    if not left or not right:
        return ZERO
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    total = sum(
        (weight * larger[bucket] for bucket, weight in smaller.items() if bucket in larger),
        start=ZERO,
    )
    return min(max(total, ZERO), Decimal(1))


def dumps(vector: Vector) -> str:
    """Serialize for storage. Decimals as strings — a float round-trip would lose exactness."""
    return canonical_json({str(bucket): str(weight) for bucket, weight in vector.items()})


def loads(payload: str) -> dict[int, Decimal]:
    """Parse a stored vector back. Raises `MoneyError` on a value that is not a decimal."""
    return {int(bucket): to_decimal(weight) for bucket, weight in json.loads(payload).items()}
