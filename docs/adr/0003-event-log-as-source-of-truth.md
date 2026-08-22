# ADR 0003 — Append-only event log with derived projections

**Status:** accepted · 2026-07-26 · implements DESIGN §6.9, PLAN §3.3

## Context

The system must be able to answer, for any order it ever placed: what data the panel saw, what
each seat argued, why risk approved or resized it, and what the venue actually did. That is a
compliance and tax artifact as much as a debugging aid, and it must survive schema changes,
dashboard rewrites, and bugs in the read model.

## Decision

- **`events` is append-only.** No code updates or deletes a row. It is the source of truth.
- **Projections are derived and disposable.** `cycles`, `orders`, `fills`, `positions`,
  `decisions`, `risk_events` exist so the dashboard can query cheaply. `rebuild_projections`
  truncates and replays the log into them.
- **An event and its projection commit in one transaction.** Otherwise the dashboard could show
  a state the audit trail contradicts, or vice versa.
- **Every projector must be idempotent under replay**, and a test asserts that replaying the log
  reproduces byte-identical projections. A projector that only works forwards silently destroys
  the guarantee, and nobody would notice until the day it was needed.
- **Event payload shapes live in one place** (`EventFactory`), so producer and projector cannot
  drift apart on a key name.
- Event types with no projector are **audit-only by design** (seat responses, risk-check
  provenance), not an omission.

## Storage decisions

- **Money is TEXT, never a numeric column.** SQLite's `NUMERIC` affinity converts through
  IEEE-754 double, which would corrupt the exact decimals the money layer exists to preserve.
  The same type decorator refuses a `float` on the way in, so the ban holds at the database
  boundary too.
- **Instants are ISO-8601 UTC TEXT.** SQLite has no timezone-aware type; naive values are
  rejected in both directions.
- **Alembic from day one**, including for a fresh database. `create_all` would work today and
  leave the first schema change with no upgrade path — unacceptable for a database holding
  financial records that cannot be recreated.
- **The baseline migration refuses to downgrade.** Dropping the `events` table is a deliberate
  manual act, not something a migration does on the way to fixing something else.

## Consequences

- Writes go through a single writer thread whose identity is asserted, so the log has a stable
  total order and no two code paths can mutate the same row.
- Reads bypass the writer; WAL mode lets the dashboard query while a cycle is in flight.
- Full transcripts and snapshots are large. DESIGN §6.9's retention policy — transcripts and
  snapshots 90 days; summaries and snapshot *hashes* forever — is implemented by
  [ADR 0028](0028-retention-is-archive-then-compact.md): a day is archived to an immutable
  verified file, then its two heavy payloads are trimmed from the database, and the archive is
  deleted once past the second window. The per-cycle hash is what keeps replay verifiable after
  compaction, and that sentence is now load-bearing rather than aspirational.
- **No event row is ever deleted, and exactly one module updates one.** `maintenance/compaction.py`
  rewrites `payload_json` for two event types and nothing else. The invariant that licenses it is
  asserted directly rather than argued: a projection rebuild after compaction is identical to one
  before it.
