"""The two closed vocabularies the cluster key and the correction rule read (spec §6.8.2.1).

Closed rather than free text, because `event_type` is a third of the cluster key and a model
inventing "regulatory-approval" beside "REGULATORY" is a key that silently stops matching. The
same argument applies to `modality` one step harder: §6.8.2's correction rule *dispatches* on it,
so an unrecognised value does not merely fail to cluster — it collapses a retraction behind the
claim it retracts.
"""

from __future__ import annotations

import pytest

from decision_lab.archive.fields import EventType, Modality


def test_every_modality_is_one_of_the_five_the_spec_names() -> None:
    assert {m.value for m in Modality} == {
        "CONFIRMED",
        "PROPOSED",
        "RUMOURED",
        "DENIED",
        "CORRECTED",
    }


def test_only_a_denial_or_a_correction_qualifies_its_cluster() -> None:
    """§6.8.2: a member that qualifies, corrects or denies is never collapsed at all.

    `PROPOSED` and `RUMOURED` are deliberately *not* qualifiers. "SEC is expected to approve" is
    a different fact from "SEC approves" and the summary says so, but it does not retract
    anything — treating it as a qualifier would give every speculative rewrite its own slot and
    reintroduce the flooding §6.8.2.1 exists to prevent.
    """
    assert [m for m in Modality if m.qualifies] == [Modality.DENIED, Modality.CORRECTED]


def test_an_unknown_value_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ValueError):
        Modality("SPECULATIVE")
    with pytest.raises(ValueError):
        EventType("regulatory-approval")


def test_event_type_carries_a_catch_all_so_a_real_article_always_classifies() -> None:
    """A vocabulary with no escape hatch refuses rows for being ordinary."""
    assert EventType.OTHER in set(EventType)
