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
"""

from __future__ import annotations

import hashlib
import json
import logging
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
