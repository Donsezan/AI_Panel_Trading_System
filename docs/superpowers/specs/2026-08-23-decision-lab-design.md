# `decision_lab` — a fine-tuning instrument for the panel's decision logic

**Status:** design, approved in brainstorming 2026-08-23; revised 2026-08-23 with the news
archive (§6), per-seat scoring and its dashboard (§9.7, §12), and the three calibration
scenarios (§10).
**Scope:** a standalone research tool that scores the bot's decision-making over recorded
history, across combinations of seats and prompts, split by market regime — and calibrates a
chosen setup over a normal day, a shock in each direction, and a six-month horizon with a
starting balance.

---

## 1. Purpose

The bot decides. Nothing measures whether it decided *well*.

`report promotion` counts incidents and cycles; `report shadow` compares exactly two panels;
`backtest run` proves the plumbing survives a long horizon and says so loudly on every page. None
of them answers the question an operator tuning seats and prompts actually has: **given this
evidence, was BUY the right call — and is a different panel right more often?**

`decision_lab` answers it. It replays recorded market history through the bot's own decision path,
runs a matrix of candidate panel configurations over one identical corpus of decision contexts,
and scores every resulting decision against what the market did next — reported separately for
ordinary volatility, for rallies, and for crashes.

It then answers the second question, which is operational rather than statistical: **of the setups
that decide well, which is most efficient to run?** §10's three calibration scenarios put a setup
through a normal day, a shock in each direction, and a six-month horizon with a real starting
balance; §11 keeps every result, so two setups are compared rather than remembered.

### 1.1 What it is not

- **Not alpha evidence.** Every model in `validation/cutoffs.py` was trained on this period. The
  contamination banner is on every report, unconditionally, exactly as `BacktestHarness` does it.
- **Not a promotion gate.** It has no authority over anything. `validation/promotion.py` remains
  the only thing that answers "may this be promoted", and it reads the production log.
- **Not part of the bot.** See §2.
- **Not a strategy search.** It compares configurations an operator wrote. It does not generate
  them, optimise them, or hill-climb. A tool that searched would overfit six months of memorised
  prices and produce a number nobody should act on.

---

## 2. The separation contract

`decision_lab` lives in its own top-level folder and **`tradebot` knows nothing about it**. No
config document, no setting, no `ConfigKind`, no CLI subcommand, no import.

```
AI_Panel_Trading_System\
  tradebot\                 <- the bot. Untouched except for §2.2.
  decision_lab\
    __main__.py             its own CLI entry point
    dataset.py              gap audit and repair
    calibration_days.py     the pinned day set: selection, storage, reuse
    corpus.py               snapshot corpus build / load
    archive\
      source.py             the ArchiveSource protocol
      api.py                CoinDesk Data / CryptoCompare News backend
      sitemap.py            archive-sitemap crawl backend, over FeedFetcher
      summarize.py          the compressor: one LLM pass per article, cached
      feed.py               ArchiveNewsFeed — a non-fetching NewsFeed over the archive
    regimes.py              NORMAL / SHOCK_UP / SHOCK_DOWN and named event windows
    candidates.py           sweep matrix -> PanelConfig -> Basket
    sweep.py                N candidates over one corpus; cache, budget, resume
    scoring.py              ATR-band verdict, oracle regret, per-seat scoring
    calibrate.py            §10 scenarios 1, 2 and 3
    registry.py             §11 the results registry
    render.py               Markdown report
    dashboard\              §12 its own read-only ASGI app
      app.py  routes.py  templates\  static\
    config\
      sweep.toml            the candidate matrix
      regimes.toml          named event windows
      archive.toml          archive source + summarizer binding
    tests\
    check.ps1               its own ruff / mypy / pytest run
    notebooks\tuning.ipynb
    workspace\              scratch databases, caches, results, registry   (gitignored)
```

### 2.1 What the separation buys, and how it is enforced

| Concern | Mechanism | Cost of the separation |
|---|---|---|
| Bot config untouched | matrices are TOML files in `decision_lab/config/`, never `ConfigStore` documents | none |
| Bot CLI untouched | `python -m decision_lab …`; `tradebot/__main__.py` is not edited | none |
| Bot database untouched | every pass opens `decision_lab/workspace/<id>/*.db` via `build_sim(db_path=…)` | none |
| Bot dashboard untouched | `decision_lab/dashboard/` is its own ASGI app, own port, own token env var | none — it *reuses* `tradebot.dashboard.auth`, which is a one-way import |
| Bot packaging untouched | matrices are TOML, read with stdlib `tomllib`; **no new dependency** | none |
| Bot wheel untouched | `packages = ["tradebot"]` already excludes siblings | none |
| Bot gates untouched | `mypy tradebot`, `testpaths = ["tests"]`, `coverage source = ["tradebot"]` all name the bot | the tool needs its own `check.ps1` |
| Lint and format | root `ruff format .` / `ruff check .` already walk the repo root | none — free coverage |

The import direction is one-way and **asserted, not intended**:
`decision_lab/tests/test_separation.py` walks every module under `tradebot/` and fails if any of
them names `decision_lab`. This is the same class of structural guard as
`test_money_discipline.py` and `test_dashboard_chart.py` — a boundary CI can prove.

### 2.2 The one seam this design requires

`build_sim` gains one optional parameter:

```python
async def build_sim(
    *,
    ...
    news_feed: NewsFeed | None = None,   # NEW
) -> Application:
```

When set, `_assemble` uses it instead of calling `build_news_hub`.

This remains the **only** change to `tradebot` in the whole design. §10's six-month scenario needs
no seam at all: `build_sim` already takes `start_equity: Decimal = Decimal(10_000)` and threads it
to `SimBroker.balances`, and `risk.aggregate.aggregate` already answers what a portfolio is worth.

**Why it is necessary.** `NewsHub.snapshot_news` calls `refresh()` on every invocation. Wired into
a replay under a `ManualClock`, it would fetch *today's* RSS and store each item with
`observed_at = <the replayed instant>`. Since `NewsHub.select` filters on `observed_at` — correctly,
per `interfaces/news.py` — a 2026 headline would be served into a 2024 decision as though it had
been known at the time. That is the look-ahead bug DESIGN [L12] exists to prevent, and no amount of
care inside `decision_lab` can prevent it from outside.

**Why it does not violate the separation.** The parameter is typed against the existing `NewsFeed`
protocol and is a peer of `market_data: MarketDataProvider | None` and
`catalogue: InstrumentCatalogue | None`, which already exist for exactly this reason: a replay
substitutes its own providers. `tradebot` still names nothing in `decision_lab` and still knows
nothing about it. It is a generic injection seam, not a hook.

**If the seam is refused**, the fallback is a news-blind corpus: `news_sources=()`, every snapshot
records "no sources configured", and every report carries the `NEWS-BLIND RUN` banner. The shock
scenarios then measure the panel's reaction to a violent price move rather than to the reporting of
an event — a narrower experiment, honestly labelled.

### 2.3 The seam is sim-only, and CI proves it

`build_sim`, `build_paper` and `build_live` all funnel into one `_assemble`. An injected
`NewsFeed` that reached live would be an archive of stale — and, since §6.5, model-summarised —
headlines feeding a panel whose orders reach a real venue. That is the single worst outcome in
this design, so it is closed twice and asserted three ways.

**Closed structurally.** The parameter exists on `build_sim` only. `build_paper` and `build_live`
do not accept it, so there is no argument to pass. `build(mode, **kwargs)` dispatches to those
functions, so `build(Mode.LIVE, news_feed=...)` fails on an unexpected keyword rather than being
quietly accepted.

**Closed defensively.** `_assemble` refuses outright:

```python
if news_feed is not None and mode is not Mode.SIM:
    raise ConfigError(
        f"a substituted news feed is simulation-only; {mode.value} must read live sources. "
        "An archived feed decides on news that is not current, and in paper it would "
        "contaminate the evidence report promotion reads."
    )
```

Belt and braces on purpose: the structural closure depends on nobody ever adding the parameter to
`build_paper` for convenience, and this refusal survives that.

Paper is refused alongside live, deliberately. A paper soak is the evidence `report promotion`
reads (ADR 0016, ADR 0020); a soak deciding on archived news is not a soak of the system that
would trade.

