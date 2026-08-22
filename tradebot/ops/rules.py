"""The alert triggers, as a dispatch table over the log.

Five are event-driven: a kill switch trip, a basket halt, an unexplained reconciliation, a run of
degraded panels, and a run of cycles that could not trust their own market data. The last — the
daily summary — is time-driven and lives in the dispatcher, because it is about a day passing
rather than about something happening.

The two *runs* share one rule and one mechanism (`OUTCOME_STREAKS`). They are the same operational
fact — cycle after cycle deciding nothing — differing only in which dependency stopped working, so
adding the third will be a row in a table rather than another counter to keep in step.

They are deliberately the **same vocabulary the promotion gates count** (`validation/evidence.py`).
Four of these five alerts correspond exactly to an `IncidentKind`, so "what needed a human" has one
definition in this codebase rather than a reporting one and an alerting one that drift apart.

**Repeated provider failure is counted as consecutive `PANEL_DEGRADED` cycles**, not as seat-level
failures. A seat's abstention lives in `SEAT_RESPONDED`, which carries the raw model text and is
the largest payload in the log — tailing it would make alerting the most expensive reader in the
system, for a signal that is only actionable once it has cost a cycle its decision. A degraded
panel is what a run of provider failures *does*, and it is one small event away.

Failure semantics: a rule that cannot read its event returns no alert. A malformed payload must
not stop the tail — the events behind it include the ones that matter most.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tradebot.control.valuation import VALUATION_RULE
from tradebot.core.enums import CycleOutcome, KillSwitchState, ReconcileClass
from tradebot.core.events import Event, EventType
from tradebot.interfaces.alerts import Alert, AlertKind
from tradebot.validation.payload import nested, text

#: The event types alerting tails. Narrow, like every other reader of the log.
ALERT_TYPES: tuple[EventType, ...] = (
    EventType.KILL_SWITCH_CHANGED,
    EventType.BASKET_STATUS_CHANGED,
    EventType.RECONCILED,
    EventType.CYCLE_COMPLETED,
    EventType.RISK_EVENT,
    EventType.MAINTENANCE_RAN,
)

#: What a maintenance notice is *about*. One scope, so the dashboard groups a pass's notices
#: together and the supersession rule has something to key on.
MAINTENANCE_SCOPE = "maintenance"

#: Consecutive degraded cycles before a human is told. Two could be one provider blipping; a
#: third is a panel that has stopped working, and every cycle since the first has traded nothing.
DEFAULT_DEGRADED_STREAK = 3


@dataclass(slots=True)
class RuleState:
    """What a rule has to remember between events. Persisted, so a restart cannot forgive it."""

    degraded_streak: int = 0
    streak_limit: int = DEFAULT_DEGRADED_STREAK
    stale_streak: int = 0

    def streak(self, field: str) -> int:
        return int(getattr(self, field))

    def advance(self, field: str, *, hit: bool) -> int:
        """Extend this streak, or reset it. Returns the new count."""
        count = self.streak(field) + 1 if hit else 0
        setattr(self, field, count)
        return count


def kill_switch(event: Event, _state: RuleState) -> Alert | None:
    """The switch tripping. Re-arming is not an alert — a human did that one on purpose."""
    if (
        KillSwitchState(text(event, "state") or KillSwitchState.ARMED)
        is not KillSwitchState.TRIPPED
    ):
        return None
    return Alert(
        kind=AlertKind.KILL_SWITCH,
        at=event.ts,
        scope=text(event, "actor") or "watchdog",
        title="KILL SWITCH TRIPPED — all runners halted, working orders cancelled",
        body=(
            f"{text(event, 'reason') or 'no reason recorded'}\n"
            "Nothing will trade until a human re-arms it with the typed phrase "
            "(`tradebot risk rearm`). Positions are NOT flattened."
        ),
    )


def basket_halted(event: Event, _state: RuleState) -> Alert | None:
    """A basket stopping for cause. Un-halting is a human act and not an alert."""
    if text(event, "status") != "halted":
        return None
    basket_id = text(event, "basket_id") or event.aggregate_id
    return Alert(
        kind=AlertKind.BASKET_HALTED,
        at=event.ts,
        scope=basket_id,
        title=f"Basket {basket_id} halted",
        body=(
            f"{text(event, 'reason') or 'no reason recorded'}\n"
            "It will not cycle again until someone clears it (`tradebot risk unhalt`)."
        ),
    )


def recon_mismatch(event: Event, _state: RuleState) -> Alert | None:
    """The ledger and the venue disagreeing in a way nothing explained.

    Only `MISMATCH`. Drift, external changes and corporate actions are *explained* and
    auto-corrected, and a venue reset is a testnet wiping itself (R15) — alerting on those would
    train an operator to ignore the one classification that means the books are wrong.
    """
    classification = str(nested(event, "report", "classification") or "")
    if classification != ReconcileClass.MISMATCH.value:
        return None
    venue = str(nested(event, "report", "venue") or "")
    return Alert(
        kind=AlertKind.RECON_MISMATCH,
        at=event.ts,
        scope=venue,
        title=f"Reconciliation mismatch on {venue or 'the venue'}",
        body=(
            "The ledger and the venue disagree and nothing explains the difference. Affected "
            "baskets are halted; above tolerance this trips the kill switch. The venue is the "
            "source of truth — do not resume until the difference is understood."
        ),
    )


@dataclass(frozen=True, slots=True)
class StreakRule:
    """An outcome that means nothing traded, and how many in a row before a human is told."""

    #: The `RuleState` field this streak is counted in, and the cursor column it survives in.
    field: str
    kind: AlertKind
    title: str
    body: str


#: Outcomes worth counting a run of. Both mean the same thing operationally — cycle after cycle
#: deciding nothing — and differ only in which dependency stopped working.
OUTCOME_STREAKS: dict[CycleOutcome, StreakRule] = {
    CycleOutcome.PANEL_DEGRADED: StreakRule(
        field="degraded_streak",
        kind=AlertKind.PROVIDER_FAILURE,
        title="Panel degraded for {count} cycles running",
        body=(
            "Seats are abstaining faster than the panel's threshold allows, so every one of "
            "those cycles resolved to WAIT and traded nothing. Check provider availability and "
            "each seat's fallback chain — a free model slot that disappeared is the usual cause."
        ),
    ),
    CycleOutcome.DATA_STALE: StreakRule(
        field="stale_streak",
        kind=AlertKind.DATA_STALE,
        title="Market data stale or holed for {count} cycles running",
        body=(
            "The context builder refused its own data, so those cycles never reached the panel "
            "and nothing was decided. Positions already open are still protected by their "
            "venue-held legs, but nothing will be entered *or exited* until data flows again. "
            "Check the venue feed, the rate-limit budget, and the system clock."
        ),
    ),
}


def cycle_streak(event: Event, state: RuleState) -> Alert | None:
    """A run of cycles that decided nothing, for one of the reasons worth counting.

    Fires **once per streak**, at the moment the limit is reached, because a dependency that
    stays down would otherwise alert on every cycle for hours. Any cycle with a *readable*
    outcome clears every streak it is not, so the next outage alerts again.

    An outcome this build cannot read leaves the streaks exactly as they were. It is not evidence
    that anything recovered, and treating it as such would silence the alert on the strength of a
    payload nobody could parse.
    """
    outcome = _outcome(event)
    if outcome is None:
        return None
    fired: tuple[StreakRule, int] | None = None
    for candidate, rule in OUTCOME_STREAKS.items():
        count = state.advance(rule.field, hit=outcome is candidate)
        if count == state.streak_limit:
            fired = (rule, count)
    if fired is None:
        return None
    rule, count = fired
    return Alert(
        kind=rule.kind,
        at=event.ts,
        scope=event.basket_id or "",
        title=rule.title.format(count=count),
        body=rule.body,
    )


def _outcome(event: Event) -> CycleOutcome | None:
    try:
        return CycleOutcome(text(event, "outcome"))
    except ValueError:
        return None


def valuation_frozen(event: Event, _state: RuleState) -> Alert | None:
    """The portfolio becoming unvaluable — and becoming valuable again.

    Narrow on purpose: `RISK_EVENT` carries every Tier-1 and Tier-2 rule that ever stood aside, and
    alerting on all of them would train an operator to ignore the tail. Only the valuation rule's
    own transitions qualify, and `PortfolioWatch` already emits those once per edge rather than
    once per sweep (ADR 0027).

    The recovery is an alert too, unlike a re-arm or an un-halt — those are things a human did on
    purpose, whereas this one clears itself, and an operator woken at 03:00 should not have to
    infer that from silence.
    """
    if text(event, "rule") != VALUATION_RULE:
        return None
    frozen = text(event, "action") == "frozen"
    detail = text(event, "detail") or "no reason recorded"
    if not frozen:
        return Alert(
            kind=AlertKind.VALUATION_FROZEN,
            at=event.ts,
            scope="portfolio",
            title="Portfolio can be valued again — trading resumes",
            body=detail,
        )
    return Alert(
        kind=AlertKind.VALUATION_FROZEN,
        at=event.ts,
        scope="portfolio",
        title="PORTFOLIO CANNOT BE VALUED — no new orders will be sent",
        body=(
            f"{detail}\n"
            "Every percentage-based risk limit is a share of equity, so none can be evaluated. "
            "The kill switch is NOT tripped and positions keep their protective legs; this clears "
            "itself as soon as prices return or the balance is converted."
        ),
    )


def _count(number: int, noun: str) -> str:
    """`1 archive`, `2 archives`. These lines are read by a person, half-asleep, on a phone."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _windows(event: Event) -> str:
    """Which retention windows the pass ran under, and whether they were the published ones.

    On the notice itself rather than a click away: this is the line that answers "why was that
    deleted", and a one-off `--older-than` would otherwise be attributed to a policy that was
    never in force (spec §7).
    """
    override = (
        " (one-off override, not the published policy)" if event.payload.get("overridden") else ""
    )
    return (
        f"windows in force: compact after {event.payload.get('compact_after_days')}d, "
        f"keep archives {event.payload.get('archive_keep_days')}d{override}"
    )


