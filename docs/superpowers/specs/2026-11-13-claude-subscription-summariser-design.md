# A summariser on the operator's Claude subscription — design

**Date:** 2026-11-13 · **Status:** draft for approval · **Roadmap:** D2 / Stage 5
([2026-10-27 roadmap](../plans/2026-10-27-roadmap-and-agent-plan.md)) · **Extends:**
[decision-lab design §6.5](2026-08-23-decision-lab-design.md)

## 1. Intent

D2 was "set `OPENROUTER_API_KEY` and pay per token for ~11 900 summaries". The operator already
pays for a Claude subscription and has Claude Code installed and logged in (`claude` 2.1.296). This
design adds a summariser provider that runs each article through the **Claude Agent SDK on that
login** — no API key, no per-token bill — and revises the summary prompt so the archive it produces
is fit to cluster and to feed a panel.

**Later, not now:** the same provider is expected to serve decision_lab's calibrate, sweep and
period-run seats (Stage 7). This design builds for summaries only, but shapes the adapter so that
step is a registration, not a rewrite (§7).

## 2. Goals, non-goals, anti-goals

**Goals**
- `archive summarise` runs end to end on the subscription, resumably, across many sittings.
- The archive's provenance names exactly the model and prompt that produced each row.
- The summaries cluster correctly (entity naming is consistent) and stay de-editorialised.
- Pure market commentary is kept and marked, never fed to the panel.

**Non-goals**
- Panel seats on the subscription (later; §7).
- Any change to the bot (`tradebot/`). This provider is not added to `PRESETS`.
- Parallel calls. One row at a time, as today.

**Anti-goals — a failure even if it "works"**
- **The repo's CLAUDE.md, Claude Code's own system prompt or any tool reaching a summary call.**
  `prompt_digest` would then describe a prompt the model never saw alone.
- **Silently billing an API key.** If `ANTHROPIC_API_KEY` is in the environment the CLI uses it;
  the pass must run on the subscription or not at all.
- **A usage-limit hit turning thousands of rows into "refused".** That is a stop, not a refusal.
- **A model alias in the provenance.** `haiku` moves when models ship; the archive would name a
  model nobody can identify.
- **Context carried between articles.** Each article is its own fresh session — §6.5's "never
  neighbouring articles" closure.

## 3. Constraints (from the repo)

- `nothing under tradebot/ may name decision_lab`; `git diff --stat main -- tradebot/` stays empty.
- No `float` in decision_lab (`test_discipline.py`). The SDK's `total_cost_usd` is a float and is
  converted at the boundary or not read as money at all (§5.4).
- Dependencies are hash-pinned. **`claude-agent-sdk` is an optional extra, `[claude-subscription]`,
  never a main dependency (my call).** A package whose job is to spawn a credentialed `claude`
  process does not belong in the bot's runtime install, which is also live's. Concretely:
  - **Locks:** its own hash-pinned lock (`requirements-claude.lock`), plus inclusion in the dev
    lock, so both gates type-check and test the adapter.
  - **Import:** guarded, so the module imports without the SDK.
  - **Refusal:** a binding to `claude-subscription` without the extra installed refuses before
    spend and names the install command.
  - **Separation test:** `test_separation.py`, or a sibling of it, asserts that nothing under
    `tradebot/` imports the SDK.
- Both gates pass: `.\decision_lab\check.ps1` and `.\check.ps1`. The suite stays offline: tests
  drive the adapter through a fake of the SDK's query function, never a real `claude` process.
- `ArchivePolicy` currently validates `provider` against the bot's `PRESETS`, and
  `cli.archive_summarise` builds the provider through `preset()` + `build_providers`.

## 4. Decisions made with the operator

1. **Purpose:** run on the existing subscription instead of OpenRouter per-token billing.
2. **Scope now:** summaries only; seats later, same provider.
3. **Price moves** (resolves a §6.5 ambiguity — "never carries price direction"):
   - A price move **with a cause the article states** is kept: the move, its figures as stated,
     and the cause. *"BTC fell 8% to $76k after Bybit disclosed a $1.5B hack."* A cause the article
     merely attributes to others ("analysts say…") is kept as their claim, modality `RUMOURED`.
   - **Pure market commentary** — technical analysis, price targets and predictions, recaps that
     state moves with no cause — is **kept in the archive but marked**, so there is a record that
     the article was seen. It never reaches the panel.

