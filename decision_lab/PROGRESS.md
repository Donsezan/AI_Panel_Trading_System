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
| **E** — news archive | shock days measure the *news*, not just the price move | ◨ collection complete, full bodies; feed and summarising pending |

**Four slices of five.** Comparing configurations — the thing the tool was built for — now runs:
N candidates over one frozen corpus, ranked, with a pairwise agreement matrix and a per-candidate
seat breakdown. And it no longer runs *unchecked*: a sweep refuses until the seats have been
calibrated over nine pinned days.

Slice D is split into two passes. **Pass 1 — the three calibration scenarios and the §10.6 gate —
has shipped**, all nine tasks, merged to `main`. **Pass 2 — the dashboard and the notebook — has
shipped too, all ten tasks, and is merged to `main`.** E (news) is the only slice left, and its
**collection is now complete, with a full body on every row**:

| Store | Rows | Bodies | Median body | Coverage |
|---|---|---|---|---|
| `cryptoslate` (2025) | 4 869 | **100%** | 3 084 ch | 363/365 days of 2025 |
| `bitcoincom` (2025) | 9 615 | **100%** | 2 454 ch | **365/365 days of 2025** |
| `coindesk` (2024) | 5 687 | **100%** | 2 516 ch | 302/366 — 62 weekends + 2 holidays |

**The working year for news is 2025**, decided 2026-09-21. It is served by CryptoSlate and
Bitcoin.com over `data\history-2025`, and the choice follows from what the archive actually is:
both are full-text by default with **no credential at all**, between them they cover every day of
the year, and 14 484 articles is roughly 40 a day. CoinDesk 2024 is kept and is complete — but it
needed a signed-in session for its bodies, which makes it the harder one to reproduce, and its
2024 window is the *price* dataset's window rather than the news year.

The consequence to plan for: the corpus that news work is scored against must be built over
`data\history-2025`, which carries its own pinned day set, its own calibration and its own §10.6
gate. Corpus `8ac130d8…` is 2024 H1 and stays `NEWS-BLIND`.

What is still owed is the `tradebot` seam, `ArchiveNewsFeed`, and the summarising pass.

**The archive keeps full raw text, and that is now a stated requirement rather than an
implementation detail.** Decided 2026-09-17: `RawArticle` stores the publisher's whole title and
whole body, uncropped and unsummarised, and `summary` is filled later by a **separate** LLM pass
the operator runs against the stored bodies. So §6.5's summariser is *not* retired after all — the
earlier note below arguing the abstract makes it unnecessary is superseded on that point, though
its measurement stands.

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

**Pass 2 — read, edit, run** ✅ **merged to `main`**

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

## Slice E — news archive ◨ collection shipped

**Collection is built and green; the feed and the summarising pass are not.** The order was chosen
deliberately on 2026-09-16: collecting is the only half that can be done with no key at all, and an
archive nobody has collected yet is the thing every later half waits on.

- [x] `archive/` — the raw store, the CoinDesk listing and article parsers, the resumable walk
- [x] `archive build` on the CLI, and the §6.7 refusal that keeps an undateable row out
- [x] `archive backfill` — re-fetch the body of rows a metered crawl stored without one, and
      `auth.py`, which presents the operator's own subscription cookie
- [x] Run the backfill against CoinDesk 2024 with a session — **5 687/5 687, done 2026-09-19** in
      seven batches. Run it in batches, not one pass: the store is rewritten only at the *end* of
      an invocation, so a single long pass loses everything to one failure near the end.
- [x] `StoreLock` — one pass at a time over one store, exit 7
- [x] Month shards and `archive reshard` — 22–28 MB files down to ≤2.5 MB

### One file per publication month

`news-raw/<source>/YYYY-MM.jsonl`, migrated by `archive reshard` on 2026-09-19. One file per source
had reached 22–28 MB and `read_all` parsed all of it on every call — every backfill batch, every
`held` check, and every point-in-time read the replay will make. The biggest shard is now 2.48 MB.

- **The rule is unconditional: `published_at[:7]` is the filename, always.** No threshold, no
  "sparse months go elsewhere", no target year the store has to know. A reader *computes* the path
  rather than consulting a rule, which is what lets `ArchiveNewsFeed` ask by date and nothing else.
  The price is a tail of near-empty shards where a sitemap chunk straddled a year — 55 of
  CryptoSlate's 88 hold five rows or fewer — and it is paid deliberately. `published_at` and not
  `fetched_at`, because that is already the only thing that dates a row (§6.7); keying on the fetch
  would scatter one month across every shard the crawl happened to run in.
- **`rewrite` touches only the shards whose contents changed**, comparing rendered text before
  writing, so an unchanged month's mtime does not move and a backup sees the truth about what
  changed. A month that empties has its shard **deleted** — a stale one would be read straight back.
