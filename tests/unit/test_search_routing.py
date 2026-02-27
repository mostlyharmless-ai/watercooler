"""Tests for tier-aware search routing.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 6:
- Free tier → baseline graph (always)
- Paid tier → memory backend (with fallback)
- Mode inference: auto, entries, entities, episodes
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Configure pytest-asyncio mode
pytestmark = pytest.mark.anyio


class TestSearchBackendSelection:
    """Tests for backend selection logic."""

    def test_get_search_backend_default_baseline(self):
        """Default backend should be baseline when no env var set and TOML returns null."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {}, clear=True):
            # Remove any existing WATERCOOLER_MEMORY_BACKEND
            os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)
            # Mock TOML config to return "null" (not graphiti/leanrag)
            with patch("watercooler.memory_config.get_memory_backend", return_value="null"):
                backend = get_search_backend("auto")
                assert backend == "baseline"

    def test_get_search_backend_explicit_baseline(self):
        """Explicit baseline backend should always use baseline."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            backend = get_search_backend("baseline")
            assert backend == "baseline"

    def test_get_search_backend_auto_with_graphiti(self):
        """Auto backend should use graphiti when WATERCOOLER_MEMORY_BACKEND=graphiti."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            backend = get_search_backend("auto")
            assert backend == "graphiti"

    def test_get_search_backend_auto_with_leanrag(self):
        """Auto backend should use leanrag when WATERCOOLER_MEMORY_BACKEND=leanrag."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "leanrag"}):
            backend = get_search_backend("auto")
            assert backend == "leanrag"

    def test_get_search_backend_explicit_graphiti(self):
        """Explicit graphiti backend should be respected."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)
            backend = get_search_backend("graphiti")
            assert backend == "graphiti"

    def test_get_search_backend_unknown_falls_back(self):
        """Unknown backend should fall back to baseline."""
        from watercooler_mcp.tools.graph import get_search_backend

        backend = get_search_backend("unknown_backend")
        assert backend == "baseline"


