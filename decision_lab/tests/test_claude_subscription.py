"""`ClaudeSubscriptionProvider` against a scripted SDK stream (spec §5, §10).

No test here spawns a `claude` process: `FakeQuery` stands where `claude_agent_sdk.query` would,
yields messages built from the SDK's own types, and records the options each call was given.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from decision_lab.providers.claude_subscription import (
    IDENTITY_PREAMBLE,
    PINNED_CLI_VERSION,
    PROVIDER_ID,
    ClaudeSubscriptionProvider,
    QuotaExhausted,
    SdkMissing,
    SubscriptionContractError,
    is_pinned,
)
from tradebot.core.errors import ConfigError, FatalError, ProviderError
from tradebot.core.money import ZERO
from tradebot.interfaces.llm import CompletionRequest, LLMProvider

MODEL = "claude-haiku-5-5"
GOOD = '{"summary": "An exchange listed a token.", "event_type": "LISTING"}'
REQ = CompletionRequest(model=MODEL, system="Summarise the article.", user="ARTICLE TEXT")
USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 600,
    "cache_read_input_tokens": 20,
    "output_tokens": 42,
}


def init(**overrides: Any) -> SystemMessage:
    data: dict[str, Any] = {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "none",
        "tools": [],
        "mcp_servers": [],
        "claude_code_version": PINNED_CLI_VERSION,
        "model": MODEL,
        "cwd": "x",
    }
    data.update(overrides)
    return SystemMessage(subtype="init", data=data)


def rate_limit(status: str = "allowed", resets_at: int | None = None) -> RateLimitEvent:
    info = RateLimitInfo(status=status, resets_at=resets_at)  # type: ignore[arg-type]
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def assistant(text: str = GOOD, model: str = MODEL) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model=model)


def result(
    *, is_error: bool = False, text: str = GOOD, num_turns: int = 1, cost: Any = 0.0012
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=4,
        is_error=is_error,
        num_turns=num_turns,
        session_id="s",
        total_cost_usd=cost,
        usage=dict(USAGE),
        result=text,
    )


@dataclass
class Call:
    prompt: str
    options: ClaudeAgentOptions
    cwd_was_empty_dir: bool


class FakeQuery:
    """Stands in for `claude_agent_sdk.query`: yields a script, then raises `error` if given."""

    def __init__(self, script: list[object], error: BaseException | None = None) -> None:
        self.script = script
        self.error = error
        self.calls: list[Call] = []
        self.closed = 0

    @classmethod
    def answering(cls, *, text: str = GOOD, model: str = MODEL) -> FakeQuery:
        return cls([init(), rate_limit(), assistant(text, model), result(text=text)])

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[object]:
        cwd = Path(str(options.cwd))
        self.calls.append(Call(prompt, options, cwd.is_dir() and not any(cwd.iterdir())))
        return self._stream()

    async def _stream(self) -> AsyncIterator[object]:
        try:
            for message in self.script:
                yield message
            if self.error is not None:
                raise self.error
        finally:
            self.closed += 1


async def test_one_request_is_one_fresh_query_with_our_prompt_alone() -> None:
    fake = FakeQuery.answering(text=GOOD, model=MODEL)
    provider = ClaudeSubscriptionProvider(query_fn=fake)
    outcome = await provider.complete(REQ)

    assert len(fake.calls) == 1
    opts = fake.calls[0].options
    assert opts.system_prompt == REQ.system
    assert isinstance(opts.system_prompt, str)
    assert opts.max_turns == 1
    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.strict_mcp_config is True
    assert opts.setting_sources == []
    assert opts.effort == "low"
    assert opts.thinking is None
    assert opts.extra_args == {"no-session-persistence": None}
    assert fake.calls[0].cwd_was_empty_dir
    assert opts.model == MODEL
    assert opts.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}
    assert fake.calls[0].prompt == REQ.user
    assert outcome.text == GOOD
    assert outcome.model_fingerprint == MODEL
    assert outcome.cost_usd == ZERO
    assert fake.closed == 1
    gone = not Path(str(opts.cwd)).exists()  # noqa: ASYNC240 - a local check, not I/O on a loop
    assert gone, "the temp directory outlived its call"


def test_it_is_an_llm_provider() -> None:
    provider = ClaudeSubscriptionProvider(query_fn=FakeQuery.answering())
    assert isinstance(provider, LLMProvider)
    assert provider.provider_id == PROVIDER_ID == "claude-subscription"
    assert IDENTITY_PREAMBLE == "You are a Claude agent, built on Anthropic's Claude Agent SDK."


async def test_two_requests_never_share_a_session() -> None:
    fake = FakeQuery.answering()
    provider = ClaudeSubscriptionProvider(query_fn=fake)
    await provider.complete(REQ)
    fake.script = FakeQuery.answering().script
    await provider.complete(REQ)

    first, second = (c.options for c in fake.calls)
    assert first is not second
    assert first.cwd != second.cwd
    for opts in (first, second):
        assert opts.resume is None
        assert opts.session_id is None
        assert opts.continue_conversation is False
        assert opts.fork_session is False


async def test_tokens_come_from_the_result_usage() -> None:
    outcome = await ClaudeSubscriptionProvider(query_fn=FakeQuery.answering()).complete(REQ)
    assert outcome.prompt_tokens == 10 + 600 + 20
    assert outcome.completion_tokens == 42


async def test_notional_cost_is_kept_as_text_never_money() -> None:
    provider = ClaudeSubscriptionProvider(query_fn=FakeQuery.answering())
    outcome = await provider.complete(REQ)
    assert provider.notional_usd == ["0.0012"]
    assert outcome.cost_usd == ZERO


async def test_an_api_key_auth_source_stops_before_any_row() -> None:
    fake = FakeQuery([init(apiKeySource="ANTHROPIC_API_KEY"), assistant(), result()])
    with pytest.raises(SubscriptionContractError, match="ANTHROPIC_API_KEY"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)
    assert fake.closed == 1


async def test_offered_tools_stop_the_pass() -> None:
    fake = FakeQuery([init(tools=["Bash"]), assistant(), result()])
    with pytest.raises(SubscriptionContractError, match="Bash"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_an_mcp_server_stops_the_pass() -> None:
    server = {"name": "github", "status": "connected"}
    fake = FakeQuery([init(mcp_servers=[server]), assistant(), result()])
    with pytest.raises(SubscriptionContractError, match="github"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_different_cli_version_stops_the_pass() -> None:
    fake = FakeQuery([init(claude_code_version="2.1.400"), assistant(), result()])
    with pytest.raises(SubscriptionContractError, match=r"2\.1\.400"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_an_answer_without_an_init_message_stops() -> None:
    fake = FakeQuery([assistant(), result()])
    with pytest.raises(SubscriptionContractError, match="init"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_more_than_one_turn_stops() -> None:
    fake = FakeQuery([init(), assistant(), result(num_turns=2)])
    with pytest.raises(SubscriptionContractError, match="turn"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_tool_use_block_stops() -> None:
    used = AssistantMessage(
        content=[ToolUseBlock(id="t", name="Bash", input={"command": "ls"})], model=MODEL
    )
    fake = FakeQuery([init(), used, result()])
    with pytest.raises(SubscriptionContractError, match="Bash"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_different_answering_model_stops_and_names_it() -> None:
    fake = FakeQuery.answering(text=GOOD, model="claude-haiku-5-5-20261001")
    with pytest.raises(SubscriptionContractError, match="claude-haiku-5-5-20261001"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_usage_limit_is_quota_exhausted_not_a_provider_error() -> None:
    fake = FakeQuery([init(), rate_limit("rejected", resets_at=1_795_000_000), assistant()])
    with pytest.raises(QuotaExhausted, match="2026-11-18T") as caught:
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)
    assert isinstance(caught.value, FatalError)
    assert not isinstance(caught.value, ProviderError)
    assert fake.closed == 1


async def test_a_limit_warning_also_stops() -> None:
    fake = FakeQuery([init(), rate_limit("allowed_warning"), assistant(), result()])
    with pytest.raises(QuotaExhausted, match="allowed_warning"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_limit_named_only_in_the_error_text_is_quota_exhausted() -> None:
    error = ResultError(
        "Claude Code returned an error result: Claude AI usage limit reached|1795000000",
        data={"subtype": "success", "result": "Claude AI usage limit reached|1795000000"},
        exit_code=1,
    )
    fake = FakeQuery([init(), rate_limit()], error=error)
    with pytest.raises(QuotaExhausted):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_crashed_cli_is_a_provider_error() -> None:
    fake = FakeQuery([init()], error=ProcessError("Command failed", exit_code=1, stderr="boom"))
    with pytest.raises(ProviderError, match="boom"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_an_error_result_that_is_not_a_limit_is_a_provider_error() -> None:
    fake = FakeQuery([init(), rate_limit(), result(is_error=True, text="API Error: overloaded")])
    with pytest.raises(ProviderError, match="overloaded"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_safeguard_block_is_a_provider_error_so_the_row_is_refused() -> None:
    text = "API Error: 400 Output blocked by content filtering policy. Details: [...]"
    error = ResultError(f"error result: {text}", data={"result": text}, exit_code=1)
    fake = FakeQuery([init(), rate_limit()], error=error)
    with pytest.raises(ProviderError, match="content filtering"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_stream_with_no_answer_is_a_provider_error() -> None:
    fake = FakeQuery([init(), rate_limit()])
    with pytest.raises(ProviderError, match="no answer"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_not_logged_in_is_refused_naming_the_login() -> None:
    text = "Invalid API key · Please run /login"
    error = ResultError(f"error result: {text}", data={"result": text}, exit_code=1)
    fake = FakeQuery([init()], error=error)
    with pytest.raises(SdkMissing, match="claude /login"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_missing_cli_is_refused_naming_the_install() -> None:
    fake = FakeQuery([], error=CLINotFoundError("Claude Code not found"))
    with pytest.raises(SdkMissing, match="pip install"):
        await ClaudeSubscriptionProvider(query_fn=fake).complete(REQ)


async def test_a_call_past_its_timeout_is_a_provider_error() -> None:
    class Hanging(FakeQuery):
        async def _stream(self) -> AsyncIterator[object]:
            import asyncio

            yield init()
            await asyncio.sleep(10)

    request = REQ.model_copy(update={"timeout_seconds": 0.01})
    with pytest.raises(ProviderError, match="timed out"):
        await ClaudeSubscriptionProvider(query_fn=Hanging([])).complete(request)


async def test_the_runtime_is_read_from_the_init_message() -> None:
    provider = ClaudeSubscriptionProvider(query_fn=FakeQuery.answering())
    assert provider.runtime is None
    await provider.complete(REQ)
    assert provider.runtime == f"claude-code {PINNED_CLI_VERSION}"


def test_a_missing_sdk_is_refused_naming_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(SdkMissing) as caught:
        ClaudeSubscriptionProvider()
    assert isinstance(caught.value, ConfigError)
    message = str(caught.value)
    assert "pip install" in message
    assert "requirements-claude.lock" in message
    assert "claude-subscription" in message


def test_is_pinned() -> None:
    for pinned in ("claude-haiku-5-5", "claude-haiku-5-5-20261001", "claude-sonnet-5-5"):
        assert is_pinned(pinned), pinned
    for alias in ("haiku", "sonnet", "default", "claude-haiku", "claude-haiku-5", "opus"):
        assert not is_pinned(alias), alias
    assert not is_pinned("claude-haiku-5-5-2026")
