"""Shared library for Project Pulse snapshot computation.

Pure Python — no MCP, no LLM, no subprocess calls.
Importable by both the PulseSnapshotDaemon and the parse_pulse.py skill script.

Public API
----------
- ``derive_repo_key(code_root)`` — deterministic repo identifier
- ``scan_session_threads(threads_dir, ...)`` — contributor aggregates from session-context-* threads
- ``compute_stalled_threads(threads_dir, ...)`` — threads with no recent activity
- ``compute_risk_tags(contributors)`` — heuristic risk surface tags
- ``check_analysis_freshness(reports_dir, ...)`` — age of most recent analysis report
- ``count_queue_pending()`` — pulse_queue.jsonl line count without draining
- ``build_snapshot(threads_dir, ...)`` — full snapshot dict (orchestrates all of the above)
- ``compute_convergence_signals(threads_dir, topics, ...)`` — per-thread convergence telemetry
  (Phase 5a: semantic_novelty_decline, concern_cluster_recurrence, tradeoff_recurrence,
  constraint_class_emergence)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.storage import get_graph_dir, load_thread_meta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_KINDS = frozenset({
    "insight", "decision", "problem", "risk",
    "exploration", "lesson", "reasoning", "stopgap", "procedure",
    # D4 delivery signals (Phase 2)
    "pr_merged", "closure", "resolved_loop", "opened_loops", "closed_loops",
})

QUEUE_PATH = Path.home() / ".watercooler" / "pulse_queue.jsonl"

_MAX_FOCUS_AREAS = 10
_MAX_RECENT_OBSERVATIONS = 20
_MAX_OPEN_LOOPS = 5
_MAX_BODY_BYTES = 50_000  # cap body ingestion to prevent runaway memory


# ---------------------------------------------------------------------------
# Repo key
# ---------------------------------------------------------------------------


def derive_repo_key(code_root: Path) -> str:
    """Deterministic, collision-resistant repo key from resolved code root path.

    Uses SHA-1 of the resolved absolute path string, truncated to 12 hex chars.
    Both the daemon and the MCP tool import this — single source of truth.
    """
    return hashlib.sha1(
        str(code_root.resolve()).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp, returning a UTC-aware datetime or None on failure.

    Naive timestamps (no Z or +offset) are assumed UTC and made timezone-aware.
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _now_utc(now: datetime | None) -> datetime:
    """Return ``now`` if provided, else ``datetime.now(timezone.utc)``."""
    if now is not None:
        return now
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime | None) -> str:
    """Format a datetime to ISO 8601 string for checkpoint storage."""
    if dt is None:
        return ""
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Entry parsing helpers
# ---------------------------------------------------------------------------


def _parse_theme_body(body: str) -> dict[str, Any] | None:
    """Try to parse an entry body as a session theme JSON."""
    try:
        data = json.loads(body)
        if data.get("record_kind") == "extracted_theme":
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _extract_contributor(topic: str) -> str:
    """Extract contributor name from session-context-<name> topic."""
    prefix = "session-context-"
    if topic.startswith(prefix):
        return topic[len(prefix):]
    return topic


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------


def scan_session_threads(
    threads_dir: Path,
    *,
    window_days: int = 7,
    code_branch: str = "*",
    max_threads: int = 50,
    now: datetime | None = None,
    session_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Scan session-context-* threads and build contributor aggregates.

    Reads baseline graph entries for each session-context-* thread.
    Per-entry exceptions are isolated so one malformed entry does not abort
    the scan.

    Args:
        threads_dir: Threads repository root (e.g. worktree path).
        window_days: Look-back window for session entries.
        code_branch: Branch filter; ``"*"`` accepts all branches.
        max_threads: Maximum session threads to scan (most-recent first).
        now: Override current time for testing.
        session_topics: Pre-computed list of session-context-* topics. When
            provided, skips the internal ``list_thread_topics`` call. Callers
            that already enumerated all topics (e.g. the daemon tick loop)
            should pass this to avoid a redundant enumeration.

    Returns:
        Dict with keys: contributors, corpus, queue_pending.
        Shape matches ``parse_pulse.py`` analyze_sessions() output.
    """
    graph_dir = get_graph_dir(threads_dir)
    current_time = _now_utc(now)
    since = current_time - timedelta(days=window_days)

    # List session-context-* topics — skip if already provided by caller
    if session_topics is None:
        all_topics = storage.list_thread_topics(graph_dir)
        session_topics = sorted(
            [t for t in all_topics if t.startswith("session-context-")]
        )

    # If more topics than max_threads, sort by most-recent meta.last_updated first
    def _topic_mtime(topic: str) -> str:
        try:
            meta = load_thread_meta(graph_dir, topic) or {}
            return meta.get("last_updated", "") or ""
        except Exception:
            return ""

    if len(session_topics) > max_threads:
        session_topics = sorted(session_topics, key=_topic_mtime, reverse=True)[:max_threads]

    contributors: dict[str, Any] = {}
    total_sessions = 0
    total_entries = 0

    for thread_topic in session_topics:
        contributor = _extract_contributor(thread_topic)

        # Load and filter entries in a single pass — avoids materializing the
        # full entry list before applying the time-window filter.
        window_entries = []
        entry_count = 0
        try:
            for entry in storage.load_thread_entries(graph_dir, thread_topic):
                entry_count += 1
                try:
                    ts = _parse_timestamp(entry.get("timestamp"))
                    if ts and ts >= since:
                        window_entries.append(entry)
                except Exception as exc:
                    logger.debug(
                        "pulse_snapshot_lib: error processing timestamp in %s: %s",
                        thread_topic, exc,
                    )
        except Exception as exc:
            logger.debug(
                "pulse_snapshot_lib: error loading entries for %s: %s", thread_topic, exc
            )
            continue

        total_entries += entry_count

        sessions: list[dict[str, Any]] = []
        for entry in window_entries:
            try:
                # Branch filter
                if code_branch != "*":
                    entry_branch = entry.get("code_branch", "")
                    if not entry_branch or entry_branch != code_branch:
                        continue

                body = str(entry.get("body") or "")[:_MAX_BODY_BYTES]
                theme = _parse_theme_body(body)

                session: dict[str, Any] = {
                    "entry_id": str(entry.get("entry_id") or ""),
                    "timestamp": entry.get("timestamp", ""),
                    "title": entry.get("title", ""),
                    "branch": "",
                    "technical_focus": [],
                    "session_intent": "",
                    "observations": [],
                    "confidence": 0.0,
                }

                if theme:
                    session["branch"] = theme.get("branch", "")
                    session["technical_focus"] = theme.get("technical_focus", [])
                    session["session_intent"] = theme.get("session_intent", "")
                    session["observations"] = theme.get("observations", [])
                    session["confidence"] = float(theme.get("confidence", 0.0))
                else:
                    session["session_intent"] = str(entry.get("title", "") or "")

                sessions.append(session)
            except Exception as exc:
                logger.debug(
                    "pulse_snapshot_lib: error processing entry in %s: %s", thread_topic, exc
                )
                continue

        if not sessions:
            continue

        total_sessions += len(sessions)

        # Aggregate focus areas
        focus_counter: Counter[str] = Counter()
        for s in sessions:
            for label in s.get("technical_focus", []):
                if label:
                    focus_counter[str(label)] += 1

        # Aggregate observations by kind
        obs_by_kind: dict[str, list[str]] = defaultdict(list)
        for s in sessions:
            for obs in s.get("observations", []):
                kind = obs.get("kind", "")
                text = obs.get("text", "")
                if kind in VALID_KINDS and text:
                    obs_by_kind[kind].append(str(text))

        # Detect open loops (problems/risks recurring 2+ times, no resolution)
        problem_texts = obs_by_kind.get("problem", []) + obs_by_kind.get("risk", [])
        problem_counter: Counter[str] = Counter()
        for t in problem_texts:
            key = t[:50].lower().strip()
            problem_counter[key] += 1
        open_loops_raw = [
            t for t in problem_texts
            if problem_counter.get(t[:50].lower().strip(), 0) >= 2
        ]
        seen_loops: set[str] = set()
        unique_loops: list[str] = []
        for loop in open_loops_raw:
            key = loop[:50].lower().strip()
            if key not in seen_loops:
                seen_loops.add(key)
                unique_loops.append(loop)

        # Sort sessions by recency
        sessions.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
        last_active = sessions[0]["timestamp"] if sessions else ""

        # Recent observations (last 5 sessions, most recent first), capped
        recent_obs: list[dict[str, Any]] = []
        for s in sessions[:5]:
            for obs in s.get("observations", []):
                recent_obs.append({
                    "kind": obs.get("kind", ""),
                    "text": str(obs.get("text", "") or ""),
                    "session_timestamp": s["timestamp"],
                })
        recent_obs = recent_obs[:_MAX_RECENT_OBSERVATIONS]

        contributors[contributor] = {
            "name": contributor,
            "thread_topic": thread_topic,
            "session_count": len(sessions),
            "last_active": last_active,
            "focus_areas": [item for item, _ in focus_counter.most_common(_MAX_FOCUS_AREAS)],
            "recent_observations": recent_obs,
            "observation_counts": {k: len(v) for k, v in obs_by_kind.items()},
            "open_loops": unique_loops[:_MAX_OPEN_LOOPS],
            "avg_confidence": (
                sum(s.get("confidence", 0.0) for s in sessions) / len(sessions)
                if sessions else 0.0
            ),
        }

    queue_pending = count_queue_pending()

    return {
        "corpus": {
            "session_context_threads": len(session_topics),
            "total_entries_scanned": total_entries,
            "sessions_in_window": total_sessions,
            "contributors_active": len(contributors),
        },
        "contributors": contributors,
        "queue_pending": queue_pending,
    }


