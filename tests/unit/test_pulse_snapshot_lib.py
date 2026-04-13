"""Unit tests for pulse_snapshot_lib.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from watercooler.baseline_graph import storage
from watercooler.pulse_snapshot_lib import (
    QUEUE_PATH,
    build_snapshot,
    check_analysis_freshness,
    compute_risk_tags,
    compute_state_signals,
    compute_stalled_threads,
    count_queue_pending,
    derive_repo_key,
    scan_session_threads,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_session_thread(
    threads_dir: Path,
    contributor: str,
    entries: list[dict],
    *,
    status: str = "OPEN",
) -> None:
    """Write a session-context-<contributor> thread to the baseline graph."""
    topic = f"session-context-{contributor}"
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
    now_str = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": f"Session context: {contributor}",
        "status": status,
        "last_updated": now_str,
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entries)


def _write_work_thread(
    threads_dir: Path,
    topic: str,
    *,
    status: str = "OPEN",
    last_updated: str | None = None,
) -> None:
    """Write a non-session work thread."""
    if last_updated is None:
        last_updated = datetime.now(timezone.utc).isoformat()
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": f"Work thread: {topic}",
        "status": status,
        "last_updated": last_updated,
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [])


def _make_theme_entry(
    contributor: str,
    *,
    timestamp: str | None = None,
    observations: list[dict] | None = None,
    technical_focus: list[str] | None = None,
    confidence: float = 0.9,
    entry_id: str = "01ENTRY0001AAAAAAAAAAA",
) -> dict:
    """Build a session-context entry containing an extracted_theme body."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    obs = observations or [
        {"kind": "decision", "text": "chose three-layer architecture"},
        {"kind": "insight", "text": "caching is helpful here"},
    ]
    focus = technical_focus or ["daemon design", "path resolution"]
    body = json.dumps({
        "record_kind": "extracted_theme",
        "branch": "main",
        "technical_focus": focus,
        "session_intent": "Build the snapshot daemon",
        "observations": obs,
        "confidence": confidence,
    })
    return {
        "entry_id": entry_id,
        "timestamp": timestamp,
        "title": f"Session theme for {contributor}",
        "body": body,
        "entry_type": "Note",
        "agent": "Claude Code",
    }


# ---------------------------------------------------------------------------
# derive_repo_key
# ---------------------------------------------------------------------------


def test_derive_repo_key_deterministic(tmp_path):
    key1 = derive_repo_key(tmp_path)
    key2 = derive_repo_key(tmp_path)
    assert key1 == key2
    assert len(key1) == 12
    assert all(c in "0123456789abcdef" for c in key1)


def test_derive_repo_key_different_paths(tmp_path):
    path_a = tmp_path / "repo_a"
    path_a.mkdir()
    path_b = tmp_path / "repo_b"
    path_b.mkdir()
    assert derive_repo_key(path_a) != derive_repo_key(path_b)


# ---------------------------------------------------------------------------
# scan_session_threads
# ---------------------------------------------------------------------------


def test_scan_session_threads_basic(tmp_path):
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry("jay", timestamp=now.isoformat())
    _write_session_thread(tmp_path, "jay", [entry])

    result = scan_session_threads(tmp_path, window_days=7, now=now)

    assert "jay" in result["contributors"]
    jay = result["contributors"]["jay"]
    assert jay["session_count"] == 1
    assert jay["focus_areas"] == ["daemon design", "path resolution"]
    assert result["corpus"]["contributors_active"] == 1
    assert result["corpus"]["sessions_in_window"] == 1


def test_scan_session_threads_excludes_old_entries(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=10)).isoformat()
    entry = _make_theme_entry("jay", timestamp=old_ts)
    _write_session_thread(tmp_path, "jay", [entry])

    result = scan_session_threads(tmp_path, window_days=7, now=now)
    # No sessions in window
    assert result["corpus"]["contributors_active"] == 0


def test_scan_session_threads_open_loop_detection(tmp_path):
    now = datetime.now(timezone.utc)
    observations = [
        {"kind": "problem", "text": "the cache is invalidated too early"},
        {"kind": "problem", "text": "the cache is invalidated too early"},
        {"kind": "insight", "text": "some unrelated insight"},
    ]
    entry = _make_theme_entry("jay", timestamp=now.isoformat(), observations=observations)
    _write_session_thread(tmp_path, "jay", [entry])

    result = scan_session_threads(tmp_path, window_days=7, now=now)
    jay = result["contributors"]["jay"]
    assert len(jay["open_loops"]) == 1
    assert "cache" in jay["open_loops"][0].lower()


