"""Logging: structure, correlation, and the redaction guarantee.

The redaction tests push *real-shaped* keys through the *real* logger, because a redaction
filter that is only unit-tested against its own regex proves nothing (PLAN §3.2).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from tradebot.core.logging import (
    REDACTED,
    SECRETS,
    SecretRedactor,
    configure_logging,
    correlate,
    current_correlation,
    get_logger,
)


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(mode="sim", stream=stream, level=logging.DEBUG)
    return stream


def emitted(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    SECRETS.clear()
    yield
    SECRETS.clear()
    logging.getLogger().handlers = []


class TestStructure:
    def test_each_line_is_one_json_object(self, log_stream: io.StringIO) -> None:
        get_logger("t").info("cycle started")
        (record,) = emitted(log_stream)
        assert record["message"] == "cycle started"
        assert record["level"] == "INFO"
        assert record["mode"] == "sim"

    def test_extra_fields_are_emitted(self, log_stream: io.StringIO) -> None:
        get_logger("t").info("sized", extra={"qty": "0.5"})
        assert emitted(log_stream)[0]["qty"] == "0.5"

    def test_exceptions_are_captured(self, log_stream: io.StringIO) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            get_logger("t").exception("failed")
        assert "boom" in str(emitted(log_stream)[0]["exception"])

    def test_unserializable_values_do_not_lose_the_line(self, log_stream: io.StringIO) -> None:
        get_logger("t").info("odd", extra={"thing": object()})
        assert emitted(log_stream)[0]["message"] == "odd"

    def test_reconfiguring_does_not_stack_handlers(self, log_stream: io.StringIO) -> None:
        configure_logging(mode="sim", stream=log_stream)
        get_logger("t").info("once")
        assert len(emitted(log_stream)) == 1


class TestCorrelation:
    def test_ids_attach_to_lines_inside_the_block(self, log_stream: io.StringIO) -> None:
        with correlate(cycle_id="c1", basket_id="b1"):
            get_logger("t").info("inside")
        get_logger("t").info("outside")

        inside, outside = emitted(log_stream)
        assert inside["cycle_id"] == "c1"
        assert inside["basket_id"] == "b1"
        assert "cycle_id" not in outside

    def test_blocks_nest_and_unwind(self) -> None:
        with correlate(basket_id="b1"), correlate(cycle_id="c1"):
            assert current_correlation() == {"basket_id": "b1", "cycle_id": "c1"}
        assert current_correlation() == {}


class TestRedaction:
    BINANCE_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
    OPENROUTER_KEY = "sk-or-v1-3f9a2b7c4d5e6f708192a3b4c5d6e7f8"
    ALPACA_KEY = "PKTEST1234567890ABCD"

    @pytest.mark.parametrize("secret", [BINANCE_KEY, OPENROUTER_KEY, ALPACA_KEY])
    def test_known_key_shapes_never_reach_the_log(
        self, log_stream: io.StringIO, secret: str
    ) -> None:
        get_logger("t").info("connecting with %s", secret)
        assert secret not in log_stream.getvalue()
        assert REDACTED in log_stream.getvalue()

    def test_registered_secrets_are_scrubbed_whatever_their_shape(
        self, log_stream: io.StringIO
    ) -> None:
        """The strong control: an odd-shaped key is caught because we know its value."""
        SECRETS.register("hunter2-not-a-recognisable-shape")
        get_logger("t").info("using hunter2-not-a-recognisable-shape")
        assert "hunter2" not in log_stream.getvalue()

    def test_secrets_hidden_in_extra_fields_are_scrubbed(self, log_stream: io.StringIO) -> None:
        """Redaction runs on the serialized line, so nesting cannot smuggle a key out."""
        SECRETS.register(self.BINANCE_KEY)
        get_logger("t").info("auth", extra={"venue": {"credentials": [self.BINANCE_KEY]}})
        assert self.BINANCE_KEY not in log_stream.getvalue()

    def test_credential_named_fields_are_dropped_entirely(self, log_stream: io.StringIO) -> None:
        get_logger("t").info("auth", extra={"api_key": "short", "venue": "binance"})
        record = emitted(log_stream)[0]
        assert "api_key" not in record
        assert record["venue"] == "binance"

    def test_hex_digests_survive_redaction(self, log_stream: io.StringIO) -> None:
        """Snapshot and prompt hashes are audit evidence; over-broad patterns would eat them."""
        digest = "a" * 40 + "0123456789abcdef" * 1
        get_logger("t").info("snapshot frozen", extra={"snapshot_hash": digest})
        assert emitted(log_stream)[0]["snapshot_hash"] == digest

    @pytest.mark.parametrize("too_short", ["", "abc", None])
    def test_short_values_are_not_registered(self, too_short: str | None) -> None:
        """Redacting a three-character string would corrupt unrelated log text."""
        redactor = SecretRedactor()
        redactor.register(too_short)
        assert redactor.scrub("abc def") == "abc def"
