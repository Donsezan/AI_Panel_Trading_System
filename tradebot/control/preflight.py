"""Venue preflight: the assertions that must hold before a single order is possible.

These run inside the startup sequence (DESIGN §8.2) and share its contract — a failure leaves the
process **up and halted**, never crashed, because an operator needs a running system they can ask
what went wrong.

Four checks, each defending a specific, documented failure:

* **Clock skew.** Binance signs on a timestamp inside a receive window, and candle alignment
  depends on the same figure. Warn past 2 s, refuse past 30 s (PLAN §3.1). Repeated signature
  rejection is itself a ban vector, so this is an account-safety check, not a tidiness one.
* **Withdrawal permission.** Where the venue will say (Binance `apiRestrictions`), a live key with
  withdrawals enabled refuses to start. Trusting a checkbox set months ago is not a control;
  asserting it every boot is (PLAN §3.2). A venue that cannot answer is recorded, not excused.
* **Query by our own id.** Without it, `SUBMIT_UNKNOWN` has no safe resolution, and an adapter
  that cannot resolve it must never trade live (PLAN §2.3).
* **Id length.** Our `client_order_id` scheme must fit the venue's cap. A truncated id at the
  venue is an id we can never query by again, which turns every ambiguous submit into a halt.

Failure semantics: `run` never raises. It returns the failures it found; anything in that tuple
halts the startup sequence. Warnings are logged and do not halt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from tradebot.core.clock import Clock
from tradebot.core.enums import Mode, OrderRole
from tradebot.core.errors import TradebotError
from tradebot.core.ids import client_order_id, protective_order_id
from tradebot.core.logging import get_logger
from tradebot.interfaces.broker import BrokerAdapter

logger = get_logger(__name__)

#: Skew we mention, and skew we refuse to trade through (PLAN §3.1).
SKEW_WARN = timedelta(seconds=2)
SKEW_HALT = timedelta(seconds=30)


@runtime_checkable
class KeyRestrictions(Protocol):
    """A venue that will report what its own API key is permitted to do.

    Optional because not every venue exposes it: Alpaca has no equivalent endpoint, and the spot
    testnet has no `sapi` at all. `None` from the call means "the venue would not say", which is
    recorded as a warning rather than silently treated as a pass.
    """

    async def withdrawals_enabled(self) -> bool | None: ...


class VenuePreflight:
    """Asserts what must be true about a venue before the system may trade on it."""

    def __init__(self, broker: BrokerAdapter, clock: Clock, *, mode: Mode) -> None:
        self._broker = broker
        self._clock = clock
        self._mode = mode

    async def run(self) -> tuple[str, ...]:
        """Every failure found, so an operator sees the whole list rather than the first item."""
        failures = [*self._check_capabilities()]
        failures.extend(await self._check_clock())
        failures.extend(await self._check_withdrawals())
        return tuple(failures)

    def _check_capabilities(self) -> tuple[str, ...]:
        capabilities = self._broker.capabilities()
        failures: list[str] = []
        if not capabilities.query_by_client_order_id:
            failures.append(
                f"{capabilities.venue_id} cannot be queried by our client_order_id, so an "
                "ambiguous submit could never be resolved safely (PLAN §2.3)"
            )
        longest = _longest_id(self._mode)
        if len(longest) > capabilities.max_client_order_id_length:
            failures.append(
                f"our client_order_id is {len(longest)} characters and {capabilities.venue_id} "
                f"caps them at {capabilities.max_client_order_id_length}; a truncated id cannot "
                "be queried by afterwards"
            )
        if not capabilities.protective_orders:
            logger.warning(
                "venue holds no protective orders; positions will carry the unprotected haircut",
                extra={"venue": capabilities.venue_id},
            )
        return tuple(failures)

    async def _check_clock(self) -> tuple[str, ...]:
        try:
            venue_now = await self._broker.server_time()
        except TradebotError as exc:
            return (f"could not read the venue's clock: {exc}",)

        skew = abs(venue_now - self._clock.now())
        if skew > SKEW_HALT:
            return (
                f"clock skew against {self._broker.venue_id} is {skew.total_seconds():.1f}s, "
                f"above the {SKEW_HALT.total_seconds():.0f}s ceiling; signed requests would be "
                "rejected and candle alignment would be wrong",
            )
        if skew > SKEW_WARN:
            logger.warning(
                "clock skew against the venue is above tolerance",
                extra={"venue": self._broker.venue_id, "skew_seconds": skew.total_seconds()},
            )
        return ()

    async def _check_withdrawals(self) -> tuple[str, ...]:
        """A live key that may withdraw is a refusal. Any other mode is a note.

        The asymmetry is the point: on a testnet or a paper account there is nothing to withdraw,
        while on live this is the single control standing between a compromised process and an
        empty account (PLAN §3.2).
        """
        if not isinstance(self._broker, KeyRestrictions):
            logger.info(
                "venue does not report key restrictions; withdrawal permission is a human "
                "precondition (see docs/OPERATIONS.md)",
                extra={"venue": self._broker.venue_id},
            )
            return ()
        try:
            enabled = await self._broker.withdrawals_enabled()
        except TradebotError as exc:
            if self._mode.is_live:
                return (f"could not verify that withdrawals are disabled on this key: {exc}",)
            logger.warning("key restrictions unavailable", extra={"error": str(exc)})
            return ()

        if enabled is None:
            logger.warning(
                "venue would not report withdrawal permission",
                extra={"venue": self._broker.venue_id},
            )
            return ()
        if enabled and self._mode.is_live:
            return (
                f"{self._broker.venue_id} reports withdrawals ENABLED on this API key; disable "
                "them at the venue before trading live (PLAN §3.2)",
            )
        if enabled:
            logger.warning(
                "api key may withdraw; disable it at the venue before this key is ever used live",
                extra={"venue": self._broker.venue_id},
            )
        return ()


def _longest_id(mode: Mode) -> str:
    """The longest id our scheme can mint, so the cap is checked against a real value.

    A protective leg's id is derived from an entry's and is the longer of the two, so it is what
    the venue's limit has to accommodate.
    """
    entry = client_order_id(
        mode=mode, basket_id="basket", cycle_id="cycle", instrument="venue:BASE/QUOTE", seq=99
    )
    return max(entry, protective_order_id(entry, OrderRole.STOP_LOSS, 99), key=len)
