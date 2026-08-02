"""Starting and stopping trading at runtime, from the CLI or from the dashboard.

Before Phase 9 the choice was made once, at boot, inside `serve_command`: either
`Supervisor.serve()` was one of the tasks the process raced for its lifetime, or it was not
(`--observe`). Turning that into an operator action needs one object that owns the task, so that
"is anything cycling right now" has a single answer nothing can disagree with.

Three things this module is careful about:

* **Stop cancels the controller's own task**, rather than calling `Supervisor.stop()` from outside
  it. Cancellation is what routes execution through `Supervisor.serve()`'s own `try/finally` — the
  same idiom the CLI's shutdown path uses. Stopping from outside and starting again quickly would
  otherwise leave two loops alive against one `Supervisor`.
* **Every precondition is re-evaluated at each start**, never cached. In live that is the four
  facts of [ADR 0012](../../docs/adr/0012-live-is-four-independent-preconditions.md), including the
  phrase, which is retyped every time (ADR 0021). Because `Supervisor.stop()` tears down every
  runner and a start rebuilds them, live permission is consulted exactly at a stop→start
  transition and can never drift mid-run.
* **Stop is not the kill switch.** It pauses supervision, the GUI equivalent of `--observe`; it
  cancels nothing at the venue and needs no typed phrase. What it does end is the only thing
  polling open orders, which is why the Control page warns while orders are still working — and
  why nothing may place a new order while stopped.

Failure semantics: `start` never raises and never half-starts. It returns the unmet preconditions
— an empty tuple means supervision is running — so a refusal is something a page can render and a
log line can list, rather than an exception the dashboard would have to catch on every route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from tradebot.control.startup import Recovery
from tradebot.core.logging import get_logger

if TYPE_CHECKING:  # the composition root builds this; importing it at runtime would be a cycle
    from tradebot.app import Application

logger = get_logger(__name__)

__all__ = ["SupervisionController"]


class SupervisionController:
    """Owns the supervisor's task. The only thing that starts or stops cycling."""

    def __init__(
        self,
        application: Application,
        *,
        recovery: Recovery | None = None,
        serve: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._application = application
        self._recovery = recovery
        self._serve = serve or application.supervisor.serve
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether baskets are cycling. Read by every page that reports what the system is doing."""
        return self._task is not None and not self._task.done()

    def blockers(self, confirmation: str | None = None) -> tuple[str, ...]:
        """Everything standing between this process and cycling baskets, in one list.

        Rendered on the Control page as well as consulted by `start`, so an operator reads the
        whole list and fixes it once instead of discovering it one refusal at a time.
        """
        return (
            *(f"startup recovery: {failure}" for failure in self._recovery_failures()),
            *self._safety_blockers(),
            *self._panel_blockers(),
            *self._application.live_permission(confirmation).unmet,
        )

    async def start(self, *, confirmation: str | None = None) -> tuple[str, ...]:
        """Begin cycling. Returns the unmet preconditions; an empty tuple means it started."""
        if self.running:
            return ()
        unmet = self.blockers(confirmation)
        if unmet:
            logger.warning("supervision refused", extra={"unmet": list(unmet)})
            return unmet
        # Recorded before the first cycle, so "what were the limits at 04:12" is answerable from
        # the log alone rather than by joining a config document against a constant (PLAN §3.3).
        await self._application.record_limits()
        self._task = asyncio.create_task(self._serve(), name="supervisor")
        logger.warning("supervision started", extra={"mode": self._application.mode.value})
        return ()

    async def stop(self) -> None:
        """Stop cycling and release every runner. Idempotent, and never refused.

        Never refused on purpose: an operator reaches for this during an incident, which is
        precisely when it must not argue back. Orders already working at the venue are left where
        they are — cancelling them is the kill switch's job, and conflating the two would make the
        cheap action carry the expensive one's consequences.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.warning("supervision stopped", extra={"mode": self._application.mode.value})

    def _recovery_failures(self) -> tuple[str, ...]:
        """What DESIGN §8.2 could not complete. A halted recovery may never start trading."""
        if self._recovery is None:
            return ()
        return self._recovery.failures

    def _panel_blockers(self) -> tuple[str, ...]:
        """Live only: a panel that cannot be reached must not be started against real money.

        Read at every Start rather than trusted from the startup check, for the same reason the
        phrase is retyped: a panel edited in the dashboard while the process was stopped would
        otherwise be started on a gate that ran against the previous version (ADR 0021, ADR 0023).
        Sim and paper are allowed to run degraded — there the same finding is a warning the
        dashboard shows and nothing refuses.
        """
        if not self._application.mode.is_live:
            return ()
        return self._application.panel_warnings

    def _safety_blockers(self) -> tuple[str, ...]:
        """The persisted safety state. A tripped switch is cleared by a human, never by a start."""
        state = self._application.states.load()
        if state.may_trade:
            return ()
        return (
            f"the kill switch is {state.kill_switch.value}"
            f"{f' ({state.reason})' if state.reason else ''}; re-arm it before starting",
        )