**Where these tests live — and why it is not in the tool's suite.** The guards belong in
`tests/`, the *bot's* suite, run by the root `.\check.ps1` and by CI. Placed in
`decision_lab/tests/` the bot could break its own seam and its own gate would not notice, which is
precisely the failure this section exists to prevent. They stay inside the separation contract
because they name only `tradebot` symbols plus a three-line local stub satisfying the `NewsFeed`
protocol — nothing imports `decision_lab`, so `test_separation.py` still passes.

| # | Test | Asserts |
|---|---|---|
| 1 | `build_paper` and `build_live` signatures carry no `news_feed` | the structural closure, and fails loudly if a future refactor adds one |
| 2 | `_assemble(mode=LIVE, news_feed=stub)` raises `ConfigError` naming the mode | the defensive closure |
| 3 | same for `mode=PAPER` | the soak's evidence stays real |
| 4 | `build(Mode.LIVE, news_feed=...)` raises | the dispatch door is shut too |
| 5 | `build_sim(news_sources=("cointelegraph",))` with **no** `news_feed` still wires a `NewsHub` | the default path is byte-for-byte the old behaviour |
| 6 | `build_paper` / `build_live` with sources still wire a `NewsHub` | live and paper news is unchanged by this design |
| 7 | `build_sim(news_feed=stub)` wires exactly that object, and `build_news_hub` is never called | the seam does what it says |

Tests 5 and 6 are the regression net: they pin the behaviour that exists today, so the seam cannot
silently change what live and paper do. The existing live-wiring scenario tests are unmodified and
keep passing, because the parameter defaults to `None`.

### 2.4 Reused, never reimplemented

This is the alignment requirement. Nothing about how the bot decides is restated here.

| Concern | Imported from `tradebot` |
|---|---|
| Snapshot construction | `control.context_builder.ContextBuilder` |
| The cycle | `control.supervisor.BasketWorker.cycle`, driven by `validation.backtest.BacktestHarness` |
| Panel deliberation | `decision.engine.DecisionEngine.deliberate` |
| Debate, consensus, fallback chains | `decision.protocols`, `decision.consensus`, `decision.seat` |
| Counterfactual consensus (§9.7 swing rate) | `decision.consensus.reach_consensus`, replayed over recorded votes |
| Prompt rendering | `decision.prompts` |
| Configuration validity | `core.config.Basket`, `PanelConfig`, `SeatConfig` |
| Prices and venue rules | `marketdata.recorder.ReplayDataset`, `app.dataset_catalogue` |
| Gap detection | `core.market.CandleSeries.gaps` |
| ATR | the `IndicatorReading` `indicators.library.REGISTRY["ATR"]` already wrote into the snapshot — read, never recomputed |
| Wiring, and the starting balance | `app.build_sim` (incl. `start_equity`), `app.dataset_basket`, `app.select_panel` |
| Portfolio value and profit (§10.4) | `risk.aggregate.aggregate` — the ADR 0027 function, never a hand-rolled sum |
| Realized round trips and incidents | `validation.evidence.Evidence` |
| News relevance, storage, normalisation | `news.relevance.KeywordRelevanceFilter`, `news.store.NewsStore`, `news.normalize` |
| Polite, robots-respecting HTTP (§6.4) | `news.http.FeedFetcher` |
| LLM access for the summarizer (§6.5) | `decision.providers` — the same three adapters the seats use |
| Dashboard auth (§12) | `dashboard.auth.Session`, `SessionMiddleware`, `assert_bind_allowed` |
| Money | `core.money` — `Decimal` throughout, no exceptions |
| Time | `core.clock.ManualClock` |

---

## 3. Architecture — four stages

```
STAGE 0   dataset integrity                          decision_lab/dataset.py
  audit every series with CandleSeries.gaps
  re-ask the venue for each hole; patch what it has
  record what it never published as a known hole
  select and PIN the calibration day set             calibration_days.py
        |
        v
STAGE 1a  the corpus  (deterministic; 0 or free LLM calls)     corpus.py
  one reference pass through BacktestHarness
  every cycle appends SNAPSHOT_FROZEN
  corpus := store.read_types(SNAPSHOT_FROZEN)
        |
        v
STAGE 1b  the sweep  (the LLM cost)                            sweep.py
  N candidates x sampled corpus entries
  DecisionEngine.deliberate(snapshot, candidate_basket)
  cached, budgeted, resumable
        |
        v
          scoring  (deterministic)                             scoring.py
  ATR-band verdict + oracle regret, per regime
  per-seat vote scoring, swing rate, marginal contribution
        |
        v
STAGE C   calibration  (§10)                                   calibrate.py
  1. normal days      snapshot-scored, on the pinned days
  2. shock days       snapshot-scored, up and down kept apart
  3. long exposure    full loop, own ledger, start_equity, cadence under test
        |
        v
          the registry, then the dashboard           registry.py, dashboard\
```

The separation of 1a from 1b is the design's load-bearing decision, and it is ADR 0018's principle
generalised from one challenger to N: **every candidate is judged on the same frozen evidence, so a
difference in score is a difference in reasoning rather than a difference in luck.** Two candidates
run through their own full loops would hold different positions from cycle two onward and be
compared across two different markets — which six months cannot tell apart.

The same principle is why §10's calibration days are **pinned rather than re-drawn** (§4.5): two
setups compared over two different sets of days are not compared at all.

It does **not** reduce LLM spend. It removes path divergence. Spend is controlled in §7.5 and
projected in §10.6.

---

## 4. Stage 0 — dataset integrity and the pinned day set

### 4.1 The defect this addresses

`marketdata.recorder.record` writes whatever `_page` returned and never audits completeness.
`_page` walks backwards a page at a time; a dropped page leaves a silent hole. Meanwhile
`CandleSeries.gaps` already exists and is never consulted at dataset level.

A hole matters here more than it does in a backtest, because ATR is both the panel's volatility
evidence *and* the denominator of the scoring band. A band computed across a hole is a wrong band,
and every verdict it produces is wrong while looking right.

### 4.2 Two kinds of hole

- **Fetch gap** — bars the venue *has* and our paging missed. **Repairable.**
- **Venue gap** — bars never published: a halt, an outage, maintenance. **Not repairable.**
  Interpolating one is forbidden (DESIGN §6.2, `CandleSeries.gaps`): a fabricated bar feeds a
  fabricated ATR.

### 4.3 The audit

`python -m decision_lab dataset verify --data <dir> [--repair]`

1. Load with `ReplayDataset.load`; for every series compute `CandleSeries.gaps`.
2. For each hole, re-ask the venue for that exact range via `binance_spot_history` — public REST,
   read-only, no key.
3. Venue serves the bars → **fetch gap** → append to the CSV, re-sort, re-verify.
4. Venue serves nothing → **venue gap** → record as a known hole.
5. Write `decision_lab-coverage.json` beside the dataset. `expected` is the bar count the
   venue's epoch-aligned grid implies over the series' own covered window — first `open_time`
   to last `close_time` — never over the manifest's *requested* window, which may legitimately
   be wider than what the venue had.

Repair is **in place** on the CSVs — a strict correction in the same format, so `ReplayDataset.load`
reads it unchanged and the bot's own backtests benefit too. `dataset.json` is **not** touched:
`DatasetManifest` is a `tradebot` model and editing it would be a bot change. The audit lives in the
sidecar instead.

```json
{
  "audited_at": "2026-08-23T09:14:02Z",
  "series": {
    "binance:BTC/USDT|1h": {
      "expected": 4380, "present": 4380, "repaired": 62, "known_holes": []
    },
    "binance:ETH/USDT|1h": {
      "expected": 4380, "present": 4374, "repaired": 0,
      "known_holes": [{"from": "2024-03-11T04:00:00Z", "to": "2024-03-11T10:00:00Z",
                       "reason": "venue served no bars on re-request"}]
    }
  }
}
```

### 4.4 Consequences

- **Corpus build refuses an unverified dataset**, the way `ReplayDataset.load` already refuses a
  directory with no manifest. Fail closed.
- **A decision whose ATR lookback or forward-scoring window crosses a known hole is
  `UNSCORED (gap)`** — counted and reported, never scored. Scoring across a hole is a wrong answer
  wearing a right one's clothes.

