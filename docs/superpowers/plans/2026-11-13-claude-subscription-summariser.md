# Claude-subscription summariser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `decision_lab archive summarise` runs on the operator's Claude Code login through the Claude Agent SDK — no API key — with a revised prompt and a `MARKET_COMMENTARY` mark.

**Architecture:** A decision_lab-only `LLMProvider` (`claude-subscription`) wraps one fresh SDK `query()` per request, with our system prompt alone, no tools, no settings, one turn. `ArchivePolicy` and the CLI resolve it beside the bot's `PRESETS` without touching `tradebot/`. Two `FatalError` subclasses stop a pass (usage limit; broken contract) and the existing `finally` keeps the rows already done.

**Tech Stack:** Python 3.11, `claude-agent-sdk` (optional extra), pydantic, pytest/pytest-asyncio, uv for hash-pinned locks.

**Spec:** [docs/superpowers/specs/2026-11-13-claude-subscription-summariser-design.md](../specs/2026-11-13-claude-subscription-summariser-design.md)

## Global Constraints

- `git diff --stat main -- tradebot/` stays **empty**. Nothing under `tradebot/` names `decision_lab` or imports `claude_agent_sdk`.
- No `float` in decision_lab (`test_discipline.py`). The SDK's `total_cost_usd` crosses as text: `str(value)` logged, never parsed into `Money`, never summed.
- Provider id is exactly `"claude-subscription"`. Default model `claude-haiku-5-5` (or its dated snapshot, per Task 1).
- `claude-agent-sdk` is the optional extra `claude-subscription`, never a main dependency. Own lock `requirements-claude.lock`; also in `requirements-dev.lock`.
- The suite never spawns `claude`: every test drives the adapter through an injected fake `query` function.
- Exit codes: usage-limit stop → `EXIT_BUDGET` (5, "partial results written"); contract stop (wrong model, wrong auth, tools offered) and every pre-spend refusal → `EXIT_CANDIDATE` (4).
- Both gates pass: `.\decision_lab\check.ps1` and `.\check.ps1`.

## Review Focus

1. **`ANTHROPIC_API_KEY` set in the operator's shell** → the pass must not bill it; refused before row 1 if the CLI reports any key source but subscription. (Task 3 test `test_an_api_key_auth_source_stops_before_any_row`.)
2. **Usage limit hit on row 400 of a 1 000 batch** → rows 1–399 are on disk, exit 5, re-running the same command resumes at row 400 and re-asks nothing. (Task 6 test `test_a_quota_stop_flushes_and_a_rerun_resumes`.)
3. **A dated snapshot answers for an undated pin** → stopped on the first call with a message naming the id to pin, not 1 000 refusals. (Task 3 test `test_a_different_answering_model_stops_and_names_it`.)
4. **An old published archive with no `MARKET_COMMENTARY` rows, and `EventType` grown by one member** → still reads, digest unchanged. (Task 5 test `test_an_archive_without_commentary_keeps_its_digest`.)
5. **A commentary reply carrying price levels** (`magnitudes: ["$100k"]`) → refused and tallied, never stored with levels. (Task 5 test `test_commentary_with_magnitudes_is_refused`.)

---

### Task 1: Spike — what does one subscription call actually send and return? (throwaway, gates the rest)

**Files:**
- Create (scratch, not committed): `<scratchpad>/spike_sdk.py`
- Modify: `docs/superpowers/specs/2026-11-13-claude-subscription-summariser-design.md` — append `## 10. Spike findings`

**Interfaces:**
- Produces: the findings section, which later tasks read for six facts: (a) exact `ClaudeAgentOptions` field names that give no tools / no settings / one turn / no thinking; (b) init `SystemMessage` keys for auth source, tool list and CLI version, and their values on this machine; (c) the model id the `AssistantMessage` reports for `claude-haiku-5-5`; (d) whether the CLI prepends text to our system prompt, and its exact text; (e) the message shape of a usage-limit / rate-limit error (from SDK source or docs if it cannot be provoked); (f) whether `env={"ANTHROPIC_API_KEY": ""}` yields subscription auth when the variable is set in the parent.

