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
| **D** — calibration + dashboard | normal day / shock day / six-month profit run | 🟡 pass 1 shipped |
| **E** — news archive | shock days measure the *news*, not just the price move | ⬜ not started |

**Three slices of five, and pass 1 of the fourth.** Comparing configurations — the thing the tool
was built for — now runs: N candidates over one frozen corpus, ranked, with a pairwise agreement
matrix and a per-candidate seat breakdown. And it no longer runs *unchecked*: a sweep refuses
until the seats have been calibrated over nine pinned days.

Slice D is split into two passes. **Pass 1 — the three calibration scenarios and the §10.6 gate —
has shipped**, all nine tasks, on branch `feat/decision-lab-slice-d`. **Pass 2 — the dashboard and
the notebook — has not started**, and E (news) is untouched.

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
Two passes; pass 1 is nine tasks, all of them done.

**Pass 1 — the scenarios and the gate** ✅

- [x] `gate.py` — the §10.6 record: its key, its persistence, and `require_satisfied`
- [x] `registry.py` — six outcome fields and `gate_unsatisfied`; `params.WINDOW_DAYS`
- [x] `calibration.py` — the four gate conditions, seat evidence, the cost projection
- [x] `render.py` — the per-day table, the spread, the gate verdict, `GATE SKIPPED`
- [x] `calibrate normal` and `calibrate shock` on the CLI
- [x] The gate refuses an uncalibrated `sweep` (exit 6); `--skip-gate` stamps the report and the row
- [x] `longrun.py` — the §10.4 profit arithmetic, `UNVALUABLE`, the veto breakdown
- [x] `calibrate long` — the six-month run and its report
- [x] Slice exit criterion end to end, and the docs

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
.venv\Scripts\python.exe -m decision_lab calibrate normal --corpus <id> `
    --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab calibrate shock  --corpus <id> `
    --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab calibrate long --data data\history `
    --configs decision_lab\config\sweep.toml --candidate baseline `
    --start-equity 1000 --every 4h --window 6m
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --configs decision_lab\config\sweep-stub.toml --budget 1
.venv\Scripts\python.exe -m decision_lab sweep --corpus <id> --budget 40   # needs OPENROUTER_API_KEY
.\decision_lab\check.ps1
```

**`sweep` and `calibrate long` now refuse with exit 6** until both calibration halves have passed
for that exact dataset, matrix and day set — which also means a `sweep` needs a pinned day set
where before it did not (`dataset days` first, or exit 3). `--skip-gate` proceeds anyway and
stamps both the report and the §11 row, because a result whose provenance reads "nobody checked
the seats first" should say so on its face.

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
- **The gate is keyed on `(dataset_digest, matrix_digest, dayset_digest)`, deliberately not on
  `corpus_id`.** Every condition it checks is a property of the candidates, the seats and the
  days, so keying on the corpus would force a re-calibration each time the long run varied
  `--every` — the one axis that run exists to vary. The cost of the key it *does* have: one
  edited prompt mints a new `matrix_digest`, so it is a calibration again. That is nine days, and
  it is the price of the guarantee.
- **Nothing has been calibrated against a real panel yet.** Every gate record on this machine was
  opened by `sweep-stub.toml`, and a stub matrix can never satisfy a real matrix's gate — the
  bindings feed `panel_digest` → `matrix_digest`, which is a third of the key. The first real
  `sweep` will therefore need its own `calibrate normal` and `calibrate shock` first.
- **The dashboard and `notebooks/tuning.ipynb` are pass 2** and are not started. Everything slice
  D produces today is read as Markdown under `decision_lab/reports/` or as JSON in the workspace.

## Next step when you pick this up

**Slice D pass 1 is complete on `feat/decision-lab-slice-d`.** Both gates pass and
`git diff --stat main -- tradebot/` is empty. What is left, in the order it is worth doing:

1. **Merge the branch**, or review it first — the full ledger of every ruling made along the way
   and what each costs if wrong is in
   `.superpowers/sdd/2026-09-05-decision-lab-slice-d-calibration/progress.md`.
2. **Calibrate against a real panel and then sweep it.** This is the first thing that has ever
   needed `OPENROUTER_API_KEY`, and the cost projection on the calibration page is what tells you
   what the sweep after it will cost — that is the whole point of running the nine days first.
3. **Slice D pass 2** — the read-only dashboard and `notebooks/tuning.ipynb`, or
4. **Slice E** — the news archive, which is the only part of this design that touches `tradebot`
   at all, and whose seam and guard tests are one commit and never two.
