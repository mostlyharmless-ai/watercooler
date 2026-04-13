"""Tests for scope-aware cache keys (Step 11)."""

from __future__ import annotations

from watercooler_mcp.cache import CacheKey


class TestCacheKeyScopeAware:
    def test_scope_id_in_key(self):
        key = CacheKey(resource="thread", scope_id="u1:org/repo", topic="auth")
        assert str(key) == "thread:u1:org/repo:auth"

    def test_user_id_in_key(self):
        key = CacheKey(resource="capability_grants", user_id="user1")
        assert str(key) == "capability_grants:user1"

    def test_full_key_ordering(self):
        key = CacheKey(
            resource="entry",
            scope_id="u1:org/repo",
            user_id="u1",
            repo="org/repo",
            branch="main",
            topic="auth",
            entry_id="01ABC",
            extra="v2",
        )
        expected = "entry:u1:org/repo:u1:org/repo:main:auth:01ABC:v2"
        assert str(key) == expected

    def test_none_values_omitted(self):
        key = CacheKey(resource="thread", topic="auth")
        assert str(key) == "thread:auth"
        # No "None" literal in the key
        assert "None" not in str(key)

    def test_backward_compatible(self):
        """Existing keys without scope_id/user_id still work."""
        key = CacheKey(resource="graph", repo="org/repo", branch="main")
        assert str(key) == "graph:org/repo:main"
