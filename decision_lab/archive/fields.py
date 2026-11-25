"""The closed vocabularies the extracted fields use (spec §6.5, §6.8.2.1).

They live in their own module, depending on nothing, because two sides validate against them: the
summariser's response contract (`summarize.py`) and the travelling archive's row model
(`published.py`). Either one owning them makes the other a client of its import graph, and two
copies that drift are a cluster key that stops matching partway through an archive.

Closed rather than free text for the reason §6.8.2.1 gives: `event_type` is a third of the cluster
key, so "regulatory-approval" beside "REGULATORY" splits one event into two slots. `modality` is
harder still — §6.8.2's correction rule *dispatches* on it, and an unrecognised value collapses a
retraction behind the claim it corrects, which is the one failure that section exists to prevent.

Failure semantics: pure data. An unknown value raises `ValueError` from `StrEnum` itself, which
pydantic converts into a field error naming the path — so a malformed model response is refused
at the boundary and tallied, never stored.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """What kind of thing happened, as a classification (§6.5).

    Classification rather than span extraction: financial events are overlong and discontinuous,
    so a span is neither a stable key nor a readable summary field. `OTHER` is deliberate — a
    vocabulary with no escape hatch refuses rows for being ordinary, and a refused row is a gap
    in coverage rather than a tidier archive.
    """

    REGULATORY = "REGULATORY"
    LISTING = "LISTING"
    SECURITY_INCIDENT = "SECURITY_INCIDENT"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    ADOPTION = "ADOPTION"
    MACRO = "MACRO"
    LEGAL = "LEGAL"
    TECHNOLOGY = "TECHNOLOGY"
    PERSONNEL = "PERSONNEL"
    OTHER = "OTHER"
    #: Technical analysis, price targets, predictions, recaps of moves with no stated cause.
    #: Not an event at all: the row is kept as the record that the article was seen and the feed
    #: drops it before the panel reads (subscription-summariser spec §6.1). Appended last, and a
    #: member an archive simply never uses, so an archive published without it still reads and
    #: still digests as it did - the digest is taken over row values, never over the vocabulary.
    MARKET_COMMENTARY = "MARKET_COMMENTARY"


class Modality(StrEnum):
    """Whether the article states the event as fact, intent, rumour, denial or correction.

    "SEC approves" and "SEC is expected to approve" are different facts, both present in the text,
    and an editorial abstract flattens them (§6.5). Load-bearing past the prompt: §6.8.2 reads
    this to refuse to collapse a correction behind the claim it corrects.
    """

    CONFIRMED = "CONFIRMED"
    PROPOSED = "PROPOSED"
    RUMOURED = "RUMOURED"
    DENIED = "DENIED"
    CORRECTED = "CORRECTED"

    @property
    def qualifies(self) -> bool:
        """True for a member that must never be collapsed into its cluster (§6.8.2).

        `PROPOSED` and `RUMOURED` are excluded on purpose. They state a weaker claim, not a
        retraction of one, so giving each its own slot would hand every speculative rewrite of a
        story a slot of its own — the flooding §6.8.2.1 exists to prevent, arriving from the
        qualifier rule instead of from a missing one.
        """
        return self in (Modality.DENIED, Modality.CORRECTED)