def test_scan_session_threads_no_duplicate_open_loops(tmp_path):
    now = datetime.now(timezone.utc)
    observations = [
        {"kind": "problem", "text": "the cache is invalidated too early"},
        {"kind": "problem", "text": "the cache is invalidated too early"},
        {"kind": "problem", "text": "the cache is invalidated too early"},
    ]
    entry = _make_theme_entry("jay", timestamp=now.isoformat(), observations=observations)
    _write_session_thread(tmp_path, "jay", [entry])

    result = scan_session_threads(tmp_path, window_days=7, now=now)
    jay = result["contributors"]["jay"]
    # Multiple occurrences of same loop → only 1 unique entry
    assert len(jay["open_loops"]) == 1


def test_scan_session_threads_branch_filter(tmp_path):
    now = datetime.now(timezone.utc)
    entry = {
        "entry_id": "01ENTRY0001",
        "timestamp": now.isoformat(),
        "title": "Session theme",
        "body": json.dumps({
            "record_kind": "extracted_theme",
            "branch": "feature/other",
            "technical_focus": ["auth"],
            "session_intent": "work",
            "observations": [],
            "confidence": 0.8,
        }),
        "code_branch": "feature/other",
    }
    _write_session_thread(tmp_path, "jay", [entry])

    # Filter for main branch → no sessions found
    result = scan_session_threads(tmp_path, window_days=7, code_branch="main", now=now)
    assert result["corpus"]["contributors_active"] == 0


def test_scan_session_threads_skips_non_session_topics(tmp_path):
    now = datetime.now(timezone.utc)
    # Write a work thread that happens to have entries
    graph_dir = storage.ensure_graph_dir(tmp_path)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, "some-work-thread")
    storage.atomic_write_json(thread_dir / "meta.json", {
        "id": "thread:some-work-thread", "topic": "some-work-thread",
        "title": "Work", "status": "OPEN", "last_updated": now.isoformat(),
    })
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [
        _make_theme_entry("agent", timestamp=now.isoformat()),
    ])

    result = scan_session_threads(tmp_path, window_days=7, now=now)
    assert result["corpus"]["contributors_active"] == 0
    assert result["corpus"]["session_context_threads"] == 0


def test_scan_session_threads_empty(tmp_path):
    result = scan_session_threads(tmp_path, window_days=7)
    assert result["contributors"] == {}
    assert result["corpus"]["contributors_active"] == 0


def test_scan_session_threads_malformed_entry_isolated(tmp_path):
    now = datetime.now(timezone.utc)
    good_entry = _make_theme_entry("jay", timestamp=now.isoformat())
    bad_entry = {
        "entry_id": None,
        "timestamp": None,
        "title": None,
        "body": None,
    }
    _write_session_thread(tmp_path, "jay", [good_entry, bad_entry])

    # Should not raise; good entry still produces contributor
    result = scan_session_threads(tmp_path, window_days=7, now=now)
    assert "jay" in result["contributors"]


def test_scan_session_threads_max_threads_cap(tmp_path):
    now = datetime.now(timezone.utc)
    for i in range(5):
        entry = _make_theme_entry(
            f"user{i}",
            timestamp=now.isoformat(),
            entry_id=f"01ENTRY{i:04d}AAAAAAAAAAA",
        )
        _write_session_thread(tmp_path, f"user{i}", [entry])

    result = scan_session_threads(tmp_path, window_days=7, max_threads=3, now=now)
    assert result["corpus"]["session_context_threads"] <= 3


def test_scan_session_threads_naive_timestamp_not_dropped(tmp_path):
    """Entries with timezone-naive timestamps must not be silently excluded."""
    now = datetime.now(timezone.utc)
    # Naive ISO timestamp — no Z, no offset
    naive_ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    entry = _make_theme_entry("alice", timestamp=naive_ts)
    _write_session_thread(tmp_path, "alice", [entry])

    result = scan_session_threads(tmp_path, window_days=7, now=now)
    # Entry should be included, not silently dropped
    assert result["corpus"]["sessions_in_window"] == 1
    assert "alice" in result["contributors"]


# ---------------------------------------------------------------------------
# compute_stalled_threads
# ---------------------------------------------------------------------------


def test_compute_stalled_threads_detects_stale(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=20)).isoformat()
    _write_work_thread(tmp_path, "old-thread", last_updated=old_ts)

    result = compute_stalled_threads(tmp_path, stale_days=14, now=now)
    assert any(r["topic"] == "old-thread" for r in result)
    assert result[0]["days_stale"] >= 20


