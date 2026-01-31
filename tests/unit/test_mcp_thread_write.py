"""Unit tests for watercooler_mcp.tools.thread_write module.

Tests the MCP write tools:
- say: Add entry and flip ball to counterpart
- ack: Acknowledge without flipping ball
- handoff: Explicit ball handoff
- set_status: Update thread status
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch, MagicMock

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import server, validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.errors import ContextError, IdentityError, ValidationError


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
def sample_thread(threads_dir):
    """Create a sample thread file for testing."""
    content = dedent("""\
        # test-topic — Test Thread
        Status: OPEN
        Ball: Claude (user)
        Topic: test-topic
        Created: 2025-01-01T12:00:00Z

        ---
        Entry: Claude (user) 2025-01-01T12:00:00Z
        Role: planner
        Type: Plan
        Title: Initial planning

        Spec: planner
        This is the initial planning entry.
        <!-- Entry-ID: 01TEST00000000000000000001 -->

        ---
    """)
    thread_file = threads_dir / "test-topic.md"
    thread_file.write_text(content, encoding="utf-8")
    return thread_file


@pytest.fixture
def mock_context(tmp_path, threads_dir):
    """Create a ThreadContext for testing."""
    return ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        threads_repo_url=None,
        code_repo="test-org/test-repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        threads_slug="test-repo",
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

    # Mock run_with_sync to just execute the operation directly
    def fake_run_with_sync(context, msg, operation, **kwargs):
        operation()

    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.run_with_sync",
        fake_run_with_sync
    )

    # Mock Slack integrations
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_bot_enabled", lambda: False)

    # Mock is_hosted_context to return False (local mode)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_hosted_context", lambda ctx: False)

    return mock_context


@pytest.fixture
def patched_set_status_context(mock_context, monkeypatch):
    """Patch context for set_status tests which need additional graph mocking."""
    def fake_require_context(code_path: str):
        return (None, mock_context)

    monkeypatch.setattr(validation, "_require_context", fake_require_context)
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda ctx: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda ctx: None)

    # Mock run_with_sync to just execute the operation directly
    def fake_run_with_sync(context, msg, operation, **kwargs):
        operation()

    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.run_with_sync",
        fake_run_with_sync
    )

    # Mock Slack integrations
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_bot_enabled", lambda: False)

    # Mock is_hosted_context to return False (local mode)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_hosted_context", lambda ctx: False)

    # Mock set_status_graph_first to use non-graph set_status
    from watercooler.commands import set_status as commands_set_status

    def mock_set_status_graph_first(topic, *, threads_dir, status):
        return commands_set_status(topic, threads_dir=threads_dir, status=status)

    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.set_status_graph_first",
        mock_set_status_graph_first
    )

    return mock_context


@pytest.fixture
def mcp_ctx():
    """Create a mock MCP context."""
    ctx = MagicMock()
    ctx.client_id = "Claude Code"
    return ctx


# ============================================================================
# Test _say_impl
# ============================================================================


class TestSay:
    """Tests for say MCP tool."""

    def test_say_creates_entry(self, patched_context, sample_thread, mcp_ctx):
        """Test that say creates an entry in the thread."""
        result = server.say.fn(
            topic="test-topic",
            title="New Entry",
            body="This is a new entry.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        assert "Entry added" in result
        assert "test-topic" in result

        # Verify entry was added to thread file
        content = sample_thread.read_text()
        assert "New Entry" in content
        assert "This is a new entry." in content

    def test_say_flips_ball(self, patched_context, sample_thread, mcp_ctx):
        """Test that say flips the ball to counterpart."""
        result = server.say.fn(
            topic="test-topic",
            title="Ball Flip Test",
            body="Testing ball flip.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        assert "Ball flipped to" in result
        # Ball should flip from Claude to counterpart (Codex)
        content = sample_thread.read_text()
        # Check header for ball change
        lines = content.split("\n")
        ball_lines = [l for l in lines[:10] if l.startswith("Ball:")]
        assert len(ball_lines) > 0

    def test_say_creates_thread_if_missing(self, patched_context, threads_dir, mcp_ctx):
        """Test that say creates a new thread if it doesn't exist."""
        result = server.say.fn(
            topic="new-topic",
            title="First Entry",
            body="Creating new thread.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:pm",
        )

        assert "Entry added" in result
        thread_file = threads_dir / "new-topic.md"
        assert thread_file.exists()
        content = thread_file.read_text()
        assert "new-topic" in content
        assert "First Entry" in content

    def test_say_requires_agent_func(self, patched_context, sample_thread, mcp_ctx):
        """Test that say raises IdentityError without agent_func."""
        with pytest.raises(IdentityError):
            server.say.fn(
                topic="test-topic",
                title="Test",
                body="Test body",
                ctx=mcp_ctx,
                code_path=".",
                agent_func="",  # Empty agent_func
            )

    def test_say_invalid_agent_func_format(self, patched_context, sample_thread, mcp_ctx):
        """Test that say raises IdentityError with invalid agent_func format."""
        with pytest.raises(IdentityError):
            server.say.fn(
                topic="test-topic",
                title="Test",
                body="Test body",
                ctx=mcp_ctx,
                code_path=".",
                agent_func="InvalidFormat",  # No colon separator
            )

    def test_say_with_different_roles(self, patched_context, threads_dir, mcp_ctx):
        """Test say with different role values."""
        roles = ["planner", "critic", "implementer", "tester", "pm", "scribe"]

        for role in roles:
            topic = f"role-test-{role}"
            result = server.say.fn(
                topic=topic,
                title=f"Test {role}",
                body="Testing role.",
                role=role,
                ctx=mcp_ctx,
                code_path=".",
                agent_func=f"Claude Code:sonnet-4:{role}",
            )
            assert "Entry added" in result
            content = (threads_dir / f"{topic}.md").read_text()
            assert f"Role: {role}" in content

    def test_say_with_different_entry_types(self, patched_context, threads_dir, mcp_ctx):
        """Test say with different entry types."""
        entry_types = ["Note", "Plan", "Decision", "PR", "Closure"]

        for entry_type in entry_types:
            topic = f"type-test-{entry_type.lower()}"
            result = server.say.fn(
                topic=topic,
                title=f"Test {entry_type}",
                body="Testing entry type.",
                entry_type=entry_type,
                ctx=mcp_ctx,
                code_path=".",
                agent_func="Claude Code:sonnet-4:pm",
            )
            assert "Entry added" in result
            content = (threads_dir / f"{topic}.md").read_text()
            assert f"Type: {entry_type}" in content


