"""Tests for MCP context validation error guidance."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from watercooler_mcp import validation


def test_hosted_require_context_without_http_context_mentions_session_headers() -> None:
    """No HTTP context means users need session/user and repo headers."""
    with patch.object(validation, "is_hosted_mode", return_value=True), patch.object(
        validation,
        "get_effective_context",
        return_value=None,
    ):
        error, context = validation._require_context("")

    assert context is None
    assert error is not None
    assert "HTTP context not available" in error
    assert "X-User-ID" in error
    assert "X-Repo" in error


def test_hosted_require_context_without_repo_mentions_repo_configuration() -> None:
    """Existing HTTP context without repo should point at X-Repo/proxy_repo."""
    http_ctx = MagicMock()
    http_ctx.repo = None

    with patch.object(validation, "is_hosted_mode", return_value=True), patch.object(
        validation,
        "get_effective_context",
        return_value=http_ctx,
    ):
        error, context = validation._require_context("")

    assert context is None
    assert error is not None
    assert "repository context not available" in error
    assert "X-Repo" in error
    assert "proxy_repo" in error