def test_compute_stalled_threads_skips_fresh(tmp_path):
    now = datetime.now(timezone.utc)
    fresh_ts = (now - timedelta(days=3)).isoformat()
    _write_work_thread(tmp_path, "fresh-thread", last_updated=fresh_ts)

    result = compute_stalled_threads(tmp_path, stale_days=14, now=now)
    assert not any(r["topic"] == "fresh-thread" for r in result)


def test_compute_stalled_threads_skips_closed(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).isoformat()
    _write_work_thread(tmp_path, "closed-thread", status="CLOSED", last_updated=old_ts)

    result = compute_stalled_threads(tmp_path, stale_days=14, now=now)
    assert not any(r["topic"] == "closed-thread" for r in result)


def test_compute_stalled_threads_skips_session_context(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).isoformat()
    _write_work_thread(
        tmp_path, "session-context-jay", last_updated=old_ts
    )

    result = compute_stalled_threads(tmp_path, stale_days=14, now=now)
    assert not any(r["topic"] == "session-context-jay" for r in result)


def test_compute_stalled_threads_sorted_by_staleness(tmp_path):
    now = datetime.now(timezone.utc)
    _write_work_thread(tmp_path, "stale-5", last_updated=(now - timedelta(days=5)).isoformat())
    _write_work_thread(tmp_path, "stale-30", last_updated=(now - timedelta(days=30)).isoformat())

    result = compute_stalled_threads(tmp_path, stale_days=4, now=now)
    assert result[0]["days_stale"] >= result[1]["days_stale"]


# ---------------------------------------------------------------------------
# compute_risk_tags
# ---------------------------------------------------------------------------


def test_compute_risk_tags_above_threshold():
    contributors = {
        "jay": {
            "focus_areas": ["security", "performance"],
            "observation_counts": {"problem": 2, "risk": 2},
        },
    }
    tags = compute_risk_tags(contributors)
    assert "security" in tags
    assert "performance" in tags


def test_compute_risk_tags_below_threshold():
    contributors = {
        "jay": {
            "focus_areas": ["security"],
            "observation_counts": {"problem": 1, "risk": 1},
        },
    }
    tags = compute_risk_tags(contributors)
    assert tags == []


def test_compute_risk_tags_empty():
    assert compute_risk_tags({}) == []


def test_compute_risk_tags_deduplicates():
    contributors = {
        "jay": {
            "focus_areas": ["security", "security", "performance"],
            "observation_counts": {"problem": 4},
        },
    }
    tags = compute_risk_tags(contributors)
    assert tags.count("security") == 1


# ---------------------------------------------------------------------------
# check_analysis_freshness
# ---------------------------------------------------------------------------


def test_check_analysis_freshness_fresh(tmp_path):
    reports_dir = tmp_path / "reports"
    # Mirror real skill output: reports live in reports_dir/usage-analysis/
    analysis_dir = reports_dir / "usage-analysis"
    analysis_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)

    report = analysis_dir / "2026-04-02-usage-analysis.md"
    report.write_text("# Analysis\n")

    result = check_analysis_freshness(reports_dir, freshness_days=7, now=now)
    assert result["is_fresh"] is True
    assert result["age_days"] is not None
    assert result["age_days"] < 1.0  # just written
    assert result["path"] is not None


def test_check_analysis_freshness_stale(tmp_path):
    reports_dir = tmp_path / "reports"
    analysis_dir = reports_dir / "usage-analysis"
    analysis_dir.mkdir(parents=True)

    report = analysis_dir / "2026-01-01-usage-analysis.md"
    report.write_text("# Old Analysis\n")

    # Simulate 90-day-old mtime
    import time
    old_mtime = time.time() - 90 * 86400
    import os
    os.utime(report, (old_mtime, old_mtime))

    now = datetime.now(timezone.utc)
    result = check_analysis_freshness(reports_dir, freshness_days=7, now=now)
    assert result["is_fresh"] is False
    assert result["age_days"] is not None
    assert result["age_days"] > 7


def test_check_analysis_freshness_missing_dir(tmp_path):
    result = check_analysis_freshness(tmp_path / "nonexistent")
    assert result["is_fresh"] is False
    assert result["path"] is None
    assert result["age_days"] is None