## 5. The provider

### 5.1 Placement

`decision_lab/providers/claude_subscription.py` (builder may choose the exact module), an
`LLMProvider` (`tradebot.interfaces.llm`) with `provider_id = "claude-subscription"`. A small
decision_lab-side resolver sits in front of `preset()`: `ArchivePolicy`'s provider check accepts
`PRESETS` **or** decision_lab's own providers, and `archive_summarise` builds this one directly.
The bot never learns it exists.

### 5.2 How a call runs

One `CompletionRequest` → one fresh SDK `query()` → one `CompletionResult`, with options:

| Option | Value | Why |
|---|---|---|
| `system_prompt` | the request's `system`, as a plain string | replaces Claude Code's prompt entirely |
| tools | none allowed; MCP none | providers are never given tools (`interfaces/llm.py`) |
| `max_turns` | 1 | one answer, nothing agentic |
| `setting_sources` | none | no CLAUDE.md, no user/project settings, no hooks |
| `cwd` | an empty temp directory | nothing in the working directory to discover |
| `model` | the request's `model`, an exact pinned id | §5.3 |
| extended thinking | off | no benefit for extraction; costs quota |
| env | `ANTHROPIC_API_KEY` removed for the child | §5.5 |

**The first task is a spike that can stop the project (my call).** A subscription login may
require the CLI to prepend Claude Code's fixed identity line to every request, whatever
`system_prompt` says. Before any production code, capture what one SDK call actually sends (CLI
debug/request logging) under these options:

