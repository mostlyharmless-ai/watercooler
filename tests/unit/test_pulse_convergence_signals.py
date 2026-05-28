"""Unit tests for Phase 5a convergence telemetry signals.

Tests each signal computation function in isolation, plus the public
compute_convergence_signals() orchestrator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from watercooler.pulse_snapshot_lib import (
    _MIN_ENTRIES_FOR_CONVERGENCE,
    _RECENT_FRACTION,
    _centroid,
    _concern_cluster_recurrence,
    _constraint_class_emergence,
    _cosine_similarity,
    _semantic_novelty_decline,
    _tradeoff_recurrence,
    compute_convergence_signals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(dim: int, hot_index: int) -> list[float]:
    """One-hot unit vector of length dim."""
    v = [0.0] * dim
    v[hot_index % dim] = 1.0
    return v


def _entry(entry_id: str, role: str = "planner", body: str = "") -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "role": role,
        "body": body,
        "entry_type": "Note",
        "title": entry_id,
        "timestamp": "2026-05-19T00:00:00Z",
        "agent": "test",
    }


def _make_entries(n: int, *, role: str = "planner", body: str = "") -> list[dict[str, Any]]:
    return [_entry(f"E{i:03d}", role=role, body=body) for i in range(n)]


def _embeddings_for(entries: list[dict[str, Any]], dim: int = 4) -> dict[str, list[float]]:
    """Assign each entry a unique one-hot embedding."""
    return {e["entry_id"]: _unit_vec(dim, i) for i, e in enumerate(entries)}


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_inputs(self):
        assert _cosine_similarity([], []) == 0.0

    def test_mismatched_length(self):
        assert _cosine_similarity([1.0], [1.0, 0.0]) == 0.0

    def test_zero_vector(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# _centroid
# ---------------------------------------------------------------------------


class TestCentroid:
    def test_single_vector(self):
        assert _centroid([[1.0, 2.0]]) == [1.0, 2.0]

    def test_two_vectors(self):
        result = _centroid([[1.0, 0.0], [0.0, 1.0]])
        assert result == pytest.approx([0.5, 0.5])

    def test_empty(self):
        assert _centroid([]) is None


# ---------------------------------------------------------------------------
# _semantic_novelty_decline
# ---------------------------------------------------------------------------


class TestSemanticNoveltyDecline:
    def test_returns_none_below_min_entries(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE - 1)
        emb = _embeddings_for(entries)
        assert _semantic_novelty_decline(entries, emb) is None

    def test_returns_none_when_no_embeddings(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        assert _semantic_novelty_decline(entries, {}) is None

    def test_high_similarity_when_recent_matches_baseline(self):
        # All entries share the same embedding → recent is similar to baseline
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        shared_vec = [1.0, 0.0, 0.0, 0.0]
        emb = {e["entry_id"]: shared_vec for e in entries}
        score = _semantic_novelty_decline(entries, emb)
        assert score is not None
        assert score == pytest.approx(1.0, abs=0.01)

    def test_low_similarity_when_recent_diverges(self):
        # Baseline entries: [1,0,0,0]; recent entries: [0,1,0,0] — orthogonal
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        n = len(entries)
        split = max(1, int(n * (1 - _RECENT_FRACTION)))
        emb = {}
        for i, e in enumerate(entries):
            if i < split:
                emb[e["entry_id"]] = [1.0, 0.0, 0.0, 0.0]
            else:
                emb[e["entry_id"]] = [0.0, 1.0, 0.0, 0.0]
        score = _semantic_novelty_decline(entries, emb)
        assert score is not None
        assert score == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# _concern_cluster_recurrence
# ---------------------------------------------------------------------------


class TestConcernClusterRecurrence:
    def test_zero_without_critic_entries(self):
        entries = _make_entries(5, role="planner")
        emb = _embeddings_for(entries)
        assert _concern_cluster_recurrence(entries, emb) == 0

    def test_zero_with_single_critic_entry(self):
        entries = _make_entries(1, role="critic")
        emb = _embeddings_for(entries)
        assert _concern_cluster_recurrence(entries, emb) == 0

    def test_counts_recurring_cluster(self):
        # Two critic entries with identical embeddings → one recurring cluster
        same_vec = [1.0, 0.0, 0.0, 0.0]
        entries = [_entry("C0", role="critic"), _entry("C1", role="critic")]
        emb = {"C0": same_vec, "C1": same_vec}
        result = _concern_cluster_recurrence(entries, emb, similarity_threshold=0.9)
        assert result == 1

    def test_distinct_embeddings_no_cluster(self):
        # Two critic entries with orthogonal embeddings → no recurring cluster
        entries = [_entry("C0", role="critic"), _entry("C1", role="critic")]
        emb = {"C0": [1.0, 0.0], "C1": [0.0, 1.0]}
        result = _concern_cluster_recurrence(entries, emb, similarity_threshold=0.9)
        assert result == 0


# ---------------------------------------------------------------------------
# _tradeoff_recurrence
# ---------------------------------------------------------------------------


class TestTradeoffRecurrence:
    def test_zero_no_tradeoff_language(self):
        entries = _make_entries(3, body="We picked PostgreSQL because it fits our needs.")
        assert _tradeoff_recurrence(entries) == 0

    def test_detects_vs_in_multiple_entries(self):
        # Same operands in different surrounding prose — operand-based hashing
        # must recognise this as the same tension regardless of context.
        entries = [
            _entry("A", body="We should consider performance vs. maintainability carefully."),
            _entry("B", body="The key concern here is performance vs. maintainability."),
        ]
        assert _tradeoff_recurrence(entries) >= 1

    def test_detects_tradeoff_between(self):
        # Same operands after "tradeoff between" connector in different sentences.
        entries = [
            _entry("A", body="There is a tradeoff between speed and accuracy in this design."),
            _entry("B", body="We keep running into a tradeoff between speed and accuracy."),
        ]
        assert _tradeoff_recurrence(entries) >= 1

    def test_unrelated_vs_phrases_do_not_collide(self):
        # "cost vs latency" and "security vs UX" share the connector but are
        # distinct tensions — should NOT be counted as a recurring tradeoff.
        entries = [
            _entry("A", body="The question is cost vs. latency in our API design."),
            _entry("B", body="We also need to balance security vs. UX in the login flow."),
        ]
        assert _tradeoff_recurrence(entries) == 0

    def test_single_occurrence_not_counted(self):
        # Only one entry mentions the tradeoff → not a recurrence
        entries = [
            _entry("A", body="performance vs. cost is a concern"),
            _entry("B", body="no tradeoff language here"),
        ]
        assert _tradeoff_recurrence(entries) == 0

    def test_either_or_pattern(self):
        # Same "either...or" operands in different surrounding prose.
        entries = [
            _entry("A", body="We can either use Redis or stick with Postgres for this."),
            _entry("B", body="The architecture choice is either use Redis or stick with Postgres."),
        ]
        assert _tradeoff_recurrence(entries) >= 1


# ---------------------------------------------------------------------------
# _constraint_class_emergence
# ---------------------------------------------------------------------------


class TestConstraintClassEmergence:
    def test_zero_below_min_entries(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE - 1)
        emb = _embeddings_for(entries)
        assert _constraint_class_emergence(entries, emb) == 0

    def test_zero_when_no_embeddings(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        assert _constraint_class_emergence(entries, {}) == 0

    def test_zero_when_recent_similar_to_baseline(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        shared_vec = [1.0, 0.0, 0.0, 0.0]
        emb = {e["entry_id"]: shared_vec for e in entries}
        assert _constraint_class_emergence(entries, emb, similarity_threshold=0.8) == 0

    def test_positive_when_recent_novel(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE)
        n = len(entries)
        split = max(1, int(n * (1 - _RECENT_FRACTION)))
        emb = {}
        for i, e in enumerate(entries):
            # Baseline: hot index 0; recent: hot index 31 (orthogonal to baseline)
            emb[e["entry_id"]] = _unit_vec(32, 0 if i < split else 31)
        count = _constraint_class_emergence(entries, emb, similarity_threshold=0.8)
        # All recent entries are orthogonal to baseline AND identical to each other,
        # so they form one cluster → exactly 1 emerging class.
        assert count == 1

    def test_two_distinct_novel_clusters(self):
        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE + 4)
        n = len(entries)
        split = max(1, int(n * (1 - _RECENT_FRACTION)))
        emb = {}
        for i, e in enumerate(entries):
            if i < split:
                emb[e["entry_id"]] = _unit_vec(32, 0)   # baseline cluster
            elif i % 2 == 0:
                emb[e["entry_id"]] = _unit_vec(32, 10)  # novel cluster A
            else:
                emb[e["entry_id"]] = _unit_vec(32, 20)  # novel cluster B
        count = _constraint_class_emergence(entries, emb, similarity_threshold=0.8)
        assert count == 2


# ---------------------------------------------------------------------------
# compute_convergence_signals (orchestrator)
# ---------------------------------------------------------------------------


class TestComputeConvergenceSignals:
    def _write_thread(
        self,
        graph_dir: Path,
        topic: str,
        entries: list[dict[str, Any]],
        embeddings: dict[str, list[float]] | None = None,
    ) -> None:
        """Write a minimal thread + optional search-index shard."""
        from watercooler.baseline_graph import storage as st

        thread_dir = st.ensure_thread_graph_dir(graph_dir, topic)
        st.atomic_write_json(
            thread_dir / "meta.json",
            {
                "id": f"thread:{topic}",
                "topic": topic,
                "title": topic,
                "status": "OPEN",
            },
        )
        st.atomic_write_jsonl(thread_dir / "entries.jsonl", entries)

        if embeddings:
            idx_path = thread_dir / "search-index.jsonl"
            records = [
                {"entry_id": eid, "thread_topic": topic, "embedding": vec}
                for eid, vec in embeddings.items()
            ]
            st.atomic_write_jsonl(idx_path, records)

    def test_empty_topics(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        from watercooler.baseline_graph import storage as st
        st.ensure_graph_dir(threads_dir)
        result = compute_convergence_signals(threads_dir, [])
        assert result == {}

    def test_insufficient_entries_returns_note(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        from watercooler.baseline_graph import storage as st
        graph_dir = st.ensure_graph_dir(threads_dir)

        entries = _make_entries(5)
        self._write_thread(graph_dir, "short-thread", entries)

        result = compute_convergence_signals(threads_dir, ["short-thread"])
        assert "short-thread" in result
        sig = result["short-thread"]
        assert sig["entry_count"] == 5
        assert sig["semantic_novelty_decline"] is None
        assert "insufficient_data" in sig.get("note", "")

    def test_sufficient_entries_returns_signals(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        from watercooler.baseline_graph import storage as st
        graph_dir = st.ensure_graph_dir(threads_dir)

        entries = _make_entries(_MIN_ENTRIES_FOR_CONVERGENCE + 2)
        emb = _embeddings_for(entries, dim=8)
        self._write_thread(graph_dir, "test-thread", entries, embeddings=emb)

        result = compute_convergence_signals(threads_dir, ["test-thread"])
        assert "test-thread" in result
        sig = result["test-thread"]
        assert sig["entry_count"] == _MIN_ENTRIES_FOR_CONVERGENCE + 2
        assert "note" not in sig
        assert "tradeoff_recurrence" in sig
        assert "concern_cluster_recurrence" in sig
        assert "constraint_class_emergence" in sig

    def test_max_threads_cap(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        from watercooler.baseline_graph import storage as st
        graph_dir = st.ensure_graph_dir(threads_dir)

        for i in range(5):
            entries = _make_entries(3)
            self._write_thread(graph_dir, f"topic-{i}", entries)

        result = compute_convergence_signals(threads_dir, [f"topic-{i}" for i in range(5)], max_threads=2)
        assert len(result) == 2

    def test_unknown_topic_omitted(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        from watercooler.baseline_graph import storage as st
        st.ensure_graph_dir(threads_dir)

        result = compute_convergence_signals(threads_dir, ["nonexistent-topic"])
        # Should not raise; missing topic is either omitted or returns empty/error-handled
        assert isinstance(result, dict)