### 4.5 The calibration day set — selected once, pinned, reused

`python -m decision_lab dataset days --data <dir> [--seed N] [--reselect] [--pin <date>…]`

§10's first two scenarios need a normal day, an up-shock and a down-shock. The operator has no
preference about *which*, so the tool chooses — but it chooses **once**, writes the choice to
`decision_lab-calibration-days.json` beside the coverage audit, and every later run reads that
file. Pinning is what makes §10 a comparison rather than three anecdotes: two setups measured on
two different sets of days are not measured against each other at all.

**Selection.** Against one declared **reference instrument** (`calibration.reference_instrument`,
default the first in the manifest), using **the same realised-volatility estimator §8.1 defines**,
evaluated over each calendar day's bars rather than over a trailing 30-bar window — the labeller
answers "is this bar in a shock", and selection asks "was this *day* one", which is the same
measurement over a different window. The percentile is taken across that instrument's own daily
distribution over the dataset:

| Pool | Eligibility |
|---|---|
| `NORMAL` | daily realised vol in the 40th–60th percentile of that instrument's own distribution |
| `SHOCK_UP` | at or above the 90th percentile **and** the day's return is positive |
| `SHOCK_DOWN` | at or above the 90th percentile **and** the day's return is negative |

A day is eligible only if it also (a) has a full §9.2 forward horizon `H` after it inside the
dataset and (b) crosses no known hole from §4.3. **Three days are drawn from each pool**, with the
seed recorded. A pool holding fewer than three eligible days refuses, naming the pool and the
count, rather than quietly calibrating on one day.

A shock day is a shock *for something*. The reference instrument is named on every report, because
a day violent for XRP and calm for BTC is a legitimate test and a different one. That the chosen
day is ordinary for the basket's other instruments is not a defect — a real trading day is mixed.

**Pinning.** The file carries the seed, the reference instrument, the thresholds in force and the
nine dates. `--pin` adds a date by hand; `--reselect` is the only way to change the set, and it is
an explicit act because it moves `dayset_digest` and therefore every §11 run identity derived from
it. Results from two day sets can never be silently compared.

```json
{
  "selected_at": "2026-08-23T09:31:00Z",
  "seed": 20260823,
  "reference_instrument": "binance:BTC/USDT",
  "scoring_timeframe": "1h",
  "thresholds": {"normal_band": ["0.40", "0.60"], "shock_percentile": "0.90"},
  "dayset_digest": "…",
  "days": {
    "NORMAL":     ["2024-02-06", "2024-04-23", "2024-06-18"],
    "SHOCK_UP":   ["2024-01-11", "2024-02-28", "2024-03-05"],
    "SHOCK_DOWN": ["2024-03-19", "2024-04-13", "2024-08-05"]
  }
}
```

---

## 5. Stage 1a — the corpus

### 5.1 What it is

An ordered collection of frozen `ContextSnapshot`s: everything the panel is given for one
instrument-set at one instant — quote, candle summaries, indicator readings, news, position,
basket state.

**It is read out of the event log.** Every cycle already appends `SNAPSHOT_FROZEN` carrying the
whole snapshot body, so `corpus.py` is `store.read_types(EventType.SNAPSHOT_FROZEN)` plus an index.
No new persistence format, no second rendering path.

### 5.2 Why a reference pass rather than a flat book

Positions. A corpus built against an empty ledger makes SELL and HOLD unreachable — the panel only
ever chooses between BUY and WAIT and half the action space goes unmeasured. The reference pass is
what puts real positions into the snapshots.

Which configuration supplied those positions is a property of the experiment, so the reference
config is declared in `sweep.toml` and printed on every report.

**The reference pass can cost nothing.** `--reference-panel sim` selects `SIM_PANEL`: three
`varied-*` stub seats drawing votes from `stub_responses.json`, offline and free, which still
produces realistic position churn. This selects *who deliberates*; prices always come from
`ReplayDataset` regardless. A real reference panel is available when the positions themselves need
to be the ones a real panel would have held.

### 5.3 Construction

```python
clock   = ManualClock(...)
dataset = ReplayDataset.load(data_dir, clock)          # refuses unverified (§4.4)
basket  = dataset_basket(dataset, select_panel(ref_panel), every_seconds=cadence)
app     = await build_sim(
    clock=clock,
    db_path=workspace / corpus_id / "corpus.db",       # never data/sim.db
    baskets=(basket,),
    start_equity=start_equity,                         # §10.4; default 10_000
    market_data=dataset.market_data,
    catalogue=dataset_catalogue(dataset),
    news_feed=archive_feed,                            # §6, or None
)
report  = await BacktestHarness(app, clock, start=..., end=...).run()
corpus  = Corpus.from_store(app.store)
```

`BacktestHarness` is used **unchanged**. `warmup_for` already moves the window past the indicators'
requirement, so the corpus never opens on a wall of `DATA_STALE`.

### 5.4 Identity

`corpus_id = blake2s(dataset_id + reference_config_digest + cadence + archive_digest)`

Changing `--every` from `1h` to `4h`, or changing the reference panel, or adding a news archive —
or re-summarising that archive with a different model (§6.6) — produces a **different corpus**
rather than silently mixing two experiments.

### 5.5 Cadence

`--every 1h | 2h | 4h | 8h | 12h | 24h`, passed to `dataset_basket(every_seconds=…)`. Over six
months:

| cadence | cycles | note |
|---|---|---|
| 1h | ~4,380 | finest; matches a typical crypto basket |
| 2h | ~2,190 | |
| 4h | ~1,095 | |
| 8h | ~547 | a 48h shock window yields 6 decisions |
| 12h | ~365 | |
| 24h | ~183 | coarsest; cheapest six-month run by a factor of ~24 |

Cadence is a corpus property, not a sweep property: every candidate in one sweep sees one cadence.
§10.4's cadence comparison is therefore **N runs, not one run** — each with its own `corpus_id`,
which §5.4 already guarantees and §11 records.

### 5.6 The trailing horizon

Forward scoring needs H bars *after* a decision. Decisions in the last H bars of the dataset are
recorded `UNSCORED (horizon)` and counted on the report — never silently dropped, which would
flatter a run by discarding its most recent behaviour.

---

## 6. News — the archive

### 6.1 The rule

`observed_at` is the only field a point-in-time filter may use. `published_at` is the publisher's
claim and may be wrong, missing, or back-dated (`interfaces/news.py`).

### 6.2 Why an archive at all, and why this is not the scraping the bot forbade

`interfaces/news.py` states the sourcing policy in its module docstring: **RSS and official APIs,
never scraping**, and `news/rss.py` records that an earlier version scraped Cointelegraph and was
deliberately replaced, because "Cointelegraph publishes RSS, so scraping bought ToS and copyright
exposure for nothing."

That reasoning is about *current* news, where a feed exists. It does not reach the case here: a
feed serves roughly fifty recent items, so **six months of 2024 history is not obtainable from RSS
at any price**. The policy's own justification — "for zero benefit when the publisher offers a
feed" — does not apply where the publisher offers no feed for the period.

So the archive is built from a licensed API where one is available and from the publisher's own
archive sitemap where it is not, and the exposure is managed by *what is kept* rather than by not
looking (§6.4). None of this touches the bot: `tradebot` keeps its RSS-only sourcing unchanged,
and nothing in `decision_lab` can reach a live cycle (§2.3).

### 6.3 Two backends behind one protocol

```python
class ArchiveSource(Protocol):
    source_id: str
    capture: Literal["api", "sitemap"]
    async def items_between(self, since: datetime, until: datetime) -> AsyncIterator[ArchiveRow]: ...
```

- **`ApiArchiveSource`** — the CoinDesk Data / CryptoCompare News API. Squarely inside the existing
  policy: an official API, a declared key, published terms. **Preferred, and the default.**
- **`SitemapArchiveSource`** — reads `https://www.coindesk.com/sitemap/archive/`, a file publishers
  publish *for* crawlers, and follows it to each article. **The fallback**, for when the API is
  unavailable or its historical range is behind a paid plan.

**The backend is chosen explicitly (`--source api|sitemap`), never by silent fallback.** An archive
whose provenance depends on whether a key happened to be set is an archive nobody can cite. A
missing key refuses, naming `--source sitemap` as the alternative; the choice is recorded in the
archive header and travels into `archive_digest`.

