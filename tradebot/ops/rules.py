"""The five PLAN Phase 7 alert triggers, as a dispatch table over the log.

Four are event-driven: a kill switch trip, a basket halt, an unexplained reconciliation, and a
run of degraded panels. The fifth — the daily summary — is time-driven and lives in the
dispatcher, because it is about a day passing rather than about something happening.

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
)

#: Consecutive degraded cycles before a human is told. Two could be one provider blipping; a
#: third is a panel that has stopped working, and every cycle since the first has traded nothing.
DEFAULT_DEGRADED_STREAK = 3


@dataclass(slots=True)
class RuleState:
    """What a rule has to remember between events. Persisted, so a restart cannot forgive it."""

    degraded_streak: int = 0
    streak_limit: int = DEFAULT_DEGRADED_STREAK


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


def provider_failure(event: Event, state: RuleState) -> Alert | None:
    """A run of cycles whose panel could not reach a view.

    Fires **once per streak**, at the moment the limit is reached, because a panel that stays
    down would otherwise alert on every cycle for hours. Any cycle with a *readable* outcome that
    is not degraded clears the streak, so the next outage alerts again.

    An outcome this build cannot read leaves the streak exactly as it was. It is not evidence
    that the providers recovered, and treating it as such would silence the alert on the strength
    of a payload nobody could parse.
    """
    outcome = _outcome(event)
    if outcome is None:
        return None
    if outcome is not CycleOutcome.PANEL_DEGRADED:
        state.degraded_streak = 0
        return None
    state.degraded_streak += 1
    if state.degraded_streak != state.streak_limit:
        return None
    return Alert(
        kind=AlertKind.PROVIDER_FAILURE,
        at=event.ts,
        scope=event.basket_id or "",
        title=f"Panel degraded for {state.degraded_streak} cycles running",
        body=(
            "Seats are abstaining faster than the panel's threshold allows, so every one of "
            "those cycles resolved to WAIT and traded nothing. Check provider availability and "
            "each seat's fallback chain — a free model slot that disappeared is the usual cause."
        ),
    )


def _outcome(event: Event) -> CycleOutcome | None:
    try:
        return CycleOutcome(text(event, "outcome"))
    except ValueError:
        return None


#: One handler per tailed type. Dispatch, never a chain of `if`s (CLAUDE.md).
RULES: dict[EventType, Callable[[Event, RuleState], Alert | None]] = {
    EventType.KILL_SWITCH_CHANGED: kill_switch,
    EventType.BASKET_STATUS_CHANGED: basket_halted,
    EventType.RECONCILED: recon_mismatch,
    EventType.CYCLE_COMPLETED: provider_failure,
}


def evaluate(event: Event, state: RuleState) -> Alert | None:
    """The alert this event justifies, if any. Mutates `state` for the streak rule."""
    rule = RULES.get(event.type)
    return rule(event, state) if rule is not None else None
