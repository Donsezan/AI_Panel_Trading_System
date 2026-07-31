"""Reaching live mode: four independent preconditions, all of which a human must satisfy.

PLAN §2.4 treats mode confusion as a catastrophic failure class, so live is not a flag. It is the
conjunction of four facts, each held in a different place, so that no single mistake produces a
live order:

1. `--mode live` on the command line — a required argument with no default.
2. The typed confirmation phrase, passed in the same invocation.
3. An `armed` row in *this mode's* database, set by a human, naming who set it.
4. A non-null notional cap on that row. "Unlimited" is not a cap anyone chose.

The reason the row lives in the database rather than in config is that the first two are transient
and the third must not be: a file left in place arms a machine after a reboot nobody authorised,
whereas a row is per-mode (paper and live never share a database) and is visible to `risk status`.

Two further preconditions are asserted elsewhere, because only the adapter can know them: the
resolved endpoint must match the mode (`venues/`), and the venue's own report must say withdrawals
are disabled on the key (`control/preflight.py`, PLAN §3.2).

Failure semantics: `assert_live_preconditions` raises `ConfigError` listing **every** unmet
precondition rather than the first, because an operator fixing them one refusal at a time is an
operator who eventually stops reading. Nothing here ever arms anything implicitly.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Engine, select

from tradebot.core.clock import Clock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import live_arming, upsert

#: Typed to arm live trading. Deliberately awkward, and deliberately not the re-arm phrase: the
#: two authorise different things, and one muscle-memorised phrase should not do both.
LIVE_CONFIRMATION_PHRASE = "I ACCEPT REAL MONEY RISK"

_SINGLETON = "global"


class LiveArming(DomainModel):
    """The persisted arming record. Absent means not armed, which is the safe default."""

    armed: bool = False
    #: Largest notional one live order may carry, in the account's quote currency.
    max_live_notional: Money | None = None
    armed_by: str = ""
    note: str = ""
    updated_at: UtcDatetime

    @property
    def ready(self) -> bool:
        return self.armed and self.max_live_notional is not None and self.max_live_notional > ZERO


class ArmingStore:
    """Reads and writes the arming row through the single writer that owns the database."""

    def __init__(self, engine: Engine, writer: SingleWriter, clock: Clock) -> None:
        self._engine = engine
        self._writer = writer
        self._clock = clock

    def load(self) -> LiveArming:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(live_arming).where(live_arming.c.scope == _SINGLETON)
            ).one_or_none()
        if row is None:
            return LiveArming(
                note="no arming row; live has never been armed in this database",
                updated_at=self._clock.now(),
            )
        return LiveArming(
            armed=bool(row.armed),
            max_live_notional=row.max_live_notional,
            armed_by=row.armed_by or "",
            note=row.note or "",
            updated_at=row.updated_at,
        )

    async def arm(self, *, actor: str, max_live_notional: Decimal, note: str = "") -> LiveArming:
        """Record that a human armed live trading with an explicit cap.

        Refuses a non-positive cap here rather than at the point of use: a zero cap would arm live
        mode and then veto every order, which looks like a broken bot instead of a refused one.
        """
        if max_live_notional <= ZERO:
            raise ConfigError(
                f"a live notional cap must be positive, got {max_live_notional}; arming with no "
                "usable cap would look like a bug rather than a decision"
            )
        return await self._save(
            LiveArming(
                armed=True,
                max_live_notional=max_live_notional,
                armed_by=actor,
                note=note,
                updated_at=self._clock.now(),
            )
        )

    async def disarm(self, *, actor: str, reason: str = "") -> LiveArming:
        """Withdraw arming. The cap is kept on the row so re-arming shows what it was."""
        current = self.load()
        return await self._save(
            current.model_copy(
                update={
                    "armed": False,
                    "armed_by": actor,
                    "note": reason,
                    "updated_at": self._clock.now(),
                }
            )
        )

    async def _save(self, arming: LiveArming) -> LiveArming:
        values: dict[str, object] = {
            "scope": _SINGLETON,
            "armed": int(arming.armed),
            "max_live_notional": arming.max_live_notional,
            "armed_by": arming.armed_by,
            "note": arming.note,
            "updated_at": arming.updated_at,
        }
        await self._writer.run(
            lambda connection: upsert(connection, live_arming, values, ["scope"])
        )
        return arming


def assert_live_confirmation(phrase: str | None) -> None:
    if phrase != LIVE_CONFIRMATION_PHRASE:
        raise ConfigError(
            f"live mode requires the exact phrase {LIVE_CONFIRMATION_PHRASE!r}; "
            "this is a deliberate human act, never a default"
        )


def assert_live_preconditions(
    mode: Mode,
    *,
    confirmation: str | None,
    arming: LiveArming,
    credentials: bool,
) -> Decimal:
    """Refuse live unless every precondition holds. Returns the cap the policy must enforce.

    Non-live modes return zero and assert nothing: the whole point is that these gates exist only
    on the one path that can lose real money.
    """
    if not mode.is_live:
        return ZERO

    unmet: list[str] = []
    if confirmation != LIVE_CONFIRMATION_PHRASE:
        unmet.append(f"the typed confirmation phrase ({LIVE_CONFIRMATION_PHRASE!r})")
    if not arming.armed:
        unmet.append("an armed row in the live database (see `tradebot risk arm-live`)")
    if arming.max_live_notional is None or arming.max_live_notional <= ZERO:
        unmet.append("a positive max_live_notional cap on that row")
    if not credentials:
        unmet.append("venue credentials in the environment")
    if unmet:
        raise ConfigError(
            "refusing to start in live mode; missing: " + "; ".join(unmet) + ". "
            "Live mode cannot be reached by a default, a typo, or a missing env var (PLAN §2.4)."
        )
    assert arming.max_live_notional is not None  # narrowed by the checks above
    return arming.max_live_notional


def capped(policy: GlobalRiskPolicy, max_order_notional: Decimal) -> GlobalRiskPolicy:
    """The Tier-2 policy with the arming row's cap applied.

    The cap is enforced as an ordinary Tier-2 rule rather than as a special live-only branch, so
    the code path that checks it is the same one every other limit uses — and is therefore covered
    by the same tests instead of by the one run nobody rehearses.
    """
    if max_order_notional <= ZERO:
        return policy
    return policy.model_copy(update={"max_order_notional": max_order_notional})
