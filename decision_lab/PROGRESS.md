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
| **D** — calibration + dashboard | normal day / shock day / six-month profit run | ✅ shipped |
| **E** — news archive | shock days measure the *news*, not just the price move | ⬜ not started |

**Four slices of five.** Comparing configurations — the thing the tool was built for — now runs:
N candidates over one frozen corpus, ranked, with a pairwise agreement matrix and a per-candidate
seat breakdown. And it no longer runs *unchecked*: a sweep refuses until the seats have been
calibrated over nine pinned days.

Slice D is split into two passes. **Pass 1 — the three calibration scenarios and the §10.6 gate —
has shipped**, all nine tasks, merged to `main`. **Pass 2 — the dashboard and the notebook — has
shipped, all ten tasks**, on the branch `slice-d-pass-2-dashboard`, not yet merged. E (news) is
untouched.

**Pass 2 is no longer the read-only surface §12 first specified.** That was reversed deliberately
and the spec records the reversal at §12.1: the tool exists to find a better panel, and a loop
that means editing TOML in one window, running a command in a second and reading Markdown in a
third is a loop nobody closes. The dashboard is now **three surfaces over one shell** — read a
result, build a seat set, run it. What the reversal did *not* license is unchanged: no authority
over the bot, no promotion authority, no automatic search.

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

## Slice D — calibration and the dashboard ✅

Planned in [docs/superpowers/plans/2026-09-05-decision-lab-slice-d-calibration.md](../docs/superpowers/plans/2026-09-05-decision-lab-slice-d-calibration.md).
Two passes, both now complete: pass 1 is nine tasks, pass 2 is ten.

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

Scenarios 1 and 2 are **the existing sweep pointed at the nine pinned days** — `sweep.run` already
takes a `Sample`, so the cache, the budget ceiling, resume and the §7.7 substitute policy are
inherited rather than rewritten. Only scenario 3 is a different instrument: its own
`BacktestHarness` pass, its own ledger, its own workspace database.

**Pass 2 — read, edit, run** ✅ on branch `slice-d-pass-2-dashboard`, **not yet merged**

Planned in [docs/superpowers/plans/2026-09-06-decision-lab-slice-d-pass-2-dashboard.md](../docs/superpowers/plans/2026-09-06-decision-lab-slice-d-pass-2-dashboard.md).
Ten tasks, executed subagent-per-task with a review after each.

- [x] **1** `analysis.py` — one read assembly, shared by the CLI, the dashboard and the notebook
- [x] **2** `matrices.py` — seat sets as versioned TOML in the workspace, and a TOML writer
- [x] **3** `jobs.py` — the OS advisory lock and the child-process launcher; CLI exit 7
- [x] **4** the shell — own token, own cookie name, pure-ASGI auth, the derivation cache
- [x] **5** Runs — the §11 registry, sortable, two rows diffable
- [x] **6** run detail — the ranking per regime, and what was not measured, with its reason
- [x] **7** seat detail and the decision drill-down
- [x] **8** the seat-set editor — `4ba6e09`, fixed at `5918a91` (it was deleting expansion axes,
      and every "+ seat" click silently *saved* the document instead of re-rendering the form)
