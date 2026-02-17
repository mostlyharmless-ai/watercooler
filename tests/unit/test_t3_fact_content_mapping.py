"""Tests for T3 fact content fallback chain (content || fact || summary).

Validates that _query_t3() correctly applies the fallback chain when building
TierEvidence from LeanRAG search_facts results.

Mirrors TestQueryT2FactEvidenceContent for the T3 (LeanRAG) path.

Regression tests for: content fallback chain in _query_t3() at tier_strategy.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler_memory.tier_strategy import (
    Tier,
    TierEvidence,
    _query_t3,
)


# ============================================================================
# Test: _query_t3 fact evidence content fallback chain
# ============================================================================


class TestQueryT3FactEvidenceContent:
    """Verify _query_t3() produces TierEvidence with populated content from facts.

    The fallback chain in _query_t3 is:
        fact.get("content") or fact.get("fact") or fact.get("summary", "")

    These tests mirror TestQueryT2FactEvidenceContent from
    test_t2_fact_content_mapping.py but target the T3 (LeanRAG) code path.
    """

    def _make_mock_backend(self, fact_results: list[dict[str, Any]]) -> MagicMock:
        """Create a mock LeanRAG backend returning given fact results."""
        backend = MagicMock()
        backend.search_nodes.return_value = []
        backend.search_facts.return_value = fact_results
        return backend

    @patch("watercooler_mcp.memory.load_leanrag_config")
    @patch("watercooler_memory.backends.leanrag.LeanRAGBackend")
    def test_fact_content_populated_from_content_field(
        self, mock_backend_cls, mock_load_config
    ):
        """When search_facts returns content, _query_t3 uses it."""
        mock_load_config.return_value = MagicMock()
        mock_backend_cls.return_value = self._make_mock_backend([{
            "id": "fact-001",
            "content": "OAuth2 with PKCE flow",
            "fact": "OAuth2 with PKCE flow",
            "name": "IMPLEMENTS",
            "score": 0.85,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t3("OAuth", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "hierarchical_fact"]
        assert len(facts) == 1
        assert facts[0].content == "OAuth2 with PKCE flow"
        assert facts[0].name == "IMPLEMENTS"
        assert facts[0].tier == Tier.T3

    @patch("watercooler_mcp.memory.load_leanrag_config")
    @patch("watercooler_memory.backends.leanrag.LeanRAGBackend")
    def test_fact_content_falls_back_to_fact_field(
        self, mock_backend_cls, mock_load_config
    ):
        """When content is missing/empty, _query_t3 falls back to fact field."""
        mock_load_config.return_value = MagicMock()
        mock_backend_cls.return_value = self._make_mock_backend([{
            "id": "fact-002",
            "content": "",  # Empty content
            "fact": "JWT session management",
            "name": "USES",
            "score": 0.7,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t3("JWT", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "hierarchical_fact"]
        assert len(facts) == 1
        assert facts[0].content == "JWT session management"

    @patch("watercooler_mcp.memory.load_leanrag_config")
    @patch("watercooler_memory.backends.leanrag.LeanRAGBackend")
    def test_fact_content_falls_back_to_summary(
        self, mock_backend_cls, mock_load_config
    ):
        """When both content and fact are missing, falls back to summary."""
        mock_load_config.return_value = MagicMock()
        mock_backend_cls.return_value = self._make_mock_backend([{
            "id": "fact-003",
            "content": "",
            "fact": "",
            "summary": "A summary of the hierarchical fact",
            "name": "RELATES",
            "score": 0.5,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t3("test", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "hierarchical_fact"]
        assert len(facts) == 1
        assert facts[0].content == "A summary of the hierarchical fact"

    @patch("watercooler_mcp.memory.load_leanrag_config")
    @patch("watercooler_memory.backends.leanrag.LeanRAGBackend")
    def test_fact_without_content_key_uses_fact(
        self, mock_backend_cls, mock_load_config
    ):
        """When content key is absent entirely, _query_t3 uses fact field."""
        mock_load_config.return_value = MagicMock()
        mock_backend_cls.return_value = self._make_mock_backend([{
            "id": "fact-004",
            # No "content" key at all
            "fact": "Direct fact text without content key",
            "name": "HAS",
            "score": 0.6,
            "group_id": "g1",
            "source": None,
            "extra": {},
        }])

        evidence = _query_t3("test", code_path=Path("/tmp"))

        facts = [e for e in evidence if e.metadata.get("node_type") == "hierarchical_fact"]
        assert len(facts) == 1
        assert facts[0].content == "Direct fact text without content key"