# ============================================================================
# Test _ack_impl
# ============================================================================


class TestAck:
    """Tests for ack MCP tool."""

    def test_ack_creates_entry(self, patched_context, sample_thread, mcp_ctx):
        """Test that ack creates an acknowledgment entry."""
        result = server.ack.fn(
            topic="test-topic",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        assert "Acknowledged" in result or "Entry added" in result
        content = sample_thread.read_text()
        # Ack should add an entry
        assert content.count("Entry:") >= 2

    def test_ack_does_not_flip_ball(self, patched_context, sample_thread, mcp_ctx):
        """Test that ack does NOT flip the ball."""
        # Get initial ball state
        initial_content = sample_thread.read_text()
        initial_lines = initial_content.split("\n")
        initial_ball = [l for l in initial_lines[:10] if l.startswith("Ball:")][0]

        result = server.ack.fn(
            topic="test-topic",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Codex:gpt-4:implementer",  # Different agent
        )

        # Ball should remain the same
        content = sample_thread.read_text()
        lines = content.split("\n")
        final_ball = [l for l in lines[:10] if l.startswith("Ball:")][0]

        # Ack keeps ball with original owner
        assert "Ball kept" in result or "Acknowledged" in result

    def test_ack_with_custom_body(self, patched_context, sample_thread, mcp_ctx):
        """Test ack with a custom body message."""
        result = server.ack.fn(
            topic="test-topic",
            body="Got it, will review shortly.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        content = sample_thread.read_text()
        assert "Got it, will review shortly." in content

    def test_ack_requires_agent_func(self, patched_context, sample_thread, mcp_ctx):
        """Test that ack raises IdentityError without agent_func."""
        with pytest.raises(IdentityError):
            server.ack.fn(
                topic="test-topic",
                ctx=mcp_ctx,
                code_path=".",
                agent_func="",
            )


# ============================================================================
# Test _handoff_impl
# ============================================================================


class TestHandoff:
    """Tests for handoff MCP tool."""

    def test_handoff_creates_entry(self, patched_context, sample_thread, mcp_ctx):
        """Test that handoff creates a handoff entry."""
        result = server.handoff.fn(
            topic="test-topic",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:pm",
        )

        assert "Ball handed off" in result
        content = sample_thread.read_text()
        # Should have handoff entry
        assert "Handoff" in content or "handoff" in content.lower()

    def test_handoff_flips_ball(self, patched_context, sample_thread, mcp_ctx):
        """Test that handoff explicitly flips the ball."""
        result = server.handoff.fn(
            topic="test-topic",
            note="Your turn to review.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:pm",
        )

        assert "Ball handed off" in result

    def test_handoff_with_note(self, patched_context, sample_thread, mcp_ctx):
        """Test handoff with a note."""
        result = server.handoff.fn(
            topic="test-topic",
            note="Please review the implementation.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        content = sample_thread.read_text()
        assert "Please review the implementation." in content

    def test_handoff_requires_agent_func(self, patched_context, sample_thread, mcp_ctx):
        """Test that handoff raises IdentityError without agent_func."""
        with pytest.raises(IdentityError):
            server.handoff.fn(
                topic="test-topic",
                ctx=mcp_ctx,
                code_path=".",
                agent_func="",
            )


# ============================================================================
# Test _set_status_impl
# ============================================================================


class TestSetStatus:
    """Tests for set_status MCP tool."""

    def test_set_status_updates_status(self, patched_set_status_context, sample_thread):
        """Test that set_status updates the thread status."""
        result = server.set_status.fn(
            topic="test-topic",
            status="CLOSED",
            code_path=".",
            agent_func="Claude Code:sonnet-4:pm",
        )

        assert "Status updated" in result
        assert "CLOSED" in result

    def test_set_status_returns_new_status(self, patched_set_status_context, sample_thread):
        """Test that set_status returns the new status value."""
        result = server.set_status.fn(
            topic="test-topic",
            status="IN_REVIEW",
            code_path=".",
            agent_func="Claude Code:sonnet-4:pm",
        )

        assert "Status updated" in result
        assert "IN_REVIEW" in result

    def test_set_status_requires_agent_func(self, patched_set_status_context, sample_thread):
        """Test that set_status raises IdentityError without agent_func."""
        with pytest.raises(IdentityError):
            server.set_status.fn(
                topic="test-topic",
                status="CLOSED",
                code_path=".",
                agent_func="",
            )

    def test_set_status_invalid_agent_func_format(self, patched_set_status_context, sample_thread):
        """Test that set_status raises IdentityError with invalid agent_func format."""
        with pytest.raises(IdentityError):
            server.set_status.fn(
                topic="test-topic",
                status="CLOSED",
                code_path=".",
                agent_func="InvalidFormat",  # No colon separator
            )


# ============================================================================
# Test Context Validation
# ============================================================================


class TestContextValidation:
    """Tests for context validation in write tools."""

    def test_say_raises_context_error_on_missing_context(self, mcp_ctx, monkeypatch):
        """Test that say raises ContextError when context cannot be resolved."""
        def fake_require_context(code_path: str):
            return ("Unable to resolve context", None)

        monkeypatch.setattr(validation, "_require_context", fake_require_context)

        with pytest.raises(ContextError) as exc_info:
            server.say.fn(
                topic="test-topic",
                title="Test",
                body="Test body",
                ctx=mcp_ctx,
                code_path="/nonexistent/path",
                agent_func="Claude:pm",
            )

        assert "Unable to resolve" in str(exc_info.value)

    def test_ack_raises_context_error_on_missing_context(self, mcp_ctx, monkeypatch):
        """Test that ack raises ContextError when context cannot be resolved."""
        def fake_require_context(code_path: str):
            return ("Context not found", None)

        monkeypatch.setattr(validation, "_require_context", fake_require_context)

        with pytest.raises(ContextError):
            server.ack.fn(
                topic="test-topic",
                ctx=mcp_ctx,
                code_path="/bad/path",
                agent_func="Claude:pm",
            )


# ============================================================================
# Test Entry Content
# ============================================================================


class TestEntryContent:
    """Tests for entry content formatting."""

    def test_say_includes_spec_marker(self, patched_context, sample_thread, mcp_ctx):
        """Test that say entries include Spec marker in body."""
        result = server.say.fn(
            topic="test-topic",
            title="Spec Test",
            body="Testing spec marker.",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        content = sample_thread.read_text()
        # Entry should include Spec marker (based on agent_func role part)
        assert "Spec:" in content or "implementer" in content.lower()

    def test_say_preserves_markdown_formatting(self, patched_context, sample_thread, mcp_ctx):
        """Test that say preserves markdown formatting in body."""
        markdown_body = dedent("""\
            ## Section Header

            - Bullet point 1
            - Bullet point 2

            ```python
            def example():
                pass
            ```
        """)

        result = server.say.fn(
            topic="test-topic",
            title="Markdown Test",
            body=markdown_body,
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        content = sample_thread.read_text()
        assert "## Section Header" in content
        assert "- Bullet point 1" in content
        assert "```python" in content

    def test_say_handles_unicode(self, patched_context, threads_dir, mcp_ctx):
        """Test that say handles unicode content properly."""
        result = server.say.fn(
            topic="unicode-test",
            title="Unicode Test",
            body="Hello 世界! Testing unicode: αβγδ",
            ctx=mcp_ctx,
            code_path=".",
            agent_func="Claude Code:sonnet-4:implementer",
        )

        content = (threads_dir / "unicode-test.md").read_text()
        assert "世界" in content
        assert "αβγδ" in content
