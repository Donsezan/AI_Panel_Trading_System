"""The form parser. One shape transform, and no rules of its own.

The rules live in the pydantic models and are tested there; what is tested here is that a flat
HTML form arrives at those models as the document the operator described — including the two
conventions that could silently change a limit: an emptied field is *omitted* so the model's own
default speaks, and a blank row is dropped rather than becoming a half-built instrument.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.config import GlobalRiskPolicy, RiskPolicy, Schedule
from tradebot.dashboard.forms import (
    FieldError,
    add_row,
    draft_of,
    nest,
    parse,
    path_of,
    remove_row,
)


def test_flat_names_become_a_nested_document() -> None:
    assert nest(
        [
            ("doc.basket_id", "alpha"),
            ("doc.schedule.every_seconds", "600"),
            ("doc.instruments[0].symbol", "BTC/USDT"),
            ("doc.instruments[1].symbol", "ETH/USDT"),
        ]
    ) == {
        "basket_id": "alpha",
        "schedule": {"every_seconds": "600"},
        "instruments": [{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}],
    }


def test_fields_outside_the_document_namespace_are_ignored() -> None:
    """A model configured `extra="forbid"` must never see the confirm phrase or a button."""
    assert (
        nest([("confirm", "LOOSEN GLOBAL LIMITS"), ("add", "instruments"), ("note", "why")]) == {}
    )


def test_repeated_keys_become_a_list_in_order() -> None:
    pairs = [("doc.timeframes[]", "1h"), ("doc.timeframes[]", "4h"), ("doc.timeframes[]", "1d")]
    assert nest(pairs) == {"timeframes": ["1h", "4h", "1d"]}


def test_the_empty_sentinel_makes_nothing_selected_reachable() -> None:
    """Without it an unselected multi-select sends no key at all, which reads as "unchanged"."""
    assert nest([("doc.timeframes[]", "")]) == {"timeframes": []}


def test_an_emptied_field_is_omitted_so_the_model_default_speaks() -> None:
    assert nest([("doc.name", ""), ("doc.basket_id", "alpha")]) == {"basket_id": "alpha"}


def test_blank_rows_are_dropped() -> None:
    """A row added and left untouched must not become a half-built instrument."""
    pairs = [("doc.instruments[0].symbol", "BTC/USDT"), ("doc.instruments[1].symbol", "")]
    assert nest(pairs) == {"instruments": [{"symbol": "BTC/USDT"}]}


def test_a_gap_left_by_a_removed_row_is_closed() -> None:
    pairs = [("doc.seats[0].id", "a"), ("doc.seats[2].id", "c")]
    assert nest(pairs) == {"seats": [{"id": "a"}, {"id": "c"}]}


def test_deeply_nested_paths_round_trip() -> None:
    """Note the compaction: the blank seat[0] the index implied is dropped, not kept as a row."""
    parsed = nest([("doc.panel.seats[1].fallbacks[0].model", "qwen")])
    assert parsed["panel"]["seats"] == [{"fallbacks": [{"model": "qwen"}]}]


@pytest.mark.parametrize("name", ["doc.[0]", "doc.a[x]", "doc."])
def test_unparseable_field_names_are_ignored_rather_than_raising(name: str) -> None:
    nest([(name, "value")])  # must not raise


# ---------------------------------------------------------------- validation


def test_a_field_level_failure_is_located_on_its_field() -> None:
    _, errors = parse(RiskPolicy, [("doc.cooldown_cycles", "-1")])

    assert any(error.field == "cooldown_cycles" for error in errors)


def test_a_cross_field_rule_reports_without_a_field() -> None:
    """A model validator has no location, and guessing one would blame the wrong input."""
    policy, errors = parse(RiskPolicy, [("doc.min_conviction", "5")])

    assert policy is None
    assert [error.field for error in errors] == [""]
    assert any("0–1 scale" in error.message for error in errors)


def test_the_message_is_the_models_own() -> None:
    """The form surfaces what the engine's validator says; it never restates a rule."""
    _, errors = parse(
        RiskPolicy, [("doc.stop_loss_atr_multiple", "3"), ("doc.take_profit_atr_multiple", "2")]
    )
    assert any("must exceed stop_loss_atr_multiple" in error.message for error in errors)


def test_a_valid_form_becomes_the_document_the_engine_consumes() -> None:
    policy, errors = parse(
        RiskPolicy, [("doc.min_conviction", "0.75"), ("doc.cooldown_cycles", "3")]
    )

    assert errors == ()
    assert policy is not None
    assert policy.min_conviction == Decimal("0.75")
    assert policy.cooldown_cycles == 3


def test_money_survives_the_round_trip_exactly() -> None:
    """A limit must not lose a digit between the database, the form and the database again."""
    original = RiskPolicy(min_conviction=Decimal("0.6125"))
    reparsed, errors = parse(
        RiskPolicy, [(f"doc.{key}", value) for key, value in draft_of(original).items()]
    )

    assert errors == ()
    assert reparsed == original


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    """`extra="forbid"`: a stray input is a defect, and a silently dropped limit is worse."""
    _, errors = parse(RiskPolicy, [("doc.max_leverage", "10")])
    assert errors


def test_path_of_matches_the_field_names_the_form_used() -> None:
    assert (
        path_of(("panel", "seats", 1, "fallbacks", 0, "model"))
        == "panel.seats[1].fallbacks[0].model"
    )
    assert path_of(()) == ""


def test_field_error_label_drops_the_indices() -> None:
    assert FieldError(field="panel.seats[1].model", message="x").label == "panel.seats.model"


# ---------------------------------------------------------------- row editing


def test_add_row_appends_a_blank_row() -> None:
    draft: dict[str, object] = {"instruments": [{"symbol": "BTC/USDT"}]}
    add_row(draft, "instruments")
    assert draft["instruments"] == [{"symbol": "BTC/USDT"}, {}]


def test_add_row_reaches_a_nested_list() -> None:
    draft: dict[str, object] = {"panel": {"seats": [{"fallbacks": []}]}}
    add_row(draft, "panel.seats[0].fallbacks")
    assert draft["panel"]["seats"][0]["fallbacks"] == [{}]  # type: ignore[index]


def test_remove_row_deletes_the_indexed_row() -> None:
    draft: dict[str, object] = {"instruments": [{"symbol": "a"}, {"symbol": "b"}]}
    remove_row(draft, "instruments[1]")
    assert draft["instruments"] == [{"symbol": "a"}]


@pytest.mark.parametrize("path", ["instruments[9]", "instruments", "nothing[0]", "instruments[x]"])
def test_removing_something_that_is_not_there_is_a_no_op(path: str) -> None:
    draft: dict[str, object] = {"instruments": [{"symbol": "a"}]}
    remove_row(draft, path)
    assert draft["instruments"] == [{"symbol": "a"}]


def test_draft_of_renders_money_as_its_exact_string() -> None:
    draft = draft_of(GlobalRiskPolicy(max_drawdown_pct=Decimal("7.5")))
    assert draft["max_drawdown_pct"] == "7.5"
    assert isinstance(draft["max_drawdown_pct"], str)


def test_draft_of_a_schedule_round_trips() -> None:
    schedule = Schedule(every_seconds=900, offset_seconds=60)
    reparsed, errors = parse(
        Schedule, [(f"doc.{key}", str(value)) for key, value in draft_of(schedule).items()]
    )
    assert errors == ()
    assert reparsed == schedule
