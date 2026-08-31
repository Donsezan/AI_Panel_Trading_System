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
| **D** — calibration + dashboard | normal day / shock day / six-month profit run | ⬜ not started |
| **E** — news archive | shock days measure the *news*, not just the price move | ⬜ not started |

**Three slices of five.** Comparing configurations — the thing the tool was built for — now runs:
N candidates over one frozen corpus, ranked, with a pairwise agreement matrix and a per-candidate
seat breakdown. D (calibration + dashboard) and E (news) are not built.

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

## Slice D — calibration and the dashboard ⬜

- [ ] Scenario 1 — a normal day, snapshot-scored on the pinned days
- [ ] Scenario 2 — a shock in each direction, kept apart
- [ ] Scenario 3 — six-month long exposure, own ledger, real starting equity
- [ ] The §10.6 calibration gate (exit 6) and the cost projection
- [ ] Its own read-only dashboard, own port, own token; `notebooks/tuning.ipynb`

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
  measured judgement.
- **Every report is `NEWS-BLIND`** until slice E. Shock blocks measure the reaction to a violent
  price move, not to the reporting of an event.
- **Corpus `61721dba…` (4h) is a stale 67/1080-cycle pass** and is reused at its identity, never
  rebuilt. Delete the directory to retry it.
- **`STUB_SEED = 2024` is pinned** in `test_slice_b_end_to_end.py` because a reference pass can die
  on [KNOWN_GAPS](../docs/KNOWN_GAPS.md) §5. Delete the pin when §5 closes.

## Next step when you pick this up

Slice D — calibration and the dashboard. The sweep the tool was built for now runs; what is
missing is the three fixed scenarios that make a run *repeatable and comparable to a gate*, and
somewhere to watch a long one land beside itself as it goes.