- **Migration is an explicit command, and the flat file is renamed, never deleted.** It survives as
  `<source>.jsonl.premigration`: collection took hours and CoinDesk's cannot be repeated without a
  signed-in session, so undoing a bad migration is a rename. `RawStore.open` **refuses** while a
  flat file is present, because shards written beside it would leave resume blind to every row it
  holds and the next crawl would re-ask the publisher for all of them. The three originals were
  deleted on 2026-09-21, once an audit had shown every row present in the shards — by the operator
  asking, never by the migration itself.

**A backfilled body ends with the publisher's related-article headlines, and that is left alone.**
Measured over the first 2 214: no body under 418 characters, **zero** carrying the disclosure
boilerplate (the `font-metadata` exclusion works), **zero** duplicate bodies, and 80% ending on
sentence punctuation. The other 20% end on a related-article title — "…JPMorgan Says", "…Could
Double in 2025" — or on the "In the Ether" section header, whose content is embedded media rather
than text. So nothing is truncated; a little extra is captured. It is **not** trimmed, because the
instruction is full raw text and over-capture is the recoverable direction while cropping is not.
The summarising pass reads these and is where the trailing headline should be handled, if anywhere.

**Two backfills over one store must never run at once**, and `StoreLock` is what stops them.
`RawStore.rewrite` replaces the store from rows read at the *start* of a pass, so a second
invocation reads the same pre-batch state and writes it back over the first's gains — hours of
collected bodies lost silently, with both passes reporting `gained: 400` and exiting 0. It is the
lost-update hazard the workspace lock exists for, one directory across; `archive` is deliberately
absent from `LOCKED` (a multi-hour crawl must not block every sweep), so that protection does not
reach here and the archive takes its own, scoped to the one store it rewrites. Refusing is
`jobs.Busy` → **exit 7**, the same code a second writer gets anywhere else in this tool.

- [x] Finish the two 2025 crawls — **done 2026-09-20**, both walked to 2025-12-31
- [ ] The `build_sim(news_feed=…)` seam — **the only `tradebot` change in the whole design**
- [ ] Its §2.3 guard tests and §16.2 contamination tests, in the *same commit* as the seam
- [ ] `ArchiveNewsFeed`, reading the archive point-in-time
- [ ] The summarising pass, which fills `RawArticle.summary` and needs the key — run as a
      separate process over the stored bodies, not inline with a crawl

### What collection looks like

```powershell
.venv\Scripts\python.exe -m decision_lab archive build --data data\history --year 2024
```

