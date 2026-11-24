"""An `LLMProvider` on the operator's Claude Code subscription login (spec §5, §10).

One `CompletionRequest` is one fresh Claude Agent SDK `query()` — a new CLI process in a new empty
temp directory, with no tools, no MCP servers, no settings, one turn, and our system prompt in
place of Claude Code's. Nothing is resumed or persisted between calls, so no article can reach the
next one's context.

**No API key, ever** (§5.5). The child is started with `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` blanked, and every call reads the init message and stops unless it reports
`apiKeySource: "none"` — a key leaking in from the environment would turn "the operator's
subscription" into metered spend nobody approved.

**What the CLI adds is recorded, not stripped** (§10 (d), operator decision 2026-11-24). It
prepends `IDENTITY_PREAMBLE` and per-call context (date, temp directory, account email). The
preamble and `PINNED_CLI_VERSION` are what the archive's `prompt_digest` covers; the version is
checked on every call, because the CLI version decides what is added, and a different one is a
different prompt.

**`temperature` and `json_mode` are ignored** — the CLI exposes neither. For the summariser that
costs nothing (`parse_summary` is the only validation that ever counted). For seats (§7) it
matters: a seat bound here cannot be run at its configured temperature, and the binding that
eventually registers this provider for seat runs has to say so rather than inherit the silence.
Thinking is likewise not sent: the CLI runs adaptive thinking whatever it is told, so `effort`
is the only lever and is set low.

**Cost is `ZERO`** (§5.4): the operator's marginal spend on a subscription. The SDK's
API-equivalent figure is a float; it is kept as text in `notional_usd` for reference and never
becomes money (ADR 0001).

Failure semantics, per fault row of §5.6:

* Usage or rate limit — a `RateLimitEvent` whose status is anything but `allowed`, or an error
  whose text names a limit — raises `QuotaExhausted` (fatal), carrying the reset time when the
  SDK gives one. The pass stops; resuming after the window is the operator's act.
* Wrong auth source, tools or MCP servers offered, a different CLI version, more than one turn,
  a tool used, or an answer from a model other than the one requested raises
  `SubscriptionContractError` (fatal). Nothing further is asked.
* The SDK not installed, the CLI missing, or not logged in raises `SdkMissing` (a `ConfigError`)
  naming the fix — refused before anything is spent.
* A CLI crash, a timeout, an error result that is not a limit (a content-safeguard block
  included), or a stream with no answer raises `ProviderError`, so the caller's retry-then-refuse
  applies and the row ends refused rather than the pass ending.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from tradebot.core.clock import Clock, SystemClock
from tradebot.core.errors import ConfigError, FatalError, ProviderError
from tradebot.core.money import ZERO
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage

PROVIDER_ID: Final = "claude-subscription"

#: System block 1 of every request the CLI sends, observed in the spike (§10 (d) 1).
IDENTITY_PREAMBLE: Final[str] = "You are a Claude agent, built on Anthropic's Claude Agent SDK."

#: The CLI bundled with the hash-pinned `claude-agent-sdk==0.2.183`. It decides what the CLI adds
#: to our prompt, so it is part of the archive's identity; an SDK upgrade is a new archive.
PINNED_CLI_VERSION: Final = "2.1.326"

INSTALL_HINT: Final = (
    "install the `claude-subscription` extra: "
    "pip install --require-hashes -r requirements-claude.lock"
)
LOGIN_HINT: Final = "log in with `claude /login` on the operator's subscription"

#: A family alias (`haiku`) is whatever the CLI decides today; only an exact id names a model.
_PINNED: Final = re.compile(r"^claude-[a-z]+-\d+-\d+(-\d{8})?$")
#: The fallback for a limit the stream did not announce with a `RateLimitEvent` (§10 (e)).
_LIMIT_TEXT: Final = re.compile(r"(?i)usage limit|rate limit|limit reached")
_LOGIN_TEXT: Final = re.compile(r"(?i)not logged in|/login")

#: The result subtype the CLI reports when a call goes past `max_turns` (SDK `ResultError`).
MAX_TURNS_SUBTYPE: Final = "error_max_turns"

QueryFn = Callable[..., AsyncIterator[object]]


class QuotaExhausted(FatalError):  # noqa: N818 - the plan's name; the class is the instruction
    """The subscription's usage or rate limit is reached. Stop the pass; resume after the reset."""


