"""Error taxonomy. Every raised error is classified, so no caller ever has to guess.

Three classes, and the class *is* the handling instruction (PLAN §6.7):

* `RetryableError`  — transient. Bounded retry with jittered backoff, honouring the rate
  budget; escalates to a basket halt if it keeps failing.
* `FailClosedError` — uncertainty. Resolves to *no trade*. Missing a trade is always
  acceptable; an unintended trade never is (PLAN §1.1).
* `FatalError`      — the process must not continue in this configuration. Refuse to start,
  or halt and require a human.

A bare `except: pass` is a defect: it converts a classified error into an unclassified one.
"""

from __future__ import annotations

from datetime import datetime


class TradebotError(Exception):
    """Base for every error this system raises deliberately."""


class RetryableError(TradebotError):
    """Transient failure. Safe to retry within the caller's retry budget."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FailClosedError(TradebotError):
    """Uncertainty that must resolve to no trade."""


class FatalError(TradebotError):
    """Unrecoverable in this configuration. Do not start, or stop and ask a human."""


# --------------------------------------------------------------------- fail-closed


class MoneyError(FailClosedError):
    """Invalid money arithmetic or a `float` in a money path."""


class DataStaleError(FailClosedError):
    """Market data exceeded its `max_age`; the cycle aborts as `DATA_STALE` (DESIGN §8.1)."""


class SchemaViolationError(FailClosedError):
    """Structured output failed validation. A malformed response is a failed vote, never a
    best-effort parse (DESIGN [L8])."""


class SourceDisallowedError(FailClosedError):
    """A publisher forbids automated fetching of this resource, or has blocked us.

    Fail-closed for that *source*: we do not fetch it, the cycle proceeds without it, and the
    snapshot records the coverage gap. Not retryable — retrying a `robots.txt` denial is the
    behaviour that turns a policy question into a legal one (PLAN §3.3).
    """


class ReconciliationMismatchError(FailClosedError):
    """Ledger and venue disagree beyond tolerance. Halt; above tolerance, kill switch."""


class SubmitUnknownError(FailClosedError):
    """A submit whose outcome the venue never confirmed.

    The only legal responses are to query the venue by `client_order_id`, or after a bounded
    window to fail the order and halt the basket. There is no resubmission path (PLAN §2.3).
    """

    def __init__(self, message: str, *, client_order_id: str) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id


class OrderRejectedError(FailClosedError):
    """The venue refused the order outright, and said so.

    A *definite* answer that nothing executed — insufficient funds, a filter violation, an
    unknown symbol. Distinguished from `SubmitUnknownError` because the two demand opposite
    handling: a rejection is recorded as `OrderState.REJECTED` and the cycle moves on, while an
    unknown outcome may only be resolved by querying the venue (PLAN §2.3).
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason or message


class OrderNotFoundError(FailClosedError):
    """The venue has no record of the order we asked about.

    Not the same as a rejection: it may mean the order was lost, or that we are querying the
    wrong account. Adapters surface it as `OrderStatus.found=False` and the execution service
    halts the basket for human review — a vanished order is never routine (DESIGN §8.1).
    """


# --------------------------------------------------------------------- retryable


class VenueError(RetryableError):
    """Venue call failed transiently (5xx, connection reset, timeout)."""


class RateLimitedError(VenueError):
    """Venue rate limit hit. Honour `retry_after_seconds`; a hard IP ban trips the kill
    switch, because continuing to hammer a banned IP extends the ban (PLAN §3.1)."""


class CircuitOpenError(RetryableError):
    """The circuit breaker for a venue is open; calls are refused without being attempted.

    Retryable by classification, but deliberately not retried *now*: continuing to call a venue
    that has failed N times in a row is how a soft failure becomes a rate-limit ban (PLAN §3.1).
    """


class ProviderError(RetryableError):
    """LLM provider failed or timed out. The seat falls back, then abstains."""


# --------------------------------------------------------------------- fatal


class ConfigError(FatalError):
    """Configuration is invalid or incomplete. Refuse to start."""


class ModeConfusionError(FatalError):
    """The adapter's resolved endpoint does not match the declared mode.

    Running live while believing you are on testnet is the classic way to lose real money
    (PLAN §2.4). Detected at startup; the process refuses to start.
    """


class VenueBannedError(FatalError):
    """The venue has banned our IP or key (Binance `418`), or is about to.

    Fatal, not retryable: every further call *extends* the ban. This trips the kill switch and
    requires a human, because an account we cannot reach is an account we cannot flatten
    (PLAN §3.1, R4).
    """

    def __init__(self, message: str, *, banned_until: datetime | None = None) -> None:
        super().__init__(message)
        self.banned_until = banned_until


class IllegalTransitionError(FatalError):
    """An order was driven through a transition its state machine forbids.

    Raised, never logged-and-continued: an order in an impossible state is a bug that must not
    reach a venue (DESIGN §6.7).
    """


class SingleWriterViolationError(FatalError):
    """A second writer tried to mutate a resource owned by one task (PLAN §2.6)."""