- **Only a fixed identity line is added** → accept it. Record the CLI version on the pass's
  provenance (the preamble is the CLI's, so the version names it), and fold the preamble text, if
  observable, into `prompt_digest` so the digest covers everything the model saw.
- **Anything more** — CLAUDE.md, tool definitions, environment or git context that the options
  cannot switch off → stop and come back to the operator. The fallback is OpenRouter (the original D2).

The adapter also asserts at runtime what it can see — no tools offered, one turn — and stops the pass
otherwise.

`temperature` and `json_mode` are not expressible through the CLI and are ignored; record that in
the module docstring (it matters for seats, §7). Our `parse_summary` remains the only validation.

### 5.3 Model

- **Default: Haiku 5.5, pinned as `claude-haiku-5-5`** in `archive.toml` (my call, confirmed by
  §8's bake-off). Extraction into a fixed schema needs no large model; Haiku uses far less of the
  subscription's quota; and §6.5's finding that memorisation is worst for heavily covered names
  (BTC, 2025) argues for a smaller model.
- `ArchivePolicy` **refuses a family alias** (`haiku`, `sonnet`, `opus`, `default`, anything not of
  the form `claude-<family>-<major>-<minor>[-<YYYYMMDD>]`) for this provider.
- **Fingerprint check (my call).** The adapter reads the model that actually answered from the SDK's
  messages and returns it as `model_fingerprint`. It must **equal the requested id, or be a dated
  snapshot of it** (`requested + "-" + 8 digits`). Anything else — a fallback model the CLI chose —
  raises a stop (§5.6), not a refusal.
- **Provenance records both.** `summary_model` stays the requested id, as today, and the pass also
  records the snapshot id that answered. If the answering snapshot **changes partway through an
  archive**, the pass stops: two snapshots under one name are two models, and `archive publish`
  must be able to refuse that mix, as it already refuses a mixed provider or prompt.

### 5.4 Cost

`cost_usd` is **`ZERO`** — the operator's real marginal spend. Token counts are recorded as usual.
The SDK's API-equivalent price is logged per batch as `notional_usd` for reference, converted
from the SDK's float as text (`Decimal(str(x))` is forbidden by ADR 0001; the builder uses the
sanctioned crossing or logs it as a string and never sums it as money). `per_row_cost` therefore
reads 0 under this binding, and the report says why.

### 5.5 Subscription, never an API key

The child process is started without `ANTHROPIC_API_KEY` (and without `ANTHROPIC_AUTH_TOKEN`).
The adapter reads the SDK's init message and **refuses unless it reports subscription/OAuth auth**
(the CLI's `apiKeySource` is `none`). Checked on the first call of a pass, before any row is
written; a mismatch is a refusal with nothing spent.

Not logged in, `claude` not on PATH, or the SDK not installed: refused before spend, naming the fix
(`claude /login`, install), like today's missing-key refusal.

### 5.6 Failures

| Fault | Class | Pass behaviour |
|---|---|---|
| Usage / rate limit reached | new `QuotaExhausted` (a `FatalError` subclass in decision_lab) | stop the batch; `finally` already flushes rows done; log when the limit resets if the SDK says |
| Wrong model answered, wrong auth source | stop (same path) | nothing further asked |
| CLI crash, timeout, transient error | `ProviderError` | today's retry-then-refuse |
| Reply not matching schema | `SchemaViolationError` | today's repair-then-refuse |

`summarize_store` must let the stop class propagate rather than tally it (today it catches only
`ProviderError`, so this may already hold — the builder verifies with a test).

Throughput: one CLI process per row, a few seconds each — roughly 10–20 hours of wall time for the
whole store, across several `--limit` batches and usage windows. Acceptable; no batching of
articles into one call (that would put neighbouring articles in one context).

## 6. The prompt (revised `SUMMARY_SYSTEM`)

Keep everything the current prompt says. Add:

1. **Entities are a cluster key.** At most three: the actor first, then whom it acted on. Each in
   its shortest common name ("SEC", "BlackRock", "Binance"), no titles, no people unless a person
   is the actor. Explain *why* in one line (two articles about one event must list the same
   names), because a model follows a reason better than a rule.
2. **Attribution.** A prediction or opinion is recorded as a claim its author made ("Analyst X
   said…"), never as the event.
3. **Own knowledge is off-limits.** State outright that the model may know what happened after the
   article and must not use any of it, nor any fact absent from the text.
4. **The register to strip**, by example: praising or alarming words ("empowers", "innovative",
   "plunges", "soars") — the CryptoSlate case from §6.5.
5. **Price moves** — §4.3's rule, verbatim in substance.
6. **Edge cases.** The body wins over the title; a roundup gets its headline event; `OTHER` in
   assets means a named crypto asset other than BTC or ETH.
7. **One short worked example** (input excerpt → JSON). Written fresh, not taken from the archive,
   and not about BTC or ETH, so it cannot tilt the asset field.

### 6.1 Marking commentary

`EventType` gains **`MARKET_COMMENTARY`**. For such an article the model still writes a one-line
neutral summary of *what kind of piece it is* ("Technical analysis of BTC support levels"), with no
levels, targets or predictions in it, `modality` `CONFIRMED`, entities and magnitudes empty. Then:

- The pass reports a **`commentary`** count beside `summarised` and `refused`.
- `archive publish` publishes the row like any other (it is the record that we saw it).
- **Where it is filtered (my call).** `ArchiveNewsFeed` exposes rows three ways, and they split by
  who is reading:
  - `snapshot_news` (what the panel reads) and `in_window` (what the drill-down rebuilds a cycle's
    evidence from, §16.2) **exclude** commentary. The drill-down must show what the panel had, and
    the panel never had it.
  - `items` (the period page's "published later" news, via `spikes.news_for`) **keeps** it, shown
    **marked as commentary**. That page is where the operator wants the record that it was seen.
- The cycle-coverage rule is unchanged: a window holding only commentary is quiet and reports ok,
  because no event happened.
- Clustering is irrelevant to these rows because the feed drops them first.

`EventType` is shared with `published.py`; adding a member is a schema change to the travelling
archive, so an archive published by a build without the member must still read (it simply has
none).

The revised prompt mints a new `prompt_digest`. No real archive has been published, so nothing is
lost; stub-summarised rows read as stale.

## 7. Built for seats later (not built now)

Keep the adapter generic — it takes a `CompletionRequest` and knows nothing about articles — so
Stage 7 can register it for decision_lab's seat runs. That later step must decide, in its own
design: a `ProviderKind` or decision_lab-side injection into `build_providers` (replay and
candidates build pools through the bot's registry today); `temperature` being unavailable through
the CLI; and the live-readiness refusal if it ever reached `tradebot`'s `PRESETS` (it should not).

## 8. Rollout (replaces the roadmap's "When the real key arrives" steps 1–3)

1. `archive summarise --dry-run` (unchanged).
2. **Bake-off:** `--limit 50` on the same 50 rows under Haiku 5.5, then under Sonnet 5.5 (a second
   store copy, or `--resummarise-stale` after a binding change). Compare refusal rate, commentary
   rate, the traceability sample score, entity consistency on the duplicate pairs, and quota used.
   Keep Haiku unless its traceability is clearly worse.
3. Batches of 1 000 under the chosen binding, resuming after each usage window.
4. Traceability sample, then `archive publish` (unchanged).

## 9. Left to the builder

Module names and layout; how the SDK fake is shaped; the exact wording of the prompt within §6;
whether the bake-off gets a helper command or is two manual runs; an ADR (suggested: 0032, "a
summariser may run on the operator's subscription") recording §5.5 and §5.4.

## 10. Spike findings (2026-11-17)

`claude-agent-sdk` 0.2.183, which runs its **bundled** CLI 2.1.326 (not the operator's installed
`claude`), on the operator's login. Facts from three real calls and the CLI's own session
transcript (`api-request-shape` / `api-request-blob` records):

- **(a) Options that work.** `tools=[]` (→ `--tools ""`), `allowed_tools=[]`, `mcp_servers={}`,
  `strict_mcp_config=True`, `setting_sources=[]`, `max_turns=1`, `system_prompt=<str>`,
  `effort="low"`; `extra_args={"no-session-persistence": None}` stops the CLI writing a session
  folder under `~/.claude/projects` (without it, **every call leaves one**, holding the article).
  `--bare` would strip most injection but **refuses OAuth** (API key only), so it is unusable here.
- **(b) Init message.** `data.apiKeySource == "none"` on the subscription; `data.tools == []`;
  `data.mcp_servers == []`; `data.claude_code_version == "2.1.326"`; `data.model`.
- **(c) Model.** `claude-haiku-5-5` is reported back exactly, on the assistant message, in
  `model_usage` and as `canonicalModel`. No dated suffix.
- **(d) What the CLI adds — more than an identity line. This trips the §5.2 gate.**
  1. System block 1: `You are a Claude agent, built on Anthropic's Claude Agent SDK.` (fixed)
  2. A system message `# Environment`: the temp working directory (changes every call), platform,
     shell, OS version, two security notes, and "You are powered by … knowledge cutoff June 2026".
  3. A user-turn reminder carrying **the operator's account email**.
  4. A user-turn reminder `Today's date is <today>` (changes daily) and a token-budget reminder.
  5. Request params: `thinking: adaptive` **whatever `thinking` says**; `effort` is honoured.
  `--exclude-dynamic-system-prompt-sections` removes none of 2–4.
  Measured overhead: ~620 input tokens per call; a real summary spent ~1 000–1 150 thinking
  tokens at effort medium.
- **(e) Errors.** Failures surface as `claude_agent_sdk.ResultError` / `ProcessError` with the
  API's message as text. A content-safeguard block is one such error (`Details: [...]`) and must
  be a **refused row**, not a stop. A `RateLimitEvent` message arrives on every call with
  `rate_limit_info.status` (`allowed` …), `resets_at`, and per-window `utilization` — the quota
  signal can be read **before** it runs out, not only parsed from an error.
- **(f) Auth.** `env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}` with the login gives
  `apiKeySource: "none"`.

**Decision (operator, 2026-11-24): continue on the subscription, and record the additions rather
than strip them.** None of them carries news or market content: the email reaches only Anthropic,
which holds it already; the date says nothing about 2025; the traceability check still measures
whether summaries stay on the text. Consequences for §5:

- `prompt_digest` covers the fixed identity line, **the pinned CLI version** and our prompt. The
  CLI version decides what the CLI adds, and it is pinned through the SDK's hash-locked version, so
  an SDK upgrade is a new archive identity. The per-call parts (date, temp directory) are named in
  ADR 0032 as known CLI context, not digested.
- The contract check also requires `claude_code_version` to equal the pinned version, and
  `mcp_servers == []`.
- `effort="low"`; `thinking` is not sent, because the CLI ignores it.
- `no-session-persistence` is always passed.
- The quota stop reads `RateLimitEvent.rate_limit_info.status` (anything but `allowed` stops,
  carrying `resets_at`), with the error-text match as a fallback.
- A safeguard block is a `ProviderError`, so it ends as a refused row.