> **Open at time of writing.** Secondary sources report that CoinDesk retired its free API tier on
> 21 May 2026 and that current plans are sales-quoted. This was not confirmed against an
> authoritative source. The two-backend design exists precisely so the answer does not block the
> build: if the API is paid and unwanted, `--source sitemap` is a supported first-class path.

### 6.4 What is kept, and what is never written

**Title, canonical URL, `published_at`, and the §6.5 summary. The article body is never written to
disk** — it is held in memory for the summarizer call and discarded.

That is the same posture `news/normalize.py` already states — *"Excerpts, not articles. Policy, not
preference: we store title + short excerpt + link and nothing more, because retaining full article
bodies is a copyright exposure we get no trading benefit from"* — reached by a different road. It
is enforced structurally, not by discipline: the archive row model has **no body field**, so there
is nowhere for one to be written.

The crawl backend is `FeedFetcher` plus a sitemap parser, and inherits its whole compliance posture
rather than restating it: `robots.txt` honoured per host with an unreachable file treated as
*disallow* (RFC 9309), conditional GET, a real identifying User-Agent, a response-size ceiling, and
the error taxonomy in which a robots denial raises `SourceDisallowedError` and we stop asking. The
crawler is therefore robots-respecting **by construction**, not by promise.

### 6.5 The summarizer is a compressor, not an analyst

Each article gets one LLM pass producing the `excerpt` the panel will read. This is the section's
load-bearing rule, and it has a home in existing doctrine: `interfaces/news.py` already says
*"Scoring only ranks and filters. **Interpreting** the news is the panel's job — that is what the
seats are for."*

A summarizer that offered market implications would have quietly become a fourteenth seat that
every candidate shares — and worse, one trained on what happened next, able to colour a March 2024
headline with April 2024's outcome. That is a second contamination channel, distinct from the
`observed_at` one §6.7 guards, and it is closed three ways:

- **By prompt.** Restate only what the text says. No outlook, no market implication, no price
  direction, no reference to anything after the article.
- **By what it is not given.** Never the instrument set, never the day's market data, never
  neighbouring articles. It cannot tilt toward BTC if it does not know we trade BTC.
- **By binding.** The summarizer is a declared `(provider, model)` in `archive.toml`, reached
  through the same `decision.providers` adapters the seats use — so the stub provider serves it in
  tests and the suite stays offline and free.

The summary lands in `NewsItem.excerpt`, which `NewsItem.view` already renders as the panel's
`summary`. No new field, no second rendering path, and the delimiting that keeps news as data
rather than instructions is the bot's own and unchanged.

**Summarise once, reuse forever.** Keyed by `url_hash`, computed at archive-build time, cached in
the archive file, and **never called during a sweep or a calibration run** — so every cycle of
every scenario reads the same text, at no repeated cost, and a sweep's LLM spend is the panel's
alone.

### 6.6 The archive format and its digest

`news.jsonl` beside the dataset, one row per line, behind a header declaring provenance:

```json
{"kind": "header", "source": "coindesk", "capture": "api", "captured_at": "2026-08-23T…",
 "observed_at_policy": "published_at+00:12:00", "summarizer": {"provider": "openrouter",
 "model": "…", "prompt_digest": "…"}, "archive_digest": "…"}
{"kind": "item", "source_id": "coindesk", "title": "…", "summary": "…", "url": "…",
 "url_hash": "…", "published_at": "2024-03-11T09:02:00Z", "observed_at": "2024-03-11T09:14:00Z"}
```

`archive_digest` covers the source, the capture mode, the `observed_at` policy, **the summarizer's
model id and prompt digest**, and the row set. It feeds `corpus_id` (§5.4), so re-summarising with
a different model yields a different corpus instead of silently mixing two experiments — the same
rule ADR 0013 applies to a basket version, one level out.

### 6.7 `observed_at` is synthetic unless the source supplies it

Neither backend gives us a moment *we* learned something, so one is derived:

- If the source publishes its own ingestion timestamp, it is used and the policy records that.
- Otherwise `observed_at = published_at + declared_lag`, with the lag stated in the header.
  Setting `observed_at = published_at` is **rejected**: it grants the panel the headline at the
  instant of publication, which is optimistic in the one direction that inflates a score.

Any archive whose `observed_at` is derived puts
**`RECONSTRUCTED NEWS — observed_at is synthetic (lag = …)`** on every report that used it, and
every archive built with §6.5 puts **`SUMMARIZED NEWS — excerpts are model-generated (model=…,
prompt=…)`** beside it, next to the contamination banner. A run whose evidence was partly derived
must never be quotable as one whose evidence was recorded.

### 6.8 `ArchiveNewsFeed`

A `NewsFeed` implementation that **never fetches**. It satisfies `snapshot_news(instruments, as_of,
limit)` by selecting archive rows with `observed_at <= as_of` and scoring them through the bot's
own `KeywordRelevanceFilter`, returning `NewsItemView`s and a `NewsCoverage`. Only the fetch half is
replaced; relevance, selection, ordering and truncation stay the bot's.

### 6.9 No archive

`news_feed=None` → `news_sources=()` → the snapshot records "no sources configured" and the report
carries `NEWS-BLIND RUN`. The panel is never left to read an empty news list as a quiet market —
that behaviour is already the bot's and is inherited.

---

## 7. Stage 1b — the sweep

### 7.1 The matrix

`decision_lab/config/sweep.toml` declares candidates over four axes:

- **seats** — `role`, `provider_id`, `model`, `temperature`, `evidence` slices, `devils_advocate`,
  `fallbacks`
- **prompts** — the `SeatConfig.instruction` text, drawn from a named library so a wording is
  reused across candidates and is diffable between them
- **panel** — `protocol` (`blind_then_debate` | `single_round`), `max_rounds`,
  `qualified_majority`, `max_abstain_fraction`, `max_cost_usd_per_cycle`
- **`decision_mode`** — `per_asset` | `basket`

```toml
[reference]
panel = "sim"
cadence = "4h"

[prompts.cautious]
text = "Favour standing aside. State the single strongest argument against your own vote."

[prompts.momentum]
text = "Weight recent trend continuation over mean reversion. Name the invalidation level."

[[candidates]]
id = "baseline"
protocol = "blind_then_debate"
max_rounds = 3
qualified_majority = "0.5"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "openrouter"
  model = "..."
  prompt = "momentum"

  [[candidates.seats]]
  seat_id = "risk"
  role = "risk officer"
  provider_id = "openrouter"
  model = "..."
  prompt = "cautious"
  devils_advocate = true

[expand]                       # optional cross product
prompts.risk = ["cautious", "momentum"]
max_rounds  = [1, 3]
limit = 24                     # refuse a larger matrix
```

`matrix_digest` is `blake2s` over the fully expanded candidate set. It is what §10.6's gate and
§11's run identity are keyed on, so changing one prompt is a new matrix and needs a new
calibration.

### 7.2 Validation before spend

Every candidate is materialised as a full `Basket` — the reference basket with its panel swapped —
and passed through `Basket.model_validate`. **Any invalid candidate refuses the whole sweep before
a single provider call.** A sweep can therefore never test a panel the bot itself would refuse:
unresolvable bindings, repeated fallbacks, a majority above 1, an over-long instruction.

The expansion cap refuses an oversized matrix in the spirit of `DEFAULT_MAX_CYCLES` — a 400-candidate
cross product is not a sweep anybody meant to start.

**Two kinds of run, decided by the binding and never by a flag.** A candidate bound to the offline
stub measures canned JSON, so a matrix containing one is a *plumbing check*: it runs, and its
report and its §11 row are stamped `PLUMBING CHECK — NOT AN EVALUATION`, unconditionally, the way
the contamination banner is. A matrix binding only real providers is an *evaluation*. The switch is
the configuration rather than a command-line flag for the reason `varied-*` is panel data: a flag
would leave a registry of rows that behaved differently under identical recorded configuration, and
"was this a real measurement" must be answerable from the artifact alone.

