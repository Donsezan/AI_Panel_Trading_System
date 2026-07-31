# ADR 0009 — LLM providers speak plain HTTP, and a seat's fallback chain crosses vendors

**Status:** accepted (2026-07-29) · **Phase:** 4 · **Supersedes:** nothing

## Context

Phase 4 needs three provider adapters — `openai_compat`, `anthropic`, `gemini` — plus a story for
what a seat does when its model is unavailable. Two things forced a decision.

**Free model slots are unreliable by design.** v1 runs on free OpenRouter slots (PLAN scope,
2026-07-26). Those slots appear, disappear, and get rate-limited without notice; that is R11 in
the risk register, not an edge case. A panel whose seats have no answer to it degrades to `WAIT`
on an ordinary Tuesday.

**The obvious fallback is not a fallback.** Falling back from one OpenRouter model to another
OpenRouter model does not survive an OpenRouter outage, a billing problem, or a regional block —
the three things most likely to take a slot away at the same time as its neighbours.

## Decision

### Adapters are written against the HTTP APIs, not the vendor SDKs

`httpx` — already a dependency for the news pipeline — carries all three. Each adapter supplies a
path, a request body, and where to find text and token counts in the response; everything else
(timeouts, error classification, cost, latency, the empty-completion check) lives once in
`decision/providers/http.py`.

Rejected: adding `openai`, `anthropic` and `google-genai`. Three more hash-pinned dependency trees
in a process that can move money is a poor trade for three JSON POSTs (PLAN §4 treats the lock
file as supply-chain defence). The SDKs' retry and streaming machinery is not wanted here anyway:
retries belong to the seat's fallback chain, and a panel does not stream.

The cost is that we own the wire format. The mitigation is that we can *test* the wire format:
the whole provider layer runs through `httpx.MockTransport`, so the contract suite asserts the
exact URL, headers and body a vendor will receive, offline and for free.

### A fallback is a `(provider, model)` binding, not a provider id

`SeatConfig.fallbacks` is a tuple of `ProviderBinding`. A model id is only meaningful to the
provider that serves it, so a chain that carried provider ids alone could not move a seat to a
different family.

### Every seat gets its own chain, and the panel declares its own providers

`PanelConfig` carries `providers[]` alongside `seats[]`, so a panel is self-describing: one GUI
form edits the endpoints and the bindings together, and validation proves every binding resolves
*before* anything runs. Nothing outside `providers[]` is constructed or contacted.

Rejected: the earlier shape, where providers came from a CLI flag and a binding naming an unwired
provider was skipped with a warning. It made the seeded panel convenient and the failure mode
invisible — a mistyped provider produced a seat that looked configured and silently had no backup.
Configuration errors belong at configuration time.

Two rules are enforced on construction:

* **A chain may not repeat a binding.** Retrying the endpoint that just failed is not a fallback,
  and it would be a silent one, since the seat would report the binding it started on.
* **Every binding must name a declared provider.** A typo in a form fails the form.

The chains are deliberately *different per seat*. The seeded free panel:

| Seat | Primary | Falls back to |
|---|---|---|
| technical | OpenRouter (DeepSeek, free) | LM Studio (local Qwen) |
| news | OpenRouter (Llama, free) | Gemini |
| skeptic | OpenRouter (Qwen, free) | LM Studio (local Mistral) |

Three seats sharing one backup would trip `PANEL_HOMOGENEOUS` at exactly the moment the panel is
already degraded — a vendor outage would turn a heterogeneous panel into one model with three
names, which is the failure the heterogeneity control exists to prevent.

### Local runtimes are first-class providers

LM Studio and `llama.cpp --server` both speak the OpenAI chat-completions shape, so they are
`openai_compat` with a different `base_url` and no key. A local model is the one binding no hosted
outage can take away, which is why it sits at the end of the chain.

Two consequences are enforced rather than documented:

* `supports_json_mode` is configurable, because several local servers reject `response_format`.
  A hard 400 there would take the fallback out of service exactly when the slot it backs up has
  already failed.
* Plain HTTP is permitted **only** to loopback. Prompts carry position size and unrealized PnL, so
  a non-loopback endpoint must be `https` or the process refuses to start.

### Every provider failure is a `ProviderError`

Timeouts, 5xx, 429, 401/403, a vanished model, a non-JSON body, an oversized body, an empty
completion, a Gemini safety block — all of them. The uniformity is the point: a seat reacts by
trying the next binding and then abstaining, and abstentions resolve to `WAIT`, so every provider
failure ends in *no trade* rather than an exception escaping a cycle (DESIGN §8.1).

Configuration defects are the exception and are caught in the registry at wiring time, where they
can still be fatal: a missing `secret_ref` value, an unknown provider, a duplicate provider id, or
a plaintext remote endpoint all raise `ConfigError` and refuse to start. A bad key that surfaced
as a per-cycle abstention would be a permanently degraded panel nobody noticed.

## Consequences

* Adding a vendor is one small class plus one row in `PRESETS` and one `ProviderCase` in the
  contract suite. A vendor whose semantics diverge fails CI.
* API versions are pinned in code (`anthropic-version: 2023-06-01`). An API version is a wire
  contract; a silent upgrade mid-soak would change panel behaviour with no config change to point
  at. Upgrading is a deliberate edit.
* We track the endpoints ourselves. If a vendor makes a breaking change we find out from the
  contract suite's cassettes going stale rather than from a dependency bump — which is slower to
  notice and easier to verify.
* Error bodies quoted into abstain reasons are truncated and scrubbed through the log redactor
  before they reach a database row, because a provider echoing back a rejected `Authorization`
  header is not hypothetical (PLAN §3.2).