**2024 is complete.** Twelve listing pages, **5 688 articles listed and 5 687 collected** — the
single refusal carried no publication instant. Title on 100% of rows, the publisher's abstract on
99.8%, keywords on 96.5%, every month between 376 and 565 articles. With bodies backfilled it is
23 MB across twelve month shards under `data\history\news-raw\coindesk\`, which `data/` gitignores.

**Stop it whenever**: the store is append-only and keyed by canonical URL, so a restart asks the
publisher only for what is missing. A year runs at roughly 2.5 s per article.

Rules that are easy to get backwards:

- **The publication instant comes from the article, never from a date.** The listing column and the
  date in the URL both exist, they *disagree with each other* on about 1% of rows, and neither is a
  time of day. `published_at` is the `datePublished` in the page's own `ld+json`, precise to the
  millisecond; a page without one is **refused and counted**, never stored with a derived stamp.
  Deriving it would hand a whole day's headlines to that day's first cycle — on the 8h corpus, up
  to twenty-four hours of intra-day look-ahead wearing a well-formed timestamp (§6.7).
- **A refusal is a tally, not an exception.** One malformed page must not end a 2½-hour crawl.
  Transport failures are the opposite and are *not* caught: a `robots.txt` denial or a 403 raises
  `SourceDisallowedError` and the crawl stops, which is the right answer to being told to go away.
  What is already on disk survives, and the next run resumes from it.
- **The body is joined across *every* `document-body` block, not the longest one.** An ad slot or a
  premium component splits it, and the lede is in the first block — so "longest wins" silently
  drops the opening of the article. The boilerplate disclosure is the block carrying
  `font-metadata`, and it is the only one excluded. The test was mutation-checked against exactly
  this bug rather than trusted because it passed.
- **Only links whose path carries `/<section>/YYYY/MM/DD/<slug>` are rows.** The sign-in link sits
  in the same list with the same markup, and `robots.txt` disallows `/auth/` — so a parser loose
  enough to admit it would also make a request the publisher forbade. That date in the path is used
  *only* to tell a row from a non-row, never as a timestamp.
- **`archive build` is deliberately absent from `LOCKED`.** It writes beside the dataset rather
  than into the workspace, exactly as `dataset` does, and a multi-hour crawl holding the workspace
  lock would block every sweep and report for its whole run.
- **No `float`, including in the pause.** `test_discipline.py` walks this package too, so the delay
  is a `timedelta` and the flag is `--pause-ms` in integer milliseconds rather than `1.5` seconds.

### An anonymous crawl is metered at three articles — superseded for a signed-in one

Established 2026-09-16 with an instrumented crawl, not inferred. **CoinDesk meters the article body
at three per session, and one crawl is one session — so a full year yields three bodies, not a
percentage.** The first three requests return a ~1.71 MB page with the text, every one after
returns ~1.62 MB without it. The page announces it: `rw_remaining` counts `2 → 1 → 0` and
`rw_allowed` flips `true → false` at exactly the request where the body vanishes. Confirmed at
1 430 rows — the only articles with a body sat at fetch positions 0, 1 and 2. Every isolated probe
that "worked" had simply been handed a fresh allowance of three.

Getting around a meter means cycling clients or identities, which is paywall circumvention and is
**not** something this tool will do. Full text is a licensing question, not an engineering one.

### A signed-in session does lift the meter — and the cookie's own claims do not predict it

**Measured 2026-09-18, after a wrong prediction.** Reading the signed-in `COINDESK_SESSION` JWT
suggested the account could fetch nothing: it declares a three-per-month plan and reports that
allowance already spent. The five-request probe then returned **`gained: 5, still_bodyless: 0`** —
real article text on all five, from a 2024-12-30 window the `allowed` list does not name.

So **the JWT is a client-side hint, not the enforcement point.** Entitlement is re-evaluated
server-side against the auth0 session (`__session__0/1`, `auth_key`); `COINDESK_SESSION`'s
`articlesRead` and `allowed` describe what the *page* believed, and the API does not consult them.
The lesson is cheaper than the four hours it nearly cost in the other direction: **the probe exists
precisely because reading a credential is not the same as testing it**, and five requests settled
what a careful reading of the payload got backwards.

What the earlier per-session reading got right and wrong is now clear too. The nine bodies stored
by the anonymous crawls do cluster as 3 + 3 + 3 across three runs, so an *anonymous* identity is
metered at three. That is the free/anonymous path. A signed-in session is a different path and is
not bound by it.

The JWT, for the record, and as an example of what not to trust:

```json
{"plan": {"name": "3-plan", "limit": 3, "period": "months", "duration": 1, "rolling": false},
 "articlesRead": 3,
 "allowed": ["/markets/2025/12/30/silver-overtakes-bitcoin…", "…", "…"]}