**An evaluation refuses before spend if any declared provider is unreachable.** `reach_of` reporting
*any* missing key refuses — not merely a fully silenced seat — because a partly-reachable seat is
one that will answer on its backup, and §7.7 says that is not a measurement. The refusal is exit 4,
names the seat, the binding and the environment variable, **and writes a §11 row with
`status = "provider_unavailable"`**: "we tried to evaluate on the 30th and could not, because of
providers" is a fact about the experiment and belongs in the registry, not in a terminal that
scrolled away. This is deliberately stricter than ADR 0023, which is right for a *trading* system —
degrade, say so, keep running — and wrong for a measuring one.

### 7.3 Sampling

Evaluating every candidate on every corpus entry is affordable only at coarse cadence. The default
is a **stratified, seeded** sample:

- **100% of named event windows and 100% of the §4.5 pinned days** — they are rare and they are
  the point,
- a fixed sample of auto-labelled `SHOCK_UP` and `SHOCK_DOWN` bars, drawn separately so neither
  direction can crowd the other out,
- a fixed sample of `NORMAL` bars.

The seed is recorded in the report, so a re-run draws the same sample and two sweeps are comparable.
`--full` disables sampling.

A corpus *cycle* covers every instrument in the reference basket, and two of them can sit in two
different regimes at the same instant. The stratum is therefore the regime of the **reference
instrument** — the one §4.5 already draws the day set from and every report already names — rather
than a per-instrument label a cycle cannot have one of. Defaults, in `params.py` beside every other
tuning constant: 100% of named windows and pinned days, then 60 `NORMAL`, 30 `SHOCK_UP` and 30
`SHOCK_DOWN`, drawn with `DEFAULT_SEED`.

### 7.4 Cache

Key: `blake2s(snapshot.digest + candidate_panel_digest)`. A hit returns the stored `PanelOutcome`
without a provider call.

What this actually buys, stated honestly: **adding a new candidate to an existing sweep re-runs only
the new one.** Changing one seat's prompt changes the panel digest and re-runs that whole candidate —
because in `blind_then_debate` the other seats see the changed seat's arguments, so their answers are
not reusable. Only round 0 is per-seat cacheable; that optimisation is out of scope for the first
version.

### 7.5 Budget

A hard USD ceiling (`--budget`). Spend is totalled with the engine's own `total_cost`, which already
de-duplicates by `call_id` — so `decision_mode = "basket"`, where one call answers for every
instrument, is not counted N times.

On breach the sweep **halts and reports what it completed**, with the ceiling and the point of halt
stated. It never overspends quietly, and it never discards the work already done. §10.6 exists so
the ceiling is a projection rather than a guess.

### 7.6 Resume

Results append to `workspace/<corpus_id>/sweep-<matrix_digest>/<candidate_id>.jsonl` as they are
produced. An interrupted sweep re-run picks up where it stopped. This matters at 40,000 calls: a
sweep that loses everything to a dropped connection is a sweep nobody runs twice. It is also what
lets §12's dashboard tail a running sweep rather than waiting for it to finish.

The directory is scoped by `matrix_digest`, not flat under the corpus: two matrices over one corpus
will both contain a candidate called `baseline`, and a flat layout would resume one experiment into
the other's file. Same reasoning as `corpus._existing` keying reuse on identity.

### 7.7 A substitute model is not the panel under test

The thing being measured is *this snapshot, through this seat, producing this answer*. A seat whose
primary binding fails and answers on its fallback has produced a different panel's answer in a row
labelled with the configured seat's name. It cannot be scored, because the configuration it claims
to measure never ran.

**Contamination is per cycle, not per seat.** One substitute answer poisons the whole decision, not
just that seat's row: under `blind_then_debate` the other seats read the substitute's arguments in
later rounds — the same argument §7.4 makes for the cache — and under either protocol the
substitute's vote enters `reach_consensus` and helps set the panel's action. So the cycle drops out
whole: the panel row and every seat row for it.

Detection is free and already written: `SeatResponse.fingerprint` is the binding that actually
answered, and §9.7's fallback rate already compares it against the seat's primary.

What happens next is declared in the matrix, because it is a property of the experiment and belongs
on the report:

```toml
[sweep]
on_fallback = "halt"      # default
# on_fallback = "exclude"
```

- **`halt`** — the first substitute answer stops the sweep. Exit 5, completed rows kept (§7.6
  appends as it goes), and the report names the candidate, the entry, the seat and both bindings.
  Recoverable rather than restarted: §7.4's cache means a re-run repeats no completed work, so the
  rhythm is halt, fix the provider, resume.
- **`exclude`** — the run continues, and every decision from a cycle in which any seat fell back is
  `UNSCORED (fallback)` and excluded from every metric. For a long run on free slots where losing
  some cycles beats losing the night.

Both settings share the invariant, and it is the point of the section: **a contaminated decision is
never scored.** The setting decides only whether the run stops. It therefore does *not* feed
`matrix_digest` and does not affect the §7.4 cache — it changes when you stop, not what is produced
— and it is recorded on the §11 row and printed on the report.

An **abstention is not a fallback**. A seat whose whole chain fails abstains, the panel resolves
`WAIT (PANEL_DEGRADED)`, and that is a real outcome of the real panel — §9.5 already reports the
degradation rate. Only "a different model answered" halts or excludes.

---

## 8. Regimes

### 8.1 Automatic labelling, and why a shock has a direction

Every bar of the scoring timeframe gets a label from its own realised volatility relative to the
dataset's distribution: realised volatility over a trailing window of **30 bars** (configurable)
of the scoring timeframe, at or above the **90th percentile** of that instrument's own
distribution across the dataset (configurable), is a shock; below it is `NORMAL`. Computed per
instrument, in `Decimal`, from the same recorded bars the panel saw.

**Realised volatility is a magnitude, so it is direction-blind — and a shock's direction is the
whole question.** The system is long-only. An up-shock asks *did the seats catch the move*; a
down-shock asks *did the seats protect capital*. Those are opposite competences, and a blended
`SHOCK` figure averages them and hides both — precisely the sin §8.3 forbids one level up.

So a shock carries its sign, taken from the signed return over the same window, in `Decimal`:

| Label | Condition |
|---|---|
| `NORMAL` | trailing realised vol below the percentile threshold |
| `SHOCK_UP` | at or above the threshold, window return positive |
| `SHOCK_DOWN` | at or above the threshold, window return negative |

A window return of exactly zero at or above the threshold is `SHOCK_UP` by the dispatch table's
default, and is vanishingly rare; it is a tie-break, never a judgement.

### 8.2 Named event windows

`decision_lab/config/regimes.toml`:

```toml
[[window]]
name = "spot ETF approval"
from = "2024-01-10T00:00:00Z"
to   = "2024-01-16T00:00:00Z"

[[window]]
name = "August carry unwind"
from = "2024-08-02T00:00:00Z"
to   = "2024-08-09T00:00:00Z"
```

A named window overrides the automatic label, keeps its own direction, and is reported **both**
inside its `SHOCK_UP` or `SHOCK_DOWN` aggregate and on its own row, so an episode can be read by
name.

### 8.3 The reporting rule

**No metric is ever shown without its regime split.** A blended accuracy over a period containing one
violent week is a number that describes neither week. Every table in the report carries `NORMAL`,
`SHOCK_UP`, `SHOCK_DOWN`, and one row per named window.

---

## 9. Scoring

### 9.1 The scoring timeframe

One timeframe answers for the band, the forward horizon and the regime label. It defaults to the
**shortest timeframe in the dataset** (`ReplayDataset.timeframes` is ordered shortest-first) and is
overridable with `--scoring-timeframe`. It must be one the basket computes indicators on, or the
snapshot carries no ATR reading for it and every decision scores `UNSCORED (no ATR)`.

### 9.2 Inputs

Per (candidate, snapshot, instrument):

- `p0` — the quote in the snapshot: what the panel actually saw.
- `atr` — `context.indicator("ATR", scoring_timeframe).value`, read **off the frozen snapshot**
  rather than recomputed. The band is then derived from exactly the evidence the panel had.
- `band = k × atr`, `k` default `1.0`, configurable.
- `H` — forward horizon in bars of the scoring timeframe, default `6`, configurable.
- `pH` — the close H bars after `as_of`, from the dataset.
- `move = pH − p0`; MFE and MAE over the same window are recorded alongside.

All `Decimal`. `decision_lab` has no float in the scoring path, and its own money-discipline test
asserts it — the same guard `test_money_discipline.py` provides for the bot.