class SubscriptionContractError(FatalError):
    """The call did not run under the contract §5 requires. Nothing further is asked."""


class SdkMissing(ConfigError):  # noqa: N818 - the plan's name, beside the bot's ConfigError
    """The SDK, the CLI or the login is missing. Refused before spend, naming the fix."""


def is_pinned(model: str) -> bool:
    """True for an exact model id, optionally dated; false for any family alias."""
    return _PINNED.fullmatch(model) is not None


def _import_sdk() -> Any:
    """The SDK module, imported lazily so this module imports without the extra."""
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise SdkMissing(f"claude-agent-sdk is not installed; {INSTALL_HINT}") from exc
    return claude_agent_sdk


class ClaudeSubscriptionProvider:
    """One fresh SDK `query()` per request, on the subscription login."""

    provider_id = PROVIDER_ID

    def __init__(self, *, query_fn: QueryFn | None = None, clock: Clock | None = None) -> None:
        sdk = _import_sdk()
        self._sdk = sdk
        self._query: QueryFn = query_fn if query_fn is not None else sdk.query
        self._clock = clock if clock is not None else SystemClock()
        #: `claude-code <version>` from the latest init message; provenance for the pass.
        self.runtime: str | None = None
        #: The SDK's API-equivalent price per call, as text. Reference only — never summed.
        self.notional_usd: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        started = self._clock.monotonic()
        with tempfile.TemporaryDirectory(prefix="claude-sub-", ignore_cleanup_errors=True) as cwd:
            stream = self._query(prompt=request.user, options=self._options(request, cwd))
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    text, prompt_tokens, completion_tokens = await self._read(stream, request)
            except TimeoutError as exc:
                raise ProviderError(
                    f"{PROVIDER_ID} timed out after {request.timeout_seconds}s"
                ) from exc
            except self._sdk.CLINotFoundError as exc:
                raise SdkMissing(
                    f"the Claude Code CLI was not found ({exc}); {INSTALL_HINT}"
                ) from exc
            except self._sdk.ProcessError as exc:
                subtype = getattr(exc, "subtype", None)
                if subtype == MAX_TURNS_SUBTYPE:
                    raise _over_one_turn(subtype) from exc
                raise _classify(_error_text(exc)) from exc
            except self._sdk.ClaudeSDKError as exc:
                raise ProviderError(f"{PROVIDER_ID} SDK failure: {exc}") from exc
            finally:
                # Ends the CLI process before its working directory is removed.
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
        return CompletionResult(
            text=text,
            model_fingerprint=request.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((self._clock.monotonic() - started) * 1000),
            cost_usd=ZERO,
        )

    def _options(self, request: CompletionRequest, cwd: str) -> ClaudeAgentOptions:
        """Every option §10 (a) found effective, and nothing that could carry state over."""
        options: ClaudeAgentOptions = self._sdk.ClaudeAgentOptions(
            system_prompt=request.system,
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            strict_mcp_config=True,
            setting_sources=[],
            max_turns=1,
            effort="low",
            model=request.model,
            cwd=cwd,
            # Blank, not absent: the SDK merges this over the parent's environment.
            env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
            # Without it every call leaves a session folder holding the article (§10 (a)).
            extra_args={"no-session-persistence": None},
        )
        return options

    async def _read(
        self, stream: AsyncIterator[object], request: CompletionRequest
    ) -> tuple[str, int, int]:
        sdk = self._sdk
        initialised = False
        answered = False
        parts: list[str] = []
        async for message in stream:
            if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                self._check_init(message)
                initialised = True
            elif isinstance(message, sdk.RateLimitEvent):
                _check_rate_limit(message.rate_limit_info.status, message.rate_limit_info.resets_at)
            elif isinstance(message, sdk.AssistantMessage):
                _require_init(initialised)
                if message.model != request.model:
                    raise SubscriptionContractError(
                        f"asked for {request.model}, answered by {message.model}"
                    )
                for block in message.content:
                    if isinstance(block, sdk.ToolUseBlock):
                        raise SubscriptionContractError(f"the model used a tool: {block.name}")
                    if isinstance(block, sdk.TextBlock):
                        parts.append(block.text)
                answered = True
            elif isinstance(message, sdk.ResultMessage):
                _require_init(initialised)
                return self._finish(message, answered, parts)
        raise ProviderError(f"{PROVIDER_ID} stream ended with no answer")

    def _check_init(self, message: SystemMessage) -> None:
        data = message.data
        version = data.get("claude_code_version")
        self.runtime = f"claude-code {version}"
        source = data.get("apiKeySource")
        if source != "none":
            raise SubscriptionContractError(
                f"auth source is {source!r}, not the subscription login ('none')"
            )
        if data.get("tools") != []:
            raise SubscriptionContractError(f"tools were offered: {data.get('tools')!r}")
        if data.get("mcp_servers") != []:
            raise SubscriptionContractError(
                f"MCP servers were offered: {data.get('mcp_servers')!r}"
            )
        if version != PINNED_CLI_VERSION:
            raise SubscriptionContractError(
                f"CLI version {version!r} is not the pinned {PINNED_CLI_VERSION}"
            )

    def _finish(
        self, message: ResultMessage, answered: bool, parts: list[str]
    ) -> tuple[str, int, int]:
        # Turns first: past `max_turns` the CLI reports an *error* result (`error_max_turns`), and
        # routed as an error it would be a retryable provider failure — retrying a call that
        # broke the one-turn contract is the opposite of stopping.
        if message.subtype == MAX_TURNS_SUBTYPE or message.num_turns > 1:
            raise _over_one_turn(message.subtype, message.num_turns)
        if message.is_error:
            raise _classify(
                " ".join([message.subtype, message.result or "", *(message.errors or [])])
            )
        if not answered:
            raise ProviderError(f"{PROVIDER_ID} returned a result with no answer")
        if message.total_cost_usd is not None:
            # A float from the SDK: text for reference, never `Decimal` and never summed.
            self.notional_usd.append(str(message.total_cost_usd))
        usage = message.usage or {}
        prompt_tokens = sum(
            _tokens(usage, key)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return "".join(parts), prompt_tokens, _tokens(usage, "output_tokens")


def _over_one_turn(subtype: str, num_turns: int | None = None) -> SubscriptionContractError:
    turns = f"{num_turns} turns" if num_turns is not None else "more than one turn"
    return SubscriptionContractError(f"the call ran {turns}, not one ({subtype})")


def _require_init(initialised: bool) -> None:
    """No init message means the auth source and tool set went unchecked: fail closed."""
    if not initialised:
        raise SubscriptionContractError("no init message; the auth source cannot be confirmed")


def _check_rate_limit(status: str, resets_at: int | None) -> None:
    # `allowed_warning` stops too (spike amendment): the pass is resumable, and running into the
    # hard limit mid-batch would spend the window on rows that then refuse.
    if status == "allowed":
        return
    when = (
        datetime.fromtimestamp(resets_at, tz=UTC).isoformat()
        if resets_at is not None
        else "an unreported time"
    )
    raise QuotaExhausted(f"subscription limit status {status!r}; resets at {when}")


def _error_text(exc: BaseException) -> str:
    """The exception's text, plus its result payload, so an empty result still names its cause."""
    extra: list[str] = []
    subtype = getattr(exc, "subtype", None)
    if isinstance(subtype, str):
        extra.append(subtype)
    result = getattr(exc, "result", None)
    if isinstance(result, str):
        extra.append(result)
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list):
        extra.extend(e for e in errors if isinstance(e, str))
    return " ".join([str(exc), *extra])


def _classify(text: str) -> FatalError | ProviderError:
    """Route an error's text: a login fault, a limit, or an ordinary provider failure."""
    if _LOGIN_TEXT.search(text):
        return SdkMissing(f"the CLI is not logged in ({text.strip()}); {LOGIN_HINT}")
    if _LIMIT_TEXT.search(text):
        return QuotaExhausted(f"subscription limit reached: {text.strip()}")
    return ProviderError(f"{PROVIDER_ID} failed: {text.strip()}")


def _tokens(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0
