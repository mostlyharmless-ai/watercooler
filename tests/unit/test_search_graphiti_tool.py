"""Tests for _search_graphiti_impl() result field mapping.

Validates that watercooler_search with mode='entries' and Graphiti backend
includes content and name fields in fact results.

Regression tests for: #141 — watercooler_search missing content/name fields.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run_search(mock_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Helper: run _search_graphiti_impl with mocked backend returning given results."""
    from watercooler_mcp.tools.graph import _search_graphiti_impl

    mock_config = MagicMock()
    mock_backend = MagicMock()
    mock_backend.search_facts.return_value = mock_results

    mock_ctx = MagicMock()

    with patch("watercooler_mcp.memory.load_graphiti_config", return_value=mock_config):
        with patch("watercooler_mcp.memory.get_graphiti_backend", return_value=mock_backend):
            raw = asyncio.run(
                _search_graphiti_impl(
                    ctx=mock_ctx,
                    threads_dir=MagicMock(),
                    query="test query",
                    code_path="/tmp",
                    limit=10,
                )
            )

    return json.loads(raw)


class TestSearchGraphitiFactFields:
    """Verify _search_graphiti_impl() includes content and name in fact results."""

    def test_search_graphiti_includes_content_field(self):
        """When backend returns content, it appears in the result."""
        output = _run_search([{
            "uuid": "edge-001",
            "score": 0.85,
            "fact": "OAuth2 with PKCE flow",
            "content": "OAuth2 with PKCE flow details",
            "name": "IMPLEMENTS",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
        }])

        assert output["count"] == 1
        result = output["results"][0]
        assert result["content"] == "OAuth2 with PKCE flow details"

    def test_search_graphiti_content_fallback_to_fact(self):
        """When content key is missing, content falls back to fact value."""
        output = _run_search([{
            "uuid": "edge-002",
            "score": 0.7,
            "fact": "JWT session management",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
        }])

        result = output["results"][0]
        assert result["content"] == "JWT session management"

    def test_search_graphiti_name_field_present(self):
        """Verify name field is included in output."""
        output = _run_search([{
            "uuid": "edge-003",
            "score": 0.9,
            "fact": "Test fact",
            "content": "Test content",
            "name": "RELATES_TO",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
        }])

        result = output["results"][0]
        assert result["name"] == "RELATES_TO"

    def test_search_graphiti_backward_compat(self):
        """Verify fact field still present for backward compatibility."""
        output = _run_search([{
            "uuid": "edge-004",
            "score": 0.6,
            "fact": "Original fact text",
            "content": "Content text",
            "name": "HAS",
            "source_node_uuid": "n1",
            "target_node_uuid": "n2",
        }])

        result = output["results"][0]
        assert result["fact"] == "Original fact text"
        assert result["content"] == "Content text"
        assert result["name"] == "HAS"
        # Original fields still present
        assert result["type"] == "fact"
        assert result["id"] == "edge-004"
        assert result["source_node"] == "n1"
        assert result["target_node"] == "n2"