### 9.3 The truth label is long-only aware

The system is long-only (Tier-1 refuses otherwise), so standing aside from a fall while flat is
**correct**, not a missed short. Getting this backwards would systematically punish exactly the
conservative behaviour the bot is built for — and it is what makes `SHOCK_DOWN` a test the bot can
pass rather than a period it is doomed to score badly in.

| Snapshot state | `move` | Truth | Correct actions |
|---|---|---|---|
| flat | `> +band` | `BUY` | BUY |
| flat | otherwise | `STAND_ASIDE` | WAIT, HOLD |
| holding | `> +band` | `ADD` | BUY, HOLD |
| holding | `< −band` | `EXIT` | SELL |
| holding | within band | `HOLD` | HOLD, WAIT |

### 9.4 Verdicts

`CORRECT` · `WRONG` · `UNSCORED (gap)` · `UNSCORED (horizon)` · `UNSCORED (no ATR)`

Unscored decisions are counted with their reason on every table. A run that quietly dropped them
would report accuracy over a subset it chose after the fact.

### 9.5 Metrics, per candidate, per regime

- **accuracy** — CORRECT / scored
- **precision on action** — of the decisions that asked for an order, how many were right. The
  figure that matters most: a WAIT-heavy panel scores well on accuracy while never trading.
- **action rate** — tradable decisions / scored, so precision is read in context.
- **mean conviction gap** — conviction on correct calls minus conviction on wrong ones. A panel
  whose conviction carries information is worth more than one that is right as often by accident,
  because conviction feeds the Tier-1 floor and sizing.
- **oracle regret** — the best achievable capture over H minus the panel's, summed and per decision.
  Reported as a **ranking aid, explicitly labelled unreachable**: an oracle trades every bar and no
  risk-managed system can match it.
- **abstention and degradation rate** — cycles resolving `WAIT (PANEL_DEGRADED)`. A candidate that
  scores well on the cycles it answered while failing a third of them is not a better panel.
- **cost per scored decision** — each candidate against its own spend.

### 9.6 Cross-candidate

- **agreement matrix** — pairwise, per regime. Two candidates agreeing 98% of the time are one
  experiment run twice.
- **tradable divergence** — where exactly one asked for an order. The disagreement that moves money,
  reusing the definition in `validation/comparison.py`.

### 9.7 Per-seat scoring

§9.5 scores what the *panel* decided. A seat is not a panel, and an operator tuning seats needs to
know which of them is carrying the result. Every `SeatResponse` already recorded — `seat_id`,
`vote`, `abstain_reason`, `round_index`, `latency_ms`, tokens, `cost_usd`, and `fingerprint`, the
binding that actually answered after any fallback — makes this free of new data and free of new
provider calls.

Each seat's own vote is scored against the **same §9.3 truth label**, per regime:

- **accuracy, precision on action, action rate, conviction gap** — the §9.5 definitions, one level
  down.
- **abstention rate** and **fallback rate** — how often `fingerprint` differs from the seat's
  primary binding. A seat that answered on its backup all sweep is a seat that was never tested,
  and today nothing would say so.
- **cost and latency per answered vote** — a seat that is marginally better and four times slower
  is a different trade-off at 1h cadence than at 24h.
- **swing rate** — how often replaying `decision.consensus.reach_consensus` over the recorded votes
  *minus this seat* changes the panel's decision. Deterministic, free, and the number that
  separates a seat that is carrying weight from one that is padding a majority.
- **marginal contribution** — cycles where the seat dissented from the panel and was right against
  a wrong panel, minus cycles where it dissented and was wrong against a right one. The question
  "does this seat earn its slot", answered in one signed figure.

**Round 0 is reported separately from the final vote,** and the split is not cosmetic. Under
`blind_then_debate` a seat's later votes are contaminated by its peers by design — that is what the
debate is for. Round 0 is the seat's own independent opinion; the final vote is the seat after
persuasion. "Which seat reasons well" and "which seat is easily talked round" are different
questions, and one column cannot answer both. Under `single_round` the two are identical and the
report says so rather than printing the same numbers twice.

---

## 10. Stage C — the three calibration scenarios

### 10.1 Two kinds of instrument, and why the spec keeps them apart

Scenarios 1 and 2 are **snapshot-scored**: one frozen context, every candidate judged on identical
evidence, verdicts from §9. Path-independent, so a difference between candidates is a difference in
reasoning — ADR 0018's principle, which §3 calls the design's load-bearing decision.

Scenario 3 is **full-loop with a ledger**: positions compound, so a candidate's cycle 400 happens
in a market its own cycle 12 helped create. Its numbers are a different *kind* of number.

The spec's job is to stop the two being read on one scale. They appear on the same report, ranked
separately, and **where the two rankings disagree the report says so explicitly.** A candidate
first on profit and fifth on accuracy made its money on a handful of trades; that is the single
most important thing to see before trusting it, and a merged leaderboard would hide it.

### 10.2 Scenario 1 — a normal day

`python -m decision_lab calibrate normal --corpus <id> --configs config\sweep.toml`

Runs every candidate over the three pinned `NORMAL` days (§4.5) at the corpus cadence, feeding each
one the full snapshot the bot would have built: quote, candle summaries, every configured
indicator, position and basket state, and the §6 archive news as of that instant.

Scored by §9 and §9.7 and reported **per day and pooled**, with the spread across the three days
shown. Three days is not a distribution, but it is enough to see when one day carried a result —
and a candidate whose pooled accuracy comes entirely from one of the three is a candidate the
report should not present as steady.

### 10.3 Scenario 2 — a shock, in each direction

`python -m decision_lab calibrate shock --corpus <id> --configs config\sweep.toml`

The same, over the three pinned `SHOCK_UP` days and the three pinned `SHOCK_DOWN` days. **The two
are never pooled.** They are reported as two blocks, because they ask opposite questions (§8.1):
`SHOCK_UP` asks whether the seats caught the move, `SHOCK_DOWN` whether they protected capital, and
a candidate can be excellent at one and dangerous at the other.

The `SHOCK_DOWN` block is the one to read first. A long-only system's worst outcome is not a missed
rally.

### 10.4 Scenario 3 — long exposure

```powershell
python -m decision_lab calibrate long --data data\history --configs config\sweep.toml `
    --candidate baseline --start-equity 1000 --every 4h --window 6m
```

`BacktestHarness` — the same class, unchanged — with the chosen panel, its own ledger, its own
path, in its own workspace database, from a declared starting balance. **No `tradebot` seam is
needed:** `build_sim(start_equity=Decimal(1000))` already threads to `SimBroker.balances`.

Unlike scenarios 1 and 2 it takes `--data` rather than `--corpus`: it does not consume frozen
snapshots, it **produces its own path** at the cadence under test. So a cadence comparison is
**N runs, not one** — each deriving its own `corpus_id` (§5.5) and filing its own §11 row.

**"Total profit" is mark-to-market, and it is not `Evidence.realized_pnl`.** That property sums
*closed* round trips only, so a run ending with an open position would report a profit that ignores
it — exactly the defect ADR 0027 exists to fix, and the same class of error as a drawdown gate
measuring cost basis. The figure is:

```
total_profit    = aggregate(...).equity  −  start_equity
                  decomposed into realized (Evidence.round_trips) and unrealized (the rest)