def maintenance(event: Event, _state: RuleState) -> Alert | None:
    """One housekeeping pass, rendered for a human. Loud on failure, quiet on success.

    A dedicated event type rather than an overloaded `RISK_EVENT`: maintenance is not a risk rule,
    and `valuation_frozen` keeps sole ownership of that type (spec §5.4). It reads no streak — it
    is one pass a day, not a "cycle after cycle" degradation — so the counters stay untouched.
    """
    if text(event, "outcome") == "failed":
        return Alert(
            kind=AlertKind.MAINTENANCE_FAILED,
            at=event.ts,
            scope=MAINTENANCE_SCOPE,
            title="Housekeeping failed — backups or retention did not complete",
            body=f"{text(event, 'detail') or 'no reason recorded'}. {_windows(event)}",
        )
    return Alert(
        kind=AlertKind.MAINTENANCE_OK,
        at=event.ts,
        scope=MAINTENANCE_SCOPE,
        title="Housekeeping ran",
        body=(
            f"backup {text(event, 'backup') or 'none'}; "
            f"{_count(int(event.payload.get('compacted_rows', 0)), 'payload')} compacted; "
            f"{_count(int(event.payload.get('deleted_archives', 0)), 'archive')} deleted. "
            f"{_windows(event)}"
        ),
    )


#: One handler per tailed type. Dispatch, never a chain of `if`s (CLAUDE.md).
RULES: dict[EventType, Callable[[Event, RuleState], Alert | None]] = {
    EventType.KILL_SWITCH_CHANGED: kill_switch,
    EventType.BASKET_STATUS_CHANGED: basket_halted,
    EventType.RECONCILED: recon_mismatch,
    EventType.CYCLE_COMPLETED: cycle_streak,
    EventType.RISK_EVENT: valuation_frozen,
    EventType.MAINTENANCE_RAN: maintenance,
}


def evaluate(event: Event, state: RuleState) -> Alert | None:
    """The alert this event justifies, if any. Mutates `state` for the streak rule."""
    rule = RULES.get(event.type)
    return rule(event, state) if rule is not None else None
