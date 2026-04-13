"""Tests for role validation at write boundaries.

Covers:
- MCP _say_impl rejecting / accepting / passing through roles
- commands_graph.append_entry rejecting invalid roles
- commands_graph.say accepting a project custom role when code_root is given
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import server, validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.tools.thread_write import _say_impl


# ============================================================================
# Fixtures (minimal subset of test_mcp_thread_write.py pattern)
# ============================================================================


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def mock_context(tmp_path, threads_dir):
    return ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        code_repo="test-org/test-repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=True,
    )


@pytest.fixture
def patched_context(mock_context, monkeypatch):
    def fake_require_context(code_path: str):
        return (None, mock_context)

    monkeypatch.setattr(validation, "_require_context", fake_require_context)
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda ctx: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda ctx: None)

    def fake_run_with_sync(context, msg, operation, **kwargs):
        operation()

    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.run_with_sync",
        fake_run_with_sync,
    )
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_bot_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_hosted_context", lambda ctx: False)
    return mock_context


@pytest.fixture
def mcp_ctx():
    ctx = MagicMock()
    ctx.client_id = "test-client"
    return ctx


# ============================================================================
# MCP _say_impl role validation tests
# ============================================================================


def test_say_rejects_invalid_role(patched_context, mcp_ctx):
    """_say_impl with role='jay' returns an error string listing valid roles."""
    result = _say_impl(
        topic="test-topic",
        title="Test",
        body="body",
        ctx=mcp_ctx,
        role="jay",
        code_path=".",
        agent_func="Claude Code:sonnet-4:implementer",
    )
    assert "jay" in result
    assert "Invalid role" in result or "invalid" in result.lower()
    # Should list valid roles
    assert "critic" in result or "implementer" in result


def test_say_accepts_valid_role(patched_context, threads_dir, mcp_ctx):
    """_say_impl with role='critic' succeeds (creates thread + entry)."""
    from watercooler.baseline_graph.writer import init_thread_in_graph
    init_thread_in_graph(threads_dir, "test-topic", title="T", status="OPEN", ball="Claude (user)")

    result = _say_impl(
        topic="test-topic",
        title="Test",
        body="body",
        ctx=mcp_ctx,
        role="critic",
        code_path=".",
        agent_func="Claude Code:sonnet-4:critic",
    )
    assert "Entry added" in result or "entry" in result.lower()


def test_say_accepts_none_role(patched_context, threads_dir, mcp_ctx):
    """_say_impl with the default role (implementer) is not rejected by validation."""
    from watercooler.baseline_graph.writer import init_thread_in_graph
    init_thread_in_graph(threads_dir, "test-topic", title="T", status="OPEN", ball="Claude (user)")

    result = _say_impl(
        topic="test-topic",
        title="Test",
        body="body",
        ctx=mcp_ctx,
        # role omitted → defaults to "implementer"
        code_path=".",
        agent_func="Claude Code:sonnet-4:implementer",
    )
    assert "Entry added" in result or "entry" in result.lower()


# ============================================================================
# commands_graph.append_entry validation tests
# ============================================================================


def test_commands_graph_append_entry_rejects_invalid_role(tmp_path):
    """commands_graph.append_entry with role='jay' raises ValueError before writing."""
    from watercooler.commands_graph import append_entry
    from watercooler.baseline_graph.writer import init_thread_in_graph

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    init_thread_in_graph(threads_dir, "test-topic", title="T", status="OPEN", ball="Claude (user)")

    with pytest.raises(ValueError, match="jay"):
        append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="TestAgent",
            role="jay",
            title="Test",
            body="body",
            entry_id="01TEST00000000000000000001",
        )


def test_commands_graph_custom_role_accepts_code_root(tmp_path):
    """append_entry accepts a project custom role when code_root points at that repo."""
    from watercooler.commands_graph import append_entry
    from watercooler.baseline_graph.writer import init_thread_in_graph

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    init_thread_in_graph(threads_dir, "test-topic", title="T", status="OPEN", ball="Claude (user)")

    # Create a project roles.toml with a custom role
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.reviewer]\ndescription = "Custom reviewer"\ncanonical_role = "critic"\n'
    )

    # Should not raise
    append_entry(
        "test-topic",
        threads_dir=threads_dir,
        agent="TestAgent",
        role="reviewer",
        title="Test",
        body="body",
        entry_id="01TEST00000000000000000002",
        code_root=tmp_path,
    )
