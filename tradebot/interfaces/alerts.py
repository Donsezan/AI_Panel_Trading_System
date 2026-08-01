"""Ops alerting: what gets sent to a human, and the seam it is sent through.

PLAN Phase 7 names five triggers — kill switch, basket halt, reconciliation mismatch, repeated
provider failure, daily summary — and Phase 8 adds the sixth a live account needs: market data
that stopped being trustworthy. All but the summary are things that already stopped the system;
the alert exists because a soak runs for weeks and nobody watches a dashboard at 03:00.

An `Alert` is a *fact that was already recorded*, rendered for a human. It carries no instruction
and nothing acts on it: alerting is downstream of the log, never upstream of a decision, so a
webhook that is slow, down, or hostile can never delay an order or change one (ADR 0019).

Failure semantics: a sink that cannot deliver raises. The dispatcher leaves its cursor where it
was and tries again on the next poll, which makes delivery **at-least-once** — a repeated
kill-switch alert is an annoyance, a missed one is the thing this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class AlertKind(StrEnum):
    """What a human is told about. Values reach a person, so they read as English."""

    KILL_SWITCH = "kill_switch"
    BASKET_HALTED = "basket_halted"
    RECON_MISMATCH = "recon_mismatch"
    PROVIDER_FAILURE = "provider_failure"
    #: A run of cycles that refused their own market data. Live additionally refuses to *start*
    #: on holed or short data (`control/readiness.py`); this is the same fault appearing later.
    DATA_STALE = "data_stale"
    DAILY_SUMMARY = "daily_summary"

    @property
    def is_urgent(self) -> bool:
        """Whether this needs someone now. Only the summary does not."""
        return self is not AlertKind.DAILY_SUMMARY


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing a human is told about, and where in the log to go and read it."""

    kind: AlertKind
    at: datetime
    title: str
    body: str
    #: What the alert is about — a basket id, a venue, `portfolio`. Never a credential.
    scope: str = ""

    @property
    def text(self) -> str:
        """The whole alert as plain text, which is all any sink actually needs."""
        prefix = "🚨" if self.kind.is_urgent else "📊"
        scope = f" [{self.scope}]" if self.scope else ""
        return f"{prefix} {self.title}{scope}\n{self.body}"


@runtime_checkable
class AlertSink(Protocol):
    """One destination. Configured from the environment, never from the database.

    Destinations are credentials — a webhook URL is a bearer secret and a Telegram bot token is
    literally in the URL path — so they follow the same rule as venue keys: environment or
    keyring, never a DB row, never a log line, never a prompt (PLAN §3.2).
    """

    sink_id: str

    async def send(self, alert: Alert) -> None: ...
