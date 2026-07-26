"""Reconciliation: the venue is the truth, and this is where we find out how wrong we were.

Runs at startup, after any connectivity gap, and periodically (DESIGN §6.8, [L10]). It fetches
the venue's own `AccountState`, diffs it against the ledger, and — crucially — *classifies* the
difference, because the classification is the response:

| Classification     | What it means                          | What happens               |
|--------------------|----------------------------------------|----------------------------|
| `MATCH`            | identical                              | trade on                   |
| `DRIFT`            | fees, funding, dust — inside tolerance | adopt venue, log           |
| `EXTERNAL_CHANGE`  | a deposit or a manual buy              | adopt, flow-adjust         |
| `CORPORATE_ACTION` | matched to a venue announcement        | adopt, log                 |
| `VENUE_RESET`      | testnet wiped, every position gone     | halt + notify              |
| `MISMATCH`         | nothing explains it                    | halt; over tolerance, kill |

Getting this table wrong is expensive in both directions. Classifying a monthly testnet wipe as
a mismatch trips the kill switch for a routine event (R15); classifying a real discrepancy as
drift means trading on a position that does not exist (R5). So every branch is a named
classifier with its own test, and the default — the one reached when nothing matched — is the
one that stops trading.

Order adoption is by `client_order_id` prefix: an order we minted in this mode is ours to
resolve, and anything else at the venue is a human's and is left alone (DESIGN §8.2 step 2).

Failure semantics: an unreachable venue raises `RetryableError` upward and *nothing is adopted*.
A reconciliation that cannot complete leaves the ledger untouched and the caller halted — a
half-applied reconciliation is worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import Mode, ReconcileClass, RiskTier
from tradebot.core.events import EventFactory
from tradebot.core.ids import owns_client_order_id
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.portfolio import AccountState, Position
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.interfaces.broker import BrokerAdapter, OrderStatus
from tradebot.ledger.portfolio import ExternalFlow, Ledger
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)


class Difference(DomainModel):
    """One line of the diff: what we thought, what the venue says, and how it was explained."""

    scope: str
    ours: Money
    theirs: Money
    classification: ReconcileClass
    detail: str = ""

    @property
    def delta(self) -> Money:
        return self.theirs - self.ours


class ReconcileReport(DomainModel):
    """The whole diff, and the single classification that governs the response."""

    venue: str
    classification: ReconcileClass
    differences: tuple[Difference, ...] = ()
    adopted_orders: tuple[str, ...] = ()
    foreign_orders: tuple[str, ...] = ()
    observed_at: UtcDatetime

    @property
    def clean(self) -> bool:
        return self.classification.is_clean

    @property
    def detail(self) -> str:
        return "; ".join(f"{d.scope}: {d.detail}" for d in self.differences if d.detail)


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A split or dividend the venue announced, used to explain an equity position change."""

    instrument_key: str
    ratio: Decimal = Decimal(1)
    cash_per_share: Decimal = ZERO
    detail: str = ""