```

with `isPremium: false` beside it in the same jar — and **every one of those claims failed to
predict the observed behaviour.** `limit: 3` did not bind, `articlesRead: 3` did not exhaust
anything, `allowed` did not restrict which paths were served, and `isPremium: false` did not
prevent full text arriving.

Two rules survive this, and one is new:

- **Cycling identities to collect three at a time is still the circumvention this tool will not
  do.** Nothing here changes that; the backfill presents one account's cookie, every request, and
  batching exists for crash-safety rather than to mint sessions.
- **`--authenticated` is worth trying before it is reasoned about.** The probe is five requests.
  A confident reading of a metering payload cost more thought than the measurement did, and was
  wrong.

**The machinery is built and correct either way.** Added 2026-09-17 —
`archive backfill --authenticated` sends one session cookie from one honest client, which is
licensed access rather than circumvention. Three rules in `archive/auth.py`, and the distinction
between them and a meter-defeating crawl is the whole point:

- **A session cookie, never a password.** We never hold the credential that could change the
  account, and — load-bearing — we never request `/auth/`, which CoinDesk's own `robots.txt`
  disallows. A crawl that logged *itself* in would have to disregard the very file `FeedFetcher`
  exists to honour, on its first act.
- **The `User-Agent` is unchanged.** `AuthenticatedFetcher` adds a cookie and nothing else. A
  subscription cookie is legitimate; a spoofed browser string is not, and
  `test_archive_auth.py` pins that structurally rather than leaving it to the reader.
- **Absent means refused, never anonymous**, before the transport is built. A quiet fallback would
  spend 5 678 requests to collect three bodies and then report that the subscription does not lift
  the meter — a wrong answer arrived at expensively. The cookie is environment-only
  (`DECISION_LAB_COINDESK_COOKIE`) and redactor-registered, never a CLI argument: shells keep
  history.

**`archive build` cannot do this job, which is why `backfill` is a second command.** Build is keyed
by canonical URL precisely so an interrupted crawl resumes — so every one of the 5 687 rows is
`held`, and a re-run collects nothing. The resume property and the repair are in direct opposition.
Four rules, each a way the obvious implementation loses data: only bodyless rows are requested; a
row that still arrives metered is kept exactly as it was (a smaller archive is not a better one);
only `body` and `fetched_at` move, so a publisher who edited the headline since cannot rewrite what
the archive recorded; and **`summary` survives**, because the summarising pass may already have
run. `RawStore.rewrite` replaces the file whole, through the same `os.replace` the registry and
the gate use — append-only is right for a crawl and cannot update a row.

**The meter does not touch anything else.** The `ld+json` record is served whole on metered pages,
and a live one-day crawl found, on 20 of 20 articles: title, canonical URL, `datePublished` to the
millisecond, `dateModified`, author, section, keywords, and the publisher's abstract. That is
§6.4's whole list, so the archive loses nothing it was specified to keep — and §6.4's
"the body is never written to disk" now holds by circumstance as well as by policy.

One consequence in the code: **`DEFAULT_BODY_RETRIES` is 0**. Past the third article a retry cannot
succeed and only triples the requests made to the publisher. The flag stays for a source that drops
a body transiently; this one does not.

**The panel never reads a body, in production or in replay — and the archive keeps one anyway.**
Those are two layers and the distinction is load-bearing. The live pipeline keeps title, a short
excerpt and a link (`news/normalize.py` says so as policy), and `NewsItem.view` renders that
excerpt as the seat-visible `summary`, capped at `DEFAULT_EXCERPT_CHARS = 280`; measured, the
CoinDesk abstracts average 148 characters and the 2025 sources' 134, so they already fill that slot
exactly. Feeding a *raw* body down that path would make the replay unfaithful — showing a 2024
decision more evidence than the live system structurally can, a milder cousin of the look-ahead
problem the §2.2 seam exists to prevent.

The archive stores the full body regardless, because it is the input to the summarising pass: that
pass turns ~2 900 characters into something excerpt-shaped, and `ArchiveNewsFeed` hands the panel
the short text like any other feed. So "keep everything" and "the seat reads 280 characters" are
both true, and neither is a compromise of the other.

### A transient failure is retried; a refusal is not

`VenueError` is a `RetryableError`, and the class **is** the handling instruction (CLAUDE.md). The
first full-year crawl did not honour that and died on a single timeout at its 1 430th row, three
quarters of an hour in. `collect` now absorbs transient transport failures within a budget
(`DEFAULT_TRANSPORT_RETRIES`, exponential backoff, honouring `RateLimitedError.retry_after_seconds`)
and still propagates `SourceDisallowedError` immediately — that one is `FailClosedError`, and being
told to go away is an answer rather than a blip. Nothing is lost either way: the store is
append-only, so a crawl that dies resumes from what it had.

### The summarizer may turn out to be unnecessary

Measured over the **complete 2024 year**, not a sample: **5 678 of 5 687 rows carry the
publisher's own `abstract`** (99.8%), at 93–297 characters against `DEFAULT_EXCERPT_CHARS = 280`,
and every stored row carries a precise `datePublished` — an undateable one is refused, so that is
true by construction. Both are stored on every row, beside the body.

A publisher's abstract is a *stronger* guarantee than §6.5's three-way closure, since it was
written at publication and cannot contain hindsight by construction — so it is the baseline any
summarising pass is measured against, and it is stored beside the body rather than instead of it.

**The conclusion this section originally drew — that the summariser is therefore unnecessary — is
superseded.** Decided 2026-09-17: the archive keeps the full body and a separate LLM pass fills
`RawArticle.summary` from it. The measurement above stands and the reasoning does not, because it
assumed the excerpt was the only thing the archive would ever need. `RawArticle.summary` is still
empty at collection; what changed is that something is now expected to fill it.

The earlier caveat that this was a June–July sample is **closed**: the full year measured 99.8%,
so the nine rows without an abstract are a rounding error rather than a pattern. The crawl counts
`refused` per run either way, so thin coverage on a future source shows up as a number rather than
as a silent gap.

### 2025: two full-text sources, and a price dataset to match

CoinDesk's meter made a full-text 2024 archive impossible, so 2025 was collected from two sources
that serve bodies to anyone: **CryptoSlate** and **Bitcoin.com News**. Both are Yoast-style, so
`sites.py` drives them from one `SiteProfile` — a sitemap index, a body container, and nothing
else differs.

`data\history-2025\` holds its own price dataset: BTC/USDT and ETH/USDT, 1h, **8 760 bars each**,
2025-01-01 → 2026-01-01. It is a **separate directory on purpose**. Recording into `data\history`
would have rewritten `dataset.json` and moved `dataset_digest`, which invalidates corpus
`8ac130d8…`, the pinned day set and every §10.6 gate record keyed on it.

**Both crawls are now complete** (2026-09-20), and for a while neither was — the wording here once
implied otherwise. Measured 2026-09-17 before resuming: CryptoSlate held 1 230 rows covering
**2025-01-01 → 03-03** and Bitcoin.com 1 055 covering **01-04 → 02-19 and then 06-16 → 06-20**,
with a **117-day hole** between. Those were stopped processes, not sparse publishers — the lesson
being that a background crawl looks identical to a finished one unless you check the last day it
reached.

**The 2025 dataset is now verified and its day set pinned** (2026-09-22). `dataset verify` found
both 1h series **8 760/8 760 bars with zero holes**, so no `--repair` was needed, and `dataset days`
drew the nine days off `binance:BTC/USDT` at the default seed `20260823`:

```
dataset_digest  09a89f7e1e36e441470fcb11ae603d23
dayset_digest   cf2a3eabc5bc14f547e18f3afd4339f3
  NORMAL        2025-06-09  2025-08-07  2025-08-13
  SHOCK_DOWN    2025-01-09  2025-02-04  2025-11-04
  SHOCK_UP      2025-03-04  2025-03-07  2025-12-09
