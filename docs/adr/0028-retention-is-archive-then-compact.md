# 28. Retention is archive-then-compact, and no event row is ever deleted

Date: 2026-08-22

## Status

Accepted. Implements Piece B of
[docs/superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md](../superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md),
and with [ADR 0003](0003-event-log-as-source-of-truth.md) supersedes that ADR's note that DESIGN
§6.9's retention policy "is not yet implemented".

## Context

DESIGN §6.9 specifies that raw transcripts and `ContextSnapshot`s are kept with a retention policy
— "full transcripts and full snapshots 90 days; summaries and snapshot hashes forever". Neither
half had been built. Payloads were uncompressed JSON stored inline in `events.payload_json`, and
nothing ever aged out.

Measured on `data/sim.db` on 2026-08-19 — 1,788 events over eleven days, 3.85 MB of payload JSON:

| Event type | Rows | Payload | Share |
|---|---:|---:|---:|
| `SEAT_RESPONDED` | 866 | 2.33 MB | 61% |
| `SNAPSHOT_FROZEN` | 96 | 1.08 MB | 27% |
| the other 14 types | 826 | 0.44 MB | 12% |

Within those two, the compactable part is `response.raw_text` and the `snapshot` body: **2.37 MB
of 3.85 MB, 62% of the log**, without losing a field any projector, report or cost total reads.

Volume is not flat. Moving the demo from the single-seat `STUB_PANEL` to `SIM_PANEL` — three seats,
blind-then-debate, per instrument — took per-cycle log cost from ~8 KB to ~47 KB. A continuously
supervised sim now writes roughly **23 MB/week**, about 60% of it seat transcripts.

There was also an operational gap with a name: `docs/OPERATIONS.md` precondition 17 — "your tax and
record-keeping requirements are known, and the event log's retention is set to match" — was
unsatisfiable, because there was no retention to set. A live-arming checklist item that cannot
honestly be ticked is worse than none.

## Decision

A three-stage lifecycle for the two heavy payloads, and **no event row is ever deleted**:

```
day 0 ──────────── day 30 ─────────────── day 90 ──────────────►
full payload       archived to disk,      archive file deleted
in the database    compacted from the db
                                          summaries, votes, costs,
                                          digests: kept forever
```

- **Archive, then compact.** A day is written to one immutable gzip file, that file is re-read and
  verified by row count and SHA-256, and only then are the payloads trimmed from the database. The
  ordering is the safety property: `raw_text` from 31 days ago is never the last copy of itself.
- **Deletion is of archive *files*, never of rows.** `delete_aged` runs last, over one mode's
  archive directory and nothing else, and decides by parsing each file's name.
- **Both windows are versioned configuration**, a third `ConfigKind`, edited on the Parameters page.
- **The whole pass is one recorded event.** `MAINTENANCE_RAN` carries what ran, under which
  windows, and whether it failed — and is also what answers "is a run due".

## Consequences

`persistence/schema.py` says of `events`: *"Append-only: no code updates or deletes a row here."*
That is now true with exactly one exception, `maintenance/compaction.py`, and the comment says so.
The exception is licensed by a directly asserted invariant, not by an argument.

Compaction is irreversible in the hot database. Recovery means reading the archive, which is
offline and deliberately not wired back into the dashboard.

Archive deletion is irreversible, full stop. After `archive_keep_days` the literal model completion
and the frozen snapshot body exist nowhere. That is DESIGN §6.9's intended policy and it is what
makes precondition 17 answerable, but it is a genuine reduction in what can be reconstructed: after
90 days, "what exactly did the model say" is answerable only down to its vote, thesis, invalidation
and key risks. If a jurisdiction ever requires the full text for longer, the window is a config edit
— the mechanism does not change.

## Rules that are easy to get backwards

- **The registry is the containment.** `COMPACTORS` has two entries and a type absent from it is
  never rewritten, so this can never grow to eat an event nobody reasoned about. Adding a third is
  a deliberate act, and `test_maintenance_compaction.py` asserts the set is exactly those two —
  including that no money-bearing type is in it.
- **The invariant is asserted, not argued.** A projection rebuild after compaction is identical to
  one before it. It holds because `SEAT_RESPONDED` has no projector and is not in `REPORT_TYPES`,
  and because `_project_snapshot_frozen` reads exactly `snapshot_id` and `digest`, both retained.
  It is tested twice: on handmade events, and on a **real cycle** driven through the actual loop —
  a handmade event carries the fields the test author remembered, not the fields the system writes.
  If it ever fails, the compactor is dropping a field a projector reads. Fix the compactor, never
  the assertion.