class Reconciler:
    """Diffs the ledger against venue truth and classifies every difference."""

    def __init__(
        self,
        broker: BrokerAdapter,
        ledger: Ledger,
        store: EventStore,
        clock: Clock,
        *,
        mode: Mode,
        instruments: Sequence[Instrument] = (),
        dust_tolerance: Decimal = Decimal("0.00000001"),
        drift_tolerance_pct: Decimal = Decimal("0.5"),
        mismatch_kill_pct: Decimal = Decimal(5),
        corporate_actions: Sequence[CorporateAction] = (),
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._store = store
        self._clock = clock
        self._mode = mode
        self._instruments = {i.key: i for i in instruments}
        self._dust = dust_tolerance
        self._drift_pct = drift_tolerance_pct
        self._mismatch_kill_pct = mismatch_kill_pct
        self._actions = {action.instrument_key: action for action in corporate_actions}

    async def reconcile(self, *, basket_id: str = "global") -> ReconcileReport:
        """Fetch venue truth, classify every difference, and adopt what is explained."""
        venue_state = await self._broker.fetch_positions_and_balances()
        open_orders = await self._broker.fetch_open_orders()

        differences = self._diff(venue_state)
        if self._is_venue_reset(venue_state, differences):
            report = self._report(venue_state, ReconcileClass.VENUE_RESET, differences)
        else:
            worst = max(
                (d.classification for d in differences),
                key=_SEVERITY.__getitem__,
                default=ReconcileClass.MATCH,
            )
            report = self._report(venue_state, worst, differences, open_orders)

        await self._publish(report, basket_id)
        if report.clean:
            self._adopt(venue_state, report)
        return report

    # ------------------------------------------------------------------ diffing

    def _diff(self, venue_state: AccountState) -> tuple[Difference, ...]:
        """Diff positions, then the currencies a position does not already account for.

        On a spot venue an instrument's base asset *is* a balance, so diffing both would report
        every position discrepancy twice — and would report a phantom one whenever the two sides
        express the same holding differently.
        """
        ours = self._ledger.snapshot()
        keys = {p.instrument_key for p in (*ours.positions, *venue_state.positions)}
        held_as_positions = {i.base_currency for i in self._instruments.values()}
        currencies = {
            b.currency for b in (*ours.balances, *venue_state.balances)
        } - held_as_positions
        return (
            *(self._diff_position(key, ours, venue_state) for key in sorted(keys)),
            *(self._diff_balance(currency, ours, venue_state) for currency in sorted(currencies)),
        )

    def _diff_position(self, key: str, ours: AccountState, theirs: AccountState) -> Difference:
        mine, yours = ours.qty(key), theirs.qty(key)
        return self._classify(scope=key, ours=mine, theirs=yours, action=self._actions.get(key))

    def _diff_balance(self, currency: str, ours: AccountState, theirs: AccountState) -> Difference:
        return self._classify(
            scope=currency, ours=ours.total(currency), theirs=theirs.total(currency)
        )

    def _classify(
        self,
        *,
        scope: str,
        ours: Decimal,
        theirs: Decimal,
        action: CorporateAction | None = None,
    ) -> Difference:
        """Walk the explanations in order of how benign they are; stop at the first that fits."""
        delta = theirs - ours
        for classifier in _CLASSIFIERS:
            outcome = classifier(self, ours, theirs, delta, action)
            if outcome is not None:
                classification, detail = outcome
                return Difference(
                    scope=scope,
                    ours=ours,
                    theirs=theirs,
                    classification=classification,
                    detail=detail,
                )
        raise AssertionError("the classifier chain must end in an unconditional branch")

    def _as_match(
        self, _ours: Decimal, _theirs: Decimal, delta: Decimal, _a: CorporateAction | None
    ) -> tuple[ReconcileClass, str] | None:
        if abs(delta) <= self._dust:
            return (ReconcileClass.MATCH, "")
        return None

    def _as_corporate_action(
        self, ours: Decimal, theirs: Decimal, _delta: Decimal, action: CorporateAction | None
    ) -> tuple[ReconcileClass, str] | None:
        if action is None or ours <= ZERO:
            return None
        expected = multiply(ours, action.ratio)
        if abs(theirs - expected) <= self._dust:
            return (
                ReconcileClass.CORPORATE_ACTION,
                f"{action.detail or 'announced action'} ratio {action.ratio}: {ours} → {theirs}",
            )
        return None

    def _as_drift(
        self, ours: Decimal, _theirs: Decimal, delta: Decimal, _a: CorporateAction | None
    ) -> tuple[ReconcileClass, str] | None:
        """Fees, funding and dust: small, and only ever *against* us."""
        if delta > ZERO or ours <= ZERO:
            return None
        if _pct_of(abs(delta), ours) <= self._drift_pct:
            return (ReconcileClass.DRIFT, f"{delta} within {self._drift_pct}% drift tolerance")
        return None

    def _as_external_change(
        self, _ours: Decimal, _theirs: Decimal, delta: Decimal, _a: CorporateAction | None
    ) -> tuple[ReconcileClass, str] | None:
        """More than we thought: a deposit or a manual buy. Nothing we do can create funds.

        A *shortfall* deliberately does not land here. It could be a manual sell, but it could
        equally be a fill we never booked or an order that filled twice, and those are the cases
        that lose money — so an unexplained decrease stays a mismatch and halts.
        """
        if delta > ZERO:
            return (ReconcileClass.EXTERNAL_CHANGE, f"unexplained increase of {delta}")
        return None

    def _as_mismatch(
        self, _ours: Decimal, _theirs: Decimal, delta: Decimal, _a: CorporateAction | None
    ) -> tuple[ReconcileClass, str] | None:
        return (ReconcileClass.MISMATCH, f"unexplained difference of {delta}")

    def _is_venue_reset(
        self, venue_state: AccountState, differences: tuple[Difference, ...]
    ) -> bool:
        """Everything we held is gone at once — a testnet wipe, not a disaster (R15).

        Deliberately narrow: partial disappearance is a mismatch. Only a state where we believe
        we hold positions and the venue reports none at all qualifies.
        """
        held = [d for d in differences if d.ours > ZERO and d.scope in self._instruments]
        return bool(held) and not venue_state.positions and all(d.theirs <= ZERO for d in held)

    # ------------------------------------------------------------------ adoption

    def _report(
        self,
        venue_state: AccountState,
        classification: ReconcileClass,
        differences: tuple[Difference, ...],
        open_orders: tuple[OrderStatus, ...] = (),
    ) -> ReconcileReport:
        ours = tuple(
            status.client_order_id
            for status in open_orders
            if owns_client_order_id(status.client_order_id, self._mode)
        )
        theirs = tuple(
            status.client_order_id
            for status in open_orders
            if not owns_client_order_id(status.client_order_id, self._mode)
        )
        return ReconcileReport(
            venue=venue_state.venue,
            classification=classification,
            differences=tuple(
                d for d in differences if d.classification is not ReconcileClass.MATCH
            ),
            adopted_orders=ours,
            foreign_orders=theirs,
            observed_at=venue_state.observed_at,
        )

    def _adopt(self, venue_state: AccountState, _report: ReconcileReport) -> None:
        """Take the venue's numbers. Only reached for a classification that permits trading."""
        reported = {position.instrument_key for position in venue_state.positions}
        for position in venue_state.positions:
            self._ledger.adopt_position(position)
        for held in self._ledger.positions():
            if held.instrument_key not in reported and not held.is_flat:
                self._ledger.adopt_position(Position(instrument_key=held.instrument_key))
        for balance in venue_state.balances:
            self._ledger.set_locked(balance.currency, balance.locked)

    async def _publish(self, report: ReconcileReport, basket_id: str) -> None:
        events = EventFactory(clock=self._clock, basket_id=basket_id, cycle_id="reconcile")
        records = [events.reconciled(report)]
        records.extend(
            events.external_change(d.scope, d.delta, d.detail)
            for d in report.differences
            if d.classification is ReconcileClass.EXTERNAL_CHANGE
        )
        records.extend(
            events.corporate_action(d.scope, d.detail, ours=str(d.ours), theirs=str(d.theirs))
            for d in report.differences
            if d.classification is ReconcileClass.CORPORATE_ACTION
        )
        if not report.clean:
            records.append(
                events.risk_event(
                    tier=RiskTier.RECONCILIATION,
                    rule=report.classification.value,
                    scope=report.venue,
                    action="halt",
                    detail=report.detail,
                )
            )
            logger.error(
                "reconciliation did not come back clean",
                extra={"classification": report.classification.value, "detail": report.detail},
            )
        await self._store.append(*records)

    def exceeds_kill_tolerance(self, report: ReconcileReport, equity: Decimal) -> bool:
        """Whether a mismatch is large enough to stop everything rather than one basket.

        A mismatch we cannot size — because equity itself is unknown — counts as severe. The
        one thing that must not happen is a large discrepancy being waved through as small.
        """
        if report.classification is not ReconcileClass.MISMATCH:
            return False
        if equity <= ZERO:
            return True
        worst = max((abs(d.delta) for d in report.differences), default=ZERO)
        return _pct_of(worst, equity) > self._mismatch_kill_pct

    def apply_external_flows(self, report: ReconcileReport) -> tuple[ExternalFlow, ...]:
        """The flows the report found, for the watchdog to adjust its baselines with."""
        flows = tuple(
            ExternalFlow(currency=d.scope, amount=d.delta, reason=d.detail)
            for d in report.differences
            if d.classification is ReconcileClass.EXTERNAL_CHANGE
            and d.scope not in self._instruments
        )
        for flow in flows:
            self._ledger.apply_external_change(flow)
        return flows


def _pct_of(amount: Decimal, base: Decimal) -> Decimal:
    return multiply(divide(amount, base), Decimal(100)) if base > ZERO else Decimal(100)


#: Ordered from most benign to least. The chain ends in `_as_mismatch`, which always matches —
#: so an unexplained difference cannot fall through into being treated as fine.
_CLASSIFIERS = (
    Reconciler._as_match,
    Reconciler._as_corporate_action,
    Reconciler._as_drift,
    Reconciler._as_external_change,
    Reconciler._as_mismatch,
)

#: How bad each classification is, so a report takes the worst line rather than the last one.
_SEVERITY: dict[ReconcileClass, int] = {
    ReconcileClass.MATCH: 0,
    ReconcileClass.DRIFT: 1,
    ReconcileClass.EXTERNAL_CHANGE: 2,
    ReconcileClass.CORPORATE_ACTION: 3,
    ReconcileClass.VENUE_RESET: 4,
    ReconcileClass.MISMATCH: 5,
}