def test_check_analysis_freshness_no_reports(tmp_path):
    reports_dir = tmp_path / "reports"
    # Create usage-analysis subdir but with no matching files
    analysis_dir = reports_dir / "usage-analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "other-file.md").write_text("not a report")
    result = check_analysis_freshness(reports_dir)
    assert result["is_fresh"] is False
    assert result["path"] is None


# ---------------------------------------------------------------------------
# count_queue_pending
# ---------------------------------------------------------------------------


def test_count_queue_pending_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "watercooler.pulse_snapshot_lib.QUEUE_PATH",
        tmp_path / "nonexistent.jsonl",
    )
    assert count_queue_pending() == 0


def test_count_queue_pending_counts_lines(tmp_path, monkeypatch):
    q = tmp_path / "pulse_queue.jsonl"
    q.write_text('{"a":1}\n{"b":2}\n\n{"c":3}\n')
    monkeypatch.setattr("watercooler.pulse_snapshot_lib.QUEUE_PATH", q)
    assert count_queue_pending() == 3


def test_count_queue_pending_empty_file(tmp_path, monkeypatch):
    q = tmp_path / "pulse_queue.jsonl"
    q.write_text("\n\n")
    monkeypatch.setattr("watercooler.pulse_snapshot_lib.QUEUE_PATH", q)
    assert count_queue_pending() == 0


# ---------------------------------------------------------------------------
# build_snapshot (integration test)
# ---------------------------------------------------------------------------


def test_build_snapshot_structure(tmp_path):
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry("jay", timestamp=now.isoformat())
    _write_session_thread(tmp_path, "jay", [entry])
    _write_work_thread(
        tmp_path, "old-work",
        last_updated=(now - timedelta(days=20)).isoformat(),
    )

    snapshot = build_snapshot(
        tmp_path,
        repo_key="abc123def456",
        code_path=str(tmp_path),
        window_days=7,
        stale_days=14,
        now=now,
    )

    assert snapshot["snapshot_version"] == "1.0"
    assert snapshot["repo_key"] == "abc123def456"
    assert snapshot["window_days"] == 7
    assert "contributors" in snapshot
    assert "jay" in snapshot["contributors"]
    assert "corpus" in snapshot
    assert "stalled_threads" in snapshot
    assert len(snapshot["stalled_threads"]) >= 1
    assert "risk_surface_tags" in snapshot
    assert "analysis" in snapshot
    assert "generated_at" in snapshot


def test_build_snapshot_is_json_serializable(tmp_path):
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry("jay", timestamp=now.isoformat())
    _write_session_thread(tmp_path, "jay", [entry])

    snapshot = build_snapshot(
        tmp_path,
        repo_key="testkey12345",
        code_path=str(tmp_path),
        now=now,
    )

    # Should not raise
    serialized = json.dumps(snapshot)
    deserialized = json.loads(serialized)
    assert deserialized["snapshot_version"] == "1.0"


# ---------------------------------------------------------------------------
# #490 — explicit topic passthrough (no double enumeration)
# ---------------------------------------------------------------------------


def test_scan_session_threads_with_explicit_topics(tmp_path, monkeypatch):
    """When session_topics is provided, list_thread_topics is not called."""
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry("alice", timestamp=now.isoformat())
    _write_session_thread(tmp_path, "alice", [entry])

    call_count = {"n": 0}
    original = storage.list_thread_topics

    def counting_list(graph_dir):
        call_count["n"] += 1
        return original(graph_dir)

    from watercooler.baseline_graph import storage as _storage
    monkeypatch.setattr(_storage, "list_thread_topics", counting_list)

    result = scan_session_threads(
        tmp_path,
        now=now,
        session_topics=["session-context-alice"],
    )

    assert call_count["n"] == 0, "list_thread_topics should not be called when session_topics is provided"
    assert "alice" in result["contributors"]


def test_build_snapshot_passes_topics_through(tmp_path, monkeypatch):
    """build_snapshot passes session_topics and all_topics to sub-functions."""
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry("jay", timestamp=now.isoformat())
    _write_session_thread(tmp_path, "jay", [entry])

    call_count = {"n": 0}
    original = storage.list_thread_topics

    def counting_list(graph_dir):
        call_count["n"] += 1
        return original(graph_dir)

    from watercooler.baseline_graph import storage as _storage
    monkeypatch.setattr(_storage, "list_thread_topics", counting_list)

    # Pre-compute topics before build_snapshot call
    from watercooler.baseline_graph.storage import get_graph_dir
    graph_dir = get_graph_dir(tmp_path)
    all_topics = original(graph_dir)
    session_topics = [t for t in all_topics if t.startswith("session-context-")]

    call_count["n"] = 0  # reset counter after manual enumeration

    snapshot = build_snapshot(
        tmp_path,
        repo_key="abc",
        code_path=str(tmp_path),
        now=now,
        session_topics=session_topics,
        all_topics=all_topics,
    )

    assert call_count["n"] == 0, "build_snapshot should not call list_thread_topics when topics are pre-provided"
    assert "jay" in snapshot["contributors"]


