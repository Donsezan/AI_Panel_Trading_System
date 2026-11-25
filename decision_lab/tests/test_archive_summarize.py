"""§6.5's response contract: what a summary may carry, and what it is refused for.

The prompt is asserted structurally rather than by its wording wherever possible — a test that
greps the prompt text for "no outlook" passes on a prompt that says it and then asks for one
anyway. What *is* asserted on the text is the inverse: that the three things §6.5 forbids giving
the model have no way to reach it, which is a property of `summary_user`'s signature.
"""

from __future__ import annotations

import json

import pytest

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.fields import EventType, Modality
from decision_lab.archive.summarize import (
    SUMMARY_SYSTEM,
    SummaryResponse,
    digest_for,
    parse_summary,
    prompt_digest,
    summary_user,
    system_for,
)
from tradebot.core.errors import SchemaViolationError

GOOD = {
    "summary": (
        "Kraken received EMI authorisation from the FCA, covering e-money services in the UK."
    ),
    "event_type": "REGULATORY",
    "entities": ["Kraken", "FCA"],
    "assets": ["NONE"],
    "magnitudes": [],
    "modality": "CONFIRMED",
}


def test_a_well_formed_reply_parses() -> None:
    parsed = parse_summary(json.dumps(GOOD), max_chars=280)
    assert parsed.event_type is EventType.REGULATORY
    assert parsed.modality is Modality.CONFIRMED
    assert parsed.entities == ("Kraken", "FCA")


def test_a_fenced_reply_parses_because_a_fence_is_a_provider_habit() -> None:
    """The bot tolerates this for a seat vote; an archive pass meets the same habit."""
    fenced = "Here is the summary:\n```json\n" + json.dumps(GOOD) + "\n```\n"
    assert parse_summary(fenced, max_chars=280).summary == GOOD["summary"]


def test_a_reply_with_no_json_at_all_is_a_schema_violation() -> None:
    with pytest.raises(SchemaViolationError):
        parse_summary("I cannot summarise this article.", max_chars=280)


def test_truncated_json_is_a_schema_violation_rather_than_a_partial_summary() -> None:
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(GOOD)[:60], max_chars=280)


def test_a_summary_longer_than_the_slot_is_refused_with_its_length() -> None:
    """Review Focus 2. `NewsItem.view` would carry an overrun into the panel's 280-character slot
    and the cut would be the renderer's, silently. Ours, named, and tallied instead."""
    long = dict(GOOD, summary="x" * 281)
    with pytest.raises(SchemaViolationError, match="281"):
        parse_summary(json.dumps(long), max_chars=280)


def test_a_summary_at_exactly_the_slot_is_accepted() -> None:
    exact = dict(GOOD, summary="x" * 280)
    assert len(parse_summary(json.dumps(exact), max_chars=280).summary) == 280


def test_an_empty_summary_is_refused() -> None:
    """A model that answered with an empty string answered nothing, and an empty excerpt in the
    archive reads to the panel as a headline with no content rather than as a missing row."""
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(dict(GOOD, summary="   ")), max_chars=280)


def test_an_invented_event_type_is_refused_rather_than_coerced() -> None:
    """Review Focus 4. `event_type` is a third of the §6.8.2.1 cluster key, so a free-text value
    splits one event across two slots and the agreement figures stop meaning anything."""
    with pytest.raises(SchemaViolationError, match="event_type"):
        parse_summary(json.dumps(dict(GOOD, event_type="regulatory-approval")), max_chars=280)


def test_an_invented_modality_is_refused_rather_than_coerced() -> None:
    """Review Focus 4, the half that bites hardest: §6.8.2's correction rule *dispatches* on
    modality, so an unrecognised value collapses a retraction behind the claim it retracts."""
    with pytest.raises(SchemaViolationError, match="modality"):
        parse_summary(json.dumps(dict(GOOD, modality="SPECULATIVE")), max_chars=280)


def test_an_unknown_asset_token_is_refused() -> None:
    """§6.5: the text either names an asset or it does not. Inferring one is the relevance
    filter's job and it is the bot's — a model writing "SOL" here would be answering a question
    the archive did not ask, and `KeywordRelevanceFilter` never reads this field."""
    with pytest.raises(SchemaViolationError, match="assets"):
        parse_summary(json.dumps(dict(GOOD, assets=["SOL"])), max_chars=280)


def test_an_extra_key_is_refused_so_sentiment_can_never_arrive() -> None:
    """§6.5's "what it never carries" list, enforced rather than requested. A model volunteering
    `sentiment` or `expected_impact` would become the fourteenth seat every candidate shares."""
    with pytest.raises(SchemaViolationError):
        parse_summary(json.dumps(dict(GOOD, sentiment="bullish")), max_chars=280)


def test_the_user_message_carries_only_the_title_and_the_body() -> None:
    """§6.5's "by what it is not given" closure, asserted on the signature's effect: there is no
    parameter through which an instrument, a price or a sibling article could arrive."""
    rendered = summary_user(title="Kraken licensed in the UK", body="The FCA granted Kraken...")
    assert "Kraken licensed in the UK" in rendered
    assert "The FCA granted Kraken" in rendered
    for leak in ("BTC/USDT", "ATR", "RSI", "position", "equity"):
        assert leak not in rendered


def test_the_system_prompt_names_the_schema_and_the_vocabularies() -> None:
    """The one thing worth asserting on the prompt's text: a model cannot answer in a closed
    vocabulary it was never shown, and a drifted prompt would refuse every row at parse time."""
    for modality in Modality:
        assert modality.value in SUMMARY_SYSTEM
    for event_type in EventType:
        assert event_type.value in SUMMARY_SYSTEM


def test_the_prompt_digest_moves_when_the_prompt_does() -> None:
    """It is a third of `archive_digest`, so an edited prompt is a different archive (§6.6)."""
    assert prompt_digest(SUMMARY_SYSTEM) != prompt_digest(SUMMARY_SYSTEM + " Be brief.")
    assert prompt_digest(SUMMARY_SYSTEM) == prompt_digest(SUMMARY_SYSTEM)


def test_the_response_model_is_the_only_validation() -> None:
    """Nothing restates a rule: `parse_summary` validates through the model, so adding a field
    constraint cannot be forgotten in a second place (the `forms.py` rule, one package over)."""
    with pytest.raises(ValueError):
        SummaryResponse.model_validate(dict(GOOD, event_type="NOPE"))


def _bound(provider: str, model: str) -> ArchivePolicy:
    return ArchivePolicy(provider_id=provider, model=model)


def test_stub_digest_is_unchanged() -> None:
    policy = _bound("stub", "stub-summary")
    assert digest_for(policy) == prompt_digest(system_for(policy))


def test_the_digest_covers_the_preamble_and_the_cli_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decision_lab.providers import claude_subscription as cs

    sub = _bound("claude-subscription", "claude-haiku-5-5")
    assert system_for(sub) == system_for(_bound("stub", "stub-summary"))
    first = digest_for(sub)
    assert first != digest_for(_bound("stub", "stub-summary"))
    monkeypatch.setattr(cs, "PINNED_CLI_VERSION", "9.9.9")
    assert digest_for(sub) != first
    monkeypatch.undo()
    monkeypatch.setattr(cs, "IDENTITY_PREAMBLE", "X")
    assert digest_for(sub) != first
