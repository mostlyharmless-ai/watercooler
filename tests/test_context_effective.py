"""Tests for effective context and scope_id (Step 10)."""

from __future__ import annotations

import pytest

from watercooler_mcp.context import (
    HttpRequestContext,
    clear_http_context,
    clear_worker_context,
    get_effective_context,
    get_worker_context,
    set_http_context,
    set_worker_context,
)


@pytest.fixture(autouse=True)
def _clean_contexts():
    """Reset both context vars before and after each test."""
    clear_http_context()
    clear_worker_context()
    yield
    clear_http_context()
    clear_worker_context()


class TestScopeId:
    def test_scope_id_with_both(self):
        ctx = HttpRequestContext(user_id="u1", repo="org/repo")
        assert ctx.scope_id == "u1:org/repo"

    def test_scope_id_missing_repo(self):
        ctx = HttpRequestContext(user_id="u1")
        assert ctx.scope_id is None

    def test_scope_id_missing_user(self):
        ctx = HttpRequestContext(user_id="", repo="org/repo")
        assert ctx.scope_id is None


class TestEffectiveContext:
    def test_http_context_overrides_worker(self):
        http_ctx = HttpRequestContext(user_id="http_user", repo="org/repo1")
        worker_ctx = HttpRequestContext(user_id="worker_user", repo="org/repo2")
        set_http_context(http_ctx)
        set_worker_context(worker_ctx)

        result = get_effective_context()
        assert result is http_ctx
        assert result.user_id == "http_user"

    def test_worker_context_used_when_no_http(self):
        worker_ctx = HttpRequestContext(user_id="worker_user", repo="org/repo")
        set_worker_context(worker_ctx)

        result = get_effective_context()
        assert result is worker_ctx
        assert result.user_id == "worker_user"

    def test_none_when_no_context(self):
        assert get_effective_context() is None

    def test_worker_context_lifecycle(self):
        ctx = HttpRequestContext(user_id="u1", repo="org/repo")
        set_worker_context(ctx)
        assert get_worker_context() is ctx
        clear_worker_context()
        assert get_worker_context() is None
