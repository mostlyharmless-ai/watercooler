"""Graph-level observability for ``watercooler_health(detail="graph")``.

Jay's request from incident bug-hybrid-static-x-repo-cross-tenant-t2-scope:
"we need a version of watercooler-health that returns more graph-related
info — locally I could inspect the graph DB with [FalkorDB Browser], so it
was easy to tell if it was working." Hosted had no equivalent, so episodes
misfiled into side graphs (``app_t2``) were invisible for weeks while the
indexer re-bought the same extractions.

Everything here is read-only. Enumeration runs wherever a FalkorDB
connection is configured (hosted, or stdio-local with a local FalkorDB);
the hybrid surface forwards to hosted instead (see
``tools/diagnostic._health_graph_impl``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Enumeration bound: a runaway server-side graph count should produce a
# truncated (and flagged) report, not an unbounded scan.
_MAX_GRAPHS = 200

_TIER_SUFFIXES = ("_t1", "_t2", "_t3")


def _split_tier(name: str) -> tuple[str, str]:
    """Return ``(base, tier_suffix)`` for a FalkorDB graph name."""
    for suffix in _TIER_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def classify_graph(name: str, canonical_bases: set[str]) -> str:
    """Classify a graph relative to the caller's canonical scope.

    - ``canonical``: one of the caller's own tier databases.
    - ``legacy_orphan``: a single-token base (cwd/threads_dir-basename
      fallback like ``app`` or ``watercooler``) or a tierless name —
      the money-loop signature; paid-for data no reader targets.
    - ``foreign``: another tenant's well-formed ``<org>_<repo>`` graph.
    """
    base, tier = _split_tier(name)
    if base in canonical_bases:
        return "canonical"
    if not tier or "_" not in base:
        return "legacy_orphan"
    return "foreign"


def _count(graph: Any, query: str) -> Optional[int]:
    try:
        result = graph.query(query)
        rows = getattr(result, "result_set", None) or []
        if rows and rows[0]:
            return int(rows[0][0] or 0)
        return 0
    except Exception as e:  # noqa: BLE001 — per-graph counts are best-effort
        logger.debug("GRAPH_DETAIL: count query failed: %s", e)
        return None


def build_graph_detail(
    *,
    canonical_bases: set[str],
    include_all_scopes: bool,
) -> Dict[str, Any]:
    """Enumerate FalkorDB graphs with counts and scope flags (read-only).

    Args:
        canonical_bases: project-group bases the caller owns (e.g.
            ``{"mostlyharmless_ai_watercooler_cloud"}``) — their tier
            databases are flagged ``canonical`` and always fully reported.
        include_all_scopes: when False (per-user caller without the
            ``graph_admin`` grant), foreign/orphan graphs are collapsed
            into an anonymized aggregate (count + total nodes, no names).
    """
    from .hosted_semantic import _get_falkor_client

    try:
        client = _get_falkor_client()
        names: List[str] = list(client.list_graphs())
    except Exception as e:
        return {
            "available": False,
            "error": f"falkordb_unreachable: {e}",
        }

    truncated = len(names) > _MAX_GRAPHS
    names = names[:_MAX_GRAPHS]

    graphs: List[Dict[str, Any]] = []
    hidden_count = 0
    hidden_nodes = 0
    latest_write: Optional[str] = None
    latest_write_db: Optional[str] = None

    for name in sorted(names):
        flag = classify_graph(name, canonical_bases)
        try:
            g = client.select_graph(name)
        except Exception:
            g = None
        nodes = _count(g, "MATCH (n) RETURN count(n)") if g else None
        if flag != "canonical" and not include_all_scopes:
            hidden_count += 1
            hidden_nodes += nodes or 0
            continue
        edges = _count(g, "MATCH ()-[r]->() RETURN count(r)") if g else None
        episodes = _count(
            g, "MATCH (e:Episodic) RETURN count(e)"
        ) if g else None
        entry: Dict[str, Any] = {
            "name": name,
            "flag": flag,
            "nodes": nodes,
            "edges": edges,
            "episodes": episodes,
        }
        if g and (episodes or 0) > 0:
            try:
                result = g.query(
                    "MATCH (e:Episodic) RETURN max(e.created_at)"
                )
                rows = getattr(result, "result_set", None) or []
                raw = rows[0][0] if rows and rows[0] else None
                if raw is not None:
                    entry["last_episode_at"] = str(raw)
                    if latest_write is None or str(raw) > latest_write:
                        latest_write = str(raw)
                        latest_write_db = name
            except Exception:
                pass
        graphs.append(entry)

    report: Dict[str, Any] = {
        "available": True,
        "graphs": graphs,
        "canonical_bases": sorted(canonical_bases),
        "last_write": {"at": latest_write, "database": latest_write_db},
    }
    if truncated:
        report["truncated_at"] = _MAX_GRAPHS
    if not include_all_scopes:
        report["other_scopes"] = {
            "graphs": hidden_count,
            "total_nodes": hidden_nodes,
            "note": (
                "cross-scope enumeration requires the graph_admin "
                "capability (or a service key); showing aggregate only"
            ),
        }
    return report


def indexer_coverage(threads_dir: Any, t2_episodes: Optional[int]) -> Dict[str, Any]:
    """Best-effort T1-entries vs T2-episodes coverage for the caller's scope.

    ``t2_episodes`` comes from the canonical ``_t2`` graph's episode count
    in the enumeration above; T1 entry count is read from the baseline
    graph on disk when available (local surfaces) — hosted callers get the
    episode count alone, which is still the load-bearing drift signal.
    """
    coverage: Dict[str, Any] = {"t2_episodes": t2_episodes}
    try:
        from watercooler.baseline_graph import storage

        graph_dir = storage.get_graph_dir(threads_dir)
        topics = storage.list_thread_topics(graph_dir)
        total = 0
        for topic in topics:
            meta = storage.load_thread_meta(graph_dir, topic) or {}
            count = meta.get("entry_count")
            if count is None:
                count = sum(
                    1 for _ in storage.load_thread_entries(graph_dir, topic)
                )
            total += int(count or 0)
        coverage["t1_entries"] = total
        if t2_episodes is not None and total:
            coverage["unindexed_estimate"] = max(0, total - t2_episodes)
    except Exception as e:
        coverage["t1_entries"] = None
        coverage["note"] = f"t1 count unavailable: {e}"
    return coverage
