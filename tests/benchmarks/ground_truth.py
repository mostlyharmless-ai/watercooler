"""Ground truth datasets for benchmark evaluation.

Frozen dataclasses ensure test data cannot be accidentally mutated.
Gold QA pairs are generated from fixture thread metadata.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelevanceSet:
    """A query with its known relevant entry IDs."""
    query: str
    relevant_entry_ids: frozenset[str]
    category: str  # "decision_recall", "cross_thread", "temporal", "entity", etc.
    notes: str = ""


@dataclass(frozen=True)
class SearchModePair:
    """A query for semantic vs keyword comparison."""
    query: str
    relevant_entry_ids: frozenset[str]
    category: str  # "keyword_friendly", "paraphrase"


def build_recall_goldens() -> list[RelevanceSet]:
    """Build gold QA pairs from benchmark fixture entry IDs.

    Each golden maps a natural-language query to the entry IDs that
    should appear in the search results. Categories:
    - decision_recall: "What was decided about X?"
    - cross_thread: spanning multiple threads
    - temporal: time-based discovery
    - entity_search: agent/role lookup
    - keyword_friendly: verbatim title match
    """
    return [
        RelevanceSet(
            query="JWT",
            relevant_entry_ids=frozenset(["BMAD002"]),
            category="decision_recall",
            notes="Find the JWT token signing decision",
        ),
        RelevanceSet(
            query="GraphQL",
            relevant_entry_ids=frozenset(["BMAR005"]),
            category="decision_recall",
            notes="Find the API protocol override decision",
        ),
        RelevanceSet(
            query="authentication",
            relevant_entry_ids=frozenset(["BMAD001", "BMAD002", "BMAD003"]),
            category="cross_thread",
            notes="Find authentication-related entries across threads",
        ),
        RelevanceSet(
            query="schema",
            relevant_entry_ids=frozenset(["BMDB002"]),
            category="temporal",
            notes="Find database schema change entries",
        ),
        RelevanceSet(
            query="Performance Optimization Plan",
            relevant_entry_ids=frozenset(["BMPO001"]),
            category="keyword_friendly",
            notes="Exact title substring match",
        ),
    ]


def build_paraphrase_pairs() -> list[SearchModePair]:
    """Build keyword vs semantic comparison pairs.

    These queries use vocabulary different from the entry content,
    so only semantic search should find them. Requires embeddings.
    """
    return [
        SearchModePair(
            query="secure login credentials",
            relevant_entry_ids=frozenset(["BMAD001", "BMAD002"]),
            category="paraphrase",
        ),
        SearchModePair(
            query="web service endpoint restructuring",
            relevant_entry_ids=frozenset(["BMAR001", "BMAR005"]),
            category="paraphrase",
        ),
        SearchModePair(
            query="persistent storage version upgrade",
            relevant_entry_ids=frozenset(["BMDB004"]),
            category="paraphrase",
        ),
    ]
