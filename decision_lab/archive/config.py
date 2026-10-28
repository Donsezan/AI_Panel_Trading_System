"""`archive.toml` — the summariser binding and the three numbers a replay reads an archive by.

Four things live here because all four end up inside `archive_digest` (§6.6) and therefore inside
`corpus_id`: which model wrote the summaries, which prompt it was given, how `observed_at` was
derived, and the window and floor the feed reads them through. Two archives differing in any one
of them are two experiments, and the digest is what stops them being compared as one.

Every refusal happens before a single provider call, for the reason `candidates.py` validates a
matrix before any spend: a pass that discovers its binding is wrong on row 9 000 has spent 9 000
calls finding out. The binding is checked against the bot's own `PRESETS`, so an unknown provider
id is a refusal here rather than a `KeyError` at the first call.

The floor's value is measured, not chosen: `Decimal("0.25")` is the operating point from the
2026-10-05 measurement in §6.8.2.1 — normalised short text separates same-event pairs at 0.308
from unrelated ones at 0.177. It still false-merges 15% of unrelated pairs, which is exactly why
§6.8.2.1 keys clustering on the extracted fields and uses the floor only as a guard.

Failure semantics: every fault is `ConfigError` naming the field. Nothing here degrades to a
default on a malformed value — a summarising pass is a measuring instrument, and an instrument
that quietly substituted a number would produce an archive whose digest describes a policy that
was not applied.
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import field_validator

from tradebot.core.errors import ConfigError, MoneyError
from tradebot.core.schema import DomainModel, Money, parse_money
from tradebot.decision.providers.registry import PRESETS
from tradebot.news.normalize import DEFAULT_EXCERPT_CHARS

#: §6.6's example header carries `published_at+00:12:00`, and §6.7 rejects a zero lag outright.
DEFAULT_LAG_SECONDS: Final = 720

#: §6.8.1's window. 48 hours, because a replay measuring a panel the bot does not run measures
#: the wrong panel — `NewsStore.select`'s own `DEFAULT_LOOKBACK`.
DEFAULT_WINDOW_SECONDS: Final = 172_800

#: §6.8.2.1, measured 2026-10-05. See the module docstring.
DEFAULT_CLUSTER_FLOOR: Final = Decimal("0.25")

#: The shipped policy. Binds the stub, so the suite is offline and costs nothing.
DEFAULT_POLICY_FILE: Final = Path(__file__).parent.parent / "config" / "archive.toml"


_SUMMARIZER_KEYS: Final = frozenset({"provider", "model"})


class ArchivePolicy(DomainModel):
    """How one archive was summarised, and how a replay reads it."""

    provider_id: str
    model: str
    observed_at_lag_seconds: int = DEFAULT_LAG_SECONDS
    news_window_seconds: int = DEFAULT_WINDOW_SECONDS
    cluster_floor: Money = DEFAULT_CLUSTER_FLOOR
    max_summary_chars: int = DEFAULT_EXCERPT_CHARS

    @field_validator("observed_at_lag_seconds")
    @classmethod
    def _lag_is_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "observed_at_lag_seconds must be positive. §6.7 rejects observed_at = "
                "published_at: it grants the panel the headline at the instant of publication, "
                "which is optimistic in the one direction that inflates a score"
            )
        return value

    @field_validator("news_window_seconds")
    @classmethod
    def _window_is_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("news_window_seconds must be positive; a zero window selects nothing")
        return value

    @field_validator("cluster_floor")
    @classmethod
    def _floor_is_a_similarity(cls, value: Money) -> Money:
        if not Decimal(0) < value <= Decimal(1):
            raise ValueError(
                f"cluster_floor must be in (0, 1]; {value} is not a cosine similarity. A floor "
                "above 1 clusters nothing while reading as though clustering were on"
            )
        return value

    @field_validator("max_summary_chars")
    @classmethod
    def _summary_fits_the_panels_slot(cls, value: int) -> int:
        if not 0 < value <= DEFAULT_EXCERPT_CHARS:
            raise ValueError(
                f"max_summary_chars must be in (0, {DEFAULT_EXCERPT_CHARS}]; {value} is outside "
                "the slot NewsItem.view renders, so the cut would happen in the bot's renderer "
                "rather than in our refusal"
            )
        return value

    @property
    def lag(self) -> timedelta:
        return timedelta(seconds=self.observed_at_lag_seconds)

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.news_window_seconds)

    @property
    def observed_at_policy(self) -> str:
        """§6.6's header field, in the spec's own format."""
        hours, rest = divmod(self.observed_at_lag_seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"published_at+{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def is_stub(self) -> bool:
        """A stub binding makes the pass a plumbing check, and the *binding* decides — never a
        flag. The same argument §7.2 makes about a matrix: a flag leaves an archive whose digest
        records a real model beside rows that are canned text."""
        return self.provider_id == "stub"


def load_policy(path: Path) -> ArchivePolicy:
    """Read and validate `archive.toml`. Every refusal here precedes any provider call."""
    if not path.is_file():
        raise ConfigError(
            f"no archive policy at {path}. The tool ships one at {DEFAULT_POLICY_FILE}, which "
            "binds the offline stub"
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    binding = document.get("summarizer", {})
    # A typo (`modle = ...`) must not quietly become `model = ""`: every row of a paid pass would
    # then be refused for a reason only the log explains.
    unknown = sorted(set(binding) - _SUMMARIZER_KEYS)
    if unknown:
        raise ConfigError(
            f"{path} has unknown key(s) {unknown} in [summarizer]; "
            f"the keys are {sorted(_SUMMARIZER_KEYS)}"
        )
    if not str(binding.get("model", "")).strip():
        raise ConfigError(f"{path} gives [summarizer].model no value; name the model to bind")
    settings: dict[str, Any] = {
        "provider_id": str(binding.get("provider", "")),
        "model": str(binding.get("model", "")),
    }
    for key, value in document.get("archive", {}).items():
        settings[key] = _money_field(path, key, value) if key == "cluster_floor" else value

    if settings["provider_id"] not in PRESETS and settings["provider_id"] != "stub":
        known = ", ".join(sorted({*PRESETS, "stub"}))
        raise ConfigError(
            f"{path} binds the summarizer to unknown provider "
            f"{settings['provider_id']!r}; known providers: {known}"
        )
    try:
        return ArchivePolicy.model_validate(settings)
    except ValueError as exc:
        raise ConfigError(f"{path} is not a usable archive policy: {exc}") from exc


def _money_field(path: Path, key: str, value: object) -> Decimal:
    """A money-shaped field arrives as text on purpose.

    `tomllib` renders `0.25` as a Python `float`, and a binary-rounded floor would make
    `archive_digest` — and so `corpus_id` — depend on a value nobody can type exactly. The rule
    `matrices.dumps` already applies one package over, enforced on the way in rather than out.

    `parse_money` raises `MoneyError` on a float (which is a `FailClosedError`, not a
    `ValueError`) and re-raises unreadable text as `ValueError`. Both are caught, and named
    explicitly, because at this boundary both mean the same thing: the operator's TOML is wrong.
    """
    try:
        return parse_money(value)
    except (MoneyError, ValueError) as exc:
        raise ConfigError(
            f"{path} gives {key} = {value!r}, which is not a decimal written as a string. "
            f'Quote it — {key} = "0.25" — because a TOML float is binary-rounded inside the '
            "digest that keys corpus_id"
        ) from exc
