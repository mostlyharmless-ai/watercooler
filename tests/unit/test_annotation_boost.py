"""Tests for annotation boost scoring in search.py."""

import json
import pytest
from pathlib import Path

from watercooler.baseline_graph.annotations import (
    AnnotationEvent,
    append_annotation,
)
from watercooler.baseline_graph.reader import GraphEntry
from watercooler.baseline_graph.search import (
    SearchQuery,
    compute_annotation_boost,
    search_graph,
)


# ============================================================================
# compute_annotation_boost unit tests
# ============================================================================


class TestComputeAnnotationBoost:
    """Test the annotation boost scoring function."""

    def _make_entry(self, **kwargs):
        defaults = dict(
            entry_id="e1",
            thread_topic="t",
            index=0,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test",
            timestamp="2025-01-01T00:00:00Z",
            summary="test",
        )
        defaults.update(kwargs)
        return GraphEntry(**defaults)

    def test_default_boost_is_one(self):
        entry = self._make_entry()
        assert compute_annotation_boost(entry) == 1.0

    def test_upvotes_increase_boost(self):
        entry = self._make_entry(vote_score=3)
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.3)

    def test_downvotes_decrease_boost(self):
        entry = self._make_entry(vote_score=-3)
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(0.7)

    def test_tags_increase_boost(self):
        entry = self._make_entry(tags=["a", "b", "c"])
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.15)

    def test_flags_are_neutral(self):
        """Flags are attention markers, not negative signals — boost unchanged."""
        entry = self._make_entry(flags=[{"agent": "a", "reason": "r", "timestamp": "t"}])
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.0)

    def test_xrefs_increase_boost(self):
        entry = self._make_entry(xrefs=["e2", "e3"])
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.1)

    def test_pinned_adds_boost(self):
        entry = self._make_entry(pinned=True)
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.2)

    def test_max_clamp(self):
        """Boost should not exceed 2.0."""
        entry = self._make_entry(
            vote_score=10,
            tags=["a", "b", "c", "d", "e"],
            flags=[{"agent": "a", "reason": "r", "timestamp": "t"}] * 5,
            xrefs=["e2", "e3", "e4"],
            pinned=True,
        )
        boost = compute_annotation_boost(entry)
        assert boost == 2.0

    def test_min_clamp(self):
        """Boost should not go below 0.5."""
        entry = self._make_entry(vote_score=-10)
        boost = compute_annotation_boost(entry)
        assert boost == 0.5

    def test_combined_signals(self):
        entry = self._make_entry(
            vote_score=2,     # +0.2
            tags=["a"],       # +0.05
            pinned=True,      # +0.2
        )
        boost = compute_annotation_boost(entry)
        assert boost == pytest.approx(1.45)


# ============================================================================
# Search integration — annotation boost changes ordering
# ============================================================================


def _setup_thread_with_entries(threads_dir, topic, entries):
    """Helper to create a thread with entries in the graph."""
    graph_dir = threads_dir / "graph" / "baseline"
    thread_dir = graph_dir / "threads" / topic
    thread_dir.mkdir(parents=True)

    meta = {
        "id": f"thread:{topic}",
        "type": "thread",
        "topic": topic,
        "title": topic.replace("-", " ").title(),
        "status": "OPEN",
        "ball": "Claude",
        "last_updated": "2025-01-15T10:00:00Z",
        "summary": "",
        "entry_count": len(entries),
    }
    (thread_dir / "meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    lines = []
    for e in entries:
        lines.append(json.dumps(e, separators=(",", ":")))
    (thread_dir / "entries.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    return thread_dir


class TestSearchAnnotationBoost:
    """Test that annotation boost affects search result ordering."""

    def test_pinned_entry_ranks_higher(self, tmp_path):
        """A pinned entry should rank higher than an unpinned one with the same keyword match."""
        entries = [
            {
                "id": "entry:e1",
                "type": "entry",
                "entry_id": "e1",
                "thread_topic": "test-topic",
                "index": 0,
                "agent": "Claude",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Authentication implementation",
                "timestamp": "2025-01-15T09:00:00Z",
                "summary": "Implementing auth",
                "body": "Working on authentication feature",
            },
            {
                "id": "entry:e2",
                "type": "entry",
                "entry_id": "e2",
                "thread_topic": "test-topic",
                "index": 1,
                "agent": "Claude",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Authentication review",
                "timestamp": "2025-01-15T10:00:00Z",
                "summary": "Reviewing auth",
                "body": "Reviewing authentication code",
            },
        ]

        thread_dir = _setup_thread_with_entries(tmp_path, "test-topic", entries)

        # Pin entry e2
        append_annotation(thread_dir, AnnotationEvent(
            id="ann-1",
            target_id="e2",
            target_type="entry",
            kind="pin",
            value="",
            actor="alice",
            timestamp="2025-01-15T11:00:00Z",
        ))

        # Search for "authentication" — both entries match
        query = SearchQuery(
            query="authentication",
            include_threads=False,
            include_entries=True,
            limit=10,
        )
        results = search_graph(tmp_path, query)

        assert results.count >= 2
        # e2 should rank higher due to pin boost
        entry_ids = [r.entry.entry_id for r in results.results if r.entry]
        assert entry_ids[0] == "e2"

    def test_downvoted_entry_ranks_lower(self, tmp_path):
        """A heavily downvoted entry should rank lower."""
        entries = [
            {
                "id": "entry:e1",
                "type": "entry",
                "entry_id": "e1",
                "thread_topic": "test-topic",
                "index": 0,
                "agent": "Claude",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Database migration plan",
                "timestamp": "2025-01-15T09:00:00Z",
                "summary": "Planning migration",
                "body": "Database migration steps",
            },
            {
                "id": "entry:e2",
                "type": "entry",
                "entry_id": "e2",
                "thread_topic": "test-topic",
                "index": 1,
                "agent": "Claude",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Database migration execution",
                "timestamp": "2025-01-15T10:00:00Z",
                "summary": "Executing migration",
                "body": "Database migration completed",
            },
        ]

        thread_dir = _setup_thread_with_entries(tmp_path, "test-topic", entries)

        # Downvote e1 heavily
        for i in range(5):
            append_annotation(thread_dir, AnnotationEvent(
                id=f"ann-{i}",
                target_id="e1",
                target_type="entry",
                kind="reaction",
                value="thumbsdown",
                actor=f"user-{i}",
                timestamp=f"2025-01-15T11:0{i}:00Z",
            ))

        query = SearchQuery(
            query="database migration",
            include_threads=False,
            include_entries=True,
            limit=10,
        )
        results = search_graph(tmp_path, query)

        assert results.count >= 2
        entry_ids = [r.entry.entry_id for r in results.results if r.entry]
        # e2 should rank higher (e1 is downvoted)
        assert entry_ids[0] == "e2"

    def test_no_annotations_no_change(self, tmp_path):
        """Without annotations, boost should be 1.0 (no effect)."""
        entries = [
            {
                "id": "entry:e1",
                "type": "entry",
                "entry_id": "e1",
                "thread_topic": "test-topic",
                "index": 0,
                "agent": "Claude",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Test entry",
                "timestamp": "2025-01-15T09:00:00Z",
                "summary": "test",
                "body": "test body",
            },
        ]

        _setup_thread_with_entries(tmp_path, "test-topic", entries)

        query = SearchQuery(
            query="test",
            include_threads=False,
            include_entries=True,
            limit=10,
        )
        results = search_graph(tmp_path, query)

        assert results.count == 1
        # Score should be the base keyword score (no annotation boost)
        assert results.results[0].score > 0
