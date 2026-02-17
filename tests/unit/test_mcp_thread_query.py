"""Unit tests for watercooler_mcp.tools.thread_query module.

Tests the MCP query tools, focusing on list_threads which is not
covered by test_mcp_entry_tools.py.

Other entry-level tools (read_thread, list_thread_entries, get_thread_entry,
get_thread_entry_range) are tested in test_mcp_entry_tools.py.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import server, validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.errors import ContextError, ValidationError


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def threads_dir(tmp_path):
    """Create a temporary threads directory."""
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def sample_threads(threads_dir):
    """Create multiple sample thread files for testing."""
    # Thread 1: Ball on Claude, OPEN
    thread1 = dedent("""\
        # feature-auth — Authentication Feature
        Status: OPEN
        Ball: Claude (user)
        Topic: feature-auth
        Created: 2025-01-01T12:00:00Z

        ---
        Entry: Claude (user) 2025-01-01T12:00:00Z
        Role: planner
        Type: Plan
        Title: Initial planning

        Spec: planner
        Starting auth feature work.
        <!-- Entry-ID: 01TEST00000000000000000001 -->

        ---
    """)
    (threads_dir / "feature-auth.md").write_text(thread1, encoding="utf-8")

    # Thread 2: Ball on Codex, OPEN
    thread2 = dedent("""\
        # bug-fix — Bug Fix
        Status: OPEN
        Ball: Codex (user)
        Topic: bug-fix
        Created: 2025-01-02T10:00:00Z

        ---
        Entry: Codex (user) 2025-01-02T10:00:00Z
        Role: implementer
        Type: Note
        Title: Working on fix

        Spec: implementer
        Fixing the bug.
        <!-- Entry-ID: 01TEST00000000000000000002 -->

        ---
    """)
    (threads_dir / "bug-fix.md").write_text(thread2, encoding="utf-8")

    # Thread 3: CLOSED
    thread3 = dedent("""\
        # old-feature — Old Feature
        Status: CLOSED
        Ball: Claude (user)
        Topic: old-feature
        Created: 2025-01-01T08:00:00Z

        ---
        Entry: Claude (user) 2025-01-01T08:00:00Z
        Role: pm
        Type: Closure
        Title: Feature complete

        Spec: pm
        All done.
        <!-- Entry-ID: 01TEST00000000000000000003 -->

        ---
    """)
    (threads_dir / "old-feature.md").write_text(thread3, encoding="utf-8")

    return threads_dir


@pytest.fixture
def mock_context(tmp_path, threads_dir):
    """Create a ThreadContext for testing."""
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
    """Patch validation to return our mock context."""
    def fake_require_context(code_path: str):
        return (None, mock_context)

    monkeypatch.setattr(validation, "_require_context", fake_require_context)
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda ctx: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda ctx: None)

    # Mock is_hosted_context to return False (local mode)
    monkeypatch.setattr("watercooler_mcp.tools.thread_query.is_hosted_context", lambda ctx: False)

    # Mock ensure_readable (sync function)
    monkeypatch.setattr("watercooler_mcp.tools.thread_query.ensure_readable", lambda *args, **kwargs: (True, []))

    # Mock graph functions to use markdown fallback
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_query._list_threads",
        _mock_list_threads_from_markdown
    )

    return mock_context


def _mock_list_threads_from_markdown(threads_dir, open_only=None, agent=None):
    """Mock implementation that reads from markdown files."""
    from watercooler.thread_entries import parse_thread_header

    threads = []
    for md_file in threads_dir.glob("*.md"):
        if md_file.name.startswith(".") or md_file.name == "index.md":
            continue

        title, status, ball, updated = parse_thread_header(md_file)

        status = status.upper()
        if open_only is True and status != "OPEN":
            continue
        if open_only is False and status == "OPEN":
            continue

        threads.append((title, status, ball, updated, md_file, False, "", 0))

    return threads


@pytest.fixture
def mcp_ctx():
    """Create a mock MCP context."""
    ctx = MagicMock()
    ctx.client_id = "Claude Code"
    return ctx


# ============================================================================
# Test list_threads
# ============================================================================


class TestListThreads:
    """Tests for list_threads MCP tool."""

    def test_list_threads_returns_all(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads returns all threads."""
        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        # Extract text from ToolResult
        text = result.content[0].text

        assert "Watercooler Threads" in text
        # Should show all 3 threads
        assert "feature-auth" in text
        assert "bug-fix" in text
        assert "old-feature" in text

    def test_list_threads_open_only(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads filters to open threads only."""
        result = server.list_threads.fn(
            mcp_ctx,
            open_only=True,
            code_path=".",
        )

        text = result.content[0].text

        # Should show open threads
        assert "feature-auth" in text
        assert "bug-fix" in text
        # Should NOT show closed thread
        assert "old-feature" not in text

    def test_list_threads_closed_only(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads filters to closed threads only."""
        result = server.list_threads.fn(
            mcp_ctx,
            open_only=False,
            code_path=".",
        )

        text = result.content[0].text

        # Should show closed thread
        assert "old-feature" in text
        # Should NOT show open threads
        assert "feature-auth" not in text
        assert "bug-fix" not in text

    def test_list_threads_shows_agent_name(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads shows current agent name."""
        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        text = result.content[0].text
        assert "You are:" in text

    def test_list_threads_shows_threads_dir(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads shows threads directory."""
        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        text = result.content[0].text
        assert "Threads dir:" in text

    def test_list_threads_empty_directory(self, patched_context, threads_dir, mcp_ctx):
        """Test list_threads with empty threads directory."""
        # Remove all thread files
        for md_file in threads_dir.glob("*.md"):
            md_file.unlink()

        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        text = result.content[0].text
        # Should indicate no threads found
        assert "no" in text.lower() and "threads" in text.lower()
        assert "found" in text.lower()

    def test_list_threads_json_format(self, patched_context, sample_threads, mcp_ctx):
        """Test that list_threads supports JSON format."""
        result = server.list_threads.fn(
            mcp_ctx,
            format="json",
            code_path=".",
        )
        import json
        text = result.content[0].text
        payload = json.loads(text)
        assert "threads" in payload
        assert "total" in payload
        assert payload["total"] > 0
        for t in payload["threads"]:
            assert "topic" in t
            assert "entry_count" in t
            assert "summary" in t


# ============================================================================
# Test Context Validation
# ============================================================================


class TestListThreadsContextValidation:
    """Tests for context validation in list_threads."""

    def test_list_threads_raises_context_error(self, mcp_ctx, monkeypatch):
        """Test that list_threads raises ContextError when context cannot be resolved."""
        def fake_require_context(code_path: str):
            return ("Unable to resolve context", None)

        monkeypatch.setattr(validation, "_require_context", fake_require_context)

        with pytest.raises(ContextError) as exc_info:
            server.list_threads.fn(
                mcp_ctx,
                code_path="/nonexistent/path",
            )

        assert "Unable to resolve" in str(exc_info.value)


# ============================================================================
# Test Thread Classification (ball ownership)
# ============================================================================


class TestThreadClassification:
    """Tests for thread classification by ball ownership."""

    def test_threads_classified_by_ball(self, patched_context, sample_threads, mcp_ctx, monkeypatch):
        """Test that threads are classified by ball ownership."""
        # Set agent name to Claude to test "Your Turn" classification
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_query.get_agent_name",
            lambda client_id: "Claude"
        )

        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        text = result.content[0].text

        # Check sections exist
        # Note: The agent name comparison is case-insensitive, so threads with
        # ball matching agent should appear in "Your Turn" section
        assert "Your Turn" in text or "Waiting on Others" in text


# ============================================================================
# Test Edge Cases and Error Handling
# ============================================================================


class TestMalformedContent:
    """Tests for handling malformed or corrupted thread files."""

    def test_list_threads_handles_malformed_thread(self, patched_context, threads_dir, mcp_ctx):
        """Test that list_threads handles malformed thread files gracefully."""
        # Create a malformed thread file (no proper headers)
        malformed = threads_dir / "broken.md"
        malformed.write_text("Not a valid thread\nNo proper headers\nJust plain text")

        # Should not crash - either skips or uses defaults
        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        text = result.content[0].text
        # Should either include it with defaults or handle gracefully
        assert "Watercooler" in text or "No" in text

    def test_list_threads_handles_empty_file(self, patched_context, threads_dir, mcp_ctx):
        """Test that list_threads handles empty thread files."""
        empty_file = threads_dir / "empty.md"
        empty_file.write_text("")

        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        # Should not crash
        text = result.content[0].text
        assert isinstance(text, str)

    def test_list_threads_handles_special_characters(self, patched_context, threads_dir, mcp_ctx):
        """Test that list_threads handles files with special characters in content."""
        special_file = threads_dir / "special.md"
        # Write content with special but valid UTF-8 characters
        special_file.write_text("# special-thread\nStatus: OPEN\nBall: Agent\n\n---\nSpecial: <>&\"'")

        result = server.list_threads.fn(
            mcp_ctx,
            code_path=".",
        )

        # Should handle gracefully
        assert result.content is not None


class TestPathTraversalSecurity:
    """Tests for path traversal prevention in topic names."""

    def test_topic_with_path_traversal_stays_in_threads_dir(self, threads_dir):
        """Test that path traversal attempts in topics stay within threads_dir."""
        from watercooler.fs import thread_path

        # Attempt path traversal
        malicious_topic = "../../../etc/passwd"
        result_path = thread_path(malicious_topic, threads_dir)

        # Key security check: file must be within threads_dir
        assert result_path.parent == threads_dir, "File must stay in threads_dir"
        # Path traversal characters should be sanitized out
        assert ".." not in result_path.name, ".. should be removed from filename"

    def test_topic_with_absolute_path_stays_in_threads_dir(self, threads_dir):
        """Test that absolute paths in topics stay within threads_dir."""
        from watercooler.fs import thread_path

        malicious_topic = "/etc/passwd"
        result_path = thread_path(malicious_topic, threads_dir)

        # Key security check: file must be within threads_dir
        assert result_path.parent == threads_dir, "File must stay in threads_dir"

    def test_topic_with_null_bytes_is_sanitized(self, threads_dir):
        """Test that null bytes in topics are handled."""
        from watercooler.fs import thread_path

        malicious_topic = "test\x00topic"
        result_path = thread_path(malicious_topic, threads_dir)

        # Should not contain null bytes in path
        assert "\x00" not in str(result_path)
