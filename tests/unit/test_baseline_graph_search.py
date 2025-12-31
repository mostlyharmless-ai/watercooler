"""Tests for baseline_graph/search.py module."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from watercooler.baseline_graph.search import (
    SearchQuery,
    SearchResult,
    SearchResults,
    search_graph,
    search_entries,
    search_threads,
    find_similar_entries,
    search_by_time_range,
    _matches_keyword,
    _matches_time_range,
    _parse_timestamp,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def graph_dir(tmp_path):
    """Create a temporary graph directory with test data."""
    graph_path = tmp_path / "graph" / "baseline"
    graph_path.mkdir(parents=True)
    return graph_path


@pytest.fixture
def threads_dir(tmp_path, graph_dir):
    """Create threads directory with graph subdirectory."""
    return tmp_path


@pytest.fixture
def sample_nodes():
    """Sample node data for testing."""
    return [
        {
            "type": "thread",
            "topic": "feature-auth",
            "title": "Authentication Feature",
            "status": "OPEN",
            "ball": "Claude",
            "last_updated": "2025-01-15T10:00:00Z",
            "summary": "Implementing user authentication",
            "entry_count": 5,
        },
        {
            "type": "thread",
            "topic": "fix-bug-123",
            "title": "Bug Fix for Login",
            "status": "CLOSED",
            "ball": "User",
            "last_updated": "2025-01-10T08:00:00Z",
            "summary": "Fixed login redirect issue",
            "entry_count": 3,
        },
        {
            "type": "entry",
            "entry_id": "01JXYZ123456",
            "thread_topic": "feature-auth",
            "index": 0,
            "agent": "Claude (user)",
            "role": "planner",
            "entry_type": "Plan",
            "title": "Authentication Plan",
            "timestamp": "2025-01-15T09:00:00Z",
            "summary": "Planning JWT-based authentication",
            "body": "We will implement JWT tokens for authentication.",
        },
        {
            "type": "entry",
            "entry_id": "01JXYZ123457",
            "thread_topic": "feature-auth",
            "index": 1,
            "agent": "Claude (user)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Implementation Started",
            "timestamp": "2025-01-15T10:00:00Z",
            "summary": "Started coding the auth module",
            "body": "Created auth.py with JWT token handling.",
        },
        {
            "type": "entry",
            "entry_id": "01JXYZ123458",
            "thread_topic": "fix-bug-123",
            "index": 0,
            "agent": "User (admin)",
            "role": "tester",
            "entry_type": "Note",
            "title": "Bug Report",
            "timestamp": "2025-01-10T08:00:00Z",
            "summary": "Login redirect not working",
            "body": "Users are not redirected after login.",
        },
    ]


@pytest.fixture
def populated_graph(threads_dir, graph_dir, sample_nodes):
    """Create a graph with sample nodes."""
    nodes_file = graph_dir / "nodes.jsonl"
    with open(nodes_file, "w") as f:
        for node in sample_nodes:
            f.write(json.dumps(node) + "\n")
    return threads_dir


# ============================================================================
# Test SearchQuery dataclass
# ============================================================================


class TestSearchQuery:
    """Tests for SearchQuery dataclass."""

    def test_default_values(self):
        """Test default values are set correctly."""
        query = SearchQuery()
        assert query.query is None
        assert query.semantic is False
        assert query.limit == 10
        assert query.combine == "AND"
        assert query.include_threads is True
        assert query.include_entries is True

    def test_custom_values(self):
        """Test custom values are accepted."""
        query = SearchQuery(
            query="auth",
            semantic=True,
            start_time="2025-01-01T00:00:00Z",
            role="implementer",
            limit=5,
            combine="OR",
        )
        assert query.query == "auth"
        assert query.semantic is True
        assert query.start_time == "2025-01-01T00:00:00Z"
        assert query.role == "implementer"
        assert query.limit == 5
        assert query.combine == "OR"


# ============================================================================
# Test SearchResult and SearchResults
# ============================================================================


class TestSearchResults:
    """Tests for SearchResult and SearchResults dataclasses."""

    def test_search_result_defaults(self):
        """Test SearchResult default values."""
        result = SearchResult(node_type="entry", node_id="test-id")
        assert result.score == 1.0
        assert result.matched_fields == []
        assert result.thread is None
        assert result.entry is None

    def test_search_results_count(self):
        """Test SearchResults count property."""
        results = SearchResults()
        assert results.count == 0

        results.results = [
            SearchResult(node_type="thread", node_id="t1"),
            SearchResult(node_type="entry", node_id="e1"),
        ]
        assert results.count == 2

    def test_search_results_threads_and_entries(self):
        """Test threads() and entries() methods."""
        from watercooler.baseline_graph.reader import GraphThread, GraphEntry

        thread = GraphThread(
            topic="test",
            title="Test",
            status="OPEN",
            ball="User",
            last_updated="",
            summary="",
            entry_count=0,
        )
        entry = GraphEntry(
            entry_id="e1",
            thread_topic="test",
            index=0,
            agent="User",
            role="pm",
            entry_type="Note",
            title="Test",
            timestamp="",
            summary="",
        )

        results = SearchResults()
        results.results = [
            SearchResult(node_type="thread", node_id="t1", thread=thread),
            SearchResult(node_type="entry", node_id="e1", entry=entry),
        ]

        assert len(results.threads()) == 1
        assert len(results.entries()) == 1
        assert results.threads()[0].topic == "test"
        assert results.entries()[0].entry_id == "e1"


# ============================================================================
# Test Helper Functions
# ============================================================================


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_parse_timestamp_valid(self):
        """Test parsing valid timestamps."""
        dt = _parse_timestamp("2025-01-15T10:00:00Z")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15

    def test_parse_timestamp_invalid(self):
        """Test parsing invalid timestamps."""
        assert _parse_timestamp("not-a-timestamp") is None
        assert _parse_timestamp("") is None
        assert _parse_timestamp(None) is None

    def test_matches_keyword_found(self):
        """Test keyword matching when found."""
        node = {"title": "Authentication Feature", "body": "JWT tokens"}
        matches, fields = _matches_keyword(node, "auth")
        assert matches is True
        assert "title" in fields

    def test_matches_keyword_not_found(self):
        """Test keyword matching when not found."""
        node = {"title": "Bug Fix", "body": "Some fix"}
        matches, fields = _matches_keyword(node, "auth")
        assert matches is False
        assert fields == []

    def test_matches_keyword_empty_query(self):
        """Test keyword matching with empty query."""
        node = {"title": "Test"}
        matches, fields = _matches_keyword(node, "")
        assert matches is True
        assert fields == []

    def test_matches_keyword_case_insensitive(self):
        """Test keyword matching is case insensitive."""
        node = {"title": "AUTHENTICATION"}
        matches, fields = _matches_keyword(node, "auth")
        assert matches is True

    def test_matches_time_range_within(self):
        """Test time range matching when within range."""
        node = {"timestamp": "2025-01-15T10:00:00Z"}
        assert _matches_time_range(node, "2025-01-01T00:00:00Z", "2025-01-31T23:59:59Z") is True

    def test_matches_time_range_before_start(self):
        """Test time range matching when before start."""
        node = {"timestamp": "2024-12-15T10:00:00Z"}
        assert _matches_time_range(node, "2025-01-01T00:00:00Z", None) is False

    def test_matches_time_range_after_end(self):
        """Test time range matching when after end."""
        node = {"timestamp": "2025-02-15T10:00:00Z"}
        assert _matches_time_range(node, None, "2025-01-31T23:59:59Z") is False

    def test_matches_time_range_no_timestamp(self):
        """Test time range matching when node has no timestamp."""
        node = {"title": "No timestamp"}
        # With no filters, should match
        assert _matches_time_range(node, None, None) is True
        # With filters, should not match
        assert _matches_time_range(node, "2025-01-01T00:00:00Z", None) is False


# ============================================================================
# Test Main Search Function
# ============================================================================


class TestSearchGraph:
    """Tests for search_graph function."""

    def test_search_no_graph(self, threads_dir):
        """Test search when no graph exists."""
        query = SearchQuery(query="test")
        results = search_graph(threads_dir, query)
        assert results.count == 0
        assert results.total_scanned == 0

    def test_search_keyword(self, populated_graph):
        """Test keyword search."""
        query = SearchQuery(query="auth", limit=10)
        results = search_graph(populated_graph, query)
        assert results.count > 0
        # Should find auth-related items
        for r in results.results:
            node_id = r.node_id
            assert "auth" in node_id.lower() or "title" in r.matched_fields or "body" in r.matched_fields

    def test_search_by_status(self, populated_graph):
        """Test filtering by thread status."""
        query = SearchQuery(
            thread_status="OPEN",
            include_threads=True,
            include_entries=False,
        )
        results = search_graph(populated_graph, query)
        assert results.count == 1
        assert results.results[0].thread.status == "OPEN"

    def test_search_by_role(self, populated_graph):
        """Test filtering by entry role."""
        query = SearchQuery(
            role="planner",
            include_threads=False,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        assert results.count == 1
        assert results.results[0].entry.role == "planner"

    def test_search_by_entry_type(self, populated_graph):
        """Test filtering by entry type."""
        query = SearchQuery(
            entry_type="Plan",
            include_threads=False,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        assert results.count == 1
        assert results.results[0].entry.entry_type == "Plan"

    def test_search_by_time_range(self, populated_graph):
        """Test filtering by time range."""
        query = SearchQuery(
            start_time="2025-01-14T00:00:00Z",
            end_time="2025-01-16T00:00:00Z",
            include_threads=True,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        # Should find feature-auth thread and its entries
        assert results.count > 0

    def test_search_by_agent(self, populated_graph):
        """Test filtering by agent name."""
        query = SearchQuery(
            agent="Claude",
            include_threads=False,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        assert results.count == 2  # Two Claude entries
        for r in results.results:
            assert "claude" in r.entry.agent.lower()

    def test_search_limit(self, populated_graph):
        """Test limit parameter."""
        query = SearchQuery(limit=1)
        results = search_graph(populated_graph, query)
        assert results.count <= 1

    def test_search_threads_only(self, populated_graph):
        """Test searching threads only."""
        query = SearchQuery(
            include_threads=True,
            include_entries=False,
        )
        results = search_graph(populated_graph, query)
        assert all(r.node_type == "thread" for r in results.results)

    def test_search_entries_only(self, populated_graph):
        """Test searching entries only."""
        query = SearchQuery(
            include_threads=False,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        assert all(r.node_type == "entry" for r in results.results)

    def test_search_or_combine(self, populated_graph):
        """Test OR combination of filters."""
        # Should find entries that match either role OR entry_type
        query = SearchQuery(
            role="planner",
            entry_type="Note",
            combine="OR",
            include_threads=False,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        # Should find planner Plan entry AND implementer Note entries AND tester Note
        assert results.count >= 1

    def test_search_score_ranking(self, populated_graph):
        """Test that results are ranked by score."""
        query = SearchQuery(query="auth", limit=10)
        results = search_graph(populated_graph, query)
        if results.count >= 2:
            # Scores should be descending
            scores = [r.score for r in results.results]
            assert scores == sorted(scores, reverse=True)


# ============================================================================
# Test Convenience Functions
# ============================================================================


class TestConvenienceFunctions:
    """Tests for convenience search functions."""

    def test_search_entries(self, populated_graph):
        """Test search_entries convenience function."""
        entries = search_entries(populated_graph, query="auth")
        assert len(entries) > 0
        # All results should be GraphEntry objects
        from watercooler.baseline_graph.reader import GraphEntry
        assert all(isinstance(e, GraphEntry) for e in entries)

    def test_search_entries_with_filters(self, populated_graph):
        """Test search_entries with multiple filters."""
        entries = search_entries(
            populated_graph,
            thread_topic="feature-auth",
            role="implementer",
        )
        assert len(entries) == 1
        assert entries[0].role == "implementer"

    def test_search_threads(self, populated_graph):
        """Test search_threads convenience function."""
        threads = search_threads(populated_graph)
        assert len(threads) == 2  # Two threads in test data

    def test_search_threads_by_status(self, populated_graph):
        """Test search_threads with status filter."""
        threads = search_threads(populated_graph, status="CLOSED")
        assert len(threads) == 1
        assert threads[0].status == "CLOSED"

    def test_find_similar_entries_found(self, populated_graph):
        """Test find_similar_entries when similar entries exist."""
        # Find entries similar to the planner entry
        similar = find_similar_entries(populated_graph, "01JXYZ123456", limit=5)
        # Should find the implementer entry in the same thread
        assert len(similar) >= 1

    def test_find_similar_entries_not_found(self, populated_graph):
        """Test find_similar_entries when entry doesn't exist."""
        similar = find_similar_entries(populated_graph, "nonexistent-id", limit=5)
        assert len(similar) == 0

    def test_search_by_time_range_function(self, populated_graph):
        """Test search_by_time_range function."""
        results = search_by_time_range(
            populated_graph,
            start_time="2025-01-01T00:00:00Z",
            end_time="2025-01-31T23:59:59Z",
        )
        assert results.count > 0