- **Nothing is compacted that is not already in a *verified* archive.** Not merely written: re-read,
  counted and hashed. Gzip's CRC catches corruption but not truncation at a record boundary, and a
  short file would license compacting events it does not contain.
- **Work is found by what is still heavy, never by event type.** `pending_days` selects on the
  registry's own `heavy_key`, so a day whose payloads are all trimmed drops out. Selecting by type
  — the obvious reading — revisits every past day forever, and once that day's archive has been
  deleted at `archive_keep_days` the next pass finds the file absent and **recreates it** from the
  already-compacted rows: a hollow archive holding none of the payloads it is named for,
  reappearing daily, growing without bound and quietly contradicting the promise that deletion is
  final.
- **Compaction batches advance by `seq`, not by rows rewritten.** A batch can legitimately rewrite
  nothing: a seat that abstained has no `raw_text`, so the compactor returns `None` and the row
  never gains a marker. A loop stopping on a zero rewrite count leaves those rows at the head of
  every batch and permanently stops compacting everything behind them — silently, with no error and
  no failed pass. Three seats debating makes a chunk's worth of abstentions an ordinary degraded
  day, not a corner case.
- **A compacted cycle is shown as archived, never as empty.** The drill-down said "No snapshot was
  frozen — the cycle was blocked before one was built" whenever the body was absent. For a compacted
  cycle that is false. It now names the archive and shows the unchanged digest. The seat transcript
  needs the same line and is easier to forget, because compaction keeps the vote, the thesis and the
  cost — so a compacted transcript renders as *complete* unless something says otherwise.
- **An absent policy document means the defaults, not a refusal.** Maintenance shares its tick with
  the daily backup, and refusing to back anything up because nobody published a retention policy
  would be fail-*useless*. The event records which windows were in force, defaults included.
- **The windows are read fresh at every pass**, never captured at wiring — the rule ADR 0021
  established for the Tier-2 cap. An edit takes effect at the next tick with no restart.
- **A failed pass is a recorded fact, never an exception.** A maintenance defect must never be what
  stops the bot trading. It is also what counts as the day's run: it has already raised a HIGH
  notification, and retrying every five minutes against a full disk only repeats the alarm.
- **The tick paces on the injected `Clock`**, unlike the WebSocket tail's deliberate departure
  (ADR 0024). This is domain time: a pass is due on a calendar boundary and ages files by day, so a
  backtest stepping its clock a month forward must not trigger thirty backups. Only a real-clock
  process may start it.
- **Every filesystem step runs off the event loop.** `VACUUM INTO` on a multi-gigabyte database is
  seconds to minutes of blocking I/O, and gzipping a day of transcripts is the same class of work.
  This task shares its loop with the supervisor, the execution monitor and the dashboard's socket.
  Compaction is already off it, through `SingleWriter`'s executor, in bounded chunks so a cycle's
  `append` never queues behind a multi-second write.

## Known consequence, stated rather than discovered

On a system that has not run maintenance for months, the first pass archives a day already older
than `archive_keep_days`, compacts it, and deletes that archive in the same run. The policy is
behaving correctly — a day past 90 is not retained — but it means the first pass after a long gap
can move data straight to gone. The daily line names how many day files were deleted, so it is
never silent.

Shortening `archive_keep_days` acts immediately and destructively: publishing 7 where 90 stood
deletes 83 days of transcripts on the next tick. It is versioned and attributable, but not undoable.
The form states this next to the field.

## Alternatives considered

- **Compact in place, no archive.** Loses `raw_text` at day 30 rather than day 90, and there is no
  copy to recover from in between.
- **Hard-delete rows.** Breaks projection rebuild and destroys the audit trail — the log is the
  compliance artifact, and a gap in it cannot be reconstructed from anywhere.
- **Keep archives indefinitely.** The record grows without bound and DESIGN §6.9's policy is never
  actually met, leaving precondition 17 exactly as unanswerable as before.
- **Retention windows as environment variables.** No record of what the policy was last month, for
  the setting that governs how long financial records are kept.
- **Two extra fields on `global_risk`.** Cheaper, but retention is not a risk limit, and publishing
  a stop-loss would then republish a retention window as a side effect.
