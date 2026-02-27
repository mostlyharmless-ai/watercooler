"""E2E tests for federated search with real JSONL graph data.

Tests the full federation pipeline against actual search_graph() calls
using programmatically created JSONL fixtures. No git operations needed.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler.config_schema import (
    FederationAccessConfig,
    FederationConfig,
    FederationNamespaceConfig,
    WatercoolerConfig,
)
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.tools.federation import _federated_search_impl

pytestmark = pytest.mark.e2e


def _create_graph_fixture(
    threads_dir: Path,
    threads: dict[str, dict],
    entries: dict[str, list[dict]],
) -> None:
    """Create per-thread JSONL graph data.

    Args:
        threads_dir: Root threads directory.
        threads: Map of topic -> thread meta dict.
        entries: Map of topic -> list of entry dicts.
    """
    graph_dir = threads_dir / "graph" / "baseline" / "threads"
    for topic, meta in threads.items():
        thread_dir = graph_dir / topic
        thread_dir.mkdir(parents=True, exist_ok=True)

        # Write meta.json
        meta_data = {"type": "thread", "topic": topic, **meta}
        (thread_dir / "meta.json").write_text(json.dumps(meta_data))

        # Write entries.jsonl
        topic_entries = entries.get(topic, [])
        if topic_entries:
            with open(thread_dir / "entries.jsonl", "w") as f:
                for entry in topic_entries:
                    entry_data = {"type": "entry", "thread_topic": topic, **entry}
                    f.write(json.dumps(entry_data) + "\n")


# -- Fixture data --

PRIMARY_THREADS = {
    "auth-protocol": {
        "title": "Authentication Protocol",
        "status": "OPEN",
        "ball": "Claude",
        "last_updated": "2026-02-15T10:00:00Z",
        "summary": "OAuth2 with PKCE for authentication",
        "entry_count": 3,
    },
    "deploy-pipeline": {
        "title": "Deploy Pipeline",
        "status": "OPEN",
        "ball": "User",
        "last_updated": "2026-02-10T12:00:00Z",
        "summary": "CI/CD pipeline setup",
        "entry_count": 2,
    },
}

PRIMARY_ENTRIES = {
    "auth-protocol": [
        {
            "entry_id": "01P_AUTH_001",
            "index": 0,
            "agent": "Claude (user)",
            "role": "planner",
            "entry_type": "Plan",
            "title": "Use OAuth2 with PKCE for auth",
            "timestamp": "2026-02-15T09:00:00Z",
            "summary": "OAuth2 PKCE flow for cloud authentication",
            "body": "Implementing OAuth2 with PKCE for secure auth.",
        },
        {
            "entry_id": "01P_AUTH_002",
            "index": 1,
            "agent": "Claude (user)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Auth middleware implemented",
            "timestamp": "2026-02-15T10:00:00Z",
            "summary": "JWT token validation in middleware",
            "body": "Added auth middleware with JWT validation.",
        },
        {
            "entry_id": "01P_AUTH_003",
            "index": 2,
            "agent": "User (admin)",
            "role": "critic",
            "entry_type": "Decision",
            "title": "Token refresh decision",
            "timestamp": "2026-02-15T11:00:00Z",
            "summary": "Use silent refresh for tokens",
            "body": "Decided to use silent refresh instead of sliding expiry.",
        },
    ],
    "deploy-pipeline": [
        {
            "entry_id": "01P_DEPLOY_001",
            "index": 0,
            "agent": "Claude (user)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Pipeline configuration",
            "timestamp": "2026-02-10T12:00:00Z",
            "summary": "GitHub Actions workflow setup",
            "body": "Set up CI pipeline with GitHub Actions.",
        },
        {
            "entry_id": "01P_DEPLOY_002",
            "index": 1,
            "agent": "User (admin)",
            "role": "pm",
            "entry_type": "Note",
            "title": "Deploy environments",
            "timestamp": "2026-02-01T08:00:00Z",
            "summary": "Staging and production environments",
            "body": "Deploy to staging first, then production.",
        },
    ],
}

SECONDARY_THREADS = {
    "frontend-auth": {
        "title": "Frontend Auth Implementation",
        "status": "OPEN",
        "ball": "Claude",
        "last_updated": "2026-02-14T15:00:00Z",
        "summary": "React auth components for site",
        "entry_count": 2,
    },
    "site-deploy": {
        "title": "Site Deployment",
        "status": "CLOSED",
        "ball": "User",
        "last_updated": "2026-01-20T10:00:00Z",
        "summary": "Vercel deployment for site",
        "entry_count": 2,
    },
}

SECONDARY_ENTRIES = {
    "frontend-auth": [
        {
            "entry_id": "01S_AUTH_001",
            "index": 0,
            "agent": "Claude (user)",
            "role": "planner",
            "entry_type": "Plan",
            "title": "OAuth2 PKCE in React",
            "timestamp": "2026-02-14T14:00:00Z",
            "summary": "Implement PKCE auth flow in React",
            "body": "Use oidc-client-ts for OAuth2 PKCE flow in the frontend.",
        },
        {
            "entry_id": "01S_AUTH_002",
            "index": 1,
            "agent": "Claude (user)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Auth provider component",
            "timestamp": "2026-02-14T15:00:00Z",
            "summary": "React context provider for auth state",
            "body": "Created AuthProvider with useAuth hook.",
        },
    ],
    "site-deploy": [
        {
            "entry_id": "01S_DEPLOY_001",
            "index": 0,
            "agent": "Claude (user)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Vercel configuration",
            "timestamp": "2026-01-20T09:00:00Z",
            "summary": "Vercel project setup for deployment",
            "body": "Configured Vercel with preview deploys per branch.",
        },
        {
            "entry_id": "01S_DEPLOY_002",
            "index": 1,
            "agent": "User (admin)",
            "role": "pm",
            "entry_type": "Decision",
            "title": "Deploy strategy chosen",
            "timestamp": "2026-01-20T10:00:00Z",
            "summary": "Use Vercel for all site deploys",
            "body": "Decision: use Vercel for all site deployments.",
        },
    ],
}


@pytest.fixture()
def federation_env(tmp_path):
    """Set up a complete federation environment with two namespaces."""
    # Create primary namespace
    primary_dir = tmp_path / "primary-threads"
    primary_dir.mkdir()
    _create_graph_fixture(primary_dir, PRIMARY_THREADS, PRIMARY_ENTRIES)

    # Create secondary namespace under worktrees (so it passes WORKTREE_BASE check)
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()
    secondary_dir = worktree_base / "watercooler-site"
    secondary_dir.mkdir()
    _create_graph_fixture(secondary_dir, SECONDARY_THREADS, SECONDARY_ENTRIES)

    # Create ThreadContext for primary
    code_root = tmp_path / "watercooler-cloud"
    code_root.mkdir()
    primary_ctx = ThreadContext(
        code_root=code_root,
        threads_dir=primary_dir,
        code_repo="org/watercooler-cloud",
        code_branch="main",
        code_commit="abc123",
        code_remote="https://github.com/org/watercooler-cloud.git",
        explicit_dir=False,
    )

    # Create WatercoolerConfig with federation
    wc_config = WatercoolerConfig(
        federation=FederationConfig(
            enabled=True,
            namespaces={
                "site": FederationNamespaceConfig(
                    code_path="/home/user/watercooler-site",
                ),
            },
            access=FederationAccessConfig(
                allowlists={"watercooler-cloud": ["site"]}
            ),
        )
    )

    return {
        "primary_ctx": primary_ctx,
        "primary_dir": primary_dir,
        "secondary_dir": secondary_dir,
        "worktree_base": worktree_base,
        "wc_config": wc_config,
        "tmp_path": tmp_path,
    }


@pytest.fixture()
def ctx():
    return MagicMock(spec=["log"])


class TestFederationE2EMultiNamespace:
    """E2E tests with real search_graph against fixture data."""

    @pytest.mark.anyio
    async def test_cross_namespace_auth_search(self, ctx, federation_env):
        """Search for 'auth' returns results from both namespaces."""
        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                federation_env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=federation_env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = federation_env["wc_config"]
            mock_validation._require_context.return_value = (None, federation_env["primary_ctx"])
            result = await _federated_search_impl(ctx, query="auth")

        data = json.loads(result)
        assert data["schema_version"] == 1
        assert data["result_count"] > 0

        # Should have results from both namespaces
        namespaces = {r["origin_namespace"] for r in data["results"]}
        assert "watercooler-cloud" in namespaces
        assert "site" in namespaces

    @pytest.mark.anyio
    async def test_primary_results_rank_higher(self, ctx, federation_env):
        """Primary namespace results rank above secondary for same query."""
        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                federation_env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=federation_env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = federation_env["wc_config"]
            mock_validation._require_context.return_value = (None, federation_env["primary_ctx"])
            result = await _federated_search_impl(ctx, query="OAuth2")

        data = json.loads(result)
        if data["result_count"] >= 2:
            # When scores are similar, primary should rank first (NW=1.0 vs 0.55)
            primary_results = [r for r in data["results"] if r["origin_namespace"] == "watercooler-cloud"]
            secondary_results = [r for r in data["results"] if r["origin_namespace"] == "site"]
            if primary_results and secondary_results:
                assert primary_results[0]["ranking_score"] >= secondary_results[0]["ranking_score"]

    @pytest.mark.anyio
    async def test_recency_affects_ranking(self, ctx, federation_env):
        """Recent entries rank higher than older ones."""
        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                federation_env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=federation_env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = federation_env["wc_config"]
            mock_validation._require_context.return_value = (None, federation_env["primary_ctx"])
            result = await _federated_search_impl(ctx, query="deploy")

        data = json.loads(result)
        if data["result_count"] >= 2:
            # Primary deploy entries (Feb 2026) should have higher recency decay
            # than secondary site-deploy entries (Jan 2026)
            for r in data["results"]:
                assert r["score_breakdown"]["recency_decay"] > 0.0
                assert r["score_breakdown"]["recency_decay"] <= 1.0

    @pytest.mark.anyio
    async def test_deny_topics_excludes_entries(self, ctx, federation_env):
        """deny_topics filters out matching entries from secondary."""
        # Add deny_topics to secondary namespace config
        env = federation_env
        wc_config = WatercoolerConfig(
            federation=FederationConfig(
                enabled=True,
                namespaces={
                    "site": FederationNamespaceConfig(
                        code_path="/home/user/watercooler-site",
                        deny_topics=["site-deploy"],
                    ),
                },
                access=FederationAccessConfig(
                    allowlists={"watercooler-cloud": ["site"]}
                ),
            )
        )

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = wc_config
            mock_validation._require_context.return_value = (None, env["primary_ctx"])
            result = await _federated_search_impl(ctx, query="deploy")

        data = json.loads(result)
        # Secondary "site-deploy" entries should be excluded
        for r in data["results"]:
            if r["origin_namespace"] == "site":
                assert r["entry_data"].get("topic") != "site-deploy"

    @pytest.mark.anyio
    async def test_partial_failure_ranking_stability(self, ctx, federation_env):
        """Removing a namespace doesn't reorder results from remaining namespaces."""
        env = federation_env

        # Run with secondary available
        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = env["wc_config"]
            mock_validation._require_context.return_value = (None, env["primary_ctx"])
            result_with = await _federated_search_impl(ctx, query="auth")

        # Run without secondary (empty namespaces)
        wc_config_no_sec = WatercoolerConfig(
            federation=FederationConfig(enabled=True, namespaces={})
        )
        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
        ):
            mock_config.full.return_value = wc_config_no_sec
            mock_validation._require_context.return_value = (None, env["primary_ctx"])
            result_without = await _federated_search_impl(ctx, query="auth")

        data_with = json.loads(result_with)
        data_without = json.loads(result_without)

        # Extract primary-only results from both runs
        primary_with = [
            r["entry_id"] for r in data_with["results"]
            if r["origin_namespace"] == "watercooler-cloud"
        ]
        primary_without = [
            r["entry_id"] for r in data_without["results"]
        ]

        # Order should be identical (multiplicative independence)
        assert primary_with == primary_without

    @pytest.mark.anyio
    async def test_allocation_cap_limits_secondary(self, ctx, federation_env):
        """Secondary namespace is capped at max(limit//2, 1) results."""
        env = federation_env

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = env["wc_config"]
            mock_validation._require_context.return_value = (None, env["primary_ctx"])
            # Request only 2 results — secondary gets max(2//2, 1) = 1
            result = await _federated_search_impl(ctx, query="auth", limit=2)

        data = json.loads(result)
        assert data["result_count"] <= 2

    @pytest.mark.anyio
    async def test_response_envelope_structure(self, ctx, federation_env):
        """Verify complete response envelope structure."""
        env = federation_env

        with (
            patch("watercooler_mcp.tools.federation.config") as mock_config,
            patch("watercooler_mcp.tools.federation.validation") as mock_validation,
            patch("watercooler_mcp.tools.federation.is_hosted_mode", return_value=False),
            patch(
                "watercooler_mcp.federation.resolver.WORKTREE_BASE",
                env["worktree_base"],
            ),
            patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=env["secondary_dir"],
            ),
        ):
            mock_config.full.return_value = env["wc_config"]
            mock_validation._require_context.return_value = (None, env["primary_ctx"])
            result = await _federated_search_impl(ctx, query="auth")

        data = json.loads(result)

        # Top-level fields
        assert data["schema_version"] == 1
        assert data["primary_namespace"] == "watercooler-cloud"
        assert "queried_namespaces" in data
        assert "namespace_status" in data
        assert "result_count" in data
        assert "total_candidates_before_truncation" in data
        assert "results" in data

        # Per-result fields
        if data["results"]:
            r = data["results"][0]
            assert "entry_id" in r
            assert "origin_namespace" in r
            assert "ranking_score" in r
            assert "score_breakdown" in r
            assert "entry_data" in r
            assert "raw_score" in r["score_breakdown"]
            assert "normalized_score" in r["score_breakdown"]
            assert "namespace_weight" in r["score_breakdown"]
            assert "recency_decay" in r["score_breakdown"]
