# ADR 0007 — News embeddings are computed locally and deterministically, not by a model

**Status:** accepted · 2026-07-28 · supersedes the vector-store choice in PLAN §4, implements DESIGN §6.4

## Context

The news pipeline needs embedding similarity for one job: noticing that two feed entries are the
same story. A widely-syndicated headline arriving from three sources under three URLs enters the
snapshot three times and gets three times the weight in the panel's evidence — a real way to bias
a decision with no bad actor involved. URL-hash dedup does not catch it, because the URLs differ.

PLAN §4 named ChromaDB, reusing the prototype's working integration. Costed out, that brings
`onnxruntime` and `tokenizers` (~200 MB) and downloads an `all-MiniLM` model from the network on
first use. Three consequences follow, and the third is the one that decided it:

1. **Supply-chain surface.** PLAN §4 treats a compromised transitive dependency in a trading bot
   as a worst case, and this is the largest such addition in the project.
2. **A runtime network dependency in the ingest path**, on first use and after any cache wipe.
3. **A non-deterministic test suite.** Dedup tests would either require the download in CI or
   assert against a mock — which proves nothing about dedup.

## Decision

**Embeddings are a hashed bag of unigrams and bigrams, computed in-process, in `Decimal`.**

- Features are hashed into 256 buckets with `blake2s` and the vector is L2-normalised, so cosine
  similarity is a plain dot product. `blake2s` rather than `hash()`, which is salted per process
  and would make yesterday's stored vectors incomparable with today's.
- Bigrams are included because unigrams alone cannot separate "Bitcoin falls" from "Bitcoin
  rises", and a dedup that cannot tell those apart is worse than none.
- Storage is a SQLite table behind the existing `VectorStore` protocol. Retrieval is a bounded
  linear scan: a few thousand headlines is sub-millisecond, and the recency window bounds it.
- Embedding runs in a thread executor. The trading loops share one event loop and must never
  stall behind text processing.

## What this costs

Stated plainly: **this captures lexical overlap, not meaning.** Two accounts of the same event in
genuinely different words score lower than a transformer would give them, so some syndicated
duplicates get through. That is acceptable for dedup — the failure mode is a duplicate headline,
not a wrong order — and weak for the *historical retrieval* half of DESIGN §6.4, which is
currently unused. Should retrieval quality start mattering, the `VectorStore` seam is the swap
point and nothing in the pipeline changes.

## Consequences

- The suite is offline, free and deterministic, so a dedup test asserts a value rather than
  approximating one, and a replayed cycle reproduces the pipeline exactly.
- No new native dependency, no model download, no first-use latency spike.
- Similarity is clamped to `[0, 1]`: accumulated decimal rounding can push a dot product a hair
  past 1, and a similarity above 1 would silently break every threshold comparison downstream.
- Empty text yields an empty vector whose similarity with anything is zero, so an item with no
  usable text can never be mistaken for a duplicate.
