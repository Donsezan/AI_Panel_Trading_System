# decision_lab/clustering.py
"""§6.8.2's fold: a cluster annotates, it never deletes.

The feed bypasses `NewsHub._ingest`, so the bot's embedding dedup does not run in a replay —
without this step a replay's eight slots can be filled by eight rewrites of one story where
production's would hold eight distinct events. Clustering is therefore required for *fidelity*,
not for tidiness.

**The key is §6.5's extracted fields, not a similarity score** (§6.8.2.1, measured twice). The
bot's embedding is a hashed bag of unigrams and bigrams whose own docstring says it captures
lexical overlap, not meaning, and the numbers say how little: a claim and its walk-back score
0.085, one event under two publishers' wording scores 0.556, and only near-verbatim syndication
reaches 0.85. Normalising to the summary nearly doubles the separation — 1.74× against bodies'
1.08× — but *lowers* the absolute numbers, so nothing crosses 0.85 after summarising and a
threshold tuned on bodies is tuned against a 0.737 noise floor.

So the fields are the key and the floor is only a guard, for the weakness §6.8.2.1 states rather
than discovers later: two genuinely distinct events sharing a type and an entity — "Kraken licensed
in the UK" and "Kraken licensed in Germany" — collide on that key. Fail-closed applies: **when in
doubt, do not cluster.** Two slots spent on one event costs a slot; one slot hiding two events
costs the evidence.

Four rules, and the last two exist because the obvious implementation loses the one item that
mattered:

* **No row is ever dropped.** The elected member occupies the slot and the line carries the count;
  every other member stays in the cluster and is reconstructible for the drill-down. The bot's own
  dedup drops at ingest with a `logger.debug`, which is unrecoverable and invisible, and a replay
  whose evidence cannot be reconstructed is not evidence.
* **The count is information the panel cannot otherwise have.** Twelve outlets reporting one event
  currently render as twelve lines, and repetition reads as corroboration.
* **The most recent member is elected, never the first**, or a story's original claim outranks its
  own correction and the correction is invisible behind a count.
* **A member whose modality qualifies is never collapsed at all** and takes its own slot. That is
  what `Modality.qualifies` is for, and `PROPOSED`/`RUMOURED` are deliberately excluded from it.

Forward-only by construction: `fold` is handed the window, from inside `snapshot_news`, and is
never cached across cycles. A count over the whole archive would encode how much coverage an event
*eventually* attracted — §6.7's failure through a new door, wearing a deterministic, model-free
disguise.

Failure semantics: pure and total. An empty window folds to nothing.
"""

from __future__ import annotations

from typing import Final

from decision_lab.archive.published import ArchiveItem
from tradebot.core.schema import DomainModel, Money
from tradebot.news.embedding import embed, similarity

#: Rendered onto the elected member's summary when a cluster holds more than one report. Only
#: then: `reports=1` on every line is noise that trains a reader to ignore the field.
COUNT_TEMPLATE: Final = " (reports={count})"

ClusterKey = tuple[str, tuple[str, ...], tuple[str, ...]]


class Cluster(DomainModel):
    """One event, and every report of it that was in the window."""

    elected: ArchiveItem
    members: tuple[ArchiveItem, ...]

    @property
    def reports(self) -> int:
        return len(self.members)


def cluster_key(item: ArchiveItem) -> ClusterKey:
    """§6.8.2.1's key: the event, who it involved, and which assets the text named.

    Sorted, so two summaries listing the same entities in a different order are one event. A
    model's ordering is not a fact about the news.
    """
    return (item.event_type.value, tuple(sorted(item.entities)), tuple(sorted(item.assets)))


def fold(items: tuple[ArchiveItem, ...], *, floor: Money) -> tuple[Cluster, ...]:
    """Collapse reports of one event into one slot, keeping every member.

    Order out is first-appearance order of each cluster, so the caller's own relevance sort is
    what decides the final ordering rather than a fold artefact.
    """
    clusters: list[Cluster] = []
    for item in sorted(items, key=lambda i: (i.observed_at, i.url)):
        if item.modality.qualifies:
            # Never collapsed, in either direction: it does not join a cluster and nothing later
            # joins it. §6.8.2's worked case — a 09:00 claim and a 15:00 walk-back are textually
            # near-identical, and collapsed either way the panel reads one without the other.
            clusters.append(Cluster(elected=item, members=(item,)))
            continue
        home = _home_for(item, clusters, floor)
        if home is None:
            clusters.append(Cluster(elected=item, members=(item,)))
            continue
        index, cluster = home
        # Elected by recency: the loop is in `observed_at` order, so the newest member seen so
        # far is always the right one and `item` is it.
        clusters[index] = cluster.model_copy(
            update={"elected": item, "members": (*cluster.members, item)}
        )
    return tuple(clusters)


def _home_for(
    item: ArchiveItem, clusters: list[Cluster], floor: Money
) -> tuple[int, Cluster] | None:
    """The cluster `item` belongs to: same key, and similar enough to its elected member.

    Compared against the *elected* member rather than any member, so a chain of near-misses
    cannot walk a cluster away from the event it started as.
    """
    key = cluster_key(item)
    for index, cluster in enumerate(clusters):
        if cluster.elected.modality.qualifies or cluster_key(cluster.elected) != key:
            continue
        if similarity(embed(item.summary), embed(cluster.elected.summary)) >= floor:
            return index, cluster
    return None


def annotate(cluster: Cluster) -> ArchiveItem:
    """The elected member as the panel sees it, with the count when there is one to state."""
    if cluster.reports < 2:
        return cluster.elected
    return cluster.elected.model_copy(
        update={"summary": cluster.elected.summary + COUNT_TEMPLATE.format(count=cluster.reports)}
    )
