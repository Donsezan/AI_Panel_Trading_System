# 32. A summariser may run on the operator's Claude subscription

Date: 2026-11-25

## Status

Accepted. Implements the [subscription summariser spec](../superpowers/specs/2026-11-13-claude-subscription-summariser-design.md)
(§5.2, §5.4, §5.5, §7, §10). Touches nothing under `tradebot/`.

## Context

Roadmap D2 is the first spend: the summarising pass over about 11 900 archive rows, priced per row
on an OpenRouter key. The operator already holds a Claude subscription whose marginal cost per call
is nothing. The spec's first task was a spike that could stop the project: a subscription login
runs through the Claude Code CLI, and the CLI may add text to a request whatever `system_prompt`
says. A pass whose summaries are the evidence a panel is later graded on must know everything the
model saw.

The spike (spec §10, `claude-agent-sdk` 0.2.183 running its bundled CLI 2.1.326) found more than an
identity line. On every call the CLI adds:

1. a fixed identity line, `You are a Claude agent, built on Anthropic's Claude Agent SDK.`;
2. an `# Environment` block: the temporary working directory (new each call), platform, shell, OS
   version and the model's knowledge cutoff;
3. a user-turn reminder carrying the operator's account email;
4. a user-turn reminder with today's date, and a token-budget reminder;
5. `thinking: adaptive` whatever the options say. `effort` is honoured.

`--bare` would strip most of it but refuses OAuth, so it cannot be used on a subscription.
`--exclude-dynamic-system-prompt-sections` removes none of items 2 to 4.

## Decision

**The operator chose to proceed and record the additions, not to strip them.** None of them carries
news or market content: the email reaches only Anthropic, which holds it already; the date says
nothing about 2025; and the traceability sample still measures whether summaries stay on the text.
The fallback, OpenRouter, remains available and is the original D2.

**What a call is.** One `CompletionRequest` is one fresh SDK `query()`: `system_prompt` is ours,
no tools, no MCP (strict), `setting_sources=[]`, `max_turns=1`, an empty temporary `cwd`,
`effort="low"`, and `no-session-persistence` always (without it every call leaves a session folder
under `~/.claude/projects` holding the article). The model is an exact pinned id
(`claude-haiku-5-5`); `archive.toml` refuses a family alias for this provider. The model that
answered must equal it exactly, or the pass stops with a contract error. If the CLI ever reports
a dated id, the operator pins that dated id in `archive.toml`; `summary_model` then names the
snapshot itself, and `archive publish`'s existing refusal of mixed models covers a change.

**The digest covers what we control and what the CLI adds that is fixed.** `prompt_digest` covers
the identity line, the pinned CLI version (`PINNED_CLI_VERSION = "2.1.326"`) and our prompt. The CLI
version decides what the CLI adds, and it moves only with the hash-locked SDK, so an SDK upgrade is
a new archive identity. The per-call parts, the date, the temporary directory and the email, are
named here as known CLI context and are **not digested**: they change every call or every day, and
digesting them would make every row its own identity.

**Subscription, never an API key.** The child runs with `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` emptied. The first call of a pass is a contract check, before any row is
written: `apiKeySource == "none"`, `tools == []`, `mcp_servers == []`, and `claude_code_version`
equal to the pin. A mismatch is a stop with nothing spent.

**Cost is zero by construction.** `cost_usd` is `ZERO`, the operator's real marginal spend, so
`per_row_cost` reads 0 under this binding and the projection says why. The SDK's API-equivalent
price is logged per batch as `notional_usd`, as text, and never summed as money. The log also
carries `runtime`.

**The quota is a stop, not a failure.** `RateLimitEvent.rate_limit_info.status` other than
`allowed` stops the pass, carrying `resets_at`; the error text is the fallback. `archive summarise`
exits 5 on it and the same command resumes. A content-safeguard block is a `ProviderError` and ends
as a refused row. A contract stop or a missing SDK exits 4.

**Commentary is a record, not evidence.** The prompt marks an attributed prediction or opinion as
`MARKET_COMMENTARY`. Those rows are kept and published, never served to the panel, never listed in
the cycle drill-down, and marked on the period page.

## Consequences

- The subscription summariser sees a preamble we did not write. It is recorded here and partly
  digested, and the rest is a stated limit of the measurement, not a hidden one.
- Summaries cost nothing, and the pass is bounded by usage windows rather than a budget: roughly
  10 to 20 hours across several `--limit` batches, resumed after each window.
- `temperature` and `json_mode` are not expressible through the CLI and are ignored. That is
  harmless for extraction and matters for seats: registering this provider for decision_lab seat
  runs (spec §7) must decide it in its own design, along with how replay's provider registry
  learns of it. It must never reach `tradebot`'s `PRESETS`.
- The SDK is an optional extra, installed from its own lock
  (`pip install --require-hashes -r requirements-claude.lock`), not a main dependency. Without it
  the binding refuses before spend. That lock is compiled constrained by `requirements.lock`
  (`-c requirements.lock`), so installing it into the bot's `.venv` adds the SDK and its own
  dependencies and moves no pin the money path runs on.
- Thinking is always adaptive, so token counts include thinking tokens the prompt did not ask for.
- The shipped binding in `archive.toml` stays the offline stub.