class TestSearchModeInference:
    """Tests for search mode inference logic."""

    def test_infer_search_mode_auto_keyword_query(self):
        """Auto mode with keyword query should use entries mode."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("auto", "authentication", False)
        assert mode == "entries"

    def test_infer_search_mode_auto_semantic_query(self):
        """Auto mode with semantic query should use entries mode."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("auto", "how does auth work", True)
        assert mode == "entries"

    def test_infer_search_mode_auto_entity_query(self):
        """Auto mode with entity-like query should suggest entities mode."""
        from watercooler_mcp.tools.graph import infer_search_mode

        # Queries that look like entity searches
        mode = infer_search_mode("auto", "Claude", False)
        # Without explicit entity markers, defaults to entries
        assert mode == "entries"

    def test_infer_search_mode_explicit_entities(self):
        """Explicit entities mode should be respected."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("entities", "any query", False)
        assert mode == "entities"

    def test_infer_search_mode_explicit_episodes(self):
        """Explicit episodes mode should be respected."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("episodes", "any query", False)
        assert mode == "episodes"

    def test_infer_search_mode_explicit_entries(self):
        """Explicit entries mode should be respected."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("entries", "any query", True)
        assert mode == "entries"


class TestSearchRouting:
    """Tests for the unified search routing function."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory with graph."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)
        # Create minimal graph files
        (graph_dir / "nodes.jsonl").write_text("")
        (graph_dir / "edges.jsonl").write_text("")
        return threads_dir

    async def test_route_to_baseline_when_backend_baseline(self, mock_context, mock_threads_dir):
        """Route to baseline graph when backend=baseline."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_baseline_impl"
        ) as mock_baseline:
            mock_baseline.return_value = json.dumps({"results": [], "count": 0})

            result = await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="baseline",
                mode="entries",
            )

            mock_baseline.assert_called_once()
            assert "results" in result

    async def test_route_to_graphiti_when_backend_graphiti(self, mock_context, mock_threads_dir):
        """Route to Graphiti when backend=graphiti."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            result = await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entries",
            )

            mock_graphiti.assert_called_once()
            assert "results" in result

    async def test_fallback_to_baseline_when_graphiti_unavailable(
        self, mock_context, mock_threads_dir
    ):
        """Fall back to baseline when Graphiti is unavailable."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock
        ) as mock_graphiti, patch(
            "watercooler_mcp.tools.graph._search_baseline_impl"
        ) as mock_baseline:
            # Graphiti raises an error
            mock_graphiti.side_effect = RuntimeError("Graphiti not available")
            mock_baseline.return_value = json.dumps({"results": [], "count": 0})

            result = await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entries",
            )

            # Should have tried graphiti first, then fallen back
            mock_graphiti.assert_called_once()
            mock_baseline.assert_called_once()
            result_data = json.loads(result)
            assert result_data.get("fallback_used") is True

    async def test_entities_mode_routes_to_graphiti(self, mock_context, mock_threads_dir):
        """Entities mode should route to Graphiti search_nodes."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_nodes_impl",
            new_callable=AsyncMock
        ) as mock_nodes:
            mock_nodes.return_value = json.dumps({"results": [], "count": 0})

            result = await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entities",
            )

            mock_nodes.assert_called_once()

    async def test_episodes_mode_routes_to_graphiti(self, mock_context, mock_threads_dir):
        """Episodes mode should route to Graphiti episodes search."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_episodes_impl",
            new_callable=AsyncMock
        ) as mock_episodes:
            mock_episodes.return_value = json.dumps({"results": [], "count": 0})

            result = await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="episodes",
            )

            mock_episodes.assert_called_once()


class TestTimeFilterRouting:
    """Tests for time filter passthrough in search routing (issue #148)."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)
        (graph_dir / "nodes.jsonl").write_text("")
        (graph_dir / "edges.jsonl").write_text("")
        return threads_dir

    async def test_episodes_time_filters_passed_through(self, mock_context, mock_threads_dir):
        """Time filters should be passed through to episodes impl."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_episodes_impl",
            new_callable=AsyncMock,
        ) as mock_episodes:
            mock_episodes.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="episodes",
                start_time="2026-02-01",
                end_time="2026-02-09",
            )

            mock_episodes.assert_called_once()
            assert mock_episodes.call_args.kwargs.get("start_time") == "2026-02-01"

    async def test_episodes_no_time_filters_unchanged(self, mock_context, mock_threads_dir):
        """Episodes without time filters should work as before."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_episodes_impl",
            new_callable=AsyncMock,
        ) as mock_episodes:
            mock_episodes.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="episodes",
            )

            mock_episodes.assert_called_once()

    async def test_episodes_start_time_only(self, mock_context, mock_threads_dir):
        """Only start_time should be passed through (end_time empty)."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_episodes_impl",
            new_callable=AsyncMock,
        ) as mock_episodes:
            mock_episodes.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="episodes",
                start_time="2026-02-01",
            )

            mock_episodes.assert_called_once()

    async def test_episodes_end_time_only(self, mock_context, mock_threads_dir):
        """Only end_time should be passed through (start_time empty)."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_episodes_impl",
            new_callable=AsyncMock,
        ) as mock_episodes:
            mock_episodes.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="episodes",
                end_time="2026-02-09",
            )

            mock_episodes.assert_called_once()

    async def test_facts_time_filters_passed_through(self, mock_context, mock_threads_dir):
        """Time filters should be passed through to facts/entries impl."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entries",
                start_time="2026-02-01",
                end_time="2026-02-09",
            )

            mock_graphiti.assert_called_once()
            assert mock_graphiti.call_args.kwargs.get("start_time") == "2026-02-01"


class TestSearchToolParameters:
    """Tests for extended watercooler_search tool parameters."""

    def test_search_accepts_mode_parameter(self):
        """watercooler_search should accept mode parameter."""
        from watercooler_mcp.tools.graph import _search_graph_impl
        import inspect

        sig = inspect.signature(_search_graph_impl)
        params = list(sig.parameters.keys())
        assert "mode" in params

    def test_search_accepts_backend_parameter(self):
        """watercooler_search should accept backend parameter."""
        from watercooler_mcp.tools.graph import _search_graph_impl
        import inspect

        sig = inspect.signature(_search_graph_impl)
        params = list(sig.parameters.keys())
        assert "backend" in params

    def test_search_mode_default_is_auto(self):
        """Mode parameter default should be 'auto'."""
        from watercooler_mcp.tools.graph import _search_graph_impl
        import inspect

        sig = inspect.signature(_search_graph_impl)
        mode_param = sig.parameters.get("mode")
        assert mode_param is not None
        assert mode_param.default == "auto"

    def test_search_backend_default_is_auto(self):
        """Backend parameter default should be 'auto'."""
        from watercooler_mcp.tools.graph import _search_graph_impl
        import inspect

        sig = inspect.signature(_search_graph_impl)
        backend_param = sig.parameters.get("backend")
        assert backend_param is not None
        assert backend_param.default == "auto"

    def test_infer_search_mode_explicit_facts(self):
        """Explicit facts mode should pass through unchanged."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("facts", "any query", False)
        assert mode == "facts"