```

both halves printed, never just the total. A **frozen** aggregate at the end of the window — a mark
too stale to value a holding — reports `UNVALUABLE` and no figure at all. Freezing is ignorance, and
a number produced in ignorance is worse than its absence.

And because the ask is *efficient*, not merely profitable, the headline carries **net of spend**:

```
net_profit      = total_profit  −  deliberation_cost_usd
```

A panel that made $80 on $120 of tokens lost money. Nothing else in this design would have said so.

Alongside: the standard `Evidence` fold, the incident count, and a **veto breakdown** — how many of
this candidate's tradable decisions became orders, and which rule refused the rest. A panel that is
right often but only in ways the price collar, the cooldown or the daily cap refuse is not an
improvement, and a corpus sweep can never discover that.

### 10.5 Ranking by profit — what the report is allowed to claim

Profit is the decision-relevant number and also the noisiest. The spec does not forbid ranking by
it; it qualifies what the ranking means:

| Comparison | Standing |
|---|---|
| **cadences, one candidate** | fair — the paths differ only by the variable under test |
| **start equities, one candidate** | fair — same |
| **different candidates, one path each** | weak — one lucky early fill compounds for six months |

For the third, the report prints the §9 snapshot-scored ranking beside the profit ranking and
names every position where they disagree. Staggered starts would turn one path into a small
distribution and make profit genuinely rankable; that is §17's first deferred item, because it
multiplies the expensive half of the spend and the disagreement column buys most of the protection
for nothing.

### 10.6 The gate, and the cost projection

**The sweep and the long run both refuse to start unless scenarios 1 and 2 have passed for this
exact `(dataset_id, matrix_digest, dayset_digest)`.** Change one prompt, change the matrix digest,
calibrate again — cheap, because it is nine days.

The key is deliberately **not** `corpus_id`. All four conditions below are properties of the
candidates, the seats and the days; none of them is cadence-dependent. Keying on the corpus would
force a re-calibration every time §10.4 changed `--every`, which is the one axis the long run
exists to vary. The cadence the gate ran at is *recorded* on its row for provenance, not used to
scope it.

Four things a pass requires, all fail-closed:

1. every candidate materialises as a valid `Basket` — §7.2, actually exercised rather than asserted;
2. **every seat answered at least once on its primary binding.** A seat that never answered, or
   only ever answered on a fallback, refuses. This is the whole point of checking seats on a short
   horizon: a seat whose key is missing abstains quietly, and over six months that is a panel you
   paid to run and never tested;
3. the panel reached decisions — an all-`PANEL_DEGRADED` calibration has proved nothing;
4. the path completed — scoring produced verdicts, unscored reasons are accounted for, the report
   rendered.

**And it calibrates cost.** The nine days measure $/decision per candidate empirically, and the
projection for the six-month run at each cadence is printed beside `--budget`, so §7.5's ceiling
stops being a guess. If the projection exceeds the ceiling you learn it for the price of nine days
rather than at hour nine.

`--skip-gate` exists and is **stamped on the report and on the §11 row**, because a result whose
provenance reads "nobody checked the seats first" should say so on its face.

---

## 11. The results registry

Every calibration and sweep run appends one row to `workspace/registry.jsonl` — scenario, every
parameter, every digest, the headline metrics, and the per-seat rollup.

```
run_id = blake2s(scenario + dataset_id + corpus_id + matrix_digest + dayset_digest
                 + candidate_id + cadence + start_equity + window + sample_seed)
```

Identical parameters **update** the row; any changed parameter creates a new one. So a re-run never
duplicates, a changed prompt never silently overwrites the result it should be compared against,
and two rows on screen are always two genuinely different experiments.

Every row carries a `status`, because a run that never produced a number is still a fact about the
experiment: `ok`, `provider_unavailable` (§7.2's pre-spend refusal), `halted_fallback` (§7.7),
`halted_budget` (§7.5). It also records `on_fallback` and whether the run was an evaluation or a
plumbing check — neither feeds `run_id`, both are needed to read the row.

`run_id` is computed over the **full** field set from the start, with §10's `scenario`,
`start_equity` and `window` empty for a sweep, so slice D lands without renumbering rows already
written.

The registry is the answer to "compare and find the most efficient setup": it is append-only, it is
a flat file that a notebook can read, and §12 renders it. Rows are never deleted by the tool —
`--prune` is an operator act naming what it removes, in the spirit of the bot's own retention rule
that deletion is the one irreversible step.

---

## 12. The dashboard

`decision_lab/dashboard/` is **its own ASGI app**, on its own port, behind its own token
(`DECISION_LAB_DASHBOARD_TOKEN`), reusing `tradebot.dashboard.auth`'s `Session`,
`SessionMiddleware` and `assert_bind_allowed` — a one-way import, so the separation contract is
untouched and the ADR 0014 auth posture is inherited rather than re-argued.

Separate rather than a page in the bot's dashboard, for three reasons: a sweep is not a running
bot, so it must be readable with nothing else up; its data lives in the workspace, not in a mode
database; and putting it in `tradebot/dashboard/` would have deleted `test_separation.py` and
pulled tuning code inside the bot's mypy and coverage gates.

**It is read-only.** No control surface at all — nothing on it can start, stop, arm, publish or
change anything. That is a genuine simplification against the bot's dashboard, and it means the
whole ADR 0021 supervision story has no analogue here.

Four views:

| View | Shows |
|---|---|
| **Runs** | the §11 registry: every run, sortable by accuracy, net profit, cost, date; two rows diffable side by side |
| **Run detail** | the candidate ranking per regime — `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN`, named windows — with unscored counts and their reasons |
| **Seat detail** | §9.7 in full: per seat, per regime, round 0 beside final, with swing rate and marginal contribution |
| **Decision drill-down** | one snapshot: the evidence the panel saw, every seat's vote and raw text, the truth label, and why the verdict landed as it did |

Because §7.6 already appends results as they are produced, the Runs view **tails a sweep in
progress** — which is the other half of "see the performance of each test run": not only what a
finished run scored, but what a running one is scoring while it still has budget left to stop.

Sizes, sorting and the selected run follow the bot's own rule (Phase 10): selection is in the URL,
so a reload and a bookmark land on the same view; only display preferences live client-side.

---

## 13. CLI

```powershell
python -m decision_lab dataset verify   --data data\history [--repair]
python -m decision_lab dataset days     --data data\history [--seed N] [--reselect] [--pin DATE]
python -m decision_lab archive build    --data data\history --source api|sitemap `
                                        --since 2024-01-01 --until 2024-07-01
python -m decision_lab corpus build     --data data\history --every 4h `
                                        --reference-panel sim [--news data\history\news.jsonl]
python -m decision_lab calibrate normal --corpus <id> --configs config\sweep.toml
python -m decision_lab calibrate shock  --corpus <id> --configs config\sweep.toml
python -m decision_lab calibrate long   --data data\history --configs config\sweep.toml `
                                        --candidate <id> --start-equity 1000 `
                                        --every 4h --window 6m
python -m decision_lab sweep            --corpus <id> --configs config\sweep.toml `
                                        --budget 40 [--full] [--seed 20260823]
python -m decision_lab report           --corpus <id> [--out reports\...]
python -m decision_lab dashboard        [--host 127.0.0.1] [--port 8788]
```

Exit codes, following the bot's convention of distinct codes for distinct refusals:

| code | meaning |
|---|---|
| 0 | success |
| 2 | misuse (bad arguments) |
| 3 | dataset unverified, holed beyond repair, or no pinned day set |
| 4 | a candidate failed `Basket` validation |
| 5 | budget ceiling reached — partial results written |
| 6 | the §10.6 calibration gate is unsatisfied for this dataset, matrix and day set |

---

## 14. The report

Markdown, to `decision_lab/reports/`. Never printed — a tuning result is filed beside the decision
it justified, exactly as `report promotion` and `report shadow` are.

Every report opens with its banners: the `BacktestHarness` contamination banner verbatim,
`NEWS-BLIND RUN` / `RECONSTRUCTED NEWS` / `SUMMARIZED NEWS` where they apply, `PLUMBING CHECK — NOT
AN EVALUATION` where any candidate binds the stub (§7.2), `GATE SKIPPED` where `--skip-gate` was
used, and the tool's own line stating it is a comparison instrument and not evidence of alpha.

There is one `report` command, not two. It renders the reference pass as it always has and *grows*
the cross-candidate sections when sweep results exist under that corpus; `--matrix <digest>` picks
between them only when more than one sweep has run. A second command would be a second rendering
path over the same tables.

Then the experiment's identity, in full, because a result whose provenance is not on the page is not
reproducible: dataset and its coverage audit, the pinned day set and its digest, the reference
instrument, corpus id, reference config, cadence, sample seed and size, regime thresholds, named
windows, scoring parameters (`k`, `H`, timeframe), archive source and summarizer binding, starting
equity, budget spent and whether the ceiling was reached.

Then, per regime — `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN`, named windows: the ranking table,
per-candidate detail, **the per-seat tables of §9.7 with round 0 beside final**, the agreement
matrix, unscored counts with reasons, and the cost table. Scenario 3 adds the profit block of
§10.4, the veto breakdown, and §10.5's disagreement column.