def test_compute_stalled_threads_with_explicit_all_topics(tmp_path, monkeypatch):
    """compute_stalled_threads skips list_thread_topics when all_topics is provided."""
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=20)).isoformat()
    _write_work_thread(tmp_path, "stale-work", last_updated=old_ts)

    call_count = {"n": 0}
    original = storage.list_thread_topics

    def counting_list(graph_dir):
        call_count["n"] += 1
        return original(graph_dir)

    from watercooler.baseline_graph import storage as _storage
    monkeypatch.setattr(_storage, "list_thread_topics", counting_list)

    from watercooler.baseline_graph.storage import get_graph_dir
    graph_dir = get_graph_dir(tmp_path)
    all_topics = original(graph_dir)
    call_count["n"] = 0

    stalled = compute_stalled_threads(tmp_path, now=now, all_topics=all_topics)

    assert call_count["n"] == 0
    assert any(s["topic"] == "stale-work" for s in stalled)


# ---------------------------------------------------------------------------
# D4 delivery kinds — aggregation in build_snapshot
# ---------------------------------------------------------------------------


def test_build_snapshot_aggregates_pr_merged(tmp_path):
    """pr_merged observations appear in contributor observation_counts after build_snapshot."""
    now = datetime.now(timezone.utc)
    entry = _make_theme_entry(
        "jay",
        timestamp=now.isoformat(),
        observations=[
            {"kind": "pr_merged", "text": "Merged PR #547 fixing dimension_scores exposure"},
            {"kind": "insight", "text": "checkpoint path was wrong"},
        ],
    )
    _write_session_thread(tmp_path, "jay", [entry])

    snapshot = build_snapshot(
        tmp_path,
        repo_key="abc123def456",
        code_path=str(tmp_path),
        window_days=7,
        now=now,
    )

    jay = snapshot["contributors"].get("jay", {})
    obs_counts = jay.get("observation_counts", {})
    assert obs_counts.get("pr_merged", 0) >= 1, (
        "pr_merged observation should appear in contributor observation_counts"
    )


# ---------------------------------------------------------------------------
# compute_state_signals (promoted from PulseSnapshotDaemon)
# ---------------------------------------------------------------------------


class TestComputeStateSignals:
    """Tests for the promoted compute_state_signals function."""

    def test_basic_stable_changing(self):
        snapshot = {
            "contributors": {
                "alice": {
                    "observation_counts": {
                        "decision": 3, "insight": 2,
                        "problem": 1, "risk": 1, "exploration": 0,
                    },
                    "focus_areas": [],
                    "open_loops": [],
                },
            },
            "stalled_threads": [],
            "corpus": {"sessions_in_window": 1},
        }
        signals = compute_state_signals(snapshot)
        alice = signals["per_contributor"]["alice"]
        assert alice["stable_count"] == 5
        assert alice["changing_count"] == 2
        assert alice["volatility_ratio"] == 0.29  # 2/7

    def test_empty_snapshot(self):
        signals = compute_state_signals({})
        assert signals["per_contributor"] == {}
        assert signals["repo_level"]["stalled_thread_count"] == 0

    def test_focus_area_overlap(self):
        snapshot = {
            "contributors": {
                "alice": {
                    "observation_counts": {},
                    "focus_areas": ["auth", "db"],
                    "open_loops": [],
                },
                "bob": {
                    "observation_counts": {},
                    "focus_areas": ["auth"],
                    "open_loops": [],
                },
            },
            "stalled_threads": [],
            "corpus": {},
        }
        signals = compute_state_signals(snapshot)
        assert "auth" in signals["repo_level"]["focus_area_overlap"]
        assert "db" not in signals["repo_level"]["focus_area_overlap"]

    def test_zero_obs_volatility_none(self):
        snapshot = {
            "contributors": {
                "nobody": {
                    "observation_counts": {},
                    "focus_areas": [],
                    "open_loops": [],
                },
            },
            "stalled_threads": [],
            "corpus": {},
        }
        signals = compute_state_signals(snapshot)
        assert signals["per_contributor"]["nobody"]["volatility_ratio"] is None