```

Both files sit beside the CSVs as `decision_lab-coverage.json` and
`decision_lab-calibration-days.json`, exactly as `data\history` carries them. Two of the three
§10.6 gate key components have therefore changed, so **nothing calibrated against 2024 transfers**:
2025 needs its own `calibrate normal` and `calibrate shock` over these nine days, from nothing.

**These two stores still have no corpus.** `data\history` is **2024-01-01 → 06-30** and corpus
`8ac130d8…` is exactly that window at 8h (540 cycles), so the year that has a corpus is 2024 while
the news year is 2025. What remains is the reference pass itself — and at 8h over a full year that
is **~1 095 cycles**, the same size as the two dead partial corpora, carrying the §5 intra-cycle
race with no `--seed` to pin it (`corpus build` has none; only `dataset days` and `sweep` do). That
is a decision, not a formality — see *What comes next*.

Rules that are easy to get backwards, all of them learned the expensive way:

- **`lastmod` lies at two levels, and the second is the one that bites.** That a URL's `lastmod`
  is not its publication date was known, and is why rows are dated only by their own `ld+json`.
  What was missed: a *chunk's* `lastmod` in the index is the **maximum over its articles**, so
  editing one 2017 post re-stamps its whole chunk with today's date. Selecting chunks by it
  collected articles published from **2017-10-17** onward under the banner of a 2025 crawl. A chunk
  is now judged by what is *inside* it — the share of its URLs carrying the year,
  `MIN_YEAR_PERCENT`.
- **The share test does not land the boundary chunk, and believing it did cost three days and a
  December.** That claim stood here until 2026-09-21, when an audit found Bitcoin.com missing
  2025-01-01..03 entirely. The cause, read off the live index: `post-sitemap34.xml` carried **46
  URLs from 2025 against 954 from 2024 — 4.6%**, under the threshold, so it was skipped — and it
  held both the year's first three days *and* all of December 2024 (4 rows where CryptoSlate had
  275). A paginated sitemap is chronological, so the boundary chunk is mostly *last* year by
  construction; the share test lands it only when the year happens to begin early in it, which is
  luck. CryptoSlate drew 33% and passed, Bitcoin.com drew 4.6% and did not.
  **A chunk beside the qualifying run is now opened when it holds *any* URL in the target year.**
  Adjacency alone is not enough — that would walk a stale 2017 chunk that merely sits beside the
  run, and three existing tests caught exactly that. Containing the year is what separates a
  boundary chunk from a neighbour. Re-running Bitcoin.com collected 938 articles and took it to
  **365/365 days**, with December 2024 going from 4 rows to 599.
- **That share selects chunks; it never filters rows.** Every URL in an opened chunk is fetched and
  kept, including the ones from the neighbouring year. Filtering rows by `lastmod` would drop
  articles published in the year and edited afterwards, and **under-collection is the one failure a
  one-shot archive cannot repair later**. A row from an adjacent year is archive material and the
  request is already paid for.
- **The end of a paginated archive is a 404, and `FeedFetcher` calls every 4xx a `ConfigError`.**
  Right for a mistyped feed URL, wrong for the sentinel request that discovers the last page.
  Unhandled it killed a *complete* CoinDesk 2024 crawl on its **5 687th row**, after every page
  that existed had already been read. The listing walk now treats it as the end and says so.
- **Neither new source publishes `articleBody`**, so the body is scoped to a per-site container
  (`post-box__content-flow`, `article__body`). Both publish **`wordCount`**, which is what makes
  extraction checkable rather than plausible: the scoped bodies measure **0.92–1.15×** the declared
  count against the live sites.
- **CryptoSlate hides two fields where the schema does not.** Its `ld+json` is wrapped in Yoast's
  `@graph`, so a top-level scan finds nothing and the source refuses **100%** of its rows — while a
  flat-ld+json fixture passes. And it carries no `description` on the article node: the abstract
  lives in `<meta name="description">`, and `articleSection` is a **list** that `str()` would store
  as a Python repr.

**A background crawl is suspended when the session ends, and wall-clock rate is then meaningless.**
Both 2025 stores showed a 429-minute gap and an apparent 239 s/row; the real rate, measured over the
minutes the processes were actually running, is **~2.5 s/row** — about 24 rows a minute, as designed.
Divide by active time, never by elapsed time.

### What the source survey settled

Detail lives in the spec at §6.3.1; the short version, so it is not re-derived:

- **CoinDesk's API is gone.** Its free tier was retired on **21 May 2026** — confirmed against
  CoinDesk's own notice, not a secondary report — and every remaining plan is sales-quoted. The
  backend §6.3 called *"preferred, and the default"* is unavailable to us.
- **Alpaca's news endpoint replaces it.** `GET https://data.alpaca.markets/v1beta1/news`,
  Benzinga-sourced: history to 2015, `start`/`end`/`symbols` range queries, 200 req/min on the free
  plan a paper account already carries, and an RFC-3339 `created_at` per article. It answers `401`
  without a key, which is the only thing now blocking the slice.
