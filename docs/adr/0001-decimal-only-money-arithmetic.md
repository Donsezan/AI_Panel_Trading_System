# ADR 0001 — Decimal-only money arithmetic with asymmetric rounding

**Status:** accepted · 2026-07-26 · implements PLAN §2.1

## Context

Binary floating point cannot represent most decimal fractions exactly. In a trading system the
error is not cosmetic: a quantity that rounds up by one ULP can exceed a risk limit or an
available balance, and a price that rounds toward the aggressive side crosses more spread than
the strategy chose.

## Decision

1. Every price, quantity, notional, fee and balance is `decimal.Decimal`.
2. All arithmetic goes through `tradebot.core.money`, which uses an explicit `Context`
   (`prec=34`) trapping `InvalidOperation`, `DivisionByZero` and `Overflow`. A NaN must never
   reach a venue as a size.
3. Rounding is asymmetric by design:
   - quantity → **down** to `lot_size`,
   - buy price → **down**, sell price → **up** (always the more passive side).
4. Half-rounding modes are banned outright — they round up half the time.
5. Below a venue minimum is a **veto**, never a bump up to the minimum. Bumping silently
   oversizes past the risk limit that produced the quantity.
6. Exactly one sanctioned `float` → `Decimal` crossing exists: `money.from_measurement`, for
   indicator output (ATR feeds sizing). It is named so it can be grepped and reviewed.

## Enforcement

Discipline does not scale; these are tests, and they fail the build:

- `tests/unit/test_money_discipline.py` walks the AST of `core/`, `risk/`, `execution/` and
  `ledger/` and fails on any `float(...)` call outside `from_measurement`, on any
  money-semantic field annotated `float`, and on any reference to a half-rounding mode.
- `tests/unit/test_money.py` states the invariants as hypothesis properties: quantization is
  idempotent, never increases quantity, and never raises a buy notional above the intended one.

`ruff` cannot express the AST rules (they need name and type awareness), so they live in the
test suite rather than the linter. This is a deliberate deviation from PLAN §2.1's "ruff custom
check" wording; the guarantee is identical and the check is stronger.

## Consequences

- Venue payloads must be parsed from **strings**, never through `float`. `to_decimal` refuses a
  `float` argument outright, so this cannot be forgotten quietly.
- `SELL` quantization can raise the notional (price rounds up while quantity rounds down). This
  is correct — it is the passive direction — so the "notional never increases" invariant is
  stated for the buy side, where overspending is the actual risk.
