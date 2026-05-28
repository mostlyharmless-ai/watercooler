"""Tests for hybrid tool routing in graph.py (Step 5)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler_mcp.capabilities import (
    HYBRID_DEFAULT_ROUTES,
    CapabilityProfile,
)
from watercooler_mcp.tool_runtime import ToolRuntime
from watercooler_mcp.tools.graph import _build_hybrid_search_wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime(
    routes: dict | None = None,
    premium_client=None,
) -> ToolRuntime:
    if routes is None:
        routes = dict(HYBRID_DEFAULT_ROUTES)
    profile = CapabilityProfile(routes=routes)
    return ToolRuntime(
        surface="local_hybrid",
        capability_profile=profile,
        premium_client=premium_client,
    )


def _mock_ctx():
    return MagicMock()


# ---------------------------------------------------------------------------
# Search wrapper
# ---------------------------------------------------------------------------


class TestHybridSearchWrapper:
    @pytest.mark.anyio
    async def test_entries_mode_routes_locally(self):
        """mode=entries resolves to baseline_search → local."""
        rt = _make_runtime()
        wrapper = _build_hybrid_search_wrapper(rt)

        with patch(
            "watercooler_mcp.tools.graph._search_graph_impl",
            new_callable=AsyncMock,
            return_value='{"results": []}',
        ) as mock_impl:
            result = await wrapper(_mock_ctx(), mode="entries", query="test")
            mock_impl.assert_called_once()
            assert '"results"' in result

    @pytest.mark.anyio
    async def test_facts_mode_routes_remote(self):
        """mode=facts resolves to memory_query → remote when premium_client exists."""
        mock_client = MagicMock()
        mock_client.call_tool_text = AsyncMock(return_value='{"remote": true}')

        rt = _make_runtime(premium_client=mock_client)
        wrapper = _build_hybrid_search_wrapper(rt)

        result = await wrapper(_mock_ctx(), mode="facts", query="test")
        mock_client.call_tool_text.assert_called_once_with(
            "watercooler_search",
            {"mode": "facts", "query": "test"},
        )
        assert '"remote"' in result

    @pytest.mark.anyio
    async def test_facts_mode_disabled_when_no_client(self):
        """memory_query=remote but no client → disabled."""
        rt = _make_runtime(premium_client=None)
        wrapper = _build_hybrid_search_wrapper(rt)

        result = await wrapper(_mock_ctx(), mode="facts", query="test")
        data = json.loads(result)
        # With no client, remote is unavailable, and the route is "remote" for memory_query
        # so it resolves to disabled
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "memory_query"

    @pytest.mark.anyio
    async def test_disabled_route_returns_error(self):
        """Explicitly disabled capability → error JSON."""
        routes = dict(HYBRID_DEFAULT_ROUTES)
        routes["baseline_search"] = "disabled"
        rt = _make_runtime(routes=routes)
        wrapper = _build_hybrid_search_wrapper(rt)

        result = await wrapper(_mock_ctx(), mode="entries", query="test")
        data = json.loads(result)
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "baseline_search"


# ---------------------------------------------------------------------------
# Search wrapper — seeded similarity & federated modes (PR6 D4/D5)
# ---------------------------------------------------------------------------


class TestHybridSearchSeedAndFederated:
    """find_similar / federated_search folded into watercooler_search; the
    search hybrid wrapper resolves the seed_entry_id= and federated= modes
    per-(tool, args)."""

    @pytest.mark.anyio
    async def test_seed_entry_id_routes_remote(self):
        """search(seed_entry_id=) → semantic_similarity → remote (Plan v20
        default) when a premium client exists."""
        mock_client = MagicMock()
        mock_client.call_tool_text = AsyncMock(return_value='{"remote": true}')
        rt = _make_runtime(premium_client=mock_client)
        wrapper = _build_hybrid_search_wrapper(rt)

        result = await wrapper(_mock_ctx(), seed_entry_id="01ABC")
        mock_client.call_tool_text.assert_called_once_with(
            "watercooler_search", {"seed_entry_id": "01ABC"}
        )
        assert '"remote"' in result

    @pytest.mark.anyio
    async def test_seed_entry_id_local_override(self):
        """With semantic_similarity overridden to local, the seed call runs
        the local impl."""
        routes = dict(HYBRID_DEFAULT_ROUTES)
        routes["semantic_similarity"] = "local"
        rt = _make_runtime(routes=routes)
        wrapper = _build_hybrid_search_wrapper(rt)

        with patch(
            "watercooler_mcp.tools.graph._search_graph_impl",
            new_callable=AsyncMock,
            return_value='{"results": []}',
        ) as mock_impl:
            result = await wrapper(
                _mock_ctx(), seed_entry_id="01ABC", code_path="."
            )
            mock_impl.assert_called_once()
            assert '"results"' in result

    @pytest.mark.anyio
    async def test_seed_entry_id_disabled(self):
        routes = dict(HYBRID_DEFAULT_ROUTES)
        routes["semantic_similarity"] = "disabled"
        rt = _make_runtime(routes=routes)
        wrapper = _build_hybrid_search_wrapper(rt)

        result = await wrapper(_mock_ctx(), seed_entry_id="01ABC")
        data = json.loads(result)
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "semantic_similarity"

    @pytest.mark.anyio
    async def test_federated_routes_local(self):
        """search(federated=True) → federation_search → local (default route)."""
        rt = _make_runtime()
        wrapper = _build_hybrid_search_wrapper(rt)

        with patch(
            "watercooler_mcp.tools.graph._search_graph_impl",
            new_callable=AsyncMock,
            return_value='{"results": []}',
        ) as mock_impl:
            result = await wrapper(_mock_ctx(), federated=True, query="x")
            mock_impl.assert_called_once()
            assert '"results"' in result


class TestSearchSeedDispatch:
    """search(seed_entry_id=) dispatches to the find_similar impl, preserving
    use_embeddings — the retired tool's heuristic-fallback mode (PR6 review)."""

    @pytest.mark.anyio
    async def test_seed_dispatch_forwards_use_embeddings(self):
        from watercooler_mcp.tools import graph as graph_mod

        with patch.object(
            graph_mod,
            "_find_similar_entries_impl",
            return_value='{"results": []}',
        ) as mock_impl:
            await graph_mod._search_graph_impl(
                _mock_ctx(), seed_entry_id="01ABC", use_embeddings=False
            )
        mock_impl.assert_called_once()
        assert mock_impl.call_args.kwargs["entry_id"] == "01ABC"
        assert mock_impl.call_args.kwargs["use_embeddings"] is False