- **That collapses most of §6.** An API publishing its own `summary` *and* its own ingestion
  timestamp deletes work rather than moving it: the summarizer goes (~2 800 LLM calls over this
  window, about four times a full calibration), and with it the `archive.toml` summarizer binding,
  its prompt digest inside `archive_digest`, both the `SUMMARIZED NEWS` and `RECONSTRUCTED NEWS`
  banners, §6.4's whole robots/crawl apparatus, §6.2's argument for needing a carve-out from the
  bot's "never scraping" policy, and one of the two contamination channels §16.2 tests. The slice
  is smaller than it was specced.
- **One hazard survives whichever source wins**, so it is recorded at §6.7 rather than beside the
  source: a **date-only `published_at`** plus a declared lag hands a whole day's headlines to that
  day's first cycle. On the 8h corpus that is up to twenty-four hours of intra-day look-ahead
  wearing a well-formed timestamp — the exact failure the §2.2 seam exists to prevent, arriving
  from inside the archive. It binds any crawl-based backend.
- **Also surveyed and rejected:** GDELT DOC 2.0 (free and historical, but one request per five
  seconds and a search API rather than a bulk archive), cryptocurrency.cv (advertises a no-key
  archive, answers `403 BOT_BLOCKED`, and republishes an aggregator), CryptoPanic (archive is paid).
  The CoinDesk sitemap path stays viable as the declared fallback — live, paginated, and permitted
  by `robots.txt`.

**Two things a key settles, and nothing else can:** how much of Benzinga's ~130 articles a day
carries a `BTCUSD`/`ETHUSD` tag in 2024 — thin crypto coverage would make the shock-day news
evidence sparse, which is a finding about the experiment rather than a defect, and worth having
*before* building on it — and whether its storage terms permit keeping what §6.4 keeps. The posture
is the bot's existing one (title, URL, timestamps, short excerpt, never a body), but it is unread.

---

## What you can run today

