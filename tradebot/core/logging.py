"""Structured JSON logging with correlation ids and mandatory secret redaction.

Every line carries `mode`, and — where the work has them — `cycle_id`, `basket_id` and
`client_order_id`, so any order can be traced back through its cycle to the data that produced
it. That trail is the audit artifact the design is built around (DESIGN §6.9).

Redaction is defence in depth and runs on the *final serialized line*, not just on the message:

1. **Literal registry** — actual secret values are registered at startup and scrubbed wherever
   they appear, whatever field or nesting they hide in. This is the strong control.
2. **Shape patterns** — known key formats, to catch a secret that reached a log without ever
   passing through configuration.

Hex digests are deliberately *not* matched: snapshot and prompt hashes are audit evidence and
must survive redaction (PLAN §3.2).

Failure semantics: logging never raises into a caller. If a record cannot be serialized it is
emitted with the offending fields stringified — losing a log line must not abort a trade cycle,
and an unserializable field must not become a bypass around redaction.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, TextIO

_NO_CORRELATION: Final[Mapping[str, str]] = MappingProxyType({})
_CORRELATION: ContextVar[Mapping[str, str]] = ContextVar("correlation", default=_NO_CORRELATION)

#: Field names that must never be emitted, whatever a caller passes as `extra`.
_FORBIDDEN_FIELDS: Final = frozenset(
    {"api_key", "api_secret", "secret", "password", "token", "private_key"}
)

_STANDARD_RECORD_FIELDS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

#: Shapes of credentials we know. Precise on purpose — over-broad patterns would redact the
#: hashes the audit trail depends on.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic / OpenRouter
    re.compile(r"\b[AP]K[A-Z0-9]{16,}\b"),  # Alpaca key ids
    re.compile(  # Binance-style 64-char mixed-case alnum (a hex digest cannot match)
        r"\b(?=[A-Za-z0-9]{64}\b)(?=[^\s]*[a-z])(?=[^\s]*[A-Z])(?=[^\s]*[0-9])[A-Za-z0-9]{64}\b"
    ),
)

REDACTED: Final = "***REDACTED***"


class SecretRedactor:
    """Scrubs known secret values and known secret shapes out of log output."""

    _MIN_LITERAL_LENGTH: Final = 8

    def __init__(self) -> None:
        self._literals: set[str] = set()

    def register(self, secret: str | None) -> None:
        """Register a live secret value. Short or empty values are ignored — redacting a
        three-character string would corrupt unrelated log text."""
        if secret and len(secret) >= self._MIN_LITERAL_LENGTH:
            self._literals.add(secret)

    def clear(self) -> None:
        self._literals.clear()

    def scrub(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, REDACTED)
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text


#: Process-wide registry. Populated by the composition root from env/keyring at startup.
SECRETS: Final = SecretRedactor()


@contextmanager
def correlate(**fields: str) -> Iterator[None]:
    """Attach correlation ids to every log line emitted inside this block."""
    token = _CORRELATION.set({**_CORRELATION.get(), **fields})
    try:
        yield
    finally:
        _CORRELATION.reset(token)


def current_correlation() -> Mapping[str, str]:
    return _CORRELATION.get()


class JsonFormatter(logging.Formatter):
    """Renders one log record as a single redacted JSON object."""

    def __init__(self, *, redactor: SecretRedactor = SECRETS, static: Mapping[str, str]) -> None:
        super().__init__()
        self._redactor = redactor
        self._static = dict(static)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._static,
            **_CORRELATION.get(),
            **_record_extras(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return self._redactor.scrub(json.dumps(payload, default=str, ensure_ascii=False))


def _utc_timestamp(epoch_seconds: float) -> str:
    """ISO8601 in UTC.

    `logging.Formatter.formatTime` renders *local* time; a trading log that mixes local and
    exchange time is unusable for reconstructing a decision.
    """
    moment = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Caller-supplied `extra` fields, minus anything named like a credential."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_FIELDS and key not in _FORBIDDEN_FIELDS
    }


def configure_logging(
    *, mode: str, level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    """Install the JSON handler as the sole root handler.

    Idempotent: reconfiguring replaces handlers rather than stacking them, so a re-entrant
    startup cannot produce duplicate — or unredacted — output.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(static={"mode": mode}))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
