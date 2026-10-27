# Roadmap and agent plan — from slice E code to a measured, news-fed panel

Written 2026-10-27. Picks up where [decision_lab/PROGRESS.md](../../../decision_lab/PROGRESS.md)
"What comes next" leaves off. Two tracks: **decision_lab** (code, driven here) and **the bot's
paper soak** (operator time, not code). They do not block each other.

---

## Part 1 — the roadmap

Each stage names what it needs before it starts and what it produces at the end. ⛔ marks a gate
that needs the operator's decision. Nothing passes one by default.

| # | Stage | Needs | Produces | Spend |
|---|---|---|---|---|
| 0 | **Housekeeping** — commit the pending spec edit (§6.8.2 floor measurement) and the slice E plan; open branch `decision-lab-slice-e` in a worktree | — | a clean `main`, an isolated branch | none |
| 1 | **Slice E code** — the 11 tasks of [2026-10-05 plan](2026-10-05-decision-lab-slice-e-summariser-and-feed.md), all offline on the stub | 0 | summariser, `archive publish`, the `build_sim(news_feed=…)` seam, `ArchiveNewsFeed`, clustering, banners; both gates green | none |
| 2 | ⛔ **D1: close KNOWN_GAPS §5's intra-cycle half?** Poll working orders between the snapshot and the decision | operator's yes/no | — | — |
| 2a | *if yes:* design note → ADR → plan → implement. This changes the order of the live money path and adds a venue call per cycle, so it gets its own spec. Afterwards remove the `STUB_SEED = 2024` pin | D1 = yes | an ADR, a fix, scenario tests, the pin removed | none |
| 2b | *if no:* add `corpus build --seed` and resume support, so a pass that dies can be re-run rather than lost | D1 = no | a reproducible pass that can be retried | none |
| 3 | **2025 reference corpus** — `corpus build --data data\history-2025 --every 8h --reference-panel sim`, about 1 095 cycles, news-blind first | 1, and 2a or 2b | a corpus id over the news year | none (sim panel) |
| 4 | ⛔ **D2: spend money.** `OPENROUTER_API_KEY` set, model ids verified against openrouter.ai/models | operator | — | — |
| 5 | **Summarise, then publish** — the six steps in the plan's "When the real key arrives": dry run, `--limit 50` to price it, batches of 1 000, traceability sample, `archive publish` | 1, D2 | the §6.6 travelling archive, `archive_digest` | about 11 894 rows × per-row cost, priced at step 3 first |
| 6 | **News-fed corpus** over 2025 using both sources | 3, 5 | a corpus whose `corpus_id` includes the archive | none |
| 7 | **Calibrate (the nine days) → sweep → `calibrate long`** on a real panel | 6, D2 | the §10.6 gate passed, a ranked candidate table, a six-month profit run | the calibration page projects the sweep's cost |
| 8 | ⛔ **D3: promote a candidate panel into the bot?** Done by hand, through Settings, never from decision_lab | 7 | a new basket version | — |

**Bot track, alongside the stages above (operator):** a paper soak, `run --mode paper`, for weeks,
then `report promotion`. Arming live stays the human's act in [OPERATIONS.md](../../OPERATIONS.md).
One known sim quirk: XRP never trades because synthetic prices and real lot sizes do not match.
That limits what a sim soak covers. Fix it before the soak only if XRP matters.

**Order of work:** 0 → 1 → (2 runs alongside the end of 1) → 3 → **stop at D2**. Everything
up to D2 is free and offline.

---

## Part 2 — the agents

Process: `superpowers:subagent-driven-development`. I am the **controller**. I hold the plan and
the ledger, make the rulings and run the gate, and I write no feature code myself.

### Roles

| Role | Agent type | Model | Count | Job |
|---|---|---|---|---|
| **Controller** | (this session) | — | 1 | preflight conflict scan, per-task briefs, dispatches, ledger at `.superpowers/sdd/<plan>/progress.md`, runs `check.ps1` (about 600 s, longer than a subagent's limit) |
| **Implementer** | general-purpose | per task, see below | 1 per task, **always sequential** | write the tests first (TDD), make them pass, commit; targeted tests only, never the full gate |
| **Task reviewer** | general-purpose | sonnet (opus for Task 7) | 1 per task | spec compliance and code quality against the brief, the report and the review package |
| **Re-reviewer** | general-purpose | sonnet | per fix round | checks each finding was fixed (ADDRESSED / NOT ADDRESSED), looking only at the fix diff |
| **Fix escalation** | general-purpose | opus | rounds 4–5 only | a fresh implementer for a task that three fix rounds did not settle |
| **§5 investigator** | Explore → Plan | sonnet → opus | 1 each, **can run alongside stage 1** (read-only) | trace snapshot → `SimBroker` match → sizing; draft the design options for D1 |
| **Final reviewer** | general-purpose | opus | 1 per branch | review the whole branch and sort the deferred minor findings |

Implementers never run in parallel, because they share `cli.py`, `newsfeed.py` and
`summarize.py`. The only work that runs alongside them is the read-only §5 investigation.

### Slice E task → model

The plan gives the full code for each task, so a task that only copies and tests that code goes
to the cheapest model. Tasks where pieces have to fit together get the standard model.

| Task | What | Touches | Implementer | Why |
|---|---|---|---|---|
| 1 | vocabularies + travelling archive (no `body`) | 2 new files | haiku | complete code, isolated |
| 2 | `archive.toml` → `ArchivePolicy` | 2 new files | haiku | complete code, isolated |
| 3 | prompt, contract, `parse_summary` | 1 new file | haiku | complete code; reviewer checks the 280-character refusal and the closed vocabularies |
| 4 | `RawArticle` fields + `summarize_store` | 2 files | sonnet | store rewrite, repair-then-refuse, empty body |
| 5 | `archive summarise` CLI + cost projection | 2 files | sonnet | exit codes, `--plumbing-check` refusal |
| 6 | `archive publish` | 3 files | haiku | complete code, mechanical |
| **7** | **`build_sim(news_feed=…)` seam + 7 guards, ONE commit** | `tradebot/app.py`, `tests/unit/` | **sonnet, reviewer opus** | the only change to `tradebot`; separation and money-path rules |
| 8 | `ArchiveNewsFeed`, coverage vs silence | 1 new file | sonnet | point-in-time correctness |
| 9 | clustering (annotates, never deletes) | 2 files | sonnet | similarity floor as a `Decimal`, corrections never collapsed |
| 10 | feed into `corpus build`, the §6.7 banners | 4 files | sonnet | the most connected task: identity, render, `analysis.py` |
| 11 | contamination tests, exit criterion, docs | tests + 3 docs | sonnet | proves the slice is done; edits CLAUDE.md |

### Checks between steps

- **Before Task 1:** the controller writes the preflight table (task pairs that share files or
  interfaces) to the ledger and rules on every conflict.
- **After each task:** task review, then up to 5 fix rounds, then a ledger line. The controller
  runs `.\decision_lab\check.ps1` in the background after Tasks 4, 7 and 10 (where risk is
  highest) and before the final review.
- **After Task 7:** `git diff --stat main -- tradebot/` shows only `tradebot/app.py` and `tests/`.
- **Before merge:** both gates green (`.\decision_lab\check.ps1` and `.\check.ps1`), final review
  clean, every `Ruling:` line reported to the operator. Merging to `main` waits for the
  operator's say-so.