# ============================================================================
# Test Edge Cases
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_graph(self, threads_dir, graph_dir):
        """Test search on empty graph file."""
        # Create empty nodes file
        (graph_dir / "nodes.jsonl").touch()

        query = SearchQuery(query="test")
        results = search_graph(threads_dir, query)
        assert results.count == 0

    def test_malformed_json_lines(self, threads_dir, graph_dir):
        """Test search handles malformed JSON gracefully."""
        nodes_file = graph_dir / "nodes.jsonl"
        with open(nodes_file, "w") as f:
            f.write("not valid json\n")
            f.write('{"type": "thread", "topic": "valid"}\n')
            f.write("another bad line\n")

        query = SearchQuery(include_threads=True, include_entries=False)
        results = search_graph(threads_dir, query)
        # Should find the one valid thread
        assert results.count == 1

    def test_search_with_all_empty_filters(self, populated_graph):
        """Test search with no filters returns all nodes."""
        query = SearchQuery()
        results = search_graph(populated_graph, query)
        # Should return all nodes up to limit
        assert results.count > 0

    def test_search_thread_topic_filter(self, populated_graph):
        """Test filtering by thread_topic."""
        query = SearchQuery(
            thread_topic="feature-auth",
            include_threads=True,
            include_entries=True,
        )
        results = search_graph(populated_graph, query)
        # Should find thread + 2 entries
        assert results.count == 3

    def test_case_insensitive_filters(self, populated_graph):
        """Test that filters are case insensitive."""
        # Role filter
        query = SearchQuery(role="PLANNER", include_entries=True, include_threads=False)
        results = search_graph(populated_graph, query)
        assert results.count == 1

        # Entry type filter
        query = SearchQuery(entry_type="note", include_entries=True, include_threads=False)
        results = search_graph(populated_graph, query)
        assert results.count == 2  # Two Note entries

        # Status filter
        query = SearchQuery(thread_status="open", include_threads=True, include_entries=False)
        results = search_graph(populated_graph, query)
        assert results.count == 1
