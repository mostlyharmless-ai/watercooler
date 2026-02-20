"""Response envelope builder, dedup, allocation cap, and result sorting.

Merges scored results from multiple namespaces into a single ranked list
with provenance metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ScoredResult",
    "allocate_candidates",
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
    timestamp: str  # ISO 8601, for tiebreaking


def allocate_candidates(limit: int) -> tuple[int, int]:
    """Compute candidate allocation.

    Primary: gets full `limit` candidates (uncapped).
    Secondary: each gets max(limit // 2, 1) candidates.

    Args:
        limit: Total result limit requested by caller.

    Returns:
        (primary_limit, per_secondary_limit).
    """
    return limit, max(limit // 2, 1)


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
    def sort_key(r: ScoredResult) -> tuple[float, int, str, str]:
        return (
            -r.ranking_score,
            0 if r.origin_namespace == primary_namespace else 1,
            # Negate timestamp for newest-first (ISO 8601 sorts lexicographically)
            _negate_timestamp(r.timestamp),
            r.entry_id,
        )

    deduped.sort(key=sort_key)
    return deduped[:limit]


def _negate_timestamp(ts: str) -> str:
    """Invert ISO 8601 timestamp for descending sort.

    Since ISO 8601 timestamps sort lexicographically in ascending order,
    we complement each character to reverse the ordering. Characters
    outside the printable ASCII sort range are left unchanged.
    """
    # For descending order on timestamps, prefix with negated chars
    # Using a simpler approach: prepend a '-' to use reverse string ordering
    # won't work directly. Instead, since all timestamps share the same format,
    # we can invert the string by complementing digits.
    inverted = []
    for c in ts:
        if c.isdigit():
            inverted.append(str(9 - int(c)))
        else:
            inverted.append(c)
    return "".join(inverted)


def build_response_envelope(
    results: list[ScoredResult],
    primary_namespace: str,
    namespace_status: dict[str, str],
    queried_namespaces: list[str],
    query: str,
    total_candidates: int,
    primary_branch_filter: str = "",
) -> dict[str, Any]:
    """Build federation response envelope with provenance metadata.

    Args:
        results: Sorted, merged results.
        primary_namespace: The primary namespace ID.
        namespace_status: Per-namespace status (ok/timeout/error/access_denied).
        queried_namespaces: All namespaces that were queried.
        query: The original search query.
        total_candidates: Total results before truncation.
        primary_branch_filter: Branch filter applied to primary namespace.

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
        "primary_namespace": primary_namespace,
        "queried_namespaces": queried_namespaces,
        "namespace_status": namespace_status,
        "result_count": len(results),
        "total_candidates_before_truncation": total_candidates,
        "results": result_dicts,
    }

    if primary_branch_filter:
        envelope["primary_branch_filter"] = primary_branch_filter

    return envelope
