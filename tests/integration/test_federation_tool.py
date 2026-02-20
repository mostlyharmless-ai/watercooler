"""Integration tests for watercooler_federated_search MCP tool.

Tests the full tool handler with mocked search_graph() and config.
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler.config_schema import (
    FederationAccessConfig,
    FederationConfig,
    FederationNamespaceConfig,
    FederationScoringConfig,
    WatercoolerConfig,
)
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.tools.federation import _federated_search_impl


# Minimal mock classes matching baseline_graph search data structures


@dataclass
class MockGraphEntry:
    entry_id: str = "01ABC"
    thread_topic: str = "auth-protocol"
    index: int = 0
    agent: str = "Claude"
    role: str = "implementer"
    entry_type: str = "Note"
    title: str = "Test entry"
    timestamp: str = "2026-02-01T12:00:00Z"
    summary: str = "Test summary"
    body: Optional[str] = None
    file_refs: Optional[List[str]] = None
    pr_refs: Optional[List[str]] = None
    commit_refs: Optional[List[str]] = None
    access_count: int = 0
    code_branch: Optional[str] = None


@dataclass
class MockSearchResult:
    node_type: str = "entry"
    node_id: str = "01ABC"
    score: float = 1.7
    matched_fields: list = field(default_factory=lambda: ["title", "body"])
    thread: object = None
    entry: Optional[MockGraphEntry] = None


@dataclass
class MockSearchResults:
    results: list = field(default_factory=list)
    total_scanned: int = 0
    query: object = None


def _make_federation_config(
    enabled: bool = True,
    namespaces: dict | None = None,
    allowlists: dict | None = None,
) -> WatercoolerConfig:
    """Create a WatercoolerConfig with federation settings."""
    if namespaces is None:
        namespaces = {
            "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
        }
    if allowlists is None:
        allowlists = {"watercooler-cloud": ["site"]}

    return WatercoolerConfig(
        federation=FederationConfig(
            enabled=enabled,
            namespaces=namespaces,
            access=FederationAccessConfig(allowlists=allowlists),
        )
    )


def _make_primary_ctx(tmp_path: Path) -> ThreadContext:
    threads_dir = tmp_path / "primary-threads"
    threads_dir.mkdir(exist_ok=True)
    code_root = tmp_path / "watercooler-cloud"
    code_root.mkdir(exist_ok=True)
    return ThreadContext(
        code_root=code_root,
        threads_dir=threads_dir,
        code_repo="org/watercooler-cloud",
        code_branch="main",
        code_commit="abc123",
        code_remote="https://github.com/org/watercooler-cloud.git",
        explicit_dir=False,
    )


def _mock_search_graph(entries: list[MockGraphEntry], score: float = 1.7):
    """Create a mock search_graph that returns given entries."""
    results = MockSearchResults(
        results=[
            MockSearchResult(
                node_id=e.entry_id,
                score=score,
                matched_fields=["title", "body"],
                entry=e,
            )
            for e in entries
        ],
        total_scanned=len(entries),
    )
    return MagicMock(return_value=results)


@pytest.fixture()
def ctx():
    return MagicMock(spec=["log"])


class TestFederatedSearchDisabled:
    """Tests for feature gate behavior."""

    @pytest.mark.anyio
    async def test_disabled_returns_error(self, ctx, tmp_path):
        wc_config = _make_federation_config(enabled=False)

        with patch("watercooler_mcp.tools.federation.config") as mock_config:
            mock_config.full.return_value = wc_config
            result = await _federated_search_impl(ctx, query="test")

        data = json.loads(result)
        assert data["error"] == "FEDERATION_DISABLED"


class TestFederatedSearchValidation:
    """Tests for input validation."""

    @pytest.mark.anyio
    async def test_empty_query(self, ctx):
        result = await _federated_search_impl(ctx, query="")
        data = json.loads(result)
        assert data["error"] == "EMPTY_QUERY"

    @pytest.mark.anyio
    async def test_query_too_long(self, ctx):
        wc_config = _make_federation_config(enabled=True)
        with patch("watercooler_mcp.tools.federation.config") as mock_config:
            mock_config.full.return_value = wc_config
            result = await _federated_search_impl(ctx, query="x" * 501)

        data = json.loads(result)
        assert data["error"] == "VALIDATION_ERROR"


class TestFederatedSearchHosted:
    """Tests for hosted mode behavior."""

    @pytest.mark.anyio
    async def test_hosted_mode_error(self, ctx, tmp_path):
        wc_config = _make_federation_config(enabled=True)
        primary_ctx = _make_primary_ctx(tmp_path)

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=True),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="test")

        data = json.loads(result)
        assert data["error"] == "FEDERATION_NOT_AVAILABLE"


class TestFederatedSearchHappyPath:
    """Tests for full happy path with mocked search_graph."""

    @pytest.mark.anyio
    async def test_single_namespace_search(self, ctx, tmp_path):
        """Primary-only search returns results."""
        wc_config = _make_federation_config(enabled=True, namespaces={})
        primary_ctx = _make_primary_ctx(tmp_path)

        entries = [MockGraphEntry(entry_id="01A", title="Auth decision")]
        mock_search = _mock_search_graph(entries)

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.tools.federation.search_graph", mock_search),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="auth")

        data = json.loads(result)
        assert data["schema_version"] == 1
        assert data["result_count"] == 1
        assert data["results"][0]["entry_id"] == "01A"
        assert data["namespace_status"]["watercooler-cloud"] == "ok"

    @pytest.mark.anyio
    async def test_multi_namespace_search(self, ctx, tmp_path):
        """Search across primary + secondary returns results from both."""
        wc_config = _make_federation_config(enabled=True)
        primary_ctx = _make_primary_ctx(tmp_path)

        # Set up worktree for secondary
        worktree_base = tmp_path / "worktrees"
        site_worktree = worktree_base / "watercooler-site"
        site_worktree.mkdir(parents=True)

        primary_entries = [MockGraphEntry(entry_id="01A", title="Auth in cloud")]
        secondary_entries = [MockGraphEntry(entry_id="02B", title="Auth in site")]

        call_count = 0
        def mock_search_multi(threads_dir, sq):
            nonlocal call_count
            call_count += 1
            if "primary" in str(threads_dir):
                return MockSearchResults(
                    results=[MockSearchResult(node_id="01A", score=1.8, entry=primary_entries[0])],
                )
            return MockSearchResults(
                results=[MockSearchResult(node_id="02B", score=1.5, entry=secondary_entries[0])],
            )

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.tools.federation.search_graph", side_effect=mock_search_multi),
            patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=site_worktree,
            ),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="auth")

        data = json.loads(result)
        assert data["result_count"] == 2
        namespaces_in_results = {r["origin_namespace"] for r in data["results"]}
        assert "watercooler-cloud" in namespaces_in_results
        assert "site" in namespaces_in_results

    @pytest.mark.anyio
    async def test_secondary_timeout_partial_results(self, ctx, tmp_path):
        """Secondary namespace timeout yields partial results."""
        wc_config = WatercoolerConfig(
            federation=FederationConfig(
                enabled=True,
                namespace_timeout=0.01,  # Very short timeout
                namespaces={"site": FederationNamespaceConfig(code_path="/home/user/site")},
                access=FederationAccessConfig(allowlists={"watercooler-cloud": ["site"]}),
            )
        )
        primary_ctx = _make_primary_ctx(tmp_path)

        worktree_base = tmp_path / "worktrees"
        site_worktree = worktree_base / "site"
        site_worktree.mkdir(parents=True)

        def mock_search_slow(threads_dir, sq):
            import time
            if "primary" not in str(threads_dir):
                time.sleep(0.5)  # Exceed timeout
            return MockSearchResults(
                results=[MockSearchResult(
                    node_id="01A", score=1.7,
                    entry=MockGraphEntry(entry_id="01A"),
                )],
            )

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.tools.federation.search_graph", side_effect=mock_search_slow),
            patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=site_worktree,
            ),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="test")

        data = json.loads(result)
        assert data["namespace_status"]["watercooler-cloud"] == "ok"
        assert data["namespace_status"]["site"] == "timeout"
        assert data["result_count"] >= 1  # At least primary results

    @pytest.mark.anyio
    async def test_access_denied_partial_results(self, ctx, tmp_path):
        """Secondary namespace denied by access control."""
        wc_config = _make_federation_config(
            enabled=True,
            allowlists={},  # No access for anyone
        )
        primary_ctx = _make_primary_ctx(tmp_path)

        entries = [MockGraphEntry(entry_id="01A")]
        mock_search = _mock_search_graph(entries)

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.tools.federation.search_graph", mock_search),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="test")

        data = json.loads(result)
        assert data["namespace_status"]["site"] == "access_denied"
        # Only primary results
        assert all(r["origin_namespace"] == "watercooler-cloud" for r in data["results"])

    @pytest.mark.anyio
    async def test_too_many_namespaces_rejected(self, ctx, tmp_path):
        """Exceeding max_namespaces returns error."""
        many_namespaces = {
            f"ns{i}": FederationNamespaceConfig(code_path=f"/tmp/ns{i}")
            for i in range(10)
        }
        wc_config = WatercoolerConfig(
            federation=FederationConfig(
                enabled=True,
                max_namespaces=3,
                namespaces=many_namespaces,
                access=FederationAccessConfig(
                    allowlists={"watercooler-cloud": list(many_namespaces.keys())}
                ),
            )
        )
        primary_ctx = _make_primary_ctx(tmp_path)

        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        # Create worktrees for all namespaces
        for ns_id in many_namespaces:
            (worktree_base / ns_id).mkdir()

        def mock_worktree(code_root):
            return worktree_base / code_root.name

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base),
            patch("watercooler_mcp.federation.resolver._worktree_path_for", side_effect=mock_worktree),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(ctx, query="test")

        data = json.loads(result)
        assert data["error"] == "TOO_MANY_NAMESPACES"

    @pytest.mark.anyio
    async def test_primary_branch_filter_in_envelope(self, ctx, tmp_path):
        """code_branch parameter included in response envelope."""
        wc_config = _make_federation_config(enabled=True, namespaces={})
        primary_ctx = _make_primary_ctx(tmp_path)

        mock_search = _mock_search_graph([MockGraphEntry(entry_id="01A")])

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.tools.federation.search_graph", mock_search),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, primary_ctx)
            result = await _federated_search_impl(
                ctx, query="test", code_branch="feat/auth"
            )

        data = json.loads(result)
        assert data["primary_branch_filter"] == "feat/auth"