# ---------------------------------------------------------------------------
# Stalled thread detection
# ---------------------------------------------------------------------------


def compute_stalled_threads(
    threads_dir: Path,
    *,
    stale_days: int = 14,
    now: datetime | None = None,
    all_topics: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Find non-session-context, non-closed threads with no recent activity.

    Args:
        threads_dir: Threads repository root.
        stale_days: Days of inactivity threshold.
        now: Override current time for testing.
        all_topics: Pre-computed full topic list. When provided, skips the
            internal ``list_thread_topics`` call.

    Returns:
        List of dicts with keys: topic, days_stale, last_entry_at.
        Sorted by days_stale descending.
    """
    graph_dir = get_graph_dir(threads_dir)
    current_time = _now_utc(now)
    cutoff = current_time - timedelta(days=stale_days)

    topics = all_topics if all_topics is not None else storage.list_thread_topics(graph_dir)
    stalled: list[dict[str, Any]] = []

    for topic in topics:
        # Skip session-context threads (contributor logs, not work threads)
        if topic.startswith("session-context-"):
            continue

        try:
            meta = load_thread_meta(graph_dir, topic) or {}
            status = (meta.get("status") or "").upper()
            if status in ("CLOSED", "RESOLVED", "MERGED", "DONE"):
                continue

            # Use last_updated from meta as proxy for last activity
            last_updated_str = meta.get("last_updated", "") or ""
            last_updated = _parse_timestamp(last_updated_str)

            if last_updated is None:
                continue

            if last_updated < cutoff:
                days_stale = (current_time - last_updated).days
                stalled.append({
                    "topic": topic,
                    "days_stale": days_stale,
                    "last_entry_at": _to_iso(last_updated),
                })
        except Exception as exc:
            logger.debug(
                "pulse_snapshot_lib: error checking staleness for %s: %s", topic, exc
            )
            continue

    stalled.sort(key=lambda x: x["days_stale"], reverse=True)
    return stalled


# ---------------------------------------------------------------------------
# Risk surface tags
# ---------------------------------------------------------------------------


def compute_risk_tags(contributors: dict[str, Any]) -> list[str]:
    """Derive risk surface tags from contributor observation counts.

    Heuristic: if total (problem + risk) observations across all contributors
    exceeds 3, collect the focus_areas of those contributors as risk tags.

    Args:
        contributors: Dict of contributor data (from scan_session_threads).

    Returns:
        Deduplicated sorted list of risk tag strings.
    """
    problem_risk_total = 0
    focus_areas: list[str] = []

    for contrib_data in contributors.values():
        counts = contrib_data.get("observation_counts", {})
        contrib_total = counts.get("problem", 0) + counts.get("risk", 0)
        if contrib_total > 0:
            problem_risk_total += contrib_total
            focus_areas.extend(contrib_data.get("focus_areas", []))

    if problem_risk_total > 3:
        return sorted(set(focus_areas))
    return []


# ---------------------------------------------------------------------------
# Analysis freshness
# ---------------------------------------------------------------------------


def check_analysis_freshness(
    reports_dir: Path,
    *,
    freshness_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check age of the most recent *-usage-analysis.md report.

    Scans the ``usage-analysis/`` subdirectory of ``reports_dir``, which is
    where the ``watercooler-analysis`` skill writes its output files.

    Args:
        reports_dir: Parent reports directory (e.g. ``dev_docs/reports``).
            Reports are looked up in ``reports_dir / "usage-analysis"``.
        freshness_days: Days before report is considered stale.
        now: Override current time for testing.

    Returns:
        Dict with keys: path (str|None), age_days (float|None), is_fresh (bool).
    """
    current_time = _now_utc(now)

    scan_dir = reports_dir / "usage-analysis"
    if not scan_dir.exists():
        return {"path": None, "age_days": None, "is_fresh": False}

    candidates: list[tuple[float, Path]] = []
    try:
        for p in scan_dir.glob("*-usage-analysis.md"):
            try:
                mtime = p.stat().st_mtime
                candidates.append((mtime, p))
            except OSError:
                continue
    except OSError:
        return {"path": None, "age_days": None, "is_fresh": False}

    if not candidates:
        return {"path": None, "age_days": None, "is_fresh": False}

    candidates.sort(reverse=True)
    newest_mtime, newest_path = candidates[0]

    newest_dt = datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
    age_days = (current_time - newest_dt).total_seconds() / 86400.0
    is_fresh = age_days <= freshness_days

    return {
        "path": str(newest_path),
        "age_days": round(age_days, 2),
        "is_fresh": is_fresh,
    }


# ---------------------------------------------------------------------------
# Queue count
# ---------------------------------------------------------------------------


def count_queue_pending() -> int:
    """Count non-empty lines in the pulse_queue.jsonl file without draining.

    Returns 0 if the file does not exist or cannot be read.
    """
    if not QUEUE_PATH.exists():
        return 0
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Full snapshot builder
# ---------------------------------------------------------------------------


def build_snapshot(
    threads_dir: Path,
    *,
    repo_key: str,
    code_path: str,
    window_days: int = 7,
    code_branch: str = "*",
    stale_days: int = 14,
    analysis_freshness_days: int = 7,
    max_threads: int = 50,
    now: datetime | None = None,
    session_topics: list[str] | None = None,
    all_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Build the complete v1.0 pulse snapshot dict.

    Orchestrates all scan helpers. The result is suitable for storage in
    ``checkpoint.extras["projects"][repo_key]["pulse_snapshot"]``.

    All datetime values are pre-formatted to ISO 8601 strings so the result
    is JSON-serializable without further conversion.

    Args:
        threads_dir: Threads repository root.
        repo_key: Deterministic repo identifier (from ``derive_repo_key``).
        code_path: Original code_path string (stored for consumer context).
        window_days: Session look-back window in days.
        code_branch: Branch filter; ``"*"`` = all branches.
        stale_days: Inactivity threshold for stalled-thread detection.
        analysis_freshness_days: Age threshold for analysis report freshness.
        max_threads: Maximum session threads to scan.
        now: Override current time for testing.
        session_topics: Pre-computed session-context-* topic list. Passed
            through to ``scan_session_threads`` to avoid re-enumeration.
        all_topics: Pre-computed full topic list. Passed through to
            ``compute_stalled_threads`` to avoid re-enumeration.

    Returns:
        Complete snapshot dict matching the v1.0 schema.
    """
    current_time = _now_utc(now)

    # Scan session threads
    session_data = scan_session_threads(
        threads_dir,
        window_days=window_days,
        code_branch=code_branch,
        max_threads=max_threads,
        now=current_time,
        session_topics=session_topics,
    )

    # Stalled threads
    stalled = compute_stalled_threads(
        threads_dir,
        stale_days=stale_days,
        now=current_time,
        all_topics=all_topics,
    )

    # Risk surface tags from contributor observations
    risk_tags = compute_risk_tags(session_data["contributors"])

    # Analysis freshness — look in dev_docs/reports/ relative to code_path
    reports_dir = Path(code_path) / "dev_docs" / "reports"
    analysis_info = check_analysis_freshness(
        reports_dir,
        freshness_days=analysis_freshness_days,
        now=current_time,
    )

    return {
        "snapshot_version": "1.0",
        "generated_at": _to_iso(current_time),
        "repo_key": repo_key,
        "window_days": window_days,
        "code_branch": code_branch,
        "corpus": session_data["corpus"],
        "contributors": session_data["contributors"],
        "queue_pending": session_data["queue_pending"],
        "stalled_threads": stalled,
        "risk_surface_tags": risk_tags,
        "analysis": {
            "latest_report_path": analysis_info["path"],
            "latest_report_age_days": analysis_info["age_days"],
            "is_fresh": analysis_info["is_fresh"],
        },
    }


# ---------------------------------------------------------------------------
# State signal computation (promoted from PulseSnapshotDaemon for shared use)
# ---------------------------------------------------------------------------


def compute_state_signals(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compute stable/changing classification from v1-native snapshot fields.

    Uses the I Ching-inspired framework: decisions and insights are stable
    elements; problems, risks, and explorations are changing elements.
    See dev_docs/brainstorms/2026-03-19-project-pulse-skill-brainstorm.md
    "Conceptual Foundations" for the full scaffold-to-measurable mapping.

    Returns Level 2A pre-computation — deterministic inputs from v1 snapshot
    fields only.  No ternary dimension estimation (Level 2B is a follow-up).

    Args:
        snapshot: A pulse snapshot dict with ``contributors``, ``stalled_threads``,
            and ``corpus`` keys.

    Returns:
        Dict with ``per_contributor`` and ``repo_level`` sub-dicts.
    """
    per_contributor: dict[str, Any] = {}
    for name, contrib in snapshot.get("contributors", {}).items():
        obs = contrib.get("observation_counts", {})
        stable = obs.get("decision", 0) + obs.get("insight", 0)
        changing = (
            obs.get("problem", 0) + obs.get("risk", 0) + obs.get("exploration", 0)
        )
        total = stable + changing
        per_contributor[name] = {
            "stable_count": stable,
            "changing_count": changing,
            "volatility_ratio": round(changing / total, 2) if total > 0 else None,
            "open_loop_count": len(contrib.get("open_loops", [])),
            "exploration_count": obs.get("exploration", 0),
        }

    all_focus: dict[str, list[str]] = {}
    all_open_loops: dict[str, list[str]] = {}
    for name, contrib in snapshot.get("contributors", {}).items():
        for fa in contrib.get("focus_areas", []):
            all_focus.setdefault(fa, []).append(name)
        for ol in contrib.get("open_loops", []):
            all_open_loops.setdefault(ol, []).append(name)

    return {
        "per_contributor": per_contributor,
        "repo_level": {
            "stalled_thread_count": len(snapshot.get("stalled_threads", [])),
            "sessions_in_window": snapshot.get("corpus", {}).get(
                "sessions_in_window", 0
            ),
            "total_contributors": len(snapshot.get("contributors", {})),
            "focus_area_overlap": [
                fa for fa, names in all_focus.items() if len(names) > 1
            ],
            "shared_open_loops": [
                ol for ol, names in all_open_loops.items() if len(names) > 1
            ],
        },
    }


# ---------------------------------------------------------------------------
# Phase 5a — Convergence telemetry
# ---------------------------------------------------------------------------

# Pattern for tradeoff language detection (case-insensitive)
_TRADEOFF_RE = re.compile(
    r"\bvs\.?\b|\bversus\b|tradeoff between|trade-off between|either .{1,60} or\b",
    re.IGNORECASE,
)

# Minimum entries in a thread before computing signals
_MIN_ENTRIES_FOR_CONVERGENCE = 10

# Fraction of entries treated as "recent" vs "baseline" for novelty computation
_RECENT_FRACTION = 0.3


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0.0 on error."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _centroid(vectors: list[list[float]]) -> list[float] | None:
    """Compute the mean vector of a list of equal-length vectors."""
    if not vectors:
        return None
    if len({len(v) for v in vectors}) > 1:
        return None
    dim = len(vectors[0])
    result = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            result[i] += x
    n = len(vectors)
    return [x / n for x in result]


def _load_topic_embeddings(
    graph_dir: Path, topic: str
) -> dict[str, list[float]]:
    """Return {entry_id: embedding} from the per-topic search-index shard."""
    result: dict[str, list[float]] = {}
    try:
        for rec in storage.load_search_index(graph_dir, topic=topic):
            eid = rec.get("entry_id", "")
            emb = rec.get("embedding")
            if eid and emb and isinstance(emb, list):
                result[eid] = emb
    except Exception as exc:
        logger.debug("pulse_snapshot_lib: failed to load embeddings for %r: %s", topic, exc)
    return result


def _semantic_novelty_decline(
    entries: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
) -> float | None:
    """Similarity of recent embeddings to baseline centroid (higher = more similar = less novel).

    Returns a value in [0, 1] or None when embeddings are insufficient.
    """
    n = len(entries)
    if n < _MIN_ENTRIES_FOR_CONVERGENCE:
        return None

    split = max(1, int(n * (1 - _RECENT_FRACTION)))
    baseline_ids = {e["entry_id"] for e in entries[:split] if "entry_id" in e}
    recent_ids = {e["entry_id"] for e in entries[split:] if "entry_id" in e}

    baseline_vecs = [embeddings[eid] for eid in baseline_ids if eid in embeddings]
    recent_vecs = [embeddings[eid] for eid in recent_ids if eid in embeddings]

    if not baseline_vecs or not recent_vecs:
        return None

    centroid = _centroid(baseline_vecs)
    if centroid is None:
        return None

    sims = [_cosine_similarity(v, centroid) for v in recent_vecs]
    return round(sum(sims) / len(sims), 4)


def _concern_cluster_recurrence(
    entries: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
    *,
    similarity_threshold: float = 0.85,
) -> int:
    """Count recurring critic-entry embedding clusters (Phase 5a rough proxy).

    Groups critic-role entries by cosine similarity; returns the number of
    groups with ≥ 2 members, indicating a concern that has re-surfaced.
    """
    critic_vecs: list[list[float]] = []
    for e in entries:
        if e.get("role") == "critic":
            eid = e.get("entry_id", "")
            if eid in embeddings:
                critic_vecs.append(embeddings[eid])

    if len(critic_vecs) < 2:
        return 0

    # Simple greedy clustering: assign each vec to the first cluster whose
    # centroid is within threshold, otherwise start a new cluster.
    clusters: list[list[list[float]]] = []
    for vec in critic_vecs:
        assigned = False
        for cluster in clusters:
            c = _centroid(cluster)
            if c is not None and _cosine_similarity(vec, c) >= similarity_threshold:
                cluster.append(vec)
                assigned = True
                break
        if not assigned:
            clusters.append([vec])

    return sum(1 for c in clusters if len(c) >= 2)


_STOP_WORDS = frozenset(
    {"a", "an", "the", "and", "or", "but", "in", "of", "for", "to", "with",
     "is", "it", "this", "that", "we", "our", "use", "can", "be", "are"}
)


def _clean_tokens(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", "", text.lower()).split()


def _extract_tradeoff_operands(body: str, m: re.Match) -> str:
    """Return a normalized key for the tension named by a tradeoff regex match.

    Extraction is connector-aware so that "performance vs. maintainability" in
    any sentence always maps to the same key:

    - vs / versus: one content word immediately left, one immediately right.
    - tradeoff between / trade-off between: first non-stop words following the
      connector (both operands appear in ``after``).
    - either … or: operands are the inner match text and the text after the match.
    """
    connector = m.group(0).lower()
    before = re.sub(r"\s+", " ", body[: m.start()].lower()).strip()
    after = re.sub(r"\s+", " ", body[m.end() :].lower()).strip()

    if connector.startswith("either"):
        # "either <left> or" — left operand is between "either " and the end of the match.
        inner = re.sub(r"^either\s*", "", connector).rstrip()
        left_tokens = [t for t in _clean_tokens(inner) if t not in _STOP_WORDS][:2]
        right_tokens = [t for t in _clean_tokens(after) if t not in _STOP_WORDS][:2]
    elif "between" in connector:
        # "tradeoff between X and Y" — the two operands are the first two
        # non-stop tokens after the connector ("and" is a stop word).
        tokens = [t for t in _clean_tokens(after) if t not in _STOP_WORDS]
        left_tokens = tokens[:1]
        right_tokens = tokens[1:2]
    else:
        # "vs" / "versus" — operands are the single content word immediately adjacent.
        left_tokens = [t for t in reversed(_clean_tokens(before)) if t not in _STOP_WORDS][:1]
        right_tokens = [t for t in _clean_tokens(after) if t not in _STOP_WORDS][:1]

    operands = sorted(left_tokens + right_tokens)
    return " ".join(operands)


def _tradeoff_recurrence(entries: list[dict[str, Any]]) -> int:
    """Count distinct tradeoff tensions that appear in ≥ 2 separate entries.

    Keys each tension by the normalized operands on each side of the connector
    rather than surrounding prose, so "performance vs. maintainability" in two
    different sentences maps to the same recurrence bucket.
    """
    phrase_entry_counts: Counter[str] = Counter()
    for e in entries:
        body = e.get("body", "") or ""
        seen_in_entry: set[str] = set()
        for m in _TRADEOFF_RE.finditer(body):
            operands_key = _extract_tradeoff_operands(body, m)
            if not operands_key:
                continue
            key = hashlib.sha1(operands_key.encode()).hexdigest()[:12]
            if key not in seen_in_entry:
                seen_in_entry.add(key)
                phrase_entry_counts[key] += 1

    return sum(1 for v in phrase_entry_counts.values() if v >= 2)


def _constraint_class_emergence(
    entries: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
    *,
    similarity_threshold: float = 0.80,
) -> int:
    """Count new embedding clusters in recent entries not present in the baseline.

    A positive value indicates new concepts/entities appearing in recent
    entries that were absent from the earlier conversation baseline.
    """
    n = len(entries)
    if n < _MIN_ENTRIES_FOR_CONVERGENCE:
        return 0

    split = max(1, int(n * (1 - _RECENT_FRACTION)))
    baseline_entries = entries[:split]
    recent_entries = entries[split:]

    baseline_vecs = [
        embeddings[e["entry_id"]]
        for e in baseline_entries
        if e.get("entry_id") in embeddings
    ]
    recent_vecs = [
        embeddings[e["entry_id"]]
        for e in recent_entries
        if e.get("entry_id") in embeddings
    ]

    if not baseline_vecs or not recent_vecs:
        return 0

    # Collect recent vectors that are dissimilar to all baseline vectors.
    novel_vecs: list[list[float]] = [
        rv
        for rv in recent_vecs
        if max((_cosine_similarity(rv, bv) for bv in baseline_vecs), default=0.0)
        < similarity_threshold
    ]
    if not novel_vecs:
        return 0

    # Cluster the novel vectors so that five entries about the same new
    # constraint count as one emerging class, not five.
    clusters: list[list[list[float]]] = []
    for vec in novel_vecs:
        assigned = False
        for cluster in clusters:
            c = _centroid(cluster)
            if c is not None and _cosine_similarity(vec, c) >= similarity_threshold:
                cluster.append(vec)
                assigned = True
                break
        if not assigned:
            clusters.append([vec])

    return len(clusters)


def _compute_thread_convergence(
    graph_dir: Path,
    topic: str,
) -> dict[str, Any]:
    """Compute the four convergence signals for a single thread.

    Args:
        graph_dir: Baseline graph directory.
        topic: Thread topic identifier.

    Returns:
        Dict with keys ``semantic_novelty_decline``, ``concern_cluster_recurrence``,
        ``tradeoff_recurrence``, ``constraint_class_emergence``, and
        ``entry_count`` (int). Signals are None when insufficient data.
    """
    entries = list(storage.load_thread_entries(graph_dir, topic))
    entry_count = len(entries)

    if entry_count < _MIN_ENTRIES_FOR_CONVERGENCE:
        return {
            "entry_count": entry_count,
            "semantic_novelty_decline": None,
            "concern_cluster_recurrence": None,
            "tradeoff_recurrence": None,
            "constraint_class_emergence": None,
            "note": f"insufficient_data (need {_MIN_ENTRIES_FOR_CONVERGENCE}, have {entry_count})",
        }

    embeddings = _load_topic_embeddings(graph_dir, topic)

    return {
        "entry_count": entry_count,
        "semantic_novelty_decline": _semantic_novelty_decline(entries, embeddings),
        "concern_cluster_recurrence": _concern_cluster_recurrence(entries, embeddings),
        "tradeoff_recurrence": _tradeoff_recurrence(entries),
        "constraint_class_emergence": _constraint_class_emergence(entries, embeddings),
    }


def _topic_last_entry_timestamp(graph_dir: Path, topic: str) -> str:
    """Return the last entry's ISO timestamp for a topic, or '' if unreadable.

    Reads the final line of entries.jsonl directly to avoid loading the full
    entry list. Uses entry data rather than filesystem mtime so the sort order
    is stable after git clone/pull/checkout operations.
    """
    p = graph_dir / "threads" / topic / "entries.jsonl"
    last_ts = ""
    try:
        with p.open("rb") as f:
            # Scan from end to find the last non-empty line.
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return ""
            # Read up to 4 KB from the end — enough for any single entry line.
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace")
            for raw in reversed(tail.splitlines()):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    last_ts = obj.get("timestamp", "") or ""
                except json.JSONDecodeError:
                    continue
                break
    except OSError:
        pass
    return last_ts


def compute_convergence_signals(
    threads_dir: Path,
    topics: list[str],
    *,
    max_threads: int = 20,
) -> dict[str, dict[str, Any]]:
    """Compute convergence signals for a list of thread topics.

    Processes up to ``max_threads`` topics, prioritising the most recently
    active threads so that the cap always covers the threads that are actually
    moving rather than an arbitrary ``Path.iterdir()`` slice.

    Topics with < 10 entries get a ``note: insufficient_data`` entry rather
    than computed signals.

    Args:
        threads_dir: Threads repository root.
        topics: List of thread topic identifiers to process.
        max_threads: Cap on topics processed (avoids tick overruns for large repos).

    Returns:
        Dict mapping topic → convergence signal dict. Topics that error are
        omitted rather than propagating exceptions.
    """
    graph_dir = storage.get_graph_dir(threads_dir)

    # Sort by last-entry timestamp descending — most-recently-active threads first.
    # Uses entry data rather than filesystem mtime so the order is stable after
    # git clone/pull/checkout operations that reset all file mtimes.
    sorted_topics = sorted(
        topics,
        key=lambda t: _topic_last_entry_timestamp(graph_dir, t),
        reverse=True,
    )

    result: dict[str, dict[str, Any]] = {}
    for topic in sorted_topics[:max_threads]:
        try:
            result[topic] = _compute_thread_convergence(graph_dir, topic)
        except Exception as exc:
            logger.debug(
                "pulse_snapshot_lib: convergence signals failed for %r: %s", topic, exc
            )

    return result
