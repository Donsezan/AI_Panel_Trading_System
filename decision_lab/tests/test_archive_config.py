"""`archive.toml`: the summariser binding, and the three numbers that ride in `archive_digest`.

Every refusal here happens before a single provider call, for the same reason `candidates.py`
validates a matrix before any spend: a pass that discovers its binding is wrong on row 9 000 has
spent 9 000 calls finding out.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab.archive.config import (
    DEFAULT_CLUSTER_FLOOR,
    DEFAULT_POLICY_FILE,
    ArchivePolicy,
    load_policy,
)
from tradebot.core.errors import ConfigError

GOOD = """
[summarizer]
provider = "stub"
model = "stub-summary"

[archive]
observed_at_lag_seconds = 720
news_window_seconds = 172800
cluster_floor = "0.25"
max_summary_chars = 280
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "archive.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_file_loads_and_binds_the_stub() -> None:
    """The suite stays offline and free, consistent with the bot's own rule that real models are
    off unless asked for. A shipped file binding a real endpoint would make `pytest` spend money."""
    policy = load_policy(DEFAULT_POLICY_FILE)
    assert policy.is_stub
    assert policy.cluster_floor == DEFAULT_CLUSTER_FLOOR


def test_a_loaded_policy_renders_the_observed_at_policy_string(tmp_path: Path) -> None:
    """§6.6's header field, and §6.7's banner reads it. The format is the spec's own."""
    policy = load_policy(write(tmp_path, GOOD))
    assert policy.observed_at_policy == "published_at+00:12:00"
    assert policy.lag == timedelta(minutes=12)
    assert policy.window == timedelta(hours=48)


def test_a_zero_lag_is_refused_by_name(tmp_path: Path) -> None:
    """§6.7 rejects `observed_at = published_at`: it grants the panel the headline at the instant
    of publication, which is optimistic in the one direction that inflates a score."""
    with pytest.raises(ConfigError, match="observed_at_lag_seconds"):
        load_policy(write(tmp_path, GOOD.replace("= 720", "= 0")))


def test_a_floor_outside_zero_to_one_is_refused(tmp_path: Path) -> None:
    """A similarity is a cosine. A floor of 1.4 clusters nothing and reads as "clustering is on"."""
    for bad in ("0", "1.4", "-0.2"):
        with pytest.raises(ConfigError, match="cluster_floor"):
            load_policy(write(tmp_path, GOOD.replace('"0.25"', f'"{bad}"')))


def test_a_float_floor_is_refused_naming_the_path(tmp_path: Path) -> None:
    """`tomllib` parses `0.25` as a Python float, and a binary-rounded floor would make the
    digest — and therefore `corpus_id` — depend on a value nobody can type exactly. Quoted
    strings only, the rule `matrices.dumps` already applies one package over."""
    with pytest.raises(ConfigError, match="cluster_floor"):
        load_policy(write(tmp_path, GOOD.replace('"0.25"', "0.25")))


def test_an_unknown_provider_is_refused_before_any_spend(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nosuchprovider"):
        load_policy(write(tmp_path, GOOD.replace('"stub"', '"nosuchprovider"')))


def test_a_missing_file_names_the_shipped_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"archive\.toml"):
        load_policy(tmp_path / "absent.toml")


def test_malformed_toml_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_policy(write(tmp_path, "[summarizer\nprovider = "))


def test_a_max_summary_above_the_panels_slot_is_refused(tmp_path: Path) -> None:
    """`DEFAULT_EXCERPT_CHARS = 280` is the slot `NewsItem.view` renders. A policy permitting 600
    would mean the cut happens in the bot's renderer, silently, instead of in our refusal."""
    with pytest.raises(ConfigError, match="max_summary_chars"):
        load_policy(write(tmp_path, GOOD.replace("= 280", "= 600")))


def test_defaults_apply_when_the_archive_table_is_absent(tmp_path: Path) -> None:
    """A policy file naming only the binding is complete: the three reading numbers have
    defaults, and the digest records whichever were in force."""
    policy = load_policy(
        write(tmp_path, '[summarizer]\nprovider = "stub"\nmodel = "stub-summary"\n')
    )
    assert policy.cluster_floor == DEFAULT_CLUSTER_FLOOR
    assert policy.lag == timedelta(minutes=12)


def test_the_policy_model_rejects_an_unknown_key(tmp_path: Path) -> None:
    """`DomainModel` forbids extras, so a mistyped `cluster_flor` is a refusal rather than a
    silently-ignored line that leaves the measured default in force."""
    with pytest.raises(ConfigError):
        load_policy(write(tmp_path, GOOD.replace("cluster_floor", "cluster_flor")))


def test_a_policy_built_directly_still_validates() -> None:
    with pytest.raises(ValueError):
        ArchivePolicy(
            provider_id="stub",
            model="stub-summary",
            observed_at_lag_seconds=-1,
            news_window_seconds=172_800,
            cluster_floor=Decimal("0.25"),
            max_summary_chars=280,
        )


def test_an_empty_model_is_refused_naming_the_key_and_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[summarizer]\nprovider = "stub"\nmodel = ""\n')
    with pytest.raises(ConfigError, match=r"archive\.toml.*model|model.*archive\.toml"):
        load_policy(path)


def test_a_misspelt_summarizer_key_is_refused_not_read_as_an_empty_model(tmp_path: Path) -> None:
    path = write(tmp_path, '[summarizer]\nprovider = "stub"\nmodle = "stub-summary"\n')
    with pytest.raises(ConfigError, match="modle"):
        load_policy(path)
