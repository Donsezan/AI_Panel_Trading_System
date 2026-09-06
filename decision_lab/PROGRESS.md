# decision_lab — where we are

**What this tool is for:** the bot can say what happened to the money. It cannot say whether a
decision was *right*. This replays recorded history through the bot's own decision path and scores
each decision against what the market did next — per regime, per seat.

Full spec: [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../docs/superpowers/specs/2026-08-23-decision-lab-design.md).
Slice order and rationale: §18.

---

## At a glance

| Slice | What it buys you | Status |
|---|---|---|
| **A** — integrity, day set, corpus | verified history + a frozen set of decision contexts | ✅ shipped |
| **B** — regimes, scoring, per-seat, report | *how did this panel do, and which seat carried it* | ✅ shipped |
| **C** — the sweep | *is a **different** panel right more often* — the stated goal | ✅ shipped |
| **D** — calibration + dashboard | normal day / shock day / six-month profit run | 🟡 pass 1 in progress |
| **E** — news archive | shock days measure the *news*, not just the price move | ⬜ not started |

**Three slices of five, and half of the fourth.** Comparing configurations — the thing the tool was
built for — now runs: N candidates over one frozen corpus, ranked, with a pairwise agreement matrix
and a per-candidate seat breakdown.

Slice D is split into two passes. **Pass 1 — the three calibration scenarios and the §10.6 gate —
is four tasks of nine done**, on branch `feat/decision-lab-slice-d`. Nothing of it is reachable
from the CLI yet: the `calibrate` commands are task 5. **Pass 2 — the dashboard and the notebook —
has not started**, and E (news) is untouched.

---

## Slice A — integrity, day set, corpus ✅

- [x] Audit every recorded series for holes → `data/history/decision_lab-coverage.json`
- [x] Repair fetch gaps in place; record what the venue never published as a known hole
- [x] Pin the nine calibration days (3 NORMAL, 3 SHOCK_UP, 3 SHOCK_DOWN), seed `20260823`
- [x] Corpus build — one reference pass through the unmodified `BacktestHarness`
- [x] `test_separation.py` (nothing under `tradebot/` names `decision_lab`) and its own `check.ps1`

Result today: both 1h series 4368/4368 bars, zero holes. Corpus `8ac130d8…`, 540/540 cycles at 8h.

## Slice B — regimes, scoring, per-seat, report ✅

- [x] Label every bar NORMAL / SHOCK_UP / SHOCK_DOWN; named event windows override
- [x] The long-only truth table, the ATR band, five verdicts, unscored-with-a-reason
- [x] Per-regime metrics, SHOCK_UP and SHOCK_DOWN never pooled
- [x] Per-seat scoring: round 0 beside final, swing rate, marginal contribution
- [x] Markdown report filed to `decision_lab/reports/`, never printed

Result today: [reports/decision-lab-8ac130d8f2ed5650dff0dcb9f969d07e.md](reports/).

## Slice C — the sweep ✅

- [x] `config/sweep.toml` — the candidate matrix, and its expansion cap
- [x] `candidates.py` — matrix → `PanelConfig` → `Basket`, validated *before* spend
- [x] `sweep.py` — N candidates over one corpus: cache, budget ceiling, resume
- [x] Cross-candidate tables (§9.6): ranking, agreement matrix
- [x] `registry.py` — keep every result, so two setups are compared rather than remembered

## Slice D — calibration and the dashboard 🟡

Planned in [docs/superpowers/plans/2026-09-05-decision-lab-slice-d-calibration.md](../docs/superpowers/plans/2026-09-05-decision-lab-slice-d-calibration.md).
Two passes; pass 1 is nine tasks, four of them done.

**Pass 1 — the scenarios and the gate** (in progress)

- [x] `gate.py` — the §10.6 record: its key, its persistence, and `require_satisfied`
- [x] `registry.py` — six outcome fields and `gate_unsatisfied`; `params.WINDOW_DAYS`
- [x] `calibration.py` — the four gate conditions, seat evidence, the cost projection
- [~] `render.py` — the per-day table, the spread, the gate verdict, `GATE SKIPPED` *(one fix round open)*
- [ ] `calibrate normal` and `calibrate shock` on the CLI
- [ ] The gate refuses an uncalibrated `sweep` (exit 6); `--skip-gate` stamps the report and the row
- [ ] `longrun.py` — the §10.4 profit arithmetic, `UNVALUABLE`, the veto breakdown
- [ ] `calibrate long` — the six-month run and its report
- [ ] Slice exit criterion end to end, and the docs

**Pass 2 — the dashboard** (not started)