- [ ] **Step 1:** `.venv\Scripts\python.exe -m pip install claude-agent-sdk` (unpinned; the spike only). Record the version installed.
- [ ] **Step 2:** Write `spike_sdk.py`: one `query()` with `system_prompt=SUMMARY_SYSTEM`-like text, the user message of one real article, `model="claude-haiku-5-5"`, no tools, `max_turns=1`, empty `setting_sources`, `cwd` an empty temp dir; print every message's type and fields. Run it with `--debug`/`ANTHROPIC_LOG=debug` style logging (whichever the CLI supports) to capture the outgoing system prompt.
- [ ] **Step 3:** Repeat with `ANTHROPIC_API_KEY=dummy` in the parent env and `env={"ANTHROPIC_API_KEY": ""}` in options; record the auth source reported.
- [ ] **Step 4:** Write §10 with facts (a)–(f), plus one amendment to §5.3: **the fingerprint check is exact equality.** If the CLI reports a dated snapshot, `archive.toml` must pin that dated id. Then `summary_model` names the snapshot itself, and the existing publish refusal of mixed models covers a snapshot change. This replaces the spec's "or a dated snapshot of it" rule and the separate snapshot field.
- [ ] **Step 5: GATE.** If (d) shows anything beyond a fixed identity line (CLAUDE.md text, tool schemas, env/git context) → **stop and report to the operator**; spec §5.2 says the fallback is OpenRouter. Otherwise uninstall nothing (Task 2 pins it properly) and commit the spec change:

```bash
git add docs/superpowers/specs/2026-11-13-claude-subscription-summariser-design.md
git commit -m "docs(decision_lab): record the Agent SDK spike findings"
```

---

### Task 2: The optional dependency and its guards

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies]`: add `claude-subscription = ["claude-agent-sdk==<version from Task 1>"]`; add the same to `dev`)
- Create: `requirements-claude.lock`; regenerate `requirements-dev.lock`
- Create: `decision_lab/providers/__init__.py` (empty docstring module)
- Modify: `decision_lab/tests/test_separation.py`

**Interfaces:**
- Produces: `decision_lab.providers` package; the SDK importable in the dev venv.

- [ ] **Step 1: Write the failing test** in `test_separation.py`:

```python
@pytest.mark.parametrize(("path", "tree"), BOT_MODULES)   # the module list the file already builds
def test_no_bot_module_imports_the_agent_sdk(path: Path, tree: ast.Module) -> None:
    assert "claude_agent_sdk" not in _imported_roots(tree), path
```
(`_imported_roots` = the helper the existing import test uses; reuse, don't copy.)

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_separation.py -q` — PASS already (guard test); confirm `test_the_guard_can_actually_fail`'s pattern covers the new test, extending it with a synthetic `import claude_agent_sdk` tree if it does not.
- [ ] **Step 3:** Edit `pyproject.toml`, then:

```powershell
python -m uv pip compile pyproject.toml --extra claude-subscription --generate-hashes -o requirements-claude.lock
python -m uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements-dev.lock
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
```
`requirements.lock` must be byte-identical afterwards (`git diff --stat requirements.lock` empty).
- [ ] **Step 4:** Run both gates. Expected: green.
- [ ] **Step 5: Commit** `build(decision_lab): claude-agent-sdk as the claude-subscription extra`.

---

### Task 3: `ClaudeSubscriptionProvider`

**Files:**
- Create: `decision_lab/providers/claude_subscription.py`
- Test: `decision_lab/tests/test_claude_subscription.py`

