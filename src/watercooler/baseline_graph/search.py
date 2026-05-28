"""Unified search module for baseline graph.

This module provides composable search functionality across threads and entries
stored in the JSONL graph format.

Key capabilities:
- Keyword search (text contains in body/title/summary)
- Semantic search with embeddings (cosine similarity)
- Time-boxed search (timestamp range)
- Filters by thread_status, role, entry_type, agent
- AND/OR combination of filters
- Similar entry lookup (by entry_id)
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Literal, Optional, Tuple

from . import storage
from .reader import get_graph_dir, GraphEntry, GraphThread, _node_to_entry, _node_to_thread
from watercooler.path_resolver import derive_group_id

logger = logging.getLogger(__name__)


# Keyword relevance score = base 1.0 + title 0.5 + body 0.3 + matched-field
# count * 0.1 + token-match ratio * TOKEN_MATCH_WEIGHT. The token-match term
# dominates the field boosts (PR2: an entry matching every query token must
# outrank one matching only some), so OR-default recall keeps AND-quality
# ordering at the top. KEYWORD_SCORE_MAX is set above the true max so no
# natural result hits the min() cap when normalizing to [0, 1] for merging
# with cosine-similarity scores.
TOKEN_MATCH_WEIGHT = 8.0
KEYWORD_SCORE_MAX = 11.0


# ============================================================================
# Search Configuration
# ============================================================================


@dataclass
class SearchQuery:
    """Search query configuration.

    Attributes:
        query: Optional keyword query (searches title, body, summary)
        semantic: If True, use semantic/vector search (requires embeddings)
        semantic_threshold: Minimum cosine similarity score for semantic matches (0.0-1.0)
        start_time: Filter entries after this ISO timestamp
        end_time: Filter entries before this ISO timestamp
        similar_to: Find entries similar to this entry_id
        thread_status: Filter by thread status (OPEN, CLOSED, etc.)
        thread_topic: Filter by specific thread topic
        role: Filter by entry role (planner, implementer, etc.)
        entry_type: Filter by entry type (Note, Plan, Decision, etc.)
        agent: Filter by agent name
        tags: Filter by tags (all must be present, case-insensitive AND semantics)
        flag: Filter by flag value substring (case-insensitive)
        pinned: Filter by pinned status. True=has pinned entries, False=has no
            pinned entries (includes unannotated items which are implicitly not pinned)
        limit: Maximum results to return
        query_operator: How to combine query tokens ("AND" or "OR"; default
            "OR" — broad recall, results ranked by token-match count)
        combine: How to combine filters ("AND" or "OR")
        include_threads: Include thread nodes in results
        include_entries: Include entry nodes in results
    """
    query: Optional[str] = None
    semantic: bool = False
    semantic_threshold: float = 0.5
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    similar_to: Optional[str] = None
    thread_status: Optional[str] = None
    thread_topic: Optional[str] = None
    role: Optional[str] = None
    entry_type: Optional[str] = None
    agent: Optional[str] = None
    tags: Optional[List[str]] = None
    flag: Optional[str] = None
    pinned: Optional[bool] = None
    limit: int = 10
    query_operator: Literal["AND", "OR"] = "OR"
    combine: Literal["AND", "OR"] = "AND"
    include_threads: bool = True
    include_entries: bool = True

    def __post_init__(self) -> None:
        self.query_operator = self.query_operator.upper()  # type: ignore[assignment]
        self.combine = self.combine.upper()  # type: ignore[assignment]
        if self.query_operator not in ("AND", "OR"):
            self.query_operator = "OR"  # type: ignore[assignment]
        if self.combine not in ("AND", "OR"):
            self.combine = "AND"  # type: ignore[assignment]


@dataclass
class SearchResult:
    """A single search result.

    Attributes:
        node_type: "thread" or "entry"
        node_id: Unique identifier (topic for threads, entry_id for entries)
        score: Relevance score (higher is better)
        matched_fields: Which fields matched the query
        thread: GraphThread if node_type is "thread"
        entry: GraphEntry if node_type is "entry"
    """
    node_type: Literal["thread", "entry"]
    node_id: str
    score: float = 1.0
    score_type: Literal["keyword", "semantic"] = "keyword"
    matched_fields: List[str] = field(default_factory=list)
    thread: Optional[GraphThread] = None
    entry: Optional[GraphEntry] = None


@dataclass
class SearchResults:
    """Container for search results.

    Attributes:
        results: List of SearchResult objects
        total_scanned: Total nodes visited in the file-based loop (includes skipped
            nodes). Retained for backward compatibility — may exceed
            sum(scanned_by_source.values()) when FalkorDB handles entries and the
            file loop only evaluates threads. Prefer scanned_by_source for accurate
            per-source counts.
        scanned_by_source: Per-source scan counts (e.g., {"graph_file": 5, "falkordb": 10}).
            Only counts nodes actually evaluated per source. T1 threads are not
            equivalent to FalkorDB entries, so this dict replaces the single total.
        query: The original search query
    """
    results: List[SearchResult] = field(default_factory=list)
    total_scanned: int = 0  # TODO: deprecate in favor of scanned_by_source
    scanned_by_source: dict[str, int] = field(default_factory=dict)
    query: Optional[SearchQuery] = None

    @property
    def count(self) -> int:
        """Number of results."""
        return len(self.results)

    def threads(self) -> List[GraphThread]:
        """Get all thread results."""
        return [r.thread for r in self.results if r.thread is not None]

    def entries(self) -> List[GraphEntry]:
        """Get all entry results."""
        return [r.entry for r in self.results if r.entry is not None]


# ============================================================================
# Annotation Boost
# ============================================================================


def compute_annotation_boost(entry: GraphEntry) -> float:
    """Compute a multiplicative boost factor from annotation signals.

    Applied to the final relevance score. Higher annotation activity
    increases discoverability; downvotes reduce it.

    Args:
        entry: GraphEntry with annotation fields populated

    Returns:
        Multiplicative boost clamped to [0.5, 2.0]
    """
    boost = 1.0
    boost += 0.1 * entry.vote_score
    boost += 0.05 * len(entry.tags)
    # Flags are neutral attention markers (e.g. "editorial_candidate",
    # "needs-review"), not negative signals — do not penalize.
    boost += 0.05 * len(entry.xrefs)
    if entry.pinned:
        boost += 0.2
    return max(0.5, min(2.0, boost))


def _apply_annotation_boosts(
    graph_dir: Path,
    results: List[SearchResult],
) -> None:
    """Apply annotation boost to entry results in-place.

    Loads annotation states lazily per topic for efficiency.

    Args:
        graph_dir: Base graph directory
        results: List of SearchResult to boost (mutated in place)
    """
    from .annotations import load_or_rebuild_state, AnnotationState

    # Collect topics that need annotation states
    topics_needed: set[str] = set()
    for r in results:
        if r.entry is not None:
            topics_needed.add(r.entry.thread_topic)

    if not topics_needed:
        return

    # Load annotation states per topic (cache per-topic)
    topic_states: dict[str, dict[str, AnnotationState]] = {}
    for topic in topics_needed:
        thread_dir = storage.get_thread_graph_dir(graph_dir, topic)
        try:
            topic_states[topic] = load_or_rebuild_state(thread_dir, read_only=True)
        except Exception:
            topic_states[topic] = {}

    # Apply boost to each entry result
    for r in results:
        if r.entry is None:
            continue
        states = topic_states.get(r.entry.thread_topic, {})
        ann = states.get(r.entry.entry_id)
        if ann:
            # Enrich the entry with annotation data
            r.entry.tags = ann.tags
            r.entry.reactions = ann.reactions
            r.entry.flags = ann.flags
            r.entry.xrefs = ann.xrefs
            r.entry.pinned = ann.pinned
            r.entry.last_touched = ann.last_touched
            r.entry.vote_score = ann.vote_score
            # Apply multiplicative boost
            r.score *= compute_annotation_boost(r.entry)


# ============================================================================
# Node Loading
# ============================================================================


def _load_nodes(graph_dir: Path) -> Iterator[dict[str, Any]]:
    """Load all nodes from per-thread graph format.

    Iterates through all thread directories and yields thread meta and entries.
    """
    if not storage.is_per_thread_format(graph_dir):
        return

    # Iterate through each thread
    for topic in storage.list_thread_topics(graph_dir):
        # Yield thread meta (thread node)
        meta = storage.load_thread_meta(graph_dir, topic)
        if meta:
            yield meta

        # Yield entry nodes
        for entry in storage.load_thread_entries(graph_dir, topic):
            yield entry


# ============================================================================
# Embedding & Similarity Functions
# ============================================================================


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Compute cosine similarity between two vectors.

    NOTE: This function is kept for fallback when FalkorDB is unavailable.
    For production semantic search, use FalkorDB vector queries instead.

    Args:
        vec_a: First embedding vector
        vec_b: Second embedding vector

    Returns:
        Cosine similarity score between -1 and 1 (higher is more similar)
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    dot_product = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for i in range(len(vec_a)):
        dot_product += vec_a[i] * vec_b[i]
        norm_a += vec_a[i] * vec_a[i]
        norm_b += vec_b[i] * vec_b[i]

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _get_falkordb_store(threads_dir: Path):
    """Get FalkorDB store for semantic search.

    Returns:
        FalkorDBEntryStoreSync instance or None if unavailable
    """
    try:
        from .falkordb_entries import get_falkordb_entry_store

        # Use unified group_id derivation from path_resolver
        group_id = derive_group_id(threads_dir=threads_dir)

        return get_falkordb_entry_store(group_id)
    except Exception as e:
        logger.debug(f"FalkorDB store unavailable: {e}")
        return None


def _search_falkordb_semantic(
    threads_dir: Path,
    query_embedding: List[float],
    limit: int = 10,
    threshold: float = 0.5,
    thread_topic: Optional[str] = None,
) -> Optional[List[SearchResult]]:
    """Perform semantic search via FalkorDB vector index.

    Args:
        threads_dir: Path to threads directory
        query_embedding: Query embedding vector
        limit: Maximum results
        threshold: Minimum similarity score (0.0-1.0)
        thread_topic: Optional thread topic filter

    Returns:
        List of SearchResult or None if FalkorDB unavailable
    """
    store = _get_falkordb_store(threads_dir)
    if not store:
        return None

    try:
        # Use FalkorDB vector search
        falkordb_results = store.search_similar(
            query_embedding,
            limit=limit,
            threshold=threshold,
            thread_topic=thread_topic,
        )

        if not falkordb_results:
            return []

        # Convert to SearchResult objects
        graph_dir = get_graph_dir(threads_dir)
        results = []

        for entry_result in falkordb_results:
            # Load the full entry from graph storage
            entries = storage.load_thread_entries_dict(graph_dir, entry_result.thread_topic)
            entry_node = entries.get(f"entry:{entry_result.entry_id}")

            if entry_node:
                result = SearchResult(
                    node_type="entry",
                    node_id=entry_result.entry_id,
                    score=entry_result.score,
                    score_type="semantic",
                    matched_fields=["embedding"],
                    entry=_node_to_entry(entry_node),
                )
                results.append(result)
            else:
                logger.debug(f"Entry {entry_result.entry_id} found in FalkorDB but not in graph storage")

        return results

    except Exception as e:
        logger.warning(f"FalkorDB semantic search failed, falling back to file-based: {e}")
        return None


def _find_similar_falkordb(
    threads_dir: Path,
    entry_id: str,
    limit: int = 5,
    threshold: float = 0.5,
    exclude_same_thread: bool = False,
) -> Optional[List[GraphEntry]]:
    """Find similar entries via FalkorDB vector index.

    Args:
        threads_dir: Path to threads directory
        entry_id: Entry ID to find similar entries to
        limit: Maximum results
        threshold: Minimum similarity score
        exclude_same_thread: Exclude entries from the same thread

    Returns:
        List of similar GraphEntry or None if FalkorDB unavailable
    """
    store = _get_falkordb_store(threads_dir)
    if not store:
        return None

    try:
        # Use FalkorDB find similar
        falkordb_results = store.find_similar_to_entry(
            entry_id,
            limit=limit,
            threshold=threshold,
            exclude_same_thread=exclude_same_thread,
        )

        if not falkordb_results:
            return []

        # Convert to GraphEntry objects
        graph_dir = get_graph_dir(threads_dir)
        results = []

        for entry_result in falkordb_results:
            entries = storage.load_thread_entries_dict(graph_dir, entry_result.thread_topic)
            entry_node = entries.get(f"entry:{entry_result.entry_id}")

            if entry_node:
                results.append(_node_to_entry(entry_node))
            else:
                logger.debug(f"Entry {entry_result.entry_id} found in FalkorDB but not in graph storage")

        return results

    except Exception as e:
        logger.warning(f"FalkorDB find_similar failed, falling back to file-based: {e}")
        return None


def _get_query_embedding(query: str) -> Optional[List[float]]:
    """Generate embedding for a search query.

    Args:
        query: The search query text

    Returns:
        Embedding vector or None if generation fails
    """
    try:
        from .sync import generate_embedding
        return generate_embedding(query)
    except Exception as e:
        logger.warning(f"Failed to generate query embedding: {e}")
        return None


# ============================================================================
# Filter Functions
# ============================================================================


def _parse_timestamp(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        # Handle Z suffix
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _matches_keyword(
    node: dict[str, Any],
    query: str,
    query_operator: Literal["AND", "OR"] = "OR",
) -> tuple[bool, List[str], float]:
    """Check if node matches a tokenized keyword query.

    Multi-word queries are split on whitespace. ``query_operator="OR"``
    (the default) matches when at least one token appears in a searchable
    field; ``query_operator="AND"`` requires every token. Tokens may match
    across different fields (e.g. token A in title, token B in body).

    Returns:
        Tuple of (matches, matched field names, token-match ratio). The ratio
        is matched-token-count / total-token-count in [0.0, 1.0] (1.0 for an
        empty query) — callers rank OR results by it so an entry matching
        every query token outranks one matching only some.
    """
    if not query or not query.strip():
        return True, [], 1.0

    tokens = query.lower().split()
    operator = query_operator.upper()
    if operator not in ("AND", "OR"):
        operator = "OR"

    searchable_fields = ["title", "body", "summary", "topic"]

    matched_fields: set[str] = set()
    matched_token_count = 0

    for token in tokens:
        found_in_any_field = False
        for field_name in searchable_fields:
            value = node.get(field_name, "")
            if value and token in str(value).lower():
                found_in_any_field = True
                matched_fields.add(field_name)
        if found_in_any_field:
            matched_token_count += 1
        elif operator == "AND":
            return False, [], 0.0

    ratio = matched_token_count / len(tokens) if tokens else 1.0

    if operator == "OR":
        matched = matched_token_count > 0
        return matched, (list(matched_fields) if matched else []), ratio

    # AND: reaching here means every token matched.
    return True, list(matched_fields), ratio


def _matches_time_range(
    node: dict[str, Any],
    start_time: Optional[str],
    end_time: Optional[str],
) -> bool:
    """Check if node timestamp falls within range."""
    # Get node timestamp
    node_ts = node.get("timestamp") or node.get("last_updated")
    if not node_ts:
        # No timestamp - include by default unless time filter is strict
        return start_time is None and end_time is None

    node_dt = _parse_timestamp(node_ts)
    if not node_dt:
        return True  # Can't parse, include by default

    if start_time:
        start_dt = _parse_timestamp(start_time)
        if start_dt and node_dt < start_dt:
            return False

    if end_time:
        end_dt = _parse_timestamp(end_time)
        if end_dt and node_dt > end_dt:
            return False

    return True


def _matches_filters(
    node: dict[str, Any],
    search_query: SearchQuery,
    combine: str = "AND",
) -> tuple[bool, List[str]]:
    """Check if node matches filters.

    Args:
        node: The node to check
        search_query: Search query with filters
        combine: "AND" requires all filters to match, "OR" requires any filter

    Returns:
        Tuple of (matches, list of matched filter names)
    """
    node_type = node.get("type")
    filter_results: List[tuple[str, bool]] = []

    # Thread-specific filters
    if node_type == "thread":
        if search_query.thread_status:
            status = node.get("status", "").upper()
            matches = status == search_query.thread_status.upper()
            filter_results.append(("thread_status", matches))
        if search_query.thread_topic:
            matches = node.get("topic") == search_query.thread_topic
            filter_results.append(("thread_topic", matches))

    # Entry-specific filters
    if node_type == "entry":
        if search_query.thread_topic:
            matches = node.get("thread_topic") == search_query.thread_topic
            filter_results.append(("thread_topic", matches))
        if search_query.role:
            role = node.get("role", "").lower()
            matches = role == search_query.role.lower()
            filter_results.append(("role", matches))
        if search_query.entry_type:
            entry_type = node.get("entry_type", "")
            matches = entry_type.lower() == search_query.entry_type.lower()
            filter_results.append(("entry_type", matches))
        if search_query.agent:
            agent = node.get("agent", "").lower()
            matches = search_query.agent.lower() in agent
            filter_results.append(("agent", matches))

    # If no filters were specified, return True
    if not filter_results:
        return True, []

    # Combine results based on mode
    matched_filters = [name for name, matches in filter_results if matches]

    if combine == "OR":
        # At least one filter must match
        return len(matched_filters) > 0, matched_filters
    else:
        # All filters must match (AND mode)
        all_match = all(matches for _, matches in filter_results)
        return all_match, matched_filters if all_match else []


def _filter_by_annotations(
    graph_dir: Path,
    results: List[SearchResult],
    search_query: SearchQuery,
) -> List[SearchResult]:
    """Post-filter results by annotation fields (tags, flag, pinned).

    Called after _apply_annotation_boosts so annotation data is populated on entries.
    For thread results, checks whether ANY entry in the thread matches the filters
    (thread-level annotation fields in GraphThread are not reliably populated from
    the graph nodes, so we load annotation state directly).

    Args:
        graph_dir: Base graph directory (for loading thread annotation state)
        results: List of SearchResult (already annotation-enriched)
        search_query: SearchQuery with annotation filter fields

    Returns:
        Filtered list of SearchResult
    """
    from .annotations import load_or_rebuild_state

    filtered: List[SearchResult] = []
    for r in results:
        if r.entry is not None:
            # Entry: annotation data populated by _apply_annotation_boosts
            if not _annotation_match(
                tags=[t.lower() for t in r.entry.tags],
                flags=r.entry.flags,
                pinned=r.entry.pinned,
                search_query=search_query,
            ):
                continue
        elif r.thread is not None:
            # Thread: check if ANY entry in the thread matches all annotation filters.
            # GraphThread.tags/pinned are not reliably populated from graph nodes,
            # so load annotation state directly per topic.
            thread_dir = storage.get_thread_graph_dir(graph_dir, r.thread.topic)
            try:
                ann_states = load_or_rebuild_state(thread_dir, read_only=True)
            except Exception:
                ann_states = {}

            if not _thread_annotations_match(ann_states, search_query):
                continue
        # else: no typed object — keep result (shouldn't happen in practice)

        filtered.append(r)
    return filtered


def _annotation_match(
    tags: List[str],
    flags: List[dict],
    pinned: bool,
    search_query: SearchQuery,
) -> bool:
    """Check if a single set of annotation values matches the search filters."""
    if search_query.tags:
        required = [t.lower() for t in search_query.tags]
        if not all(rt in tags for rt in required):
            return False

    if search_query.flag is not None:
        reason_lower = search_query.flag.lower()
        if not any(reason_lower in (f.get("reason", "")).lower() for f in flags):
            return False

    if search_query.pinned is not None:
        if pinned != search_query.pinned:
            return False

    return True


def _thread_annotations_match(
    ann_states: dict,
    search_query: SearchQuery,
) -> bool:
    """Check if a thread's annotation state matches the search filters.

    Semantics:
    - tags: thread passes if at least one entry has ALL required tags
    - flag: thread passes if at least one entry has a matching flag
    - pinned=True: thread passes if at least one entry is pinned
    - pinned=False: thread passes if NO entry is pinned

    When no annotations exist, the thread is treated as having default state
    (no tags, no flags, not pinned).
    """
    if not ann_states:
        # No annotations — thread has no tags, no flags, is not pinned.
        # Passes pinned=False but fails tags/flag/pinned=True.
        if search_query.tags:
            return False
        if search_query.flag is not None:
            return False
        if search_query.pinned is True:
            return False
        return True

    # pinned=False requires a whole-thread check: NO entry may be pinned.
    # Other filters use any-entry-matches semantics.
    if search_query.pinned is False:
        if any(ann.pinned for ann in ann_states.values()):
            return False

    # Build a query without pinned for per-entry matching (pinned already handled above)
    # For pinned=True, any-entry-matches is correct. For pinned=False/None, already handled.
    needs_per_entry_check = bool(search_query.tags) or search_query.flag is not None
    if search_query.pinned is True:
        needs_per_entry_check = True

    if not needs_per_entry_check:
        return True  # pinned=False passed above, no other filters

    for ann in ann_states.values():
        entry_ok = True

        if search_query.tags:
            entry_tags_lower = [t.lower() for t in ann.tags]
            required = [t.lower() for t in search_query.tags]
            if not all(rt in entry_tags_lower for rt in required):
                entry_ok = False

        if entry_ok and search_query.flag is not None:
            reason_lower = search_query.flag.lower()
            if not any(reason_lower in (f.get("reason", "")).lower() for f in ann.flags):
                entry_ok = False

        if entry_ok and search_query.pinned is True:
            if not ann.pinned:
                entry_ok = False

        if entry_ok:
            return True

    return False


# ============================================================================
# Main Search Function
# ============================================================================


def search_graph(
    threads_dir: Path,
    search_query: SearchQuery,
) -> SearchResults:
    """Execute a search against the graph.

    Supports two search modes:
    - Keyword search (default): text matching in title/body/summary
    - Semantic search (semantic=True): cosine similarity with embeddings

    Args:
        threads_dir: Path to threads directory
        search_query: Search configuration

    Returns:
        SearchResults containing matching nodes
    """
    graph_dir = get_graph_dir(threads_dir)
    results = SearchResults(query=search_query)

    if not storage.is_per_thread_format(graph_dir):
        logger.debug("No graph available for search")
        return results

    # For semantic search, generate query embedding upfront
    query_embedding: Optional[List[float]] = None
    if search_query.semantic and search_query.query:
        query_embedding = _get_query_embedding(search_query.query)
        if not query_embedding:
            logger.warning("Semantic search requested but failed to generate query embedding, falling back to keyword")

    # Try FalkorDB vector search for semantic entry search.
    # FalkorDB only indexes entries (not threads), so when include_threads
    # is set we supplement with keyword matches for thread nodes below.
    falkordb_entry_results: Optional[List[SearchResult]] = None
    if (
        search_query.semantic
        and query_embedding
        and search_query.include_entries
        and not search_query.start_time
        and not search_query.end_time
        and not search_query.role
        and not search_query.entry_type
        and not search_query.agent
        and not search_query.tags
        and search_query.flag is None
        and search_query.pinned is None
    ):
        falkordb_entry_results = _search_falkordb_semantic(
            threads_dir,
            query_embedding,
            limit=search_query.limit,
            threshold=search_query.semantic_threshold,
            thread_topic=search_query.thread_topic,
        )
        if falkordb_entry_results is not None:
            logger.debug(f"FalkorDB semantic search returned {len(falkordb_entry_results)} results")
            if not search_query.include_threads:
                # Pure entry search — return FalkorDB results directly
                results.results = falkordb_entry_results
                results.total_scanned = len(falkordb_entry_results)
                results.scanned_by_source = {"falkordb": len(falkordb_entry_results)}
                return results
            # include_threads=True: supplement with thread keyword matches below
        else:
            logger.debug("FalkorDB unavailable, falling back to file-based semantic search")

    # Load search index for file-based semantic search (embeddings stored separately)
    # Convert iterator to dict for efficient lookup
    search_index: dict[str, Any] = {}
    if search_query.semantic and query_embedding:
        for index_entry in storage.load_search_index(graph_dir):
            eid = index_entry.get("entry_id")
            if eid:
                search_index[eid] = index_entry

    matching_results: List[SearchResult] = []

    # If FalkorDB already handled entry semantic search, skip entries in
    # the main loop to avoid duplicates and pointless file-based lookups.
    skip_entries_in_loop = falkordb_entry_results is not None
    file_evaluated = 0  # nodes actually evaluated (not skipped)

    for node in _load_nodes(graph_dir):
        results.total_scanned += 1
        node_type = node.get("type")

        # Filter by node type
        if node_type == "thread" and not search_query.include_threads:
            continue
        if node_type == "entry" and (not search_query.include_entries or skip_entries_in_loop):
            continue
        if node_type not in ("thread", "entry"):
            continue
        file_evaluated += 1

        # Collect filter results
        filter_results = []
        matched_fields: List[str] = []
        semantic_score: Optional[float] = None
        # Defaults to 0.0 so the token-match bonus is added only when a
        # keyword query actually participated — a filter-only search
        # (e.g. role=critic, no query) must not receive the token bonus.
        token_ratio: float = 0.0

        # Semantic search with embeddings
        if search_query.semantic and query_embedding and search_query.query:
            # Get embedding from search index (file-based fallback)
            # Embeddings are no longer stored in entry nodes (Phase 2 migration)
            entry_id = node.get("entry_id")
            node_embedding = search_index.get(entry_id, {}).get("embedding") if entry_id else None
            if node_embedding:
                similarity = _cosine_similarity(query_embedding, node_embedding)
                if similarity >= search_query.semantic_threshold:
                    filter_results.append(True)
                    matched_fields.append("embedding")
                    semantic_score = similarity
                    logger.debug(f"Semantic match: {node.get('entry_id', node.get('topic'))} score={similarity:.3f}")
                else:
                    filter_results.append(False)
            else:
                # No embedding available - skip for pure semantic search
                filter_results.append(False)
        # Keyword match (fallback or primary)
        elif search_query.query:
            keyword_match, keyword_fields, token_ratio = _matches_keyword(
                node,
                search_query.query,
                search_query.query_operator,
            )
            filter_results.append(keyword_match)
            matched_fields.extend(keyword_fields)

        # Time range
        if search_query.start_time or search_query.end_time:
            time_match = _matches_time_range(
                node, search_query.start_time, search_query.end_time
            )
            filter_results.append(time_match)
            if time_match and (search_query.start_time or search_query.end_time):
                matched_fields.append("timestamp")

        # Other filters (role, entry_type, agent, thread_status, thread_topic)
        filters_match, filter_fields = _matches_filters(
            node, search_query, search_query.combine
        )
        if filter_fields:
            matched_fields.extend(filter_fields)

        # Add filter results to the overall list
        filter_results.append(filters_match)

        # Combine results
        if search_query.combine == "AND":
            passes = all(filter_results) if filter_results else True
        else:  # OR
            passes = any(filter_results) if filter_results else True

        if not passes:
            continue

        # Build result
        node_id = node.get("topic") if node_type == "thread" else node.get("entry_id", "")

        # Calculate score
        if semantic_score is not None:
            # Use cosine similarity as the score for semantic search
            score = semantic_score
        else:
            # Relevance scoring for keyword search.
            score = 1.0
            if matched_fields:
                # Boost for title matches
                if "title" in matched_fields:
                    score += 0.5
                # Boost for body matches
                if "body" in matched_fields:
                    score += 0.3
                score += len(matched_fields) * 0.1
            # Token-match-count ranking (PR2): reward matching more of the
            # query's tokens. The weight dominates the field boosts so that,
            # under the OR default, an entry matching every token always
            # outranks one matching only some.
            score += token_ratio * TOKEN_MATCH_WEIGHT

        result = SearchResult(
            node_type=node_type,
            node_id=node_id,
            score=score,
            score_type="semantic" if semantic_score is not None else "keyword",
            matched_fields=matched_fields,
        )

        # Attach typed object
        if node_type == "thread":
            result.thread = _node_to_thread(node)
        else:
            result.entry = _node_to_entry(node)

        matching_results.append(result)

    # Merge FalkorDB entry results with thread keyword results from the loop
    if falkordb_entry_results is not None:
        matching_results.extend(falkordb_entry_results)
        results.scanned_by_source = {
            "graph_file": file_evaluated,
            "falkordb": len(falkordb_entry_results),
        }
    else:
        results.scanned_by_source = {"graph_file": file_evaluated}

    # Normalize keyword scores to [0, 1] so they sort meaningfully alongside
    # cosine similarity scores from FalkorDB.  Raw keyword scores are in
    # [1.0, ~2.0] (base + field boosts) which would always outrank semantic
    # results capped at 1.0.
    if falkordb_entry_results is not None:
        for r in matching_results:
            if r.score_type == "keyword":
                r.score = min(r.score / KEYWORD_SCORE_MAX, 1.0)

    # Apply annotation boost to entry results (lazy per-topic state loading)
    _apply_annotation_boosts(graph_dir, matching_results)

    # Post-filter by annotation fields (tags, flag, pinned).
    # Applied after _apply_annotation_boosts so entry.tags/flags/pinned are populated.
    if search_query.tags or search_query.flag is not None or search_query.pinned is not None:
        matching_results = _filter_by_annotations(graph_dir, matching_results, search_query)

    # Sort by score descending
    matching_results.sort(key=lambda r: r.score, reverse=True)

    # Apply limit
    results.results = matching_results[:search_query.limit]

    return results


# ============================================================================
# Convenience Functions
# ============================================================================


def search_entries(
    threads_dir: Path,
    query: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    thread_topic: Optional[str] = None,
    role: Optional[str] = None,
    entry_type: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 10,
    query_operator: str = "OR",
) -> List[GraphEntry]:
    """Search entries with common filters.

    Args:
        threads_dir: Path to threads directory
        query: Optional keyword search
        start_time: Filter entries after this timestamp
        end_time: Filter entries before this timestamp
        thread_topic: Filter by thread topic
        role: Filter by role
        entry_type: Filter by entry type
        agent: Filter by agent
        limit: Maximum results
        query_operator: Token matching logic - "OR" (default — any token
            matches, results ranked by how many tokens matched) or "AND"
            (every token must match). Default: "OR".

    Returns:
        List of matching GraphEntry objects
    """
    search_query = SearchQuery(
        query=query,
        start_time=start_time,
        end_time=end_time,
        thread_topic=thread_topic,
        role=role,
        entry_type=entry_type,
        agent=agent,
        limit=limit,
        query_operator=query_operator,
        include_threads=False,
        include_entries=True,
    )

    results = search_graph(threads_dir, search_query)
    return results.entries()


def search_threads(
    threads_dir: Path,
    query: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10,
) -> List[GraphThread]:
    """Search threads with common filters.

    Args:
        threads_dir: Path to threads directory
        query: Optional keyword search
        status: Filter by thread status
        limit: Maximum results

    Returns:
        List of matching GraphThread objects
    """
    search_query = SearchQuery(
        query=query,
        thread_status=status,
        limit=limit,
        include_threads=True,
        include_entries=False,
    )

    results = search_graph(threads_dir, search_query)
    return results.threads()


def semantic_search(
    threads_dir: Path,
    query: str,
    threshold: float = 0.5,
    limit: int = 10,
    include_threads: bool = False,
    include_entries: bool = True,
) -> SearchResults:
    """Perform semantic search using embedding similarity.

    Args:
        threads_dir: Path to threads directory
        query: Natural language query
        threshold: Minimum cosine similarity (0.0-1.0, default 0.5)
        limit: Maximum results
        include_threads: Include thread nodes in results
        include_entries: Include entry nodes in results

    Returns:
        SearchResults ranked by cosine similarity
    """
    search_query = SearchQuery(
        query=query,
        semantic=True,
        semantic_threshold=threshold,
        limit=limit,
        include_threads=include_threads,
        include_entries=include_entries,
    )

    return search_graph(threads_dir, search_query)


def find_similar_entries(
    threads_dir: Path,
    entry_id: str,
    limit: int = 5,
    use_embeddings: bool = True,
    similarity_threshold: float = 0.5,
    exclude_same_thread: bool = False,
) -> List[GraphEntry]:
    """Find entries similar to a given entry.

    Uses FalkorDB vector search when available, falls back to file-based
    embedding search (search-index.jsonl), then to same-thread heuristic.

    Args:
        threads_dir: Path to threads directory
        entry_id: ID of entry to find similar entries to
        limit: Maximum results
        use_embeddings: Try to use embedding similarity (default True)
        similarity_threshold: Minimum cosine similarity for embedding matches
        exclude_same_thread: Exclude entries from the same thread

    Returns:
        List of similar GraphEntry objects, sorted by similarity
    """
    graph_dir = get_graph_dir(threads_dir)

    # Try FalkorDB first (preferred - uses HNSW vector index)
    if use_embeddings:
        falkordb_results = _find_similar_falkordb(
            threads_dir,
            entry_id,
            limit=limit,
            threshold=similarity_threshold,
            exclude_same_thread=exclude_same_thread,
        )
        if falkordb_results is not None:
            logger.debug(f"FalkorDB find_similar returned {len(falkordb_results)} results")
            return falkordb_results
        # FalkorDB unavailable, fall through to file-based search
        logger.debug("FalkorDB unavailable, falling back to file-based similarity search")

    # First, find the source entry
    source_entry = None
    for node in _load_nodes(graph_dir):
        if node.get("type") == "entry" and node.get("entry_id") == entry_id:
            source_entry = node
            break

    if not source_entry:
        return []

    # Try to get embedding from search index (file-based fallback)
    source_embedding = None
    search_index_dict: dict[str, Any] = {}
    if use_embeddings:
        # Load from search-index.jsonl (fallback when FalkorDB unavailable)
        # Convert iterator to dict for efficient lookup
        for index_entry in storage.load_search_index(graph_dir):
            eid = index_entry.get("entry_id")
            if eid:
                search_index_dict[eid] = index_entry
        source_embedding = search_index_dict.get(entry_id, {}).get("embedding")

    # If we have an embedding, compute similarity against all entries in search index
    if source_embedding:
        similar_entries: List[Tuple[float, GraphEntry]] = []

        for other_id, index_entry in search_index_dict.items():
            if other_id == entry_id:
                continue  # Skip self

            # Optionally exclude same thread
            if exclude_same_thread:
                other_topic = index_entry.get("thread_topic")
                if other_topic == source_entry.get("thread_topic"):
                    continue

            other_embedding = index_entry.get("embedding")
            if not other_embedding:
                continue

            similarity = _cosine_similarity(source_embedding, other_embedding)
            if similarity >= similarity_threshold:
                # Load full entry from graph storage
                other_topic = index_entry.get("thread_topic")
                if other_topic:
                    entries = storage.load_thread_entries_dict(graph_dir, other_topic)
                    entry_node = entries.get(f"entry:{other_id}")
                    if entry_node:
                        entry = _node_to_entry(entry_node)
                        similar_entries.append((similarity, entry))

        # Sort by similarity descending
        similar_entries.sort(key=lambda x: x[0], reverse=True)

        return [entry for _, entry in similar_entries[:limit]]

    # Fallback: same thread heuristic
    search_query = SearchQuery(
        thread_topic=source_entry.get("thread_topic"),
        limit=limit + 1,  # +1 to exclude self
        include_threads=False,
        include_entries=True,
    )

    results = search_graph(threads_dir, search_query)

    # Filter out the source entry
    similar = [e for e in results.entries() if e.entry_id != entry_id]

    return similar[:limit]


def search_by_time_range(
    threads_dir: Path,
    start_time: str,
    end_time: Optional[str] = None,
    include_threads: bool = True,
    include_entries: bool = True,
    limit: int = 50,
) -> SearchResults:
    """Search for nodes within a time range.

    Args:
        threads_dir: Path to threads directory
        start_time: ISO timestamp for range start
        end_time: Optional ISO timestamp for range end
        include_threads: Include thread nodes
        include_entries: Include entry nodes
        limit: Maximum results

    Returns:
        SearchResults within the time range
    """
    search_query = SearchQuery(
        start_time=start_time,
        end_time=end_time,
        include_threads=include_threads,
        include_entries=include_entries,
        limit=limit,
    )

    return search_graph(threads_dir, search_query)