- [ ] Its own read-only ASGI app, own port, own token; `notebooks/tuning.ipynb`

Scenarios 1 and 2 are **the existing sweep pointed at the nine pinned days** — `sweep.run` already
takes a `Sample`, so the cache, the budget ceiling, resume and the §7.7 substitute policy are
inherited rather than rewritten. Only scenario 3 is a different instrument: its own
`BacktestHarness` pass, its own ledger, its own workspace database.

## Slice E — news archive ⬜

- [ ] The `build_sim(news_feed=…)` seam — **the only `tradebot` change in the whole design**
- [ ] Its §2.3 guard tests and §16.2 contamination tests, in the *same commit* as the seam
- [ ] `archive/` — the API and sitemap backends behind one protocol, no fallback between them
- [ ] The summarizer (a compressor, not an analyst) and `ArchiveNewsFeed`

---

## What you can run today

```powershell
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history   # --repair re-asks the venue
.venv\Scripts\python.exe -m decision_lab dataset days   --data data\history
.venv\Scripts\python.exe -m decision_lab corpus build --data data\history --every 8h --reference-panel sim
.venv\Scripts\python.exe -m decision_lab report --corpus 8ac130d8f2ed5650dff0dcb9f969d07e
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --budget 40   # needs OPENROUTER_API_KEY
.\decision_lab\check.ps1
```

## Open items inside what already shipped

- **No real panel has ever been scored, but it is now one command away.** `sweep --configs
  decision_lab\config\sweep.toml` is a real measurement and needs `OPENROUTER_API_KEY`.
  `sweep-stub.toml` remains a plumbing check — every report and registry row it produces is
  stamped `PLUMBING CHECK — NOT AN EVALUATION`, so a stub run can never be mistaken for one that
  measured judgement. **A matrix is one kind of run or the other**, so a stub "control" candidate
  cannot be added to `sweep.toml` as a baseline: that label is whole-run, and one stub binding
  would both waive the missing-key refusal for the real candidates and stamp the page carrying
  their ranking as a plumbing check. Run the two files separately.
- **Every report is `NEWS-BLIND`** until slice E. Shock blocks measure the reaction to a violent
  price move, not to the reporting of an event.
- **Corpus `61721dba…` (4h) is a stale 67/1080-cycle pass** and is reused at its identity, never
  rebuilt. Delete the directory to retry it.
- **`STUB_SEED = 2024` is pinned** in `test_slice_b_end_to_end.py` because a reference pass can die
  on [KNOWN_GAPS](../docs/KNOWN_GAPS.md) §5. Delete the pin when §5 closes.
- **Once slice D pass 1 lands, `sweep` will refuse an uncalibrated run with exit 6**, and will
  therefore need a pinned day set where today it does not. `--skip-gate` is the escape hatch and
  stamps both the report and the §11 row, because a result whose provenance reads "nobody checked
  the seats first" should say so on its face. The gate is keyed on
  `(dataset_digest, matrix_digest, dayset_digest)` and deliberately **not** on `corpus_id`: every
  condition it checks is a property of the candidates, the seats and the days, so keying on the
  corpus would force a re-calibration each time the long run varied `--every` — the one axis that
  run exists to vary.

## Next step when you pick this up

**Finish slice D pass 1, task 4's open fix round, then tasks 5–9.** State on
`feat/decision-lab-slice-d`, ten commits in:

- `render.py` and `test_render.py` carry **uncommitted** work — the three review fixes are in and
  correct (per-pool spread, no doubled blank line, no dangling gate sentence), and one new test is
  red. The implementation is right; the test's own assertion is wrong. It asserts `"90.0%" not in
  text` as a proxy for "the two shock directions were not pooled", but `90.0%` legitimately
  appears both in the per-day table and inside SHOCK_DOWN's own `(10.0% to 90.0%)` range. Replace
  that line with one that actually discriminates — assert two spread lines exist and that no line
  matches the un-pooled form `**baseline** — accuracy spread` (no ` / POOL` segment). The other
  two assertions already pass and already prove the fix.
- Then the second test the round asked for and never got: a spacing regression asserting no
  `"\n\n\n"` in a rendered calibration report.
- Then run `.\decision_lab\check.ps1`, commit, and continue with task 5.

The full ledger — every ruling made along the way and what each costs if wrong — is in
`.superpowers/sdd/2026-09-05-decision-lab-slice-d-calibration/progress.md`, which survives a
session restart and is the recovery map if this context is lost.

Nothing here is reachable from the CLI yet, so **"What you can run today" is unchanged**: the
`calibrate` commands land in task 5, and the gate does not refuse a `sweep` until task 6.