- [x] **9** the run forms — argv builders, required budgets, the cost projection, stop —
      `424ef86`, fixed at `0737851` (a vacuous assertion that passed on every possible page, and a
      refusal page that reported a finished job's status differently from the index page)
- [x] **10** `notebooks/tuning.ipynb`, the slice exit criterion, and the docs

Result today: `python -m decision_lab dashboard --port 8788` serves three surfaces over one
shell — Runs (sortable, two rows diffable), the seat-set editor, and a job launcher that starts,
watches and stops a real child process of this tool's own CLI — plus `notebooks/tuning.ipynb`,
which reads the same `analysis.py` through a kernel instead of a browser.
`git diff --stat main -- tradebot/` is empty, the slice's own exit criterion.

Three rules this pass added that are easy to get backwards:

- **A run started from the page is a child process of this tool's own CLI.** The page cannot
  diverge from the command, a six-month `calibrate long` never blocks the event loop serving the
  page, and Stop is a real termination. Every refusal it renders is an exit code the CLI already
  had.
- **One writer at a time, through an OS advisory lock — never a pid file.** The OS releases it when
  the holder dies, and there is no portable way to ask whether a pid is alive: on Windows
  `os.kill(pid, 0)` *terminates* the process. The CLI takes the same lock and refuses with exit 7.
- **The lab's session cookie has its own name.** Cookies are not port-scoped, so a lab app setting
  `tradebot_session` on `127.0.0.1` would overwrite the bot dashboard's cookie on another port and
  log the operator out of the surface holding the kill switch.

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

The tuning dashboard serves as well, on the `slice-d-pass-2-dashboard` branch (not yet merged to
`main`). Its token is its own — never the bot's — and it refuses to start without one:

```powershell
$env:DECISION_LAB_DASHBOARD_TOKEN = "at-least-sixteen-characters"
.venv\Scripts\python.exe -m decision_lab dashboard --port 8788   # --allow-remote to leave loopback
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
- **`calibrate long` has no `--budget` and no mid-run ceiling.** Found while planning pass 2:
  scenario 3 drives `BacktestHarness` directly, with no engine seam to meter, so the only ceiling
  is the operator stopping it — and stopping it means deleting that run's directory before it can
  run again, because the database it leaves behind is the record of why the pass failed. The
  dashboard's form says so rather than implying a ceiling that does not exist; the CLI says
  nothing, which is a gap worth closing.

## Next step when you pick this up

**The branch is finished, and unmerged.** `slice-d-pass-2-dashboard`, twenty-one commits including
this one, branched from `main` at `4ef58d8` plus the spec revision `e578e24` and the plan
`ab1365c`. **Nothing is merged and nothing is pushed.** `git diff --stat main -- tradebot/` is
empty, which is the slice's exit criterion.

### Where exactly

- **All ten tasks are complete and independently reviewed**, each with its fix rounds closed.
  Task 8's review found 2 Critical and 2 Important, all four fixed in `5918a91`. Task 9's found 0
  Critical and 2 Important, both fixed in `0737851`. Task 10 — the notebook, the exit criterion
  and the docs — landed in `177dd75`.
- **Both authoritative gates were green at `177dd75`**, run by the controller rather than by an
  implementer: `.\decision_lab\check.ps1` — **815 passed**, mypy clean over 81 files — and the
  root `.\check.ps1` — **2848 passed**, all 17 coverage gates met.
- **The whole-branch review is done: 0 Critical, 5 Important.** The controller ruled two further
  items in, making seven, and all seven landed as one fix wave in `4d2f525`. What it changed:
  the slice's own exit-criterion test could not tell a *ranked* candidate from one merely named in
  the **Not measured** list; the round-0 test passed on exactly the page state that suppresses
  round-0 rows; `jobs.py` had neither Task 8's unreadable-file guard nor the rename-atomic write
  the package's three other writers have; the `dashboard` CLI command had no tests at all,
  including nothing pinning its deliberate absence from `LOCKED`; `seats.rounds_are_identical`
  keyed across regimes and so could hide round-0 rows on a false positive; and the seat-set editor
  rendered no control for `SeatConfig.instruction`, silently deleting one on save.
- **This file is the last commit of that wave.** After it, the branch is ready to be offered for
  merge — a scoped re-review of the fix wave is the only thing still scheduled.

### One thing the fix wave found and deliberately left alone

`_fold` reports swing rate and marginal contribution as **final-round concepts only**, zeroing
them on the round-0 row. `rounds_are_identical` compares whole rows, so a round-0 row can never
equal its final twin for any seat that ever swung — which means §9.7's "say so rather than
printing the same numbers twice" banner almost never fires, `max_rounds = 1` included. Verified on
the stub sweep: neither expanded candidate reaches it, under the old keying or the new. That is
pre-existing, presentation-only, and outside the seven items; it is recorded here rather than
fixed, and the dashboard's suppression branch now has a test of its own either way.

### How to resume

The run was driven by `superpowers:subagent-driven-development`, controller in a subagent so the
main session stays clean. Two files carry everything:

- `.superpowers/sdd/2026-09-06-decision-lab-slice-d-pass-2-dashboard/progress.md` — the ledger:
  the pre-flight conflict scan, every ruling made and what each costs if wrong, and a
  `Task N: complete` line per finished task. **Trust it and `git log` over any recollection.**
- `.../handover.md` — the running summary, updated as each task closed.

A fresh session resumes by reading those two, checking `git status` and `git log`, and dispatching
the next step. A dirty tree means an implementer died mid-task: its work is inherited, and the
next implementer verifies, finishes and commits it rather than starting over.

### What has actually gone wrong, so you can avoid it

Neither failure has been in the work; both are operational, and between them they account for
every interruption this run has had.

- **Seven implementers have parked on a long test run.** They background the suite or
  `check.ps1` behind a monitor and then wait instead of finishing. Dispatch with
  foreground-only verification stated explicitly — as *two* commands, the targeted file then
  `pytest decision_lab/tests -q`, split by file if one call cannot hold it — and when a child goes
  quiet, check whether it is *dead* rather than slow: no python process, and a log whose last line
  stops advancing.
- **The session rate limit has killed the controller and its implementer simultaneously, seven
  times.** Nothing to do but resume after the reset — which is cheap precisely because the ledger
  is current. Keep it that way: a line before and after every dispatch, not only at completion.
- **Never grep or tail an agent's `.output` file.** They are full JSONL transcripts and reading one
  floods the context. Only plain bash-command logs are safe to read.
- **A half-finished task leaves its work in the tree, and that is the normal case here.** Five of
  the ten tasks were inherited that way. The next implementer verifies, finishes and commits what
  is there; it does not start over.
- **Settle a contradiction yourself before escalating a model at it.** Task 9 was escalated to opus
  because two gate logs disagreed — but the disagreement was answerable in four minutes by running
  `ruff format --check`, `ruff check` and `mypy` against the bytes on disk. A more capable model
  cannot make an old log describe a tree it never ran against.
- **Move the ~600s gate off the implementer's plate entirely.** It exceeds the Bash tool's 600s cap,
  so the harness backgrounds it out from under whoever launched it — the shape of ten of this run's
  stalls. The controller runs it; the implementer runs targeted files only, in the foreground.
- **Verify the bytes a test run describes.** One full-suite run here was silently invalidated when
  an implementer edited a template after it started. A number that does not name its commit is not
  evidence; kill the run and re-launch rather than record it.
- **Write the ledger line AFTER the tool call returns, never before.** The controller wrote
  "Task 10: dispatching implementer" and then did not dispatch, leaving a false entry in the one
  file a resuming session is told to trust. The ledger is only a recovery map if every line in it
  describes something that actually happened.

### After this branch lands

1. **Calibrate against a real panel and then sweep it.** The first thing here that has ever needed
   `OPENROUTER_API_KEY`, and the cost projection on the calibration page is what tells you what
   the sweep after it will cost — the whole point of running the nine days first.
2. **Slice E** — the news archive, the only part of this design that touches `tradebot` at all,
   and whose seam and guard tests are one commit and never two.

Pass 1 was written across two sessions and **carries no independent task review for tasks 4–9** —
the reviewing agents in session 1 stalled, and session 2 was executed directly. Its ledger is
`.superpowers/sdd/2026-09-05-decision-lab-slice-d-calibration/progress.md`; the diffs worth a
second pair of eyes are `2554c00..8408d86`.
