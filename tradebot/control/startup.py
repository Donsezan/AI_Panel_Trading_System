"""The startup / recovery sequence. The same steps on every start (DESIGN §8.2).

1. Open the database and **verify the projections against the event log** by replaying it.
2. **Preflight the venue**: clock skew, key restrictions, and the capabilities that
   `SUBMIT_UNKNOWN` recovery depends on (`control/preflight.py`, PLAN §3.1/§3.2). Before
   reconciliation, because a skewed clock makes every later signed call unreliable.
3. For each venue: fetch open orders and `AccountState`, adopt orders carrying our
   `client_order_id` prefix, and reconcile the ledger.
4. Resolve every non-terminal order in the database to a terminal or monitored state.
5. **Re-verify every configured instrument's trading rules against the venue** — a filter changed
   under a stopped process is a lot size the risk layer would size against and the venue would
   reject (`control/reference.py`, ADR 0025). This one halts *baskets*, not the process.
6. In **live only**, check readiness: alerting configured, panels reachable, market data complete,
   every stored basket building (`control/readiness.py`). Last, because it spends provider calls
   and venue weight on a system the cheaper steps have already agreed is sound.
7. Restore persisted risk state — kill switch, halted baskets, high-water mark, day-start
   equity — and arm the watchdog. Only then may runners start.
8. **Any step failing leaves the process up and halted.** Nothing trades, and the reason is in
   the log.

Step 7 is the point of the whole module. The tempting alternative — crash on a failed recovery —
loses the one thing an operator needs, which is a running system that can be asked what went
wrong. The tempting *other* alternative — carry on and hope — is how a process resumes trading
against a position it has already lost track of.

Failure semantics: `recover` never raises. It returns a `Recovery` whose `halted` flag is the
answer, and the caller refuses to trade when it is set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from tradebot.control.preflight import VenuePreflight
from tradebot.control.readiness import LiveReadiness
from tradebot.control.reference import DriftWatch
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.enums import KillSwitchState, OrderState, RiskTier
from tradebot.core.errors import TradebotError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.orders import Fill, Order
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import RestorableVenue
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler, ReconcileReport
from tradebot.persistence.schema import fills as fills_table
from tradebot.persistence.schema import orders as orders_table
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskState, RiskStateStore
from tradebot.risk.watchdog import Watchdog

logger = get_logger(__name__)


@dataclass(slots=True)
class Recovery:
    """What the startup sequence found, and whether the system may trade."""

    state: RiskState
    replayed: int = 0
    reports: tuple[ReconcileReport, ...] = ()
    resolved: tuple[Order, ...] = ()
    halted_baskets: dict[str, str] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    @property
    def halted(self) -> bool:
        """Anything unresolved means nothing trades — a partial recovery is not a recovery."""
        return bool(self.failures) or not self.state.may_trade

    def may_run(self, basket: Basket) -> bool:
        return not self.halted and basket.basket_id not in self.halted_baskets


class StartupSequence:
    """Runs DESIGN §8.2 in order, and stops at the first step that cannot complete."""

    def __init__(
        self,
        store: EventStore,
        ledger: Ledger,
        reconciler: Reconciler,
        execution: ExecutionService,
        monitor: ExecutionMonitor,
        states: RiskStateStore,
        watchdog: Watchdog,
        clock: Clock,
        *,
        instruments: Sequence[Instrument] = (),
        quote_currency: str = "USDT",
        venue_restore: RestorableVenue | None = None,
        preflight: VenuePreflight | None = None,
        readiness: LiveReadiness | None = None,
        drift: DriftWatch | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._reconciler = reconciler
        self._execution = execution
        self._monitor = monitor
        self._states = states
        self._watchdog = watchdog
        self._clock = clock
        self._instruments = {i.key: i for i in instruments}
        self._quote_currency = quote_currency
        self._venue_restore = venue_restore
        #: Absent for a simulated venue, which has no clock of its own to disagree with and no key
        #: to hold permissions. Present for every real one.
        self._preflight = preflight
        #: Live only. Sim and paper are allowed to run degraded — that is what they are for.
        self._readiness = readiness
        #: Every mode. Unlike the gates above, it halts *baskets* rather than the process, and
        #: only in the modes whose cycles are evidence (ADR 0025).
        self._drift = drift

    async def recover(self) -> Recovery:
        """Bring the process to a known state, or to a halted one."""
        failures: list[str] = []
        replayed = 0
        reports: tuple[ReconcileReport, ...] = ()
        resolved: tuple[Order, ...] = ()
        # Captured before anything writes: reconciliation against a real venue records the
        # account's funds as an external flow, which persists a risk row of its own.
        first_run = not self._states.initialised()

        try:
            replayed = await self._replay()
            self._restore_simulated_venue()
        except TradebotError as exc:
            failures.append(f"projection replay failed: {exc}")

        if not failures and self._preflight is not None:
            failures.extend(await self._preflight.run())

        if not failures:
            try:
                reports = await self._reconcile()
            except TradebotError as exc:
                failures.append(f"reconciliation failed: {exc}")

        if not failures:
            try:
                resolved = await self._resolve_open_orders()
            except TradebotError as exc:
                failures.append(f"open-order resolution failed: {exc}")

        # A basket whose trading rules have moved under it is halted, not a process failure: the
        # rest of the system is sound and the other baskets keep their evidence coming (ADR 0025).
        # Before readiness, so live does not spend provider calls probing a basket it will refuse.
        if not failures and self._drift is not None:
            await self._drift.check()

        # Last, and only in live: it spends provider calls and venue weight, so it runs once the
        # cheaper steps have agreed there is a system worth checking.
        if not failures and self._readiness is not None:
            failures.extend(await self._readiness.run())

        if not failures and first_run:
            await self._arm_first_run()

        recovery = Recovery(
            state=self._states.load(),
            replayed=replayed,
            reports=reports,
            resolved=resolved,
            halted_baskets=self._states.halted_baskets(),
            failures=tuple(failures),
        )
        await self._announce(recovery)
        return recovery

    async def _replay(self) -> int:
        """Rebuild both the read model and the in-memory ledger from the log."""
        replayed = await self._store.rebuild()
        self._ledger.replay(
            self._store.read_all(),
            {key: (i.base_currency, i.quote_currency) for key, i in self._instruments.items()},
        )
        return replayed

    def _restore_simulated_venue(self) -> None:
        """Hand a simulated venue back the books it lost when the last process exited.

        Runs between the replay and the reconciliation, so the reconciler still diffs against a
        venue rather than against itself — and a *genuine* wipe of a real venue is unaffected,
        because no real adapter implements this.
        """
        if self._venue_restore is not None:
            self._venue_restore.restore(self._ledger.snapshot(), self._persisted_open_orders())

    async def _reconcile(self) -> tuple[ReconcileReport, ...]:
        """Adopt venue truth. An unclean report halts rather than being trusted."""
        report = await self._reconciler.reconcile()
        for flow in self._reconciler.apply_external_flows(report):
            await self._watchdog.record_flow(flow.amount, flow.reason)
        if report.clean:
            return (report,)

        equity = self._ledger.equity({}, quote_currency=self._quote_currency)
        if self._reconciler.exceeds_kill_tolerance(report, equity):
            await self._watchdog.trip(report.classification.value, report.detail)
        raise _RecoveryHaltError(
            f"{report.classification.value}: {report.detail or 'see event log'}"
        )

    async def _resolve_open_orders(self) -> tuple[Order, ...]:
        """Every non-terminal order in the database becomes terminal or monitored (step 3)."""
        resolved: list[Order] = []
        for order in self._persisted_open_orders():
            instrument = self._instruments.get(order.instrument_key)
            if instrument is None:
                raise _RecoveryHaltError(
                    f"order {order.client_order_id} references unknown instrument "
                    f"{order.instrument_key}; its state cannot be resolved"
                )
            synced = await self._execution.recover(order, instrument)
            if synced.state.is_open:
                self._monitor.track(synced, instrument)
            resolved.append(synced)
        return tuple(resolved)

    def _persisted_open_orders(self) -> tuple[Order, ...]:
        """Orders the database still believes are working, including `SUBMIT_UNKNOWN` ones.

        Their fills are loaded too. Without them a partially filled entry looks untouched after
        a restart, and the monitor would size its protective legs against a quantity that is not
        held — the exact mistake the legs exist to prevent.
        """
        with self._store.engine.connect() as connection:
            rows = connection.execute(
                select(orders_table).where(
                    orders_table.c.state.in_(
                        [state.value for state in OrderState if not state.is_terminal]
                    )
                )
            ).all()
            booked = {
                client_order_id: tuple(
                    Fill(
                        fill_id=fill.fill_id,
                        client_order_id=fill.client_order_id,
                        instrument_key=fill.instrument_key,
                        side=fill.side,
                        qty=fill.qty,
                        price=fill.price,
                        fee=fill.fee,
                        fee_currency=fill.fee_currency or "",
                        filled_at=fill.filled_at,
                    )
                    for fill in connection.execute(
                        select(fills_table).where(fills_table.c.client_order_id == client_order_id)
                    )
                )
                for client_order_id in {row.client_order_id for row in rows}
            }
        return tuple(
            Order(
                fills=booked[row.client_order_id],
                client_order_id=row.client_order_id,
                basket_id=row.basket_id,
                cycle_id=row.cycle_id,
                instrument_key=row.instrument_key,
                side=row.side,
                qty=row.qty,
                order_type=row.order_type,
                limit_price=row.limit_price,
                stop_price=row.stop_price,
                role=row.role,
                group_id=row.group_id or row.client_order_id,
                expires_at=row.expires_at,
                state=OrderState(row.state),
                venue_order_id=row.venue_order_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        )

    async def _announce(self, recovery: Recovery) -> None:
        events = EventFactory(clock=self._clock, basket_id="global", cycle_id="startup")
        if not recovery.failures:
            logger.info(
                "startup recovery complete",
                extra={
                    "replayed": recovery.replayed,
                    "resolved": len(recovery.resolved),
                    "kill_switch": recovery.state.kill_switch.value,
                    "halted_baskets": sorted(recovery.halted_baskets),
                },
            )
            return
        detail = "; ".join(recovery.failures)
        await self._store.append(
            events.risk_event(
                tier=RiskTier.RECONCILIATION,
                rule="startup_recovery",
                scope="process",
                action="halted",
                detail=detail,
            )
        )
        logger.error("startup recovery failed; nothing will trade", extra={"detail": detail})

    async def _arm_first_run(self) -> RiskState:
        """Establish the baselines so the watchdog has something to measure against.

        Reached only on a database that has never held risk state — the caller checks that before
        any step can write one. A switch tripped by a real breach stays tripped until a human types
        the re-arm phrase; that is the entire point of persisting it (DESIGN §8.2 step 4).
        """
        state = self._states.load()
        if state.kill_switch is KillSwitchState.ARMED:
            return state
        equity = self._ledger.equity(
            {p.instrument_key: p.avg_entry for p in self._ledger.positions()},
            quote_currency=self._quote_currency,
        )
        return await self._watchdog.rearm(equity, actor="first_run")


class _RecoveryHaltError(TradebotError):
    """Internal: a recovery step that cannot complete. Converted into a halt, never raised out."""