`notebooks/tuning.ipynb` imports the same library — one implementation, two front doors — runs a
sweep, renders the tables inline, and diffs a result against a previous one from the §11 registry.

---

## 15. Failure semantics

- **An unverified or holed dataset refuses the corpus build.** Fail closed: a corpus is the basis of
  every number downstream.
- **A missing or stale pinned day set refuses a calibration**, naming `dataset days`. A day set
  selected against a dataset that has since been repaired is stale, because §4.3 may have changed
  the distribution the days were drawn from.
- **An invalid candidate refuses the whole sweep**, before spend.
- **An unsatisfied calibration gate refuses the sweep and the long run** (§10.6), naming which of
  the four conditions failed and which seat, if that is the reason.
- **A budget breach halts and reports**, never overspends, never discards completed work.
- **A candidate that raises during deliberation** is recorded as a failed evaluation for that
  snapshot and counted, exactly as `ShadowEvaluator` does — a candidate that silently stopped being
  evaluated would leave a comparison built on fewer cycles than it claims.
- **A decision that cannot be scored is unscored and counted**, never dropped.
- **A frozen portfolio aggregate reports `UNVALUABLE`**, never a number (§10.4).
- **An archive source that refuses is a refusal, not a fallback.** A robots denial, a 403, or a
  paywalled range names the other backend and stops; it never silently degrades to the other one,
  because provenance that depends on what failed is provenance nobody can cite.
- **`decision_lab` never writes to a bot database, never constructs a venue broker, and has no code
  path from a candidate's `Decision` to an `OrderIntent`.** Scenario 3's orders reach `SimBroker`
  only, in a workspace database.

---

## 16. Testing

`decision_lab/check.ps1` runs `ruff`, `mypy decision_lab`, and `pytest decision_lab/tests`. The root
`.\check.ps1` is unmodified; root `ruff` already covers the folder.

| Rung | What |
|---|---|
| unit | scoring truth table across all five snapshot/move combinations; regime labelling **including the sign split**; gap classification; day-set eligibility and pool refusal; TOML matrix expansion and its cap; budget accounting; cache keys; `net_profit` arithmetic; registry identity |
| property | scoring is invariant to instrument scale — the same ATR-relative move scores identically for BTC and XRP |
| structural | `decision_lab` uses no float in the scoring path; no module under `tradebot/` names `decision_lab`; **the archive row model has no body field**; no dashboard route resolves a path under `data/` |
| round-trip | a corpus written and re-read yields identical snapshot digests; a pinned day set re-read yields identical days and digest |
| scenario | a small end-to-end run on the stub panel: two candidates, one pinned day of each kind, a short long-run at 24h cadence, a rendered report — offline, deterministic, free |
| **contamination** | **the point-in-time and hindsight guards, asserted directly — see below** |

### 16.1 Per-seat scoring tests

Swing rate and marginal contribution are the two metrics a reader will trust without checking, so
they are asserted against handmade vote sets rather than only end to end: a three-seat panel where
removing seat A flips the decision and removing seat B does not, and a case where a seat dissents
correctly against a wrong panel and another where it dissents wrongly against a right one. The
round-0/final split is asserted under both protocols, including that `single_round` reports them as
identical rather than duplicating the table.

### 16.2 The contamination tests

The seam of §2.3 exists to stop future news reaching a past decision. That has to be asserted
against the real failure, not merely against the wiring. In `decision_lab/tests/`:

| Test | Asserts |
|---|---|
| `ArchiveNewsFeed` holds no fetcher and no HTTP client | it cannot reach the network even by accident |
| a corpus cycle run with an archive feed, behind a transport that **raises on any request**, completes | no network call happens on the real path, not just in principle |
| an archive holding one item `observed_at` **before** the replay instant and one **after** it: the snapshot carries the first and not the second | the actual look-ahead guard, on the real `ContextBuilder` output |
| the summarizer is called at archive-build time and **never** during a corpus, sweep or calibration run | §6.5's reuse promise, and that a sweep's spend is the panel's alone |
| the summarizer's prompt is given no instrument, no market data and no sibling article | §6.5's hindsight closure, asserted on the call arguments rather than on the prompt text |

The third is the one that matters most. It is the exact scenario §2.2 describes, run through the
real snapshot path, and it fails if anyone ever reintroduces a fetch into the replay — including by
wiring a live `NewsHub` back in.

A further test lives in the bot's suite rather than the tool's, because it pins bot behaviour: a
`NewsHub` built on a `ManualClock` set to a past instant stamps fetched items with **that** instant.
That is the underlying mechanism, currently unasserted anywhere, and the reason the seam is needed
at all. Written as a characterisation test — it records what the code does today, so a future change
to `observed_at` stamping surfaces here instead of silently in a replay.

The scenario test runs on `STUB_PANEL` and `SIM_PANEL`, with the stub provider serving the
summarizer, so the whole suite stays offline and costs nothing — consistent with the bot's own rule
that real models are off unless asked for.

---

## 17. Non-goals and deferred

- **Staggered starts for scenario 3.** The only real defence against one lucky path (§10.5).
  Deferred because it multiplies the expensive half of the spend, and the accuracy-vs-profit
  disagreement column buys most of the protection for free. Revisit when a finalist is being
  chosen on profit alone.
- **Per-seat round-0 caching.** Possible, but debate rounds make later rounds uncacheable; deferred
  until sweep cost proves it necessary.
- **Automatic search or optimisation.** Deliberately excluded (§1.1).
- **A generic Reports pane in the bot's dashboard.** Would render any Markdown under `reports/`
  without knowing what wrote it, so it stays inside the separation contract — but it is a `tradebot`
  change serving `tradebot`, and belongs to that backlog rather than this spec.
- **Equity instruments.** Blocked by the same four fail-closed refusals as Phase 12 Piece 2. Crypto
  only, and the tool inherits the refusal rather than working around it.
- **Any promotion authority.** `validation/promotion.py` remains the only gate.

---

## 18. Suggested implementation order

This spec is larger than one sitting. Five slices, each independently useful and independently
testable — a property worth preserving, because the value arrives before the last one lands.

| Slice | Delivers | Usable on its own? |
|---|---|---|
| **A — integrity, day set, corpus** | §4 dataset audit and repair, §4.5 the pinned day set, §5 corpus build, the separation test, `check.ps1` | Yes: it repairs the holed history the bot's own backtests already run on |
| **B — scoring and regimes** | §8 labelling **with the direction split**, §9 scoring, §9.7 per-seat scoring, §14 report, run over the *reference* pass alone | Yes: scores the existing panel over six months in all three regimes, and says which seat carried it — the core question, one configuration |
| **C — the sweep** | §7 matrix, cache, budget, resume; the cross-candidate tables of §9.6; §11 the registry | Yes: the seat and prompt comparison, which is the stated goal |
| **D — calibration and the dashboard** | §10 all three scenarios and the §10.6 gate, §12 the dashboard, the notebook | Yes: the short-horizon seat check and the six-month profit run, on whatever news the corpus has |
| **E — news** | §2.2 seam **with the §2.3 guard tests and §16.2 contamination tests landing in the same change**, §6 the archive, both backends, the summarizer, `ArchiveNewsFeed` | Completes the news-driven half of the shock scenarios |

Slice A is worth doing first even in isolation: the gap audit found a real defect in
`marketdata/recorder.py` (§4.1), and everything downstream is built on the data it verifies.

**B carries the direction split, and it must not be deferred into D.** Adding `SHOCK_UP` and
`SHOCK_DOWN` after §9's tables exist means rewriting every one of them; landing it with the
labeller costs nothing.

**D before E is deliberate.** The calibration scenarios work news-blind (§6.9) — scenario 2 then
measures the reaction to a violent price move rather than to the reporting of an event, which is
narrower but honest and clearly banner-labelled. Getting the seat check and the profit run in hand
is worth more than getting them with news.

**E is last because it carries the only `tradebot` change in the design.** The seam and the tests
that fence it in are **one change, never two**: a commit adding `news_feed` without the §2.3 guards
is a commit that opened a path from an archived, model-summarised feed to a live venue and shipped
it unasserted. If the seam is refused entirely, A through D still deliver a working tool and the
news half degrades to §6.9.
