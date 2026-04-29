"""Regression tests for hosted-mode `watercooler_search` T2 routing.

Companion to PR #667 (smart_query) — covers the contract that hosted-mode
`watercooler_search` must:

- route ``mode="entities"``  → ``_search_graphiti_nodes_impl``,
- route ``mode="facts"``     → ``_search_graphiti_impl``,
- route ``mode="episodes"``  → ``_search_graphiti_episodes_impl``,
- still route ``mode="entries"`` to the existing T1 HNSW / GitHub-keyword
  paths (no regression on the Plan v20 Phase 8 path),
- gracefully fall through from ``mode="auto"`` + auto-inferred ``facts``
  to entries when the Graphiti backend is unavailable,
- surface a structured ``graphiti_unavailable_hosted`` error when an
  *explicit* T2 mode hits a backend failure (no silent GitHub fallback).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _hosted_context():
    """Minimal stub matching what is_hosted_context() checks."""
    ctx = MagicMock()
    ctx.threads_dir = MagicMock()
    ctx.threads_dir.exists.return_value = True
    ctx.code_repo = "mostlyharmless-ai/watercooler"
    return ctx


def _patch_hosted_context_resolver(stub_ctx):
    """Patch the validation/hosted-detection chain used by _search_graph_impl."""
    return [
        patch(
            "watercooler_mcp.tools.graph.validation._require_context",
            return_value=(None, stub_ctx),
        ),
        patch(
            "watercooler_mcp.tools.graph.is_hosted_context",
            return_value=True,
        ),
    ]


@pytest.mark.anyio
async def test_hosted_search_entities_routes_to_graphiti_nodes():
    """mode='entities' in hosted mode → _search_graphiti_nodes_impl."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    nodes_mock = AsyncMock(return_value='{"count": 1, "results": [{"name": "Alpha"}]}')
    facts_mock = AsyncMock()
    episodes_mock = AsyncMock()
    github_mock = MagicMock(return_value=("never_called", None))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_nodes_impl", nodes_mock),
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph._search_graphiti_episodes_impl", episodes_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await _search_graph_impl(
            ctx=MagicMock(), query="alpha", mode="entities",
        )

    nodes_mock.assert_called_once()
    _, kwargs = nodes_mock.call_args
    assert kwargs["query"] == "alpha"
    facts_mock.assert_not_called()
    episodes_mock.assert_not_called()
    github_mock.assert_not_called()
    payload = json.loads(result)
    assert payload["count"] == 1


@pytest.mark.anyio
async def test_hosted_search_facts_routes_to_graphiti_impl_and_passes_active_only():
    """mode='facts' in hosted mode → _search_graphiti_impl(active_only=True)."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    facts_mock = AsyncMock(return_value='{"count": 0, "mode": "facts", "results": []}')
    github_mock = MagicMock(return_value=("never_called", None))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3]:
        await _search_graph_impl(
            ctx=MagicMock(), query="changed", mode="facts", active_only=True,
            superseded_start="2026-01-01", superseded_end="2026-04-01",
        )

    facts_mock.assert_called_once()
    _, kwargs = facts_mock.call_args
    assert kwargs["mode"] == "facts"
    assert kwargs["active_only"] is True
    assert kwargs["superseded_start"] == "2026-01-01"
    assert kwargs["superseded_end"] == "2026-04-01"
    github_mock.assert_not_called()


@pytest.mark.anyio
async def test_hosted_search_episodes_routes_to_graphiti_episodes_with_time_filters():
    """mode='episodes' in hosted mode → _search_graphiti_episodes_impl with time filters."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    episodes_mock = AsyncMock(return_value='{"count": 0, "results": []}')
    github_mock = MagicMock(return_value=("never_called", None))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_episodes_impl", episodes_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3]:
        await _search_graph_impl(
            ctx=MagicMock(), query="release", mode="episodes",
            start_time="2026-04-01", end_time="2026-04-30",
        )

    episodes_mock.assert_called_once()
    _, kwargs = episodes_mock.call_args
    assert kwargs["start_time"] == "2026-04-01"
    assert kwargs["end_time"] == "2026-04-30"
    github_mock.assert_not_called()


@pytest.mark.anyio
async def test_hosted_search_entries_keyword_still_uses_github_endpoint():
    """mode='entries' (no semantic) — preserve the existing GitHub keyword path."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    facts_mock = AsyncMock()
    nodes_mock = AsyncMock()
    github_mock = MagicMock(return_value=(None, {"count": 0, "results": []}))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph._search_graphiti_nodes_impl", nodes_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        await _search_graph_impl(
            ctx=MagicMock(), query="auth", mode="entries", semantic=False,
        )

    github_mock.assert_called_once()
    facts_mock.assert_not_called()
    nodes_mock.assert_not_called()


@pytest.mark.anyio
async def test_hosted_search_explicit_facts_surfaces_graphiti_error():
    """mode='facts' (explicit) + Graphiti error → structured error, no GitHub fallback."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    facts_mock = AsyncMock(side_effect=RuntimeError("Graphiti backend unavailable: boom"))
    github_mock = MagicMock(return_value=("never_called", None))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3]:
        result = await _search_graph_impl(
            ctx=MagicMock(), query="x", mode="facts",
        )

    payload = json.loads(result)
    assert payload["error"] == "graphiti_unavailable_hosted"
    assert "boom" in payload["message"]
    github_mock.assert_not_called()


@pytest.mark.anyio
async def test_hosted_search_auto_facts_falls_back_to_entries_when_graphiti_down():
    """mode='auto' + auto-inferred facts + Graphiti error → graceful fall to entries."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    facts_mock = AsyncMock(side_effect=RuntimeError("Graphiti backend unavailable"))
    github_mock = MagicMock(return_value=(None, {"count": 0, "results": []}))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
        # Force temporal pattern detection so 'auto' resolves to 'facts'.
        patch("watercooler_mcp.tools.graph._has_temporal_pattern", return_value=True),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        await _search_graph_impl(
            ctx=MagicMock(), query="what changed", mode="auto",
        )

    facts_mock.assert_called_once()
    github_mock.assert_called_once()


@pytest.mark.anyio
async def test_hosted_search_entries_semantic_still_uses_t1_hnsw():
    """mode='entries' + semantic=True — preserve Plan v20 Phase 8 T1 HNSW path."""
    from watercooler_mcp.tools.graph import _search_graph_impl

    sem_mock = MagicMock(return_value='{"results": []}')
    facts_mock = AsyncMock()
    github_mock = MagicMock(return_value=("never_called", None))

    patches = _patch_hosted_context_resolver(_hosted_context()) + [
        patch("watercooler_mcp.tools.graph._search_entries_hosted_semantic", sem_mock),
        patch("watercooler_mcp.tools.graph._search_graphiti_impl", facts_mock),
        patch("watercooler_mcp.tools.graph.search_entries_hosted", github_mock),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        await _search_graph_impl(
            ctx=MagicMock(), query="auth", mode="entries", semantic=True,
        )

    sem_mock.assert_called_once()
    facts_mock.assert_not_called()
    github_mock.assert_not_called()