class TestFactsModeRouting:
    """Tests for mode='facts' routing and error handling."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)
        (graph_dir / "nodes.jsonl").write_text("")
        (graph_dir / "edges.jsonl").write_text("")
        return threads_dir

    async def test_facts_mode_routes_to_graphiti_with_mode_kwarg(
        self, mock_context, mock_threads_dir
    ):
        """mode='facts' must pass mode='facts' to _search_graphiti_impl."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="facts",
            )

            mock_graphiti.assert_called_once()
            assert mock_graphiti.call_args.kwargs.get("mode") == "facts"

    async def test_entries_mode_graphiti_passes_mode_entries(
        self, mock_context, mock_threads_dir
    ):
        """mode='entries' with graphiti backend must pass mode='entries', not 'facts'."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entries",
            )

            mock_graphiti.assert_called_once()
            assert mock_graphiti.call_args.kwargs.get("mode") == "entries"

    async def test_facts_mode_returns_structured_json_error_on_any_exception(
        self, mock_context, mock_threads_dir
    ):
        """Any exception from Graphiti in facts mode → structured JSON (not bare string)."""
        from watercooler_mcp.tools.graph import route_search

        for exc in [
            RuntimeError("Graphiti not configured"),
            ConnectionError("FalkorDB unreachable"),
            OSError("socket timeout"),
        ]:
            with patch(
                "watercooler_mcp.tools.graph._search_graphiti_impl",
                new_callable=AsyncMock,
            ) as mock_graphiti:
                mock_graphiti.side_effect = exc

                result = await route_search(
                    ctx=mock_context,
                    threads_dir=mock_threads_dir,
                    query="test query",
                    backend="graphiti",
                    mode="facts",
                )

                data = json.loads(result)
                assert data["error"] == "facts_mode_requires_graphiti"
                assert data["results"] == []
                assert data["count"] == 0
                assert "hint" in data, "structured error envelope must include a 'hint' key"
                # Must not expose raw exception text in the message field
                assert str(exc) not in data.get("message", "")

    async def test_facts_mode_does_not_fall_back_to_baseline(
        self, mock_context, mock_threads_dir
    ):
        """facts mode must NOT silently fall back to baseline — hard error only."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti, patch(
            "watercooler_mcp.tools.graph._search_baseline_impl",
        ) as mock_baseline:
            mock_graphiti.side_effect = RuntimeError("Graphiti not available")

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="facts",
            )

            # Baseline must never be called in facts mode
            mock_baseline.assert_not_called()

    async def test_active_only_passed_through_in_facts_mode(
        self, mock_context, mock_threads_dir
    ):
        """active_only kwarg must be forwarded to _search_graphiti_impl in facts mode."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="facts",
                active_only=True,
            )

            mock_graphiti.assert_called_once()
            assert mock_graphiti.call_args.kwargs.get("active_only") is True

    async def test_active_only_passed_through_in_entries_graphiti_mode(
        self, mock_context, mock_threads_dir
    ):
        """active_only must also be forwarded when mode=entries and backend=graphiti."""
        from watercooler_mcp.tools.graph import route_search

        with patch(
            "watercooler_mcp.tools.graph._search_graphiti_impl",
            new_callable=AsyncMock,
        ) as mock_graphiti:
            mock_graphiti.return_value = json.dumps({"results": [], "count": 0})

            await route_search(
                ctx=mock_context,
                threads_dir=mock_threads_dir,
                query="test query",
                backend="graphiti",
                mode="entries",
                active_only=True,
            )

            mock_graphiti.assert_called_once()
            assert mock_graphiti.call_args.kwargs.get("active_only") is True