**Interfaces:**
- Consumes: `CompletionRequest`, `CompletionResult`, `LLMProvider` (`tradebot.interfaces.llm`); `ProviderError`, `FatalError` (`tradebot.core.errors`).
- Produces:
  - `PROVIDER_ID: Final = "claude-subscription"`
  - `IDENTITY_PREAMBLE: Final[str]` — Task 1 fact (d), `""` if none observed.
  - `class QuotaExhausted(FatalError)` — usage/rate limit; message carries the reset time when the SDK gives one.
  - `class SubscriptionContractError(FatalError)` — auth source not subscription, tools offered, more than one turn, or answering model ≠ requested.
  - `class SdkMissing(ConfigError)` — extra not installed / `claude` not on PATH / not logged in; message names the fix.
  - `QueryFn = Callable[..., AsyncIterator[object]]` (the SDK's `query` signature: `prompt=`, `options=`)
  - `class ClaudeSubscriptionProvider: provider_id = PROVIDER_ID; def __init__(self, *, query_fn: QueryFn | None = None) -> None; async def complete(self, request: CompletionRequest) -> CompletionResult; runtime: str | None` (CLI version string from the first init message, e.g. `"claude-code 2.1.296"`); `notional_usd: list[str]` (the SDK's `total_cost_usd` per call, as `str`).
  - `def is_pinned(model: str) -> bool` — `^claude-[a-z]+-\d+-\d+(-\d{8})?$`.

- [ ] **Step 1: Write the failing tests.** A `FakeQuery` class yields scripted SDK message objects (built from the real SDK types, which the dev venv has) and records each call's `options`. Tests:

```python
async def test_one_request_is_one_fresh_query_with_our_prompt_alone():
    fake = FakeQuery.answering(text=GOOD, model="claude-haiku-5-5")
    result = await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)
    opts = fake.calls[0].options
    assert opts.system_prompt == REQ.system          # a plain string, not a preset
    assert opts.max_turns == 1
    assert <no-tools fields from Task 1 (a)> 
    assert not opts.setting_sources                  # no CLAUDE.md, no settings
    assert Path(opts.cwd).is_dir() and not any(Path(opts.cwd).iterdir())
    assert opts.model == "claude-haiku-5-5"
    assert opts.env["ANTHROPIC_API_KEY"] == "" and opts.env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert fake.calls[0].prompt == REQ.user
    assert result.text == GOOD and result.model_fingerprint == "claude-haiku-5-5"
    assert result.cost_usd == ZERO

async def test_two_requests_never_share_a_session():          # two fake.calls, distinct cwd, no resume/continue option set
async def test_tokens_come_from_the_result_usage():          # prompt_tokens/completion_tokens == scripted usage
async def test_notional_cost_is_kept_as_text_never_money():  # provider.notional_usd == ["0.0012"]; result.cost_usd == ZERO
async def test_an_api_key_auth_source_stops_before_any_row(): # init apiKeySource="ANTHROPIC_API_KEY" -> SubscriptionContractError
async def test_offered_tools_stop_the_pass():                 # init tools=["Bash"] -> SubscriptionContractError
async def test_a_different_answering_model_stops_and_names_it():
    fake = FakeQuery.answering(text=GOOD, model="claude-haiku-5-5-20261001")
    with pytest.raises(SubscriptionContractError, match="claude-haiku-5-5-20261001"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)
async def test_a_usage_limit_is_quota_exhausted_not_a_provider_error():  # Task 1 fact (e) shape
async def test_a_crashed_cli_is_a_provider_error():          # fake raises the SDK's process error -> ProviderError
async def test_an_error_result_that_is_not_a_limit_is_a_provider_error():
async def test_the_runtime_is_read_from_the_init_message():  # provider.runtime == "claude-code 2.1.296"
async def test_a_missing_sdk_is_refused_naming_the_install(monkeypatch):
    # sys.modules["claude_agent_sdk"] = None; ClaudeSubscriptionProvider() raises SdkMissing
    # whose message contains "pip install" and "claude-subscription"
def test_is_pinned():  # True: claude-haiku-5-5, claude-haiku-5-5-20261001; False: haiku, sonnet, default, claude-haiku, claude-haiku-5
```

- [ ] **Step 2:** `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_claude_subscription.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** Import the SDK inside `__init__` (guarded; `ImportError` → `SdkMissing`). Each `complete` makes a fresh `tempfile.TemporaryDirectory()` as `cwd`. Classify on the stream: init → check auth source and tools (contract), record runtime; assistant → collect text blocks and model; result → `is_error` routed by the Task 1 (e) predicate to `QuotaExhausted` or `ProviderError`, else tokens and `str(total_cost_usd)`. `temperature`/`json_mode` ignored; the module docstring says so and why it matters for seats (spec §7), plus failure semantics for each fault row of spec §5.6.
- [ ] **Step 4:** Tests PASS; `.\decision_lab\check.ps1` green.
- [ ] **Step 5: Commit** `feat(decision_lab): a provider on the operator's Claude subscription`.

---

### Task 4: Policy and resolution — bind it without touching the bot

**Files:**
- Modify: `decision_lab/archive/config.py` (`load_policy`'s known-provider check; `ArchivePolicy` validator)
- Create: `decision_lab/providers/resolve.py`
- Modify: `decision_lab/archive/summarize.py` (`prompt_digest` call sites → `digest_for`)
- Test: `decision_lab/tests/test_archive_config.py`, `decision_lab/tests/test_archive_summarize.py`

**Interfaces:**
- Consumes: `PROVIDER_ID`, `IDENTITY_PREAMBLE`, `is_pinned`, `ClaudeSubscriptionProvider`, `SdkMissing` (Task 3).
- Produces:
  - `LAB_PROVIDERS: Final = frozenset({PROVIDER_ID})` in `resolve.py`
  - `def build_summariser(policy: ArchivePolicy) -> LLMProvider | None` — `ClaudeSubscriptionProvider()` for `PROVIDER_ID`; otherwise today's `preset()` + `build_providers` path (moved here from `cli.archive_summarise`), returning `None` when unwired.
  - `def digest_for(policy: ArchivePolicy) -> str` in `summarize.py` = `prompt_digest(IDENTITY_PREAMBLE + system_for(policy))` when `policy.provider_id == PROVIDER_ID`, else `prompt_digest(system_for(policy))`. `summarize_store` and `is_stale` use it.

- [ ] **Step 1: Failing tests:**

```python
def test_the_subscription_provider_is_a_known_binding(tmp_path):      # load_policy accepts provider="claude-subscription", model="claude-haiku-5-5"
def test_an_alias_is_refused_for_the_subscription_provider(tmp_path):
    with pytest.raises(ConfigError, match="pin"): load_policy(<toml with model = "haiku">)
def test_an_alias_is_still_allowed_for_other_providers(tmp_path):     # openrouter + "anthropic/claude-x" loads unchanged
def test_the_digest_covers_the_preamble(monkeypatch):                 # monkeypatch IDENTITY_PREAMBLE="X"; digest_for(sub) != digest_for(stub with same text)
def test_stub_digest_is_unchanged():                                  # digest_for(POLICY) == prompt_digest(system_for(POLICY))
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** Known set becomes `{*PRESETS, "stub", *LAB_PROVIDERS}`; the alias refusal is a `ConfigError` naming `is_pinned`'s form. `config.py` imports `LAB_PROVIDERS` from `resolve.py`, which must not import the SDK at module import (Task 3's guard is in `__init__`).
- [ ] **Step 4:** Tests PASS; gate green.
- [ ] **Step 5: Commit** `feat(decision_lab): archive.toml may bind the subscription provider`.

---

### Task 5: The revised prompt and `MARKET_COMMENTARY`

**Files:**
- Modify: `decision_lab/archive/fields.py` (`EventType.MARKET_COMMENTARY`)
- Modify: `decision_lab/archive/summarize.py` (`SUMMARY_SYSTEM`; `SummaryResponse` commentary rule; `SummaryReport.commentary`)
- Test: `decision_lab/tests/test_archive_summarize.py`, `test_archive_fields.py`, `test_archive_published.py`, `test_archive_summarize_pass.py`

**Interfaces:**
- Produces: `EventType.MARKET_COMMENTARY = "MARKET_COMMENTARY"`; `SummaryReport.commentary: int` (a subset of `summarised`, so `per_row_cost` is unchanged); markers `EXAMPLE_START = "<example>"` / `EXAMPLE_END = "</example>"` delimiting the worked example in `SUMMARY_SYSTEM`.

- [ ] **Step 1: Failing tests:**

```python
def test_the_prompt_limits_entities_and_says_why():      # "at most three" and "same names" (or the chosen wording's key phrase) in SUMMARY_SYSTEM
def test_the_prompt_forbids_outside_knowledge():         # "what happened after" and "your own knowledge" in SUMMARY_SYSTEM
def test_the_prompt_names_commentary_and_the_price_rule(): # "MARKET_COMMENTARY" in SUMMARY_SYSTEM; "cause" in SUMMARY_SYSTEM
def test_the_worked_example_names_neither_traded_asset():
    ex = SUMMARY_SYSTEM.split(EXAMPLE_START)[1].split(EXAMPLE_END)[0]
    for word in ("BTC", "ETH", "Bitcoin", "Ethereum", "bitcoin", "ether"): assert word not in ex
def test_the_worked_example_parses():                    # the example's JSON passes parse_summary(max_chars=280)
def test_commentary_parses_with_no_entities_or_magnitudes():
def test_commentary_with_magnitudes_is_refused():        # SchemaViolationError
def test_commentary_with_entities_is_refused():
def test_an_archive_without_commentary_keeps_its_digest(): # a fixture archive's archive_digest equals the pinned hex literal recorded before the change
async def test_the_pass_counts_commentary_inside_summarised(tmp_path):  # 2 rows, one commentary reply -> summarised=2, commentary=1
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** Rewrite `SUMMARY_SYSTEM` covering spec §6 items 1–7 and §4.3 verbatim in substance (price move with a stated cause kept with figures and cause; an attributed cause recorded as that party's claim, `RUMOURED`; pure commentary → `MARKET_COMMENTARY`, one neutral line saying what kind of piece it is, no levels/targets/predictions, entities and magnitudes empty, `CONFIRMED`). `SummaryResponse` gains a `model_validator(mode="after")` refusing commentary with entities or magnitudes. Keep `{max_chars}` and the `str.replace` rule.
- [ ] **Step 4:** Tests PASS; the existing `test_contamination.py` and summarise tests still pass; gate green.
- [ ] **Step 5: Commit** `feat(decision_lab): a summary prompt that clusters, and a commentary mark`.

---

### Task 6: The pass stops cleanly; the CLI wires and reports

**Files:**
- Modify: `decision_lab/archive/summarize.py` (`summarize_store`)
- Modify: `decision_lab/cli.py` (`archive_summarise`)
- Test: `decision_lab/tests/test_archive_summarize_pass.py`, `decision_lab/tests/test_cli_archive.py`

**Interfaces:**
- Consumes: `build_summariser` (Task 4); `QuotaExhausted`, `SubscriptionContractError`, `SdkMissing`, `ClaudeSubscriptionProvider.runtime`/`.notional_usd` (Task 3).

- [ ] **Step 1: Failing tests:**

```python
async def test_a_quota_stop_flushes_and_a_rerun_resumes(tmp_path):
    # provider answers rows 1-3 then raises QuotaExhausted on row 4
    # -> raises QuotaExhausted; store has 3 summarised; a second pass with a healthy provider asks for exactly the remaining rows
async def test_a_contract_stop_is_not_tallied_as_a_refusal(tmp_path):  # SubscriptionContractError propagates; nothing counted refused
def test_summarise_exits_budget_on_a_quota_stop(tmp_path, monkeypatch):     # build_summariser patched to a raising fake -> exit 5, log names "resume"
def test_summarise_exits_candidate_on_a_contract_stop(tmp_path, monkeypatch): # -> exit 4
def test_summarise_refuses_a_missing_sdk_before_spend(tmp_path, monkeypatch): # SdkMissing -> exit 4, store untouched
def test_the_summarised_log_carries_runtime_and_notional(tmp_path, monkeypatch, caplog):  # "runtime" and "notional_usd" keys present for the subscription binding; per_row_cost "0"
```

- [ ] **Step 2:** Run → FAIL (the pass likely already propagates `FatalError`; the CLI tests fail).
- [ ] **Step 3: Implement.** `archive_summarise` builds through `build_summariser`; catches `SdkMissing` before spend (exit 4), `QuotaExhausted` (exit 5, logging the reset time and "re-run the same command to resume"), `SubscriptionContractError` (exit 4). For the subscription binding the final log adds `runtime` and `notional_usd` (the list joined as text, never summed) and a note that `per_row_cost` is 0 because the subscription is not billed per call.
- [ ] **Step 4:** Tests PASS; gate green.
- [ ] **Step 5: Commit** `feat(decision_lab): a subscription pass stops on its limit and resumes`.

---

### Task 7: Commentary stays out of evidence and is marked on the period page

**Files:**
- Modify: `decision_lab/newsfeed.py` (`in_window`; `snapshot_news` goes through it already)
- Modify: `decision_lab/dashboard/templates/_period_event.html` (published-later list)
- Test: `decision_lab/tests/test_newsfeed.py`, `decision_lab/tests/test_spikes.py`, `decision_lab/tests/test_lab_dashboard_periods.py`

- [ ] **Step 1: Failing tests:**

```python
async def test_commentary_is_never_served():            # feed with one REGULATORY + one MARKET_COMMENTARY in window -> views hold only the first
def test_commentary_is_not_in_the_drill_down_window():  # in_window excludes it
async def test_a_window_of_only_commentary_inside_the_span_reports_ok(): # sources_ok has the source; views empty
def test_items_keeps_commentary():                      # feed.items includes it
def test_published_later_keeps_commentary():           # spikes.news_for returns it in published_later
def test_the_period_page_marks_commentary(...):         # rendered event HTML contains "commentary" label beside that item's title, not beside the regular one
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `in_window` filters `event_type is not EventType.MARKET_COMMENTARY`; its docstring and `items`' say which surface sees what and why (spec §6.1). Template: a `<span class="tag">commentary</span>` before the title when `item.event_type == "MARKET_COMMENTARY"`.
- [ ] **Step 4:** Tests PASS; gate green.
- [ ] **Step 5: Commit** `feat(decision_lab): commentary is a record, never evidence`.

---

### Task 8: Records — ADR, CLAUDE.md, archive.toml, roadmap

**Files:**
- Create: `docs/adr/0032-a-summariser-may-run-on-the-operators-subscription.md`
- Modify: `CLAUDE.md` (decision_lab command block + one rule bullet), `decision_lab/config/archive.toml` (comment naming the binding; the shipped binding **stays the stub**), `docs/superpowers/plans/2026-10-27-roadmap-and-agent-plan.md` (D2 row)

- [ ] **Step 1:** ADR 0032: context (D2 cost), decision (spec §5.2, §5.4, §5.5), consequences (preamble in the digest, $0 cost, temperature unavailable for future seats, extra not main dep).
- [ ] **Step 2:** CLAUDE.md: the install line `pip install --require-hashes -r requirements-claude.lock`; the bake-off as two commands over a copied store; one bullet: *a subscription pass is billed to nobody and stops on its limit — exit 5 is "resume later", never a failure, and `per_row_cost` reads 0 by construction*.
- [ ] **Step 3:** Roadmap D2 row: "subscription binding (ADR 0032) or `OPENROUTER_API_KEY`".
- [ ] **Step 4:** Both gates green; `git diff --stat main -- tradebot/` empty.
- [ ] **Step 5: Commit** `docs: ADR 0032 and the subscription summariser's operating notes`.

---

## After the plan (operator, not a task)

Spec §8: `--dry-run`; bake-off `--limit 50` under `claude-haiku-5-5` and under `claude-sonnet-5-5` on copies of one store; compare refusals, commentary rate, traceability, entity consistency, quota; then batches of 1 000; traceability sample; `archive publish`.
