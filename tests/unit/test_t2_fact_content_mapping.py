"""Tests for T2 fact content mapping (edge.fact → content field).

Validates that search_memory_facts(), search_facts(), and _query_t2()
correctly surface edge.fact as the content field in results.

Regression tests for: empty fact content in T2 Graphiti search results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler_memory.tier_strategy import (
    Tier,
    TierEvidence,
    _query_t2,
)


# ============================================================================
# Test: search_memory_facts includes name and content fields
# ============================================================================


class TestSearchMemoryFactsResultFormat:
    """Verify search_memory_facts() returns name and content from edge data."""

    @pytest.fixture
    def mock_backend(self):
        """Create a GraphitiBackend with mocked internals."""
        from watercooler_memory.backends.graphiti import GraphitiBackend

        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend.__new__(GraphitiBackend)
            backend.config = MagicMock()
            backend.config.reranker = "cross_encoder"
            backend.entry_episode_index = None
            yield backend

    def test_result_includes_content_from_fact(self, mock_backend):
        """search_memory_facts result dict must include content = edge.fact."""
        mock_edge = MagicMock()
        mock_edge.uuid = "edge-001"
        mock_edge.name = "IMPLEMENTS"
        mock_edge.fact = "Claude implemented OAuth2 with PKCE flow"
        mock_edge.source_node_uuid = "node-src"
        mock_edge.target_node_uuid = "node-tgt"
        mock_edge.valid_at = None
        mock_edge.invalid_at = None
        mock_edge.created_at = None
        mock_edge.group_id = "group-1"

        mock_search_results = MagicMock()
        mock_search_results.edges = [mock_edge]
        mock_search_results.edge_reranker_scores = [0.85]

        async def fake_search_(**kwargs):
            return mock_search_results

        mock_client = MagicMock()
        mock_client.search_ = fake_search_

        with patch.object(mock_backend, "_create_graphiti_client", return_value=mock_client):
            with patch.object(mock_backend, "_get_search_config", return_value=MagicMock()):
                with patch.object(mock_backend, "_sanitize_thread_id", side_effect=lambda x: x):
                    results = mock_backend.search_memory_facts(
                        query="OAuth implementation",
                        group_ids=["group-1"],
                    )

        assert len(results) == 1
        result = results[0]

        # Core assertion: content must be populated from edge.fact
        assert result["content"] == "Claude implemented OAuth2 with PKCE flow"

        # name must be populated from edge.name
        assert result["name"] == "IMPLEMENTS"

        # fact field preserved for backwards compat
        assert result["fact"] == "Claude implemented OAuth2 with PKCE flow"

    def test_result_content_matches_fact_when_empty(self, mock_backend):
        """When edge.fact is empty string, content should also be empty string."""
        mock_edge = MagicMock()
        mock_edge.uuid = "edge-002"
        mock_edge.name = "RELATES_TO"
        mock_edge.fact = ""
        mock_edge.source_node_uuid = "n1"
        mock_edge.target_node_uuid = "n2"
        mock_edge.valid_at = None
        mock_edge.invalid_at = None
        mock_edge.created_at = None
        mock_edge.group_id = "g1"

        mock_search_results = MagicMock()
        mock_search_results.edges = [mock_edge]
        mock_search_results.edge_reranker_scores = [0.5]

        async def fake_search_(**kwargs):
            return mock_search_results

        mock_client = MagicMock()
        mock_client.search_ = fake_search_

        with patch.object(mock_backend, "_create_graphiti_client", return_value=mock_client):
            with patch.object(mock_backend, "_get_search_config", return_value=MagicMock()):
                with patch.object(mock_backend, "_sanitize_thread_id", side_effect=lambda x: x):
                    results = mock_backend.search_memory_facts(
                        query="test", group_ids=["g1"],
                    )

        assert results[0]["content"] == ""
        assert results[0]["name"] == "RELATES_TO"


# ============================================================================
# Test: search_facts wrapper preserves content mapping
# ============================================================================


class TestSearchFactsContentPreservation:
    """Verify search_facts() wrapper does not clobber content from search_memory_facts."""

    @pytest.fixture
    def mock_backend(self):
        """Create a GraphitiBackend with mocked search_memory_facts."""
        from watercooler_memory.backends.graphiti import GraphitiBackend

        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend.__new__(GraphitiBackend)
            backend.config = MagicMock()
            backend.config.reranker = "cross_encoder"
            backend.MIN_SEARCH_RESULTS = 1
            backend.MAX_SEARCH_RESULTS = 50
            backend.entry_episode_index = None
            yield backend

    def test_content_not_clobbered_by_setdefault(self, mock_backend):
        """search_facts() must not overwrite content already set by search_memory_facts."""
        raw_result = {
            "uuid": "edge-001",
            "name": "IMPLEMENTS",
            "fact": "OAuth2 implementation details",
            "content": "OAuth2 implementation details",  # Already set by search_memory_facts
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
            "score": 0.85,
        }

        with patch.object(mock_backend, "search_memory_facts", return_value=[raw_result]):
            results = mock_backend.search_facts("OAuth", max_results=10)

        assert results[0]["content"] == "OAuth2 implementation details"
        assert results[0]["name"] == "IMPLEMENTS"

    def test_setdefault_fallback_maps_fact_to_content(self, mock_backend):
        """If search_memory_facts omits content, setdefault maps fact → content."""
        # Simulate a result without content (e.g., from a different code path)
        raw_result = {
            "uuid": "edge-002",
            "fact": "JWT tokens for session management",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
            "score": 0.7,
        }

        with patch.object(mock_backend, "search_memory_facts", return_value=[raw_result]):
            results = mock_backend.search_facts("JWT", max_results=10)

        # content should be mapped from fact by setdefault
        assert results[0]["content"] == "JWT tokens for session management"

        # name should be derived from fact[:100]
        assert results[0]["name"] == "JWT tokens for session management"

    def test_setdefault_name_truncates_long_facts(self, mock_backend):
        """Name fallback from fact should be truncated to 100 chars."""
        long_fact = "A" * 200
        raw_result = {
            "uuid": "edge-003",
            "fact": long_fact,
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
            "score": 0.6,
        }

        with patch.object(mock_backend, "search_memory_facts", return_value=[raw_result]):
            results = mock_backend.search_facts("test", max_results=10)

        assert results[0]["name"] == "A" * 100

    def test_setdefault_handles_none_fact(self, mock_backend):
        """When fact is None, content and name should be None."""
        raw_result = {
            "uuid": "edge-004",
            "fact": None,
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
            "score": 0.5,
        }

        with patch.object(mock_backend, "search_memory_facts", return_value=[raw_result]):
            results = mock_backend.search_facts("test", max_results=10)

        assert results[0]["content"] is None
        assert results[0]["name"] is None


# ============================================================================
# Test: _query_t2 fact evidence content
# ============================================================================


class TestQueryT2FactEvidenceContent:
    """Verify _query_t2() produces TierEvidence with populated content from facts."""

    def _make_mock_backend(self, fact_results: list[dict[str, Any]]) -> MagicMock:
        """Create a mock backend returning given fact results."""
        backend = MagicMock()
        backend.search_nodes.return_value = []
        backend.search_facts.return_value = fact_results
        return backend

    @patch("watercooler_mcp.memory.get_graphiti_backend")
    @patch("watercooler_mcp.memory.load_graphiti_config")
    def test_fact_content_populated_from_content_field(
        self, mock_load_config, mock_get_backend
    ):
        """When search_facts returns content, _query_t2 uses it."""
        mock_load_config.return_value = MagicMock()
        mock_get_backend.return_value = self._make_mock_backend([{
            "id": "edge-001",
            "content": "OAuth2 with PKCE flow",
            "fact": "OAuth2 with PKCE flow",
            "name": "IMPLEMENTS",
            "score": 0.85,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t2("OAuth", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "fact"]
        assert len(facts) == 1
        assert facts[0].content == "OAuth2 with PKCE flow"
        assert facts[0].name == "IMPLEMENTS"

    @patch("watercooler_mcp.memory.get_graphiti_backend")
    @patch("watercooler_mcp.memory.load_graphiti_config")
    def test_fact_content_falls_back_to_fact_field(
        self, mock_load_config, mock_get_backend
    ):
        """When content is missing/empty, _query_t2 falls back to fact field."""
        mock_load_config.return_value = MagicMock()
        mock_get_backend.return_value = self._make_mock_backend([{
            "id": "edge-002",
            "content": "",  # Empty content
            "fact": "JWT session management",
            "name": "USES",
            "score": 0.7,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t2("JWT", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "fact"]
        assert len(facts) == 1
        assert facts[0].content == "JWT session management"

    @patch("watercooler_mcp.memory.get_graphiti_backend")
    @patch("watercooler_mcp.memory.load_graphiti_config")
    def test_fact_content_falls_back_to_summary(
        self, mock_load_config, mock_get_backend
    ):
        """When both content and fact are missing, falls back to summary."""
        mock_load_config.return_value = MagicMock()
        mock_get_backend.return_value = self._make_mock_backend([{
            "id": "edge-003",
            "content": "",
            "fact": "",
            "summary": "A summary of the edge",
            "name": "RELATES",
            "score": 0.5,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t2("test", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "fact"]
        assert len(facts) == 1
        assert facts[0].content == "A summary of the edge"

    @patch("watercooler_mcp.memory.get_graphiti_backend")
    @patch("watercooler_mcp.memory.load_graphiti_config")
    def test_fact_without_content_key_uses_fact(
        self, mock_load_config, mock_get_backend
    ):
        """When content key is absent entirely, _query_t2 uses fact field."""
        mock_load_config.return_value = MagicMock()
        mock_get_backend.return_value = self._make_mock_backend([{
            "id": "edge-004",
            # No "content" key at all
            "fact": "Direct fact text without content key",
            "name": "HAS",
            "score": 0.6,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t2("test", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "fact"]
        assert len(facts) == 1
        assert facts[0].content == "Direct fact text without content key"
