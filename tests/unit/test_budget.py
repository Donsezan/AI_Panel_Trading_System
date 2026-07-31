"""The per-cycle cost ceiling.

Small, but it is the only thing standing between a bug in the debate loop and an unbounded bill
(R6). The properties that matter: the scope is the cycle rather than the panel run, truncation
biases toward fewer rounds and therefore toward `WAIT`, and free models are never truncated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.budget import CycleBudget


class TestCycleBudget:
    def test_a_fresh_budget_has_its_whole_limit(self) -> None:
        budget = CycleBudget(Decimal("0.50"))
        assert (budget.remaining, budget.spent, budget.truncated) == (
            Decimal("0.50"),
            Decimal(0),
            False,
        )

    def test_spending_reduces_what_is_left(self) -> None:
        budget = CycleBudget(Decimal("0.50"))
        budget.spend(Decimal("0.20"))
        assert budget.remaining == Decimal("0.30")

    def test_a_round_that_fits_is_allowed_and_does_not_latch_truncation(self) -> None:
        budget = CycleBudget(Decimal("0.50"))
        budget.spend(Decimal("0.20"))
        assert budget.can_afford(Decimal("0.30"))
        assert not budget.truncated

    def test_a_round_that_does_not_fit_latches_truncation(self) -> None:
        budget = CycleBudget(Decimal("0.50"))
        budget.spend(Decimal("0.40"))
        assert not budget.can_afford(Decimal("0.20"))
        assert budget.truncated

    def test_overspend_is_recorded_rather_than_hidden(self) -> None:
        """The round already happened; understating the cycle's cost would be the worse lie."""
        budget = CycleBudget(Decimal("0.10"))
        budget.spend(Decimal("0.30"))
        assert budget.spent == Decimal("0.30")
        assert budget.remaining == Decimal(0)

    def test_a_zero_limit_still_affords_a_free_round(self) -> None:
        """v1 runs on free slots: a zero-cost panel must debate its full course."""
        budget = CycleBudget(Decimal(0))
        assert budget.can_afford(Decimal(0))
        assert not budget.truncated

    def test_a_zero_limit_refuses_a_priced_round(self) -> None:
        assert not CycleBudget(Decimal(0)).can_afford(Decimal("0.01"))

    def test_a_negative_ceiling_is_a_configuration_error(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            CycleBudget(Decimal("-1"))

    def test_a_negative_charge_cannot_refund_the_budget(self) -> None:
        budget = CycleBudget(Decimal("0.50"))
        budget.spend(Decimal("0.40"))
        budget.spend(Decimal("-1"))
        assert budget.spent == Decimal("0.40")
