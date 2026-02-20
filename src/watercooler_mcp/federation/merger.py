"""Response envelope builder, dedup, allocation cap, and result sorting.

Merges scored results from multiple namespaces into a single ranked list
with provenance metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ScoredResult",
    "merge_results",
    "build_response_envelope",
]


@dataclass(frozen=True)
class ScoredResult:
    """Immutable scored result — constructed once, never mutated."""

    entry_id: str
    origin_namespace: str
    raw_score: float
    normalized_score: float
    namespace_weight: float
    recency_decay: float
    ranking_score: float
    entry_data: dict[str, Any]
    timestamp: str  # ISO 8601, for display
    timestamp_epoch: float  # Pre-computed epoch for sort tiebreaking


def merge_results(
    namespace_results: dict[str, list[ScoredResult]],
    primary_namespace: str,
    limit: int,
    min_score: float = 0.01,
) -> list[ScoredResult]:
    """Merge, filter, dedup, sort, truncate.

    1. Filter out results with RankingScore < min_score
    2. Dedup by entry_id (safety net — ULIDs are globally unique)
    3. Sort by RankingScore descending
    4. Tiebreak: primary first, then newest timestamp, then entry_id
    5. Truncate to limit

    Args:
        namespace_results: Results grouped by namespace.
        primary_namespace: The primary namespace ID.
        limit: Maximum results to return.
        min_score: Minimum ranking score threshold.

    Returns:
        Sorted, deduplicated, truncated list of ScoredResults.
    """
    all_results: list[ScoredResult] = []
    for results in namespace_results.values():
        all_results.extend(results)

    # Filter by min_score
    all_results = [r for r in all_results if r.ranking_score >= min_score]

    # Dedup by entry_id (safety net)
    seen: set[str] = set()
    deduped: list[ScoredResult] = []
    for r in all_results:
        if r.entry_id not in seen:
            seen.add(r.entry_id)
            deduped.append(r)

    # Sort: RankingScore desc, tiebreak primary first, newest, entry_id
    def sort_key(r: ScoredResult) -> tuple[float, int, float, str]:
        return (
            -r.ranking_score,
            0 if r.origin_namespace == primary_namespace else 1,
            -r.timestamp_epoch,
            r.entry_id,
        )

    deduped.sort(key=sort_key)
    return deduped[:limit]


def build_response_envelope(
    results: list[ScoredResult],
    primary_namespace: str,
    namespace_status: dict[str, Any],
    queried_namespaces: list[str],
    query: str,
    total_candidates: int,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build federation response envelope with provenance metadata.

    Args:
        results: Sorted, merged results.
        primary_namespace: The primary namespace ID.
        namespace_status: Per-namespace status dicts with optional diagnostics.
        queried_namespaces: All namespaces that were queried.
        query: The original search query.
        total_candidates: Total scored results before limit truncation
            (post-deny_topics filtering, pre-limit).
        warnings: Optional list of warning messages (e.g., namespace collisions).

    Returns:
        Response envelope dict with schema_version for forward compatibility.
    """
    result_dicts = []
    for r in results:
        result_dicts.append({
            "entry_id": r.entry_id,
            "origin_namespace": r.origin_namespace,
            "ranking_score": round(r.ranking_score, 4),
            "score_breakdown": {
                "raw_score": round(r.raw_score, 4),
                "normalized_score": round(r.normalized_score, 4),
                "namespace_weight": round(r.namespace_weight, 4),
                "recency_decay": round(r.recency_decay, 4),
            },
            "entry_data": r.entry_data,
        })

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "query": query,
        "primary_namespace": primary_namespace,
        "queried_namespaces": queried_namespaces,
        "namespace_status": namespace_status,
        "result_count": len(results),
        "total_candidates_before_truncation": total_candidates,
        "results": result_dicts,
    }
    if warnings:
        envelope["warnings"] = warnings

    return envelope
