"""Multi-tier memory query orchestration.

Provides intelligent routing across three memory tiers:

T1 (Baseline): JSONL-based graph with keyword/semantic search
   - Lowest cost (no LLM calls, just embeddings)
   - Good for: keyword searches, time-based queries, simple lookups

T2 (Graphiti): FalkorDB temporal graph with hybrid search
   - Medium cost (LLM for entity extraction during indexing)
   - Good for: entity relationships, temporal queries, verified facts

T3 (LeanRAG): Hierarchical clustering with multi-hop reasoning
   - Highest cost (LLM for clustering and reasoning)
   - Good for: synthesis, complex multi-hop queries, narratives

Orchestration Principle:
    "Always choose the cheapest tier that can satisfy the intent"

Escalation Path:
    T1 -> T2 -> T3 (Scout -> Resolve -> Synthesize)

Safety Rules:
    - Never escalate to T3 "just to be helpful"
    - Never allow T3 to invent facts beyond what T1/T2 provide
    - Surface uncertainty explicitly instead of hallucinating
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


def _get_int_env(key: str, default: int) -> int:
    """Get integer from environment with fallback on invalid values."""
    val = os.getenv(key)
    if val:
        try:
            return int(val)
        except ValueError:
            logger.warning(f"Invalid integer for {key}: {val!r}, using default {default}")
    return default


# ============================================================================
# Types and Constants
# ============================================================================


class Tier(str, Enum):
    """Memory tier identifiers."""
    T1 = "T1"  # Baseline graph (JSONL + embeddings)
    T2 = "T2"  # Graphiti (FalkorDB temporal graph)
    T3 = "T3"  # LeanRAG (hierarchical clustering)


class QueryIntent(str, Enum):
    """Detected query intent for tier selection."""
    LOOKUP = "lookup"          # Simple fact lookup -> T1/T2
    ENTITY_SEARCH = "entity"   # Find specific entities -> T2
    TEMPORAL = "temporal"      # Time-based queries -> T2
    RELATIONAL = "relational"  # Relationship queries -> T2
    SUMMARIZE = "summarize"    # Synthesis/narrative -> T2/T3
    MULTI_HOP = "multi_hop"    # Complex reasoning -> T3
    UNKNOWN = "unknown"        # Default -> T1


# LeanRAG level_mode values:
#   0 = Base entities only (precise, individual nodes)
#   1 = Clusters only (hierarchical summaries)
#   2 = All levels (base + clusters combined)
LEANRAG_LEVEL_MODE_BASE = 0
LEANRAG_LEVEL_MODE_CLUSTERS = 1
LEANRAG_LEVEL_MODE_ALL = 2


def _get_leanrag_level_mode(intent: QueryIntent) -> int:
    """Map query intent to optimal LeanRAG level_mode.

    LeanRAG's hierarchical graph has multiple levels:
    - Level 0 (base entities): Individual extracted entities with descriptions
    - Level 1+ (clusters): Hierarchical summaries via GMM clustering

    This function selects the optimal level_mode based on query intent:
    - LOOKUP/ENTITY_SEARCH: Use base entities (level_mode=0) for precision
      (maps to issue #119's "entity_context" concept)
    - SUMMARIZE/MULTI_HOP: Use clusters (level_mode=1) for synthesis
      (maps to issue #119's "community_summary" concept)
    - TEMPORAL/RELATIONAL: Use all levels (level_mode=2) for completeness
      (maps to issue #119's "hybrid" concept)
    - UNKNOWN: Default to clusters (level_mode=1)

    Note: LeanRAG's API uses integer level_modes (0, 1, 2), not the string
    identifiers described in issue #119.

    Args:
        intent: The detected query intent

    Returns:
        LeanRAG level_mode value (0, 1, or 2)

    Example:
        >>> _get_leanrag_level_mode(QueryIntent.LOOKUP)
        0
        >>> _get_leanrag_level_mode(QueryIntent.SUMMARIZE)
        1
        >>> _get_leanrag_level_mode(QueryIntent.RELATIONAL)
        2
    """
    if intent in (QueryIntent.LOOKUP, QueryIntent.ENTITY_SEARCH):
        # Precision queries: use base entities for exact matches
        return LEANRAG_LEVEL_MODE_BASE
    elif intent in (QueryIntent.SUMMARIZE, QueryIntent.MULTI_HOP):
        # Synthesis queries: use cluster summaries for broader context
        return LEANRAG_LEVEL_MODE_CLUSTERS
    elif intent in (QueryIntent.TEMPORAL, QueryIntent.RELATIONAL):
        # Relationship/temporal queries: use all levels for completeness
        return LEANRAG_LEVEL_MODE_ALL
    else:
        # Default: clusters provide good balance
        return LEANRAG_LEVEL_MODE_CLUSTERS


# Relative cost weights for tier budget estimation (not billing).
# Actual per-query cost is tracked via elapsed_ms and tier_timings.
TIER_COSTS = {
    Tier.T1: 1,   # ~50ms typical, no LLM calls
    Tier.T2: 10,  # ~200-500ms typical, FalkorDB + optional LLM
    Tier.T3: 100, # ~2-5s typical, LLM reasoning + clustering
}

# Default thresholds for sufficiency checks
DEFAULT_MIN_RESULTS = 3
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_MAX_TIERS = 2  # Don't go beyond T2 by default


# ============================================================================
# Configuration
# ============================================================================


@dataclass
class TierConfig:
    """Configuration for tier orchestration.

    Attributes:
        t1_enabled: Enable T1 (Baseline) tier
        t2_enabled: Enable T2 (Graphiti) tier
        t3_enabled: Enable T3 (LeanRAG) tier - requires explicit opt-in
        min_results: Minimum results before considering tier sufficient
        min_confidence: Minimum average confidence score
        max_tiers: Maximum number of tiers to query (budget control)
        t1_limit: Max results from T1
        t2_limit: Max results from T2
        t3_limit: Max results from T3
        threads_dir: Path to threads directory (for T1)
        code_path: Path to code repository (for T2/T3)
    """
    t1_enabled: bool = True
    t2_enabled: bool = False  # Requires WATERCOOLER_GRAPHITI_ENABLED=1
    t3_enabled: bool = False  # Requires explicit opt-in (expensive)
    min_results: int = DEFAULT_MIN_RESULTS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    max_tiers: int = DEFAULT_MAX_TIERS
    t1_limit: int = 10
    t2_limit: int = 10
    t3_limit: int = 5
    threads_dir: Optional[Path] = None
    code_path: Optional[Path] = None


def load_tier_config(
    threads_dir: Optional[Path] = None,
    code_path: Optional[Path] = None,
) -> TierConfig:
    """Load tier configuration from unified config system.

    Uses the unified config system with proper priority chain:
        1. Environment variables (WATERCOOLER_TIER_T*_ENABLED, etc.)
        2. TOML config (memory.tiers.*)
        3. Built-in defaults

    Environment Variables:
        WATERCOOLER_TIER_T1_ENABLED: "1" to enable T1 (default: "1")
        WATERCOOLER_TIER_T2_ENABLED: "1" to enable T2 (auto-enables with graphiti backend)
        WATERCOOLER_TIER_T3_ENABLED: "1" to enable T3 (expensive, opt-in)
        WATERCOOLER_TIER_MAX_TIERS: Maximum tiers to query (default: "2")
        WATERCOOLER_TIER_MIN_RESULTS: Min results for sufficiency (default: "3")
        WATERCOOLER_TIER_MIN_CONFIDENCE: Min confidence score (default: "0.5")

    TOML Config (config.toml):
        [memory.tiers]
        t1_enabled = true
        t2_enabled = true
        t3_enabled = false
        max_tiers = 2
        min_results = 3
        min_confidence = 0.5
        t1_limit = 10
        t2_limit = 10
        t3_limit = 5

    Args:
        threads_dir: Path to threads directory
        code_path: Path to code repository

    Returns:
        TierConfig instance
    """
    try:
        from watercooler.memory_config import resolve_tier_config
        resolved = resolve_tier_config()
        return TierConfig(
            t1_enabled=resolved.t1_enabled,
            t2_enabled=resolved.t2_enabled,
            t3_enabled=resolved.t3_enabled,
            max_tiers=resolved.max_tiers,
            min_results=resolved.min_results,
            min_confidence=resolved.min_confidence,
            t1_limit=resolved.t1_limit,
            t2_limit=resolved.t2_limit,
            t3_limit=resolved.t3_limit,
            threads_dir=threads_dir,
            code_path=code_path,
        )
    except ImportError:
        logger.warning("watercooler.memory_config not available, using env vars only")
        # Fallback to env vars only
        graphiti_env = os.getenv("WATERCOOLER_GRAPHITI_ENABLED", "0") == "1"
        return TierConfig(
            t1_enabled=os.getenv("WATERCOOLER_TIER_T1_ENABLED", "1") == "1",
            t2_enabled=os.getenv("WATERCOOLER_TIER_T2_ENABLED", "1" if graphiti_env else "0") == "1",
            t3_enabled=os.getenv("WATERCOOLER_TIER_T3_ENABLED", "0") == "1",
            max_tiers=_get_int_env("WATERCOOLER_TIER_MAX_TIERS", DEFAULT_MAX_TIERS),
            min_results=_get_int_env("WATERCOOLER_TIER_MIN_RESULTS", DEFAULT_MIN_RESULTS),
            threads_dir=threads_dir,
            code_path=code_path,
        )


# ============================================================================
# Unified Result Format
# ============================================================================


@dataclass
class TierEvidence:
    """A single piece of evidence from a tier.

    Attributes:
        tier: Which tier produced this evidence
        id: Backend-specific ID
        content: Text content
        score: Relevance score (0.0-1.0)
        name: Entity/node name (if applicable)
        provenance: Source tracking metadata
        metadata: Additional backend-specific data
    """
    tier: Tier
    id: str
    content: str
    score: float = 0.0
    name: Optional[str] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TierResult:
    """Result from multi-tier query orchestration.

    Attributes:
        query: Original query string
        evidence: List of evidence items from all queried tiers
        tiers_queried: List of tiers that were actually queried
        primary_tier: The tier that provided the best results
        escalation_reason: Why we escalated (if applicable)
        synthesis: Optional T3 synthesis (only if T3 was used)
        total_cost: Estimated cost units
        sufficient: Whether results met sufficiency criteria
        message: Human-readable status message
    """
    query: str
    evidence: list[TierEvidence] = field(default_factory=list)
    tiers_queried: list[Tier] = field(default_factory=list)
    primary_tier: Optional[Tier] = None
    escalation_reason: Optional[str] = None
    synthesis: Optional[str] = None
    total_cost: int = 0
    sufficient: bool = False
    message: str = ""
    elapsed_ms: int = 0  # Total wall-clock time across all tiers
    tier_timings: dict[str, int] = field(default_factory=dict)  # Per-tier ms {"T1": 45, "T2": 230}

    @property
    def result_count(self) -> int:
        """Total number of evidence items."""
        return len(self.evidence)

    def top_results(self, limit: int = 5) -> list[TierEvidence]:
        """Get top results by score."""
        sorted_evidence = sorted(self.evidence, key=lambda e: e.score, reverse=True)
        return sorted_evidence[:limit]

    def by_tier(self, tier: Tier) -> list[TierEvidence]:
        """Get results from a specific tier."""
        return [e for e in self.evidence if e.tier == tier]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "query": self.query,
            "result_count": self.result_count,
            "tiers_queried": [t.value for t in self.tiers_queried],
            "primary_tier": self.primary_tier.value if self.primary_tier else None,
            "escalation_reason": self.escalation_reason,
            "synthesis": self.synthesis,
            "total_cost": self.total_cost,
            "elapsed_ms": self.elapsed_ms,
            "tier_timings": dict(self.tier_timings),
            "sufficient": self.sufficient,
            "message": self.message,
            "evidence": [
                {
                    "tier": e.tier.value,
                    "id": e.id,
                    "content": e.content,
                    "score": e.score,
                    "name": e.name,
                    "provenance": dict(e.provenance),
                    "metadata": dict(e.metadata),
                }
                for e in self.evidence
            ],
        }


# ============================================================================
# Intent Detection
# ============================================================================


def detect_intent(query: str) -> QueryIntent:
    """Detect query intent for tier selection.

    Simple heuristic-based detection. Future versions may use LLM classification.

    Args:
        query: The search query string

    Returns:
        Detected QueryIntent
    """
    query_lower = query.lower()

    # Intent detection order matters: more specific patterns checked first.
    # Note: "history" appears in both summarize and temporal keywords;
    # summarize is checked first as it implies synthesis intent.

    # Synthesis indicators -> T2/T3
    summarize_keywords = [
        "summarize", "summary", "overview", "explain", "evolution",
        "history", "journey", "narrative", "story", "describe",
    ]
    if any(kw in query_lower for kw in summarize_keywords):
        return QueryIntent.SUMMARIZE

    # Multi-hop indicators -> T3
    multi_hop_keywords = [
        "how did", "why did", "what led to", "connection between",
        "relationship between", "trace", "path from", "reasoning",
    ]
    if any(kw in query_lower for kw in multi_hop_keywords):
        return QueryIntent.MULTI_HOP

    # Temporal indicators -> T2
    # Note: "history" appears in both summarize_keywords and temporal_keywords.
    # Summarize is checked first, so prioritization is:
    # - "evolution history" → SUMMARIZE (narrative)
    # - "when in history" → TEMPORAL (timeline, if not matched by summarize first)
    temporal_keywords = [
        "when", "before", "after", "during", "timeline", "history",
        "yesterday", "last week", "recent", "first", "latest",
    ]
    if any(kw in query_lower for kw in temporal_keywords):
        return QueryIntent.TEMPORAL

    # Relational indicators -> T2 (check before entity, more specific)
    relational_keywords = [
        "related to", "connected", "depends on", "uses", "calls",
        "implements", "extends", "references",
    ]
    if any(kw in query_lower for kw in relational_keywords):
        return QueryIntent.RELATIONAL

    # Entity indicators -> T2
    entity_keywords = [
        "who", "what is", "find", "entity", "person", "component",
        "module", "class", "function",
    ]
    if any(kw in query_lower for kw in entity_keywords):
        return QueryIntent.ENTITY_SEARCH

    # Default to lookup (T1-suitable)
    return QueryIntent.LOOKUP


# ============================================================================
# Sufficiency Evaluation
# ============================================================================


def evaluate_sufficiency(
    evidence: list[TierEvidence],
    min_results: int = DEFAULT_MIN_RESULTS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    total_results: Optional[int] = None,
) -> tuple[bool, str]:
    """Evaluate whether current evidence is sufficient.

    Args:
        evidence: List of evidence items to evaluate (typically from the current tier)
        min_results: Minimum number of results required (uses total_results when provided)
        min_confidence: Minimum average confidence score
        total_results: Optional total result count across tiers for the quantity check

    Returns:
        Tuple of (is_sufficient, reason)
    """
    if not evidence:
        return False, "No results found"

    result_count = total_results if total_results is not None else len(evidence)
    if result_count < min_results:
        return False, f"Only {result_count} results (need {min_results})"

    avg_score = sum(e.score for e in evidence) / len(evidence)
    if avg_score < min_confidence:
        return False, f"Low confidence ({avg_score:.2f} < {min_confidence})"

    return True, "Sufficient results"


def evaluate_dual_stream_sufficiency(
    evidence: list[TierEvidence],
    min_results: int = DEFAULT_MIN_RESULTS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    total_results: Optional[int] = None,
) -> tuple[bool, str]:
    """Evaluate sufficiency for dual-stream tiers (T2/T3).

    Dual-stream tiers run separate searches for entities and facts.
    Checks each stream independently — either meeting min_results with
    min_confidence is sufficient. Falls back to combined evaluation.

    Uses endswith() matching for node_type to handle both T2 ('entity'/'fact')
    and T3 ('hierarchical_entity'/'hierarchical_fact') metadata values.

    Args:
        evidence: List of evidence items to evaluate
        min_results: Minimum number of results required
        min_confidence: Minimum average confidence score
        total_results: Optional total result count for the quantity check

    Returns:
        Tuple of (is_sufficient, reason)
    """
    if not evidence:
        return False, "No results found"

    entities = [e for e in evidence if e.metadata.get("node_type", "").endswith("entity")]
    facts = [e for e in evidence if e.metadata.get("node_type", "").endswith("fact")]

    for stream_name, stream in [("Entity", entities), ("Fact", facts)]:
        if len(stream) >= min_results:
            avg = sum(e.score for e in stream) / len(stream)
            if avg >= min_confidence:
                return True, f"{stream_name} stream sufficient ({len(stream)} results, avg {avg:.2f})"

    # Neither stream alone sufficient — check combined as fallback
    combined_count = total_results if total_results is not None else len(evidence)
    if combined_count >= min_results and evidence:
        avg = sum(e.score for e in evidence) / len(evidence)
        if avg >= min_confidence:
            return True, f"Combined streams sufficient ({combined_count} results, avg {avg:.2f})"

    entity_info = f"entities: {len(entities)}"
    fact_info = f"facts: {len(facts)}"
    avg_info = f"combined avg: {sum(e.score for e in evidence) / max(len(evidence), 1):.2f}" if evidence else "no evidence"
    return False, f"Insufficient ({entity_info}, {fact_info}, {avg_info})"


# ============================================================================
# Tier Adapters
# ============================================================================


def _query_t1(
    query: str,
    threads_dir: Path,
    limit: int = 10,
    semantic: bool = False,
) -> list[TierEvidence]:
    """Query T1 (Baseline graph).

    Args:
        query: Search query
        threads_dir: Path to threads directory
        limit: Maximum results
        semantic: Use semantic search if available

    Returns:
        List of TierEvidence from T1
    """
    try:
        from watercooler.baseline_graph import SearchQuery, search_graph
    except ImportError:
        logger.warning("T1: baseline_graph module not available")
        return []

    search_query = SearchQuery(
        query=query,
        semantic=semantic,
        limit=limit,
        include_threads=True,
        include_entries=True,
    )

    try:
        results = search_graph(threads_dir, search_query)
    except Exception as e:
        logger.error(f"T1 search failed: {e}")
        return []

    evidence = []
    for r in results.results:
        # Build provenance from matched fields
        provenance = {
            "matched_fields": r.matched_fields,
        }

        if r.entry:
            evidence.append(TierEvidence(
                tier=Tier.T1,
                id=r.entry.entry_id,
                content=r.entry.body or r.entry.summary or "",
                score=r.score,
                name=r.entry.title,
                provenance={
                    **provenance,
                    "thread_topic": r.entry.thread_topic,
                    "timestamp": r.entry.timestamp,
                    "agent": r.entry.agent,
                    "role": r.entry.role,
                },
                metadata={"node_type": "entry"},
            ))
        elif r.thread:
            evidence.append(TierEvidence(
                tier=Tier.T1,
                id=r.thread.topic,
                content=r.thread.summary or "",
                score=r.score,
                name=r.thread.title or r.thread.topic,
                provenance={
                    **provenance,
                    "thread_topic": r.thread.topic,
                    "status": r.thread.status,
                },
                metadata={"node_type": "thread"},
            ))

    return evidence


def _query_t2(
    query: str,
    code_path: Path,
    limit: int = 10,
    group_ids: Optional[Sequence[str]] = None,
) -> list[TierEvidence]:
    """Query T2 (Graphiti backend).

    Args:
        query: Search query
        code_path: Path to code repository
        limit: Maximum results
        group_ids: Optional group IDs to filter

    Returns:
        List of TierEvidence from T2
    """
    try:
        from watercooler_mcp.memory import load_graphiti_config, get_graphiti_backend
    except ImportError:
        logger.warning("T2: watercooler_mcp.memory not available")
        return []

    config = load_graphiti_config(code_path=code_path)
    if config is None:
        logger.debug("T2: Graphiti not enabled")
        return []

    backend = get_graphiti_backend(config)
    if backend is None or isinstance(backend, dict):
        logger.warning(f"T2: Backend initialization failed: {backend}")
        return []

    evidence = []

    # Search nodes
    try:
        nodes = backend.search_nodes(query, group_ids=group_ids, max_results=limit)
        for node in nodes:
            evidence.append(TierEvidence(
                tier=Tier.T2,
                id=node.get("id", ""),
                content=node.get("summary") or node.get("content", ""),
                score=node.get("score", 0.0) or 0.0,
                name=node.get("name"),
                provenance={
                    "group_id": node.get("group_id"),
                    "source": node.get("source"),
                },
                metadata={
                    "node_type": "entity",
                    "backend": "graphiti",
                    **node.get("extra", {}),
                },
            ))
    except Exception as e:
        logger.warning(f"T2 node search failed: {e}")

    # Search facts/edges
    try:
        facts = backend.search_facts(query, group_ids=group_ids, max_results=limit)
        for fact in facts:
            evidence.append(TierEvidence(
                tier=Tier.T2,
                id=fact.get("id", ""),
                content=fact.get("content") or fact.get("fact") or fact.get("summary", ""),
                score=fact.get("score", 0.0) or 0.0,
                name=fact.get("name"),
                provenance={
                    "group_id": fact.get("group_id"),
                    "source": fact.get("source"),
                },
                metadata={
                    "node_type": "fact",
                    "backend": "graphiti",
                    **fact.get("extra", {}),
                },
            ))
    except Exception as e:
        logger.warning(f"T2 fact search failed: {e}")

    return evidence


def _query_t3(
    query: str,
    code_path: Path,
    limit: int = 5,
    group_ids: Optional[Sequence[str]] = None,
    intent: Optional[QueryIntent] = None,
) -> list[TierEvidence]:
    """Query T3 (LeanRAG backend).

    Args:
        query: Search query
        code_path: Path to code repository
        limit: Maximum results
        group_ids: Optional group IDs to filter
        intent: Query intent for level_mode selection (auto-detected if None)

    Returns:
        List of TierEvidence from T3
    """
    try:
        from watercooler_memory.backends.leanrag import LeanRAGBackend
    except ImportError:
        logger.warning("T3: LeanRAG backend not available")
        return []

    # Use unified config loader (parallel to T2's load_graphiti_config)
    try:
        from watercooler_mcp.memory import load_leanrag_config
    except ImportError:
        logger.warning("T3: watercooler_mcp.memory not available")
        return []

    config = load_leanrag_config(code_path=code_path)
    if config is None:
        logger.debug("T3: LeanRAG not configured")
        return []

    try:
        backend = LeanRAGBackend(config)
    except Exception as e:
        logger.warning(f"T3: Backend initialization failed: {e}")
        return []

    # Determine level_mode based on intent
    if intent is None:
        intent = detect_intent(query)
    level_mode = _get_leanrag_level_mode(intent)
    logger.info(f"T3: Using level_mode={level_mode} for intent={intent.value}")

    evidence = []

    # Search nodes (hierarchical)
    try:
        nodes = backend.search_nodes(
            query,
            group_ids=group_ids,
            max_results=limit,
            level_mode=level_mode,
        )
        for node in nodes:
            evidence.append(TierEvidence(
                tier=Tier.T3,
                id=node.get("id", ""),
                content=node.get("summary") or node.get("content", ""),
                score=node.get("score", 0.0) or 0.0,
                name=node.get("name"),
                provenance={
                    "group_id": node.get("group_id"),
                    "source": node.get("source"),
                },
                metadata={
                    "node_type": "hierarchical_entity",
                    "backend": "leanrag",
                    "level_mode": level_mode,
                    **node.get("extra", {}),
                },
            ))
    except Exception as e:
        logger.warning(f"T3 node search failed: {e}")

    # Search facts (hierarchical paths)
    try:
        facts = backend.search_facts(query, group_ids=group_ids, max_results=limit)
        for fact in facts:
            evidence.append(TierEvidence(
                tier=Tier.T3,
                id=fact.get("id", ""),
                content=fact.get("content") or fact.get("fact") or fact.get("summary", ""),
                score=fact.get("score", 0.0) or 0.0,
                name=fact.get("name"),
                provenance={
                    "group_id": fact.get("group_id"),
                    "source": fact.get("source"),
                },
                metadata={
                    "node_type": "hierarchical_fact",
                    "backend": "leanrag",
                    **fact.get("extra", {}),
                },
            ))
    except Exception as e:
        logger.warning(f"T3 fact search failed: {e}")

    return evidence


# ============================================================================
# Orchestrator
# ============================================================================


class TierOrchestrator:
    """Multi-tier memory query orchestrator.

    Implements the "cheapest sufficient tier" principle with automatic
    escalation when lower tiers don't provide adequate results.

    Example:
        >>> config = load_tier_config(threads_dir=Path("./threads"))
        >>> orchestrator = TierOrchestrator(config)
        >>> result = orchestrator.query("What authentication was implemented?")
        >>> print(result.primary_tier, result.result_count)
        T1 5
    """

    def __init__(self, config: TierConfig):
        """Initialize orchestrator with configuration.

        Args:
            config: TierConfig instance
        """
        self.config = config
        self._available_tiers: list[Tier] = []
        self._detect_available_tiers()

    def _detect_available_tiers(self) -> None:
        """Detect which tiers are available and enabled."""
        self._available_tiers = []

        if self.config.t1_enabled:
            # T1 needs graph files to exist (not just threads_dir)
            if self.config.threads_dir:
                graph_dir = self.config.threads_dir / "graph" / "baseline"
                if (graph_dir / "nodes.jsonl").exists():
                    self._available_tiers.append(Tier.T1)
                else:
                    logger.debug("T1 disabled: graph files not found at %s", graph_dir)
            else:
                logger.debug("T1 disabled: threads_dir not set")

        if self.config.t2_enabled:
            # T2 needs Graphiti configuration
            try:
                from watercooler_mcp.memory import load_graphiti_config
                if load_graphiti_config(self.config.code_path) is not None:
                    self._available_tiers.append(Tier.T2)
                else:
                    logger.debug("T2 disabled: Graphiti not configured")
            except ImportError:
                logger.debug("T2 disabled: watercooler_mcp.memory not available")

        if self.config.t3_enabled:
            # T3 needs LeanRAG configuration (parallel to T2's Graphiti check)
            try:
                from watercooler_mcp.memory import load_leanrag_config
                if load_leanrag_config(self.config.code_path) is not None:
                    self._available_tiers.append(Tier.T3)
                else:
                    logger.debug("T3 disabled: LeanRAG not configured")
            except ImportError:
                logger.debug("T3 disabled: watercooler_mcp.memory not available")

    @property
    def available_tiers(self) -> list[Tier]:
        """Get list of available tiers."""
        return self._available_tiers.copy()

    def _select_starting_tier(self, intent: QueryIntent) -> Tier:
        """Select the starting tier based on query intent.

        Args:
            intent: Detected query intent

        Returns:
            The tier to start with
        """
        if not self._available_tiers:
            raise ValueError("No tiers available")

        # Intent-based tier selection with availability checks
        if intent == QueryIntent.MULTI_HOP and Tier.T3 in self._available_tiers:
            return Tier.T3
        elif intent in (QueryIntent.TEMPORAL, QueryIntent.ENTITY_SEARCH, QueryIntent.RELATIONAL):
            if Tier.T2 in self._available_tiers:
                return Tier.T2
            elif Tier.T1 in self._available_tiers:
                return Tier.T1
        elif intent == QueryIntent.SUMMARIZE:
            # Start with T2 for verified facts, may escalate to T3
            if Tier.T2 in self._available_tiers:
                return Tier.T2
            elif Tier.T1 in self._available_tiers:
                return Tier.T1

        # Default: start with cheapest available tier
        for tier in [Tier.T1, Tier.T2, Tier.T3]:
            if tier in self._available_tiers:
                return tier

        raise ValueError("No tiers available")

    def query(
        self,
        query: str,
        intent: Optional[QueryIntent] = None,
        group_ids: Optional[Sequence[str]] = None,
        force_tier: Optional[Tier] = None,
        allow_escalation: bool = True,
    ) -> TierResult:
        """Execute multi-tier query with automatic escalation.

        Args:
            query: Search query string
            intent: Optional explicit intent (auto-detected if not provided)
            group_ids: Optional group IDs to filter results
            force_tier: Force query to specific tier (no escalation)
            allow_escalation: Allow automatic tier escalation (default: True)

        Returns:
            TierResult with evidence from queried tiers
        """
        result = TierResult(query=query)

        if not self._available_tiers:
            result.message = "No memory tiers available"
            return result

        # Detect intent if not provided
        if intent is None:
            intent = detect_intent(query)

        # Select starting tier
        if force_tier:
            if force_tier not in self._available_tiers:
                result.message = f"Tier {force_tier.value} not available"
                return result
            current_tier = force_tier
            allow_escalation = False
        else:
            current_tier = self._select_starting_tier(intent)

        tiers_to_try = [current_tier]
        tiers_tried = 0

        # Track queried tiers to prevent duplicates (O(1) lookup)
        queried_tiers: set[Tier] = set()

        while tiers_tried < self.config.max_tiers and tiers_to_try:
            tier = tiers_to_try.pop(0)

            # Skip if already queried (defensive guard)
            if tier in queried_tiers:
                continue

            queried_tiers.add(tier)
            tiers_tried += 1
            result.tiers_queried.append(tier)
            result.total_cost += TIER_COSTS[tier]

            # Query the tier with timing
            before_count = result.result_count
            t0 = time.monotonic()
            tier_evidence = self._query_tier(tier, query, group_ids, intent)
            elapsed = round((time.monotonic() - t0) * 1000)
            result.tier_timings[tier.value] = elapsed
            # elapsed_ms is exact (not approximate): both fields are set from
            # the same `elapsed` variable in the same loop iteration.
            result.elapsed_ms += elapsed
            result.evidence.extend(tier_evidence)
            new_count = result.result_count - before_count

            # Evaluate confidence on current tier's evidence only, but fall back to
            # all evidence if current tier returned nothing (for cross-tier queries)
            evidence_for_eval = tier_evidence if tier_evidence else result.evidence

            # Check sufficiency — use dual-stream for T2/T3 (entities + facts)
            if tier in (Tier.T2, Tier.T3):
                is_sufficient, reason = evaluate_dual_stream_sufficiency(
                    evidence_for_eval,
                    min_results=self.config.min_results,
                    min_confidence=self.config.min_confidence,
                    total_results=result.result_count,
                )
            else:
                is_sufficient, reason = evaluate_sufficiency(
                    evidence_for_eval,
                    min_results=self.config.min_results,
                    min_confidence=self.config.min_confidence,
                    total_results=result.result_count,
                )

            if is_sufficient:
                result.sufficient = True
                result.primary_tier = tier
                result.message = f"Found {result.result_count} results from {tier.value}"
                break

            # Consider escalation
            if allow_escalation and tiers_tried < self.config.max_tiers:
                next_tier = None

                # Prefer cheaper fallback if the current tier produced nothing
                if new_count == 0:
                    for lower_tier in self._get_lower_tiers(tier):
                        if lower_tier not in queried_tiers and lower_tier not in tiers_to_try:
                            next_tier = lower_tier
                            break

                # Otherwise escalate upward
                if next_tier is None:
                    next_tier = self._get_next_tier(tier)

                if next_tier and next_tier not in queried_tiers and next_tier not in tiers_to_try:
                    result.escalation_reason = reason
                    tiers_to_try.append(next_tier)
                    logger.info(f"Escalating from {tier.value} to {next_tier.value}: {reason}")

        # Finalize result
        if not result.primary_tier and result.evidence:
            # Pick the tier that contributed most evidence
            tier_counts = {}
            for e in result.evidence:
                tier_counts[e.tier] = tier_counts.get(e.tier, 0) + 1
            result.primary_tier = max(tier_counts, key=lambda t: tier_counts[t])

        if not result.sufficient:
            result.message = f"Partial results ({result.result_count}) from {len(result.tiers_queried)} tiers"

        return result

    def _query_tier(
        self,
        tier: Tier,
        query: str,
        group_ids: Optional[Sequence[str]] = None,
        intent: Optional[QueryIntent] = None,
    ) -> list[TierEvidence]:
        """Query a specific tier.

        Args:
            tier: Which tier to query
            query: Search query
            group_ids: Optional group IDs to filter
            intent: Query intent (used by T3 for level_mode selection)

        Returns:
            List of TierEvidence from the tier
        """
        if tier == Tier.T1:
            if not self.config.threads_dir:
                logger.debug("T1 query skipped: threads_dir not configured")
                return []
            return _query_t1(
                query,
                self.config.threads_dir,
                limit=self.config.t1_limit,
                semantic=True,  # Use semantic search for natural language queries
            )
        elif tier == Tier.T2:
            if not self.config.code_path:
                logger.debug("T2 query skipped: code_path not configured")
                return []
            return _query_t2(
                query,
                self.config.code_path,
                limit=self.config.t2_limit,
                group_ids=group_ids,
            )
        elif tier == Tier.T3:
            if not self.config.code_path:
                logger.debug("T3 query skipped: code_path not configured")
                return []
            return _query_t3(
                query,
                self.config.code_path,
                limit=self.config.t3_limit,
                group_ids=group_ids,
                intent=intent,
            )

        return []

    def _get_next_tier(self, current: Tier) -> Optional[Tier]:
        """Get next tier for escalation.

        Args:
            current: Current tier

        Returns:
            Next tier or None if at highest available tier
        """
        tier_order = [Tier.T1, Tier.T2, Tier.T3]
        try:
            idx = tier_order.index(current)
            for next_tier in tier_order[idx + 1:]:
                if next_tier in self._available_tiers:
                    return next_tier
        except (ValueError, IndexError):
            pass
        return None

    def _get_lower_tiers(self, current: Tier) -> list[Tier]:
        """Get cheaper tiers than the current one, nearest first."""
        tier_order = [Tier.T1, Tier.T2, Tier.T3]
        try:
            idx = tier_order.index(current)
            return [
                tier
                for tier in reversed(tier_order[:idx])
                if tier in self._available_tiers
            ]
        except ValueError:
            return []


# ============================================================================
# Convenience Functions
# ============================================================================


def smart_query(
    query: str,
    threads_dir: Optional[Path] = None,
    code_path: Optional[Path] = None,
    group_ids: Optional[Sequence[str]] = None,
    max_tiers: int = DEFAULT_MAX_TIERS,
) -> TierResult:
    """Execute a smart multi-tier query.

    Convenience function that creates an orchestrator and runs a query.

    Args:
        query: Search query string
        threads_dir: Path to threads directory (for T1)
        code_path: Path to code repository (for T2/T3)
        group_ids: Optional group IDs to filter results
        max_tiers: Maximum tiers to query (default: 2)

    Returns:
        TierResult with evidence from queried tiers

    Example:
        >>> result = smart_query(
        ...     "What authentication was implemented?",
        ...     threads_dir=Path("./threads"),
        ...     code_path=Path("."),
        ... )
        >>> for e in result.top_results(3):
        ...     print(f"[{e.tier.value}] {e.content[:100]}")
    """
    config = load_tier_config(threads_dir=threads_dir, code_path=code_path)
    config.max_tiers = max_tiers
    orchestrator = TierOrchestrator(config)
    return orchestrator.query(query, group_ids=group_ids)


__all__ = [
    # Enums
    "Tier",
    "QueryIntent",
    # Config
    "TierConfig",
    "load_tier_config",
    # Results
    "TierEvidence",
    "TierResult",
    # Functions
    "detect_intent",
    "evaluate_sufficiency",
    "evaluate_dual_stream_sufficiency",
    "smart_query",

    # Orchestrator
    "TierOrchestrator",
    # Constants
    "TIER_COSTS",
    "DEFAULT_MIN_RESULTS",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MAX_TIERS",
    "LEANRAG_LEVEL_MODE_BASE",
    "LEANRAG_LEVEL_MODE_CLUSTERS",
    "LEANRAG_LEVEL_MODE_ALL",
]
