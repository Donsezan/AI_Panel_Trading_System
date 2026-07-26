# ADR 0006 — The reconciler classifies the difference; the classification *is* the response

**Status:** accepted · 2026-07-26 · implements DESIGN §6.8, §8.1, PLAN §5 Phase 2c

## Context

The venue is the source of truth and the ledger is a projection of it, so the two will differ.
Most differences are routine — a fee, a dust rounding, a deposit the operator made, a stock
split, a testnet that wiped itself overnight. One kind is not: a difference nothing explains,
which may mean we are holding a position we do not know about, or have lost one we think we
have.

Reacting identically to all of them is wrong in both directions. Halting for a monthly testnet
reset makes the control noise, and operators learn to clear noise without reading it (R15).
Waving through a real discrepancy means trading against a fiction (R5).

## Decision

Every difference is classified before anything is done about it, and the classification decides
the response:

| Classification | Recognised by | Response |
|---|---|---|
| `MATCH` | identical within dust | trade on |
| `DRIFT` | small, and *against* us — fees, funding, dust | adopt the venue's number, log |
| `EXTERNAL_CHANGE` | more than we thought | adopt, flow-adjust the risk baselines |
| `CORPORATE_ACTION` | matches an announced ratio | adopt, log `CORPORATE_ACTION` |
| `VENUE_RESET` | every position gone at once | halt + notify — **not** the kill switch |
| `MISMATCH` | nothing above fits | halt; above tolerance, kill switch |

Three properties make the table trustworthy:

- **The chain ends in `MISMATCH`, which always matches.** An unexplained difference cannot fall
  through into being treated as fine. The default is the branch that stops trading.
- **A shortfall is never `EXTERNAL_CHANGE`.** A gain we did not trade for must have come from
  outside — nothing we do creates funds. A *loss* we did not trade for could be a manual sell,
  or a fill we never booked, or an order that filled twice. Only one of those is safe, so the
  whole class halts.
- **`VENUE_RESET` is deliberately narrow.** Partial disappearance is a mismatch. Only "we
  believe we hold positions and the venue reports none at all" qualifies.

## Two supporting decisions

**Positions and balances are not diffed twice.** On a spot venue an instrument's base asset *is*
a balance. Diffing both reported every position discrepancy twice and manufactured a phantom one
whenever the two sides expressed the same holding differently, so base currencies of known
instruments are excluded from the balance diff.

**"Not found" is part of the adapter contract.** `OrderStatus.found` distinguishes an order the
venue *rejected* — a definite answer, it did not execute — from one the venue has never heard
of, which may mean it was lost or that we are querying the wrong account. Inferring this from a
reject-reason string is not portable across venues, and getting it wrong is both an over-halt
(rejections treated as vanishings) and an under-halt (vanishings treated as rejections).

## Consequences

- A half-applied reconciliation is impossible: an unreachable venue raises and nothing is
  adopted. A partial reconciliation is worse than none.
- `SimBroker` implements `RestorableVenue` so an ordinary restart against the in-process
  simulated venue does not present as a wipe. No real adapter implements it — a real venue keeps
  its own books, and handing it ours would be exactly backwards.
- Corporate-action data is injected (`CorporateAction`); Phase 5 wires the venues' announcement
  feeds behind the same seam.
