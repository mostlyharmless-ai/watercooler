"""Unit tests for federation namespace resolver module."""

from unittest.mock import patch

import pytest

from watercooler.config_schema import FederationConfig, FederationNamespaceConfig
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.federation.resolver import (
    WorktreeStatus,
    discover_namespace_worktree,
    resolve_all_namespaces,
)


@pytest.fixture()
def primary_context(tmp_path):
    """Create a ThreadContext for the primary namespace."""
    threads_dir = tmp_path / "primary-threads"
    threads_dir.mkdir()
    return ThreadContext(
        code_root=tmp_path / "watercooler-cloud",
        threads_dir=threads_dir,
        code_repo="org/watercooler-cloud",
        code_branch="main",
        code_commit="abc123",
        code_remote="https://github.com/org/watercooler-cloud.git",
        explicit_dir=False,
    )


class TestDiscoverNamespaceWorktree:
    """Tests for discover_namespace_worktree."""

    def test_existing_worktree_discovered(self, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree = worktree_base / "watercooler-site"
        worktree.mkdir(parents=True)

        ns_config = FederationNamespaceConfig(code_path="/home/user/watercooler-site")

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree,
            ):
                result = discover_namespace_worktree("site", ns_config)
        # Returns the resolved path
        assert result == worktree.resolve()

    def test_missing_worktree_returns_none(self, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        worktree = worktree_base / "watercooler-site"

        ns_config = FederationNamespaceConfig(code_path="/home/user/watercooler-site")

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree,
            ):
                result = discover_namespace_worktree("site", ns_config)
        assert result is WorktreeStatus.NOT_INITIALIZED

    def test_symlink_rejected(self, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        symlink = worktree_base / "watercooler-site"
        symlink.symlink_to(real_dir)

        ns_config = FederationNamespaceConfig(code_path="/home/user/watercooler-site")

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=symlink,
            ):
                result = discover_namespace_worktree("site", ns_config)
        assert result is WorktreeStatus.SECURITY_REJECTED

    def test_path_escaping_worktree_base_rejected(self, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        escape_path = tmp_path / "outside"
        escape_path.mkdir()

        ns_config = FederationNamespaceConfig(code_path="/home/user/watercooler-site")

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=escape_path,
            ):
                result = discover_namespace_worktree("site", ns_config)
        assert result is WorktreeStatus.SECURITY_REJECTED

    def test_similar_prefix_path_rejected(self, tmp_path):
        """Path with similar prefix but outside WORKTREE_BASE is rejected."""
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        # Similar prefix: "worktrees-other" starts with "worktrees" as string
        similar_prefix = tmp_path / "worktrees-other"
        similar_prefix.mkdir()

        ns_config = FederationNamespaceConfig(code_path="/home/user/watercooler-site")

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=similar_prefix,
            ):
                result = discover_namespace_worktree("site", ns_config)
        assert result is WorktreeStatus.SECURITY_REJECTED


class TestResolveAllNamespaces:
    """Tests for resolve_all_namespaces."""

    def test_primary_always_resolved(self, primary_context):
        fed_config = FederationConfig()
        results = resolve_all_namespaces(primary_context, fed_config)
        assert "watercooler-cloud" in results
        r = results["watercooler-cloud"]
        assert r.is_primary is True
        assert r.status == "ok"
        assert r.threads_dir is not None

    def test_secondary_not_initialized(self, primary_context, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        fed_config = FederationConfig(
            namespaces={
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree_base / "watercooler-site",
            ):
                results = resolve_all_namespaces(primary_context, fed_config)

        assert "site" in results
        r = results["site"]
        assert r.status == "not_initialized"
        assert r.action_hint  # Should include the code_path
        assert "/home/user/watercooler-site" in r.action_hint

    def test_secondary_security_rejected(self, primary_context, tmp_path):
        """Symlink/path-escape produces security_rejected, not not_initialized."""
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        symlink = worktree_base / "watercooler-site"
        symlink.symlink_to(real_dir)

        fed_config = FederationConfig(
            namespaces={
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=symlink,
            ):
                results = resolve_all_namespaces(primary_context, fed_config)

        r = results["site"]
        assert r.status == "security_rejected"
        assert "security checks" in r.error_message
        assert r.action_hint == ""  # No action hint for security rejections

    def test_secondary_resolved(self, primary_context, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree = worktree_base / "watercooler-site"
        worktree.mkdir(parents=True)

        fed_config = FederationConfig(
            namespaces={
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree,
            ):
                results = resolve_all_namespaces(primary_context, fed_config)

        assert results["site"].status == "ok"
        assert results["site"].threads_dir == worktree

    def test_namespace_override(self, primary_context, tmp_path):
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        fed_config = FederationConfig(
            namespaces={
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
                "docs": FederationNamespaceConfig(code_path="/home/user/watercooler-docs"),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree_base / "nonexistent",
            ):
                results = resolve_all_namespaces(
                    primary_context, fed_config, namespace_override=["site"]
                )

        # Only primary + site (docs excluded by override)
        assert "watercooler-cloud" in results
        assert "site" in results
        assert "docs" not in results

    def test_override_with_unknown_namespace(self, primary_context):
        fed_config = FederationConfig()
        results = resolve_all_namespaces(
            primary_context, fed_config, namespace_override=["nonexistent"]
        )
        assert "nonexistent" in results
        assert results["nonexistent"].status == "error"
        assert "not found" in results["nonexistent"].error_message

    def test_primary_secondary_collision_skips_secondary(self, primary_context, tmp_path):
        """Secondary namespace ID that collides with primary is excluded."""
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        # "watercooler-cloud" matches the primary_context.code_root.name
        fed_config = FederationConfig(
            namespaces={
                "watercooler-cloud": FederationNamespaceConfig(
                    code_path="/home/user/watercooler-cloud"
                ),
                "site": FederationNamespaceConfig(
                    code_path="/home/user/watercooler-site"
                ),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree_base / "watercooler-site",
            ):
                results = resolve_all_namespaces(primary_context, fed_config)

        # Primary is present and is_primary
        assert results["watercooler-cloud"].is_primary is True
        assert results["watercooler-cloud"].status == "ok"
        # The colliding secondary is NOT present as a separate entry
        # (primary wins, secondary with same ID is skipped)
        assert len([r for r in results.values() if r.is_primary]) == 1
        # Non-colliding secondary is still resolved
        assert "site" in results

    def test_no_git_operations(self, primary_context, tmp_path):
        """Verify no subprocess calls (no git operations)."""
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        fed_config = FederationConfig(
            namespaces={
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
            }
        )

        with patch("watercooler_mcp.federation.resolver.WORKTREE_BASE", worktree_base):
            with patch(
                "watercooler_mcp.federation.resolver._worktree_path_for",
                return_value=worktree_base / "watercooler-site",
            ):
                with patch("subprocess.run") as mock_subprocess:
                    resolve_all_namespaces(primary_context, fed_config)
                    mock_subprocess.assert_not_called()
