"""trend_snapshot_lib — pure Python trend metric computation.

Computes Tier 3 trend signals from raw Graphiti fact data (EntityEdge dicts).
No I/O, no external imports — stdlib only. Designed for use by
TrendSnapshotDaemon and unit tests.

Key design note:
    EntityEdge.model_dump() exposes source_node_uuid/target_node_uuid (UUIDs),
    NOT node names. Node name resolution would require batch graph queries.
    Instead, topic grouping uses the edge ``name`` field (relation type like
    "uses", "decided", "depends_on") which is always available on serialized edges.
"""

from __future__ import annotations

from typing import Any

# Closed set of valid trend directions produced by compute_trend_metrics.
# Import this constant in consumers (e.g. pulse_report.py) rather than re-defining it.
TREND_DIRECTIONS: frozenset[str] = frozenset({"stable", "improving", "degrading"})


def compute_trend_metrics(
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute trend metrics from raw fact data.

    Args:
        facts: List of fact dicts from EntityEdge.model_dump(). Each has:
            - "fact": str  (the fact text)
            - "name": str  (edge relation type, e.g. "uses", "decided")
            - "invalid_at": str | None  (ISO timestamp if superseded, else None/absent)
            - "source_node_uuid": str  (entity UUID — NOT name)
            - "target_node_uuid": str  (entity UUID — NOT name)
            - "uuid": str

    Returns:
        Dict matching TrendSignals fields:
            supersession_rate, active_fact_count, superseded_fact_count,
            sample_size, top_volatile_topics, top_stable_topics,
            trend_direction.
    """
    if not facts:
        return {
            "supersession_rate": 0.0,
            "active_fact_count": 0,
            "superseded_fact_count": 0,
            "sample_size": 0,
            "top_volatile_topics": [],
            "top_stable_topics": [],
            "trend_direction": "stable",
        }

    # Partition into active vs superseded
    active: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for f in facts:
        if f.get("invalid_at") is not None:
            superseded.append(f)
        else:
            active.append(f)

    total = len(facts)
    supersession_rate = len(superseded) / total

    # Group by edge name (relation type)
    # topic_active[name] = count of active facts with this edge name
    # topic_superseded[name] = count of superseded facts with this edge name
    topic_active: dict[str, int] = {}
    topic_superseded: dict[str, int] = {}

    for f in active:
        name = f.get("name") or "unknown"
        topic_active[name] = topic_active.get(name, 0) + 1

    for f in superseded:
        name = f.get("name") or "unknown"
        topic_superseded[name] = topic_superseded.get(name, 0) + 1

    all_topics = set(topic_active) | set(topic_superseded)

    # Volatile topics: relation types where >50% of facts are superseded,
    # sorted by supersession count desc, then alpha
    volatile: list[tuple[int, str]] = []  # (-count, name)
    for name in all_topics:
        sup_count = topic_superseded.get(name, 0)
        act_count = topic_active.get(name, 0)
        topic_total = sup_count + act_count
        if topic_total > 0 and sup_count / topic_total > 0.5:
            volatile.append((-sup_count, name))

    volatile.sort()
    top_volatile_topics = [t[1] for t in volatile[:5]]

    # Stable topics: relation types where 100% active AND count >= 2,
    # sorted by count desc, then alpha
    stable: list[tuple[int, str]] = []  # (-count, name)
    for name in all_topics:
        sup_count = topic_superseded.get(name, 0)
        act_count = topic_active.get(name, 0)
        if sup_count == 0 and act_count >= 2:
            stable.append((-act_count, name))

    stable.sort()
    top_stable_topics = [t[1] for t in stable[:5]]

    # Trend direction heuristic (simplified, no cross-pulse history required)
    if supersession_rate < 0.2:
        trend_direction = "stable"
    elif supersession_rate > 0.5:
        trend_direction = "degrading"
    else:
        trend_direction = "improving"  # moderate churn = healthy evolution

    return {
        "supersession_rate": supersession_rate,
        "active_fact_count": len(active),
        "superseded_fact_count": len(superseded),
        "sample_size": total,
        "top_volatile_topics": top_volatile_topics,
        "top_stable_topics": top_stable_topics,
        "trend_direction": trend_direction,
    }