```powershell
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history   # --repair re-asks the venue
.venv\Scripts\python.exe -m decision_lab dataset days   --data data\history
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history-2025   # clean, 2026-09-22
.venv\Scripts\python.exe -m decision_lab dataset days   --data data\history-2025   # cf2a3eab…
.venv\Scripts\python.exe -m decision_lab archive build  --data data\history --year 2024  # resumable
.venv\Scripts\python.exe -m decision_lab archive build  --data data\history-2025 --source cryptoslate --year 2025
.venv\Scripts\python.exe -m decision_lab archive build  --data data\history-2025 --source bitcoincom  --year 2025
$env:DECISION_LAB_COINDESK_COOKIE = "<the whole Cookie: header from a signed-in browser>"
.venv\Scripts\python.exe -m decision_lab archive backfill --data data\history --authenticated --limit 5
.venv\Scripts\python.exe -m decision_lab archive backfill --data data\history --authenticated --limit 1000
.venv\Scripts\python.exe -m decision_lab archive reshard  --data data\history --source coindesk
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

The tuning dashboard serves as well. Its token is its own — never the bot's — and it refuses to
start without one:

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
- **The two dead partial corpora are gone** (deleted 2026-09-21): `61721dba…` at 67/1080 cycles
  and `d7c390c6…` at 118/1080, the latter still holding `-wal`/`-shm` from the pass that died. A
  corpus is reused at its identity and never rebuilt, so a partial one persists until the
  directory is removed — which is the only way to retry that configuration.
- **`STUB_SEED = 2024` is pinned** in `test_slice_b_end_to_end.py` because a reference pass can die
  on [KNOWN_GAPS](../docs/KNOWN_GAPS.md) §5. Delete the pin when §5 closes.
- **The gate is keyed on `(dataset_digest, matrix_digest, dayset_digest)`, deliberately not on
  `corpus_id`.** Every condition it checks is a property of the candidates, the seats and the
  days, so keying on the corpus would force a re-calibration each time the long run varied
  `--every` — the one axis that run exists to vary. The cost of the key it *does* have: one
  edited prompt mints a new `matrix_digest`, so it is a calibration again. That is nine days, and
  it is the price of the guarantee.
- **Nothing has been calibrated against a real panel yet, and the workspace is emptier than this
  file used to claim.** Re-checked 2026-09-21: `decision_lab/workspace/` holds **one** corpus
  directory — `8ac130d8…`, the complete 540/540 pass — and **nothing else**: no `registry.jsonl`,
  no `gates/`. No sweep has ever run outside
  the test suite, so there is not even a stub gate record to disambiguate. A stub matrix could never
  satisfy a real matrix's gate in any case — the bindings feed `panel_digest` → `matrix_digest`,
  which is a third of the key — so the first real `sweep` needs its own `calibrate normal` and
  `calibrate shock` first, from nothing.
- **§9.7's `single_round` banner almost never fires**, because `_fold` zeroes swing rate and
  marginal contribution on the round-0 row while `rounds_are_identical` compares whole rows — so a
  round-0 row can never equal its final twin for any seat that ever swung. Pre-existing and
  presentation-only; found by pass 2's fix wave and deliberately left alone. Detail below.
- **`calibrate long` has no `--budget` and no mid-run ceiling.** Found while planning pass 2:
  scenario 3 drives `BacktestHarness` directly, with no engine seam to meter, so the only ceiling
  is the operator stopping it — and stopping it means deleting that run's directory before it can
  run again, because the database it leaves behind is the record of why the pass failed. The
  dashboard's form says so rather than implying a ceiling that does not exist; the CLI says
  nothing, which is a gap worth closing.

## How pass 2 was closed out

`slice-d-pass-2-dashboard` was merged to `main` with an explicit merge commit, matching how slices
A, C and D-pass-1 landed. It branched from `main` at `4ef58d8` plus the spec revision `e578e24` and
the plan `ab1365c`, and carried twenty-two commits. `git diff --stat main -- tradebot/` is empty,
which is the slice's exit criterion, and `decision_lab/candidates.py` is byte-identical to the
merge base — so nothing here renumbered an experiment or invalidated a stored §10.6 verdict.

- **All ten tasks complete and independently reviewed**, each with its fix rounds closed. Task 8's
  review found 2 Critical and 2 Important, all four fixed in `5918a91`. Task 9's found 0 Critical
  and 2 Important, both fixed in `0737851`. Task 10 — the notebook, the exit criterion and the
  docs — landed in `177dd75`.
- **The whole-branch review returned 0 Critical, 5 Important.** Two further items were ruled in,
  making seven, and all seven landed as one fix wave in `4d2f525`. What it changed: the slice's own
  exit-criterion test could not tell a *ranked* candidate from one merely named in the **Not
  measured** list; the round-0 test passed on exactly the page state that suppresses round-0 rows;
  `jobs.py` had neither Task 8's unreadable-file guard nor the rename-atomic write the package's
  three other writers have; the `dashboard` CLI command had no tests at all, including nothing
  pinning its deliberate absence from `LOCKED`; `seats.rounds_are_identical` keyed across regimes
  and so could hide round-0 rows on a false positive; and the seat-set editor rendered no control
  for `SeatConfig.instruction`, silently deleting one on save.
- **The scoped re-review of that wave verdicted all seven ADDRESSED, with no new breakage.** It
  reproduced the claims rather than accepting them — running Jinja's installed `do_urlencode` to
  confirm the exit criterion asserts on markup that actually exists, and confirming
  `rounds_are_identical` has exactly two callers, neither feeding a digest or a gate key.
- **Both authoritative gates green at `1f16908`**, the tree that was merged:
  `.\decision_lab\check.ps1` — **826 passed**, mypy clean over 82 source files — and the root
  `.\check.ps1` — **2848 passed**, all seventeen coverage gates met.

**Three assertions on this branch were found to be vacuous and fixed** — `assert "configs" in
response.text` (satisfied by `name="configs"` on three always-rendered forms), `assert
rows[0].candidate_id in listing` (a sweep row carries no `candidate_id`, so this was `assert "" in
listing`), and the exit criterion's own. Every one was a substring match against a whole rendered
page. The defence, applied throughout the fix wave: **when asserting that a page shows a derived
value, assert on the markup only the deriving branch emits** — `href="/runs/…/seats/…"`,
`<td>round 0</td>` — never on the bare value.

### One thing the fix wave found and deliberately left alone

`_fold` reports swing rate and marginal contribution as **final-round concepts only**, zeroing
them on the round-0 row. `rounds_are_identical` compares whole rows, so a round-0 row can never
equal its final twin for any seat that ever swung — which means §9.7's "say so rather than
printing the same numbers twice" banner almost never fires, `max_rounds = 1` included. Verified on
the stub sweep: neither expanded candidate reaches it, under the old keying or the new. That is
pre-existing, presentation-only, and outside the seven items; it is recorded here rather than
fixed, and the dashboard's suppression branch now has a test of its own either way.

### Where the run's own record lives

Pass 2 was driven by `superpowers:subagent-driven-development`. Its ledger —
`.superpowers/sdd/2026-09-06-decision-lab-slice-d-pass-2-dashboard/progress.md` — holds the
pre-flight conflict scan, all twenty-three rulings with what each costs if wrong, every task
review's findings, and a `Task N: complete` line per task. It is git-ignored scratch, so it exists
only on the machine that ran it; `git log` is the durable record. **Trust either over recollection.**

### What went wrong during the run, so the next one can avoid it

None of it was in the work — all of it was operational, and between them these account for every
interruption a nine-session run had.

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

### What comes next

1. **Finish [KNOWN_GAPS](../docs/KNOWN_GAPS.md) §5 — the half a replay actually hits.** The
   *between-cycle* half closed on 2026-09-21: `Supervisor._poll_orders` polls on the resync sweep,
   and `ManualCloser` polls its own close, so a venue-held stop that fires overnight is booked
   within thirty seconds instead of waiting for some basket to trade. **That does nothing for this
   tool**, and the distinction is worth keeping straight: `BacktestHarness._replay` drives
   `worker.cycle()` directly and polls only *after* it, so a replay has no sweep between two
   cycles. The race that kills a reference pass is inside one — freezing the snapshot reads
   prices, which hands the bar to the simulated venue and can match a resting stop right then, and
   nothing polls between that and the panel sizing its SELL against a ledger that has not booked
   it. `sell of … exceeds holding …`, three seeds in seven. Closing it means a poll between the
   snapshot and the decision, which changes the live money path's ordering and its per-cycle venue
   cost — a decision of its own. Until it is taken, every `corpus build` and every `calibrate long`
   still carries the risk, `calibrate long` still has neither a budget nor a resume, and the
   `STUB_SEED = 2024` pin in `test_slice_b_end_to_end.py` stays.
2. **Calibrate against a real panel and then sweep it**, once `OPENROUTER_API_KEY` exists. The cost
   projection on the calibration page is what tells you what the sweep after it will cost — the
   whole point of running the nine days first. **Verify the model ids first:** the three `:free`
   slots in `decision/presets.py` are 2024-era and unchecked against openrouter.ai/models, and with
   `on_fallback = "halt"` and the gemini/lmstudio fallbacks equally unreachable, a retired slot
   becomes an abstention — and a seat that never answered on its primary **fails the §10.6 gate**.
   That would burn the nine days for a reason having nothing to do with the panel's judgement.
3. **Slice E's remaining half. Collection is done** — 20 171 articles, every one with a body, and
   2025 covered every day. What is left, in order:
   - **A corpus over `data\history-2025`**, because the news year is 2025 and corpus `8ac130d8…`
     is 2024 H1. Its two prerequisites are **done** (2026-09-22): the dataset verified clean at
     8 760/8 760 bars per series and the day set is pinned at `cf2a3eab…`. What is left is the
     reference pass itself, which carries its own §10.6 gate and needs no key — but at 8h over a
     full year it is **~1 095 cycles**, unseeded, and both prior passes of that size died short.
     Weigh closing §5's intra-cycle half first.
   - **Then the `build_sim(news_feed=…)` seam and `ArchiveNewsFeed`.** The seam is the only part of
     this design that touches `tradebot` at all, and it lands with its §2.3 guard tests in one
     commit, never two. `RawStore.read_months(start, end)` is what it should read: sharding means
     a cycle deciding on 2025-03-15 parses one ~1.5 MB file rather than the whole year.
   - **The summarising pass is the operator's own separate process** over the stored bodies, and is
     the only piece wanting an LLM key.

Pass 1 was written across two sessions and **carries no independent task review for tasks 4–9** —
the reviewing agents in session 1 stalled, and session 2 was executed directly. Its ledger is
`.superpowers/sdd/2026-09-05-decision-lab-slice-d-calibration/progress.md`; the diffs worth a
second pair of eyes are `2554c00..8408d86`.
