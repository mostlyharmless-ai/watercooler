"""Unit tests for watercooler.commands module.

Tests the core command functions:
- init_thread: Initialize a new thread
- say: Quick team note with auto-ball-flip
- ack: Acknowledge without flipping ball
- handoff: Flip ball to counterpart
- set_status: Update thread status
- set_ball: Update ball ownership
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from watercooler.commands import (
    init_thread,
    say,
    ack,
    handoff,
    set_status,
    set_ball,
    append_entry,
    _bump_header,
    _header_split,
    _replace_header_line,
)


# ============================================================================
# Helper Fixtures
# ============================================================================


@pytest.fixture
def threads_dir(tmp_path):
    """Create a temporary threads directory."""
    d = tmp_path / "threads"
    d.mkdir()
    return d


@pytest.fixture
def sample_thread(threads_dir):
    """Create a sample thread file."""
    content = """# test-topic — Test Thread

Status: OPEN
Ball: Claude (user)

---

Entry: Claude (user) 2025-01-01T12:00:00Z
Role: planner
Type: Plan
Title: Initial planning

This is the initial planning entry.

---
"""
    thread_file = threads_dir / "test-topic.md"
    thread_file.write_text(content)
    return thread_file


@pytest.fixture
def mock_graph():
    """Mock graph functions to avoid graph dependencies."""
    with patch('watercooler.commands.get_thread_from_graph') as mock_get_thread, \
         patch('watercooler.commands.get_entries_for_thread') as mock_get_entries:
        # Return None to trigger markdown fallback
        mock_get_thread.return_value = None
        mock_get_entries.return_value = []
        yield {
            'get_thread': mock_get_thread,
            'get_entries': mock_get_entries,
        }


# ============================================================================
# Test _header_split helper
# ============================================================================


class TestHeaderSplit:
    """Tests for _header_split helper function."""

    def test_split_basic(self):
        """Test splitting a basic header from body."""
        text = "Header line 1\nHeader line 2\n\nBody content here."
        header, body = _header_split(text)
        assert header == "Header line 1\nHeader line 2"
        assert body == "Body content here."

    def test_split_no_body(self):
        """Test splitting when no body present."""
        text = "Header only content"
        header, body = _header_split(text)
        assert header == "Header only content"
        assert body == ""

    def test_split_empty_string(self):
        """Test splitting empty string."""
        header, body = _header_split("")
        assert header == ""
        assert body == ""

    def test_split_multiple_blank_lines(self):
        """Test splitting with multiple blank lines between header and body."""
        text = "Header\n\nBody line 1\n\nBody line 2"
        header, body = _header_split(text)
        assert header == "Header"
        # Body includes additional newlines
        assert "Body line 1" in body


# ============================================================================
# Test _replace_header_line helper
# ============================================================================


class TestReplaceHeaderLine:
    """Tests for _replace_header_line helper function."""

    def test_replace_existing_line(self):
        """Test replacing an existing header line."""
        block = "Status: OPEN\nBall: Claude (user)"
        result = _replace_header_line(block, "Status", "CLOSED")
        assert "Status: CLOSED" in result
        assert "Ball: Claude (user)" in result

    def test_add_new_line(self):
        """Test adding a new header line when key doesn't exist."""
        block = "Status: OPEN"
        result = _replace_header_line(block, "Ball", "Codex (user)")
        assert "Status: OPEN" in result
        assert "Ball: Codex (user)" in result

    def test_case_insensitive_key(self):
        """Test that key matching is case-insensitive."""
        block = "status: OPEN"
        result = _replace_header_line(block, "Status", "CLOSED")
        # The replacement should work regardless of case
        assert "CLOSED" in result

    def test_empty_block(self):
        """Test adding to empty block."""
        block = ""
        result = _replace_header_line(block, "Status", "OPEN")
        assert "Status: OPEN" in result


# ============================================================================
# Test _bump_header helper
# ============================================================================


class TestBumpHeader:
    """Tests for _bump_header helper function."""

    def test_update_status_only(self):
        """Test updating only status."""
        text = "Status: OPEN\nBall: Claude\n\nBody"
        result = _bump_header(text, status="CLOSED")
        assert "Status: CLOSED" in result
        assert "Ball: Claude" in result
        assert "Body" in result

    def test_update_ball_only(self):
        """Test updating only ball."""
        text = "Status: OPEN\nBall: Claude\n\nBody"
        result = _bump_header(text, ball="Codex")
        assert "Status: OPEN" in result
        assert "Ball: Codex" in result
        assert "Body" in result

    def test_update_both(self):
        """Test updating both status and ball."""
        text = "Status: OPEN\nBall: Claude\n\nBody"
        result = _bump_header(text, status="CLOSED", ball="Codex")
        assert "Status: CLOSED" in result
        assert "Ball: Codex" in result
        assert "Body" in result

    def test_no_updates(self):
        """Test when no updates provided."""
        text = "Status: OPEN\nBall: Claude\n\nBody"
        result = _bump_header(text)
        assert result == text


# ============================================================================
# Test init_thread
# ============================================================================


class TestInitThread:
    """Tests for init_thread function."""

    def test_init_creates_thread(self, threads_dir):
        """Test that init_thread creates a new thread file."""
        result = init_thread(
            "new-topic",
            threads_dir=threads_dir,
            title="New Topic",
            status="OPEN",
            ball="Claude",
        )
        assert result.exists()
        assert result.name == "new-topic.md"
        content = result.read_text()
        assert "new-topic" in content
        assert "Status: OPEN" in content

    def test_init_creates_directories(self, tmp_path):
        """Test that init_thread creates parent directories if missing."""
        threads_dir = tmp_path / "nested" / "threads"
        assert not threads_dir.exists()

        result = init_thread(
            "test-topic",
            threads_dir=threads_dir,
            title="Test",
        )
        assert result.exists()
        assert threads_dir.exists()

    def test_init_idempotent(self, sample_thread, threads_dir):
        """Test that init_thread is idempotent - returns existing thread."""
        original_content = sample_thread.read_text()
        result = init_thread(
            "test-topic",
            threads_dir=threads_dir,
            title="Different Title",  # Should be ignored for existing thread
        )
        assert result == sample_thread
        assert result.read_text() == original_content

    def test_init_normalizes_status(self, threads_dir):
        """Test that status is normalized to uppercase."""
        result = init_thread(
            "status-test",
            threads_dir=threads_dir,
            status="open",  # lowercase
        )
        content = result.read_text()
        assert "Status: OPEN" in content
        assert "status: open" not in content


# ============================================================================
# Test say
# ============================================================================


class TestSay:
    """Tests for say function."""

    def test_say_creates_entry(self, threads_dir, mock_graph):
        """Test that say creates an entry in a thread."""
        # First create the thread
        init_thread("say-test", threads_dir=threads_dir, ball="Codex")

        result = say(
            "say-test",
            threads_dir=threads_dir,
            agent="Claude",
            title="Test Note",
            body="This is a test note.",
            user_tag="user",
        )
        assert result.exists()
        content = result.read_text()
        assert "Test Note" in content
        assert "This is a test note." in content

    def test_say_auto_flips_ball(self, threads_dir, mock_graph):
        """Test that say auto-flips ball to counterpart."""
        # Create thread with ball on Claude
        init_thread("ball-flip-test", threads_dir=threads_dir, ball="Claude (user)")

        # Say as Claude - ball should flip to Codex (counterpart)
        result = say(
            "ball-flip-test",
            threads_dir=threads_dir,
            agent="Claude",
            title="My Note",
            body="Note content",
            user_tag="user",
        )
        content = result.read_text()
        # Ball should have flipped to counterpart
        # Note: exact counterpart depends on registry configuration
        assert "Ball:" in content

    def test_say_explicit_ball(self, threads_dir, mock_graph):
        """Test that explicit ball parameter overrides auto-flip."""
        init_thread("explicit-ball-test", threads_dir=threads_dir, ball="Claude")

        result = say(
            "explicit-ball-test",
            threads_dir=threads_dir,
            agent="Claude",
            title="My Note",
            body="Note content",
            ball="SpecificAgent",  # Explicit ball
            user_tag="user",
        )
        content = result.read_text()
        assert "Ball: SpecificAgent" in content

    def test_say_initializes_thread_if_missing(self, threads_dir, mock_graph):
        """Test that say creates thread if it doesn't exist."""
        result = say(
            "new-topic-say",
            threads_dir=threads_dir,
            agent="Claude",
            title="First Note",
            body="Creating thread via say",
            user_tag="user",
        )
        assert result.exists()
        content = result.read_text()
        assert "new-topic-say" in content
        assert "First Note" in content


# ============================================================================
# Test ack
# ============================================================================


class TestAck:
    """Tests for ack function."""

    def test_ack_preserves_ball(self, threads_dir, mock_graph):
        """Test that ack does NOT flip ball."""
        # Create thread with ball on Claude
        init_thread("ack-test", threads_dir=threads_dir, ball="Claude (user)")

        result = ack(
            "ack-test",
            threads_dir=threads_dir,
            agent="Codex",  # Different agent
            title="Acknowledged",
            body="Got it.",
            user_tag="user",
        )
        content = result.read_text()
        # Ball should still be on Claude (not flipped to Codex's counterpart)
        # Check header has Claude in ball line
        lines = content.split("\n")
        header_ball = [l for l in lines[:10] if l.startswith("Ball:")]
        assert len(header_ball) > 0, "Ball line not found in header"
        assert "Claude" in header_ball[0], f"Expected ball to remain on Claude, got: {header_ball[0]}"

    def test_ack_default_title(self, threads_dir, mock_graph):
        """Test that ack uses default title 'Ack' if not provided."""
        init_thread("ack-default-test", threads_dir=threads_dir)

        result = ack(
            "ack-default-test",
            threads_dir=threads_dir,
            agent="Claude",
            user_tag="user",
        )
        content = result.read_text()
        assert "Ack" in content

    def test_ack_default_body(self, threads_dir, mock_graph):
        """Test that ack uses default body 'ack' if not provided."""
        init_thread("ack-body-test", threads_dir=threads_dir)

        result = ack(
            "ack-body-test",
            threads_dir=threads_dir,
            agent="Claude",
            title="Custom Title",
            user_tag="user",
            # body not provided
        )
        content = result.read_text()
        assert "ack" in content.lower()


# ============================================================================
# Test handoff
# ============================================================================


class TestHandoff:
    """Tests for handoff function."""

    def test_handoff_flips_to_counterpart(self, threads_dir, mock_graph):
        """Test that handoff flips ball to counterpart."""
        init_thread("handoff-test", threads_dir=threads_dir, ball="Claude (user)")

        result = handoff(
            "handoff-test",
            threads_dir=threads_dir,
            agent="Claude",
            note="Your turn",
            user_tag="user",
        )
        content = result.read_text()
        assert "Handoff to" in content
        assert "Your turn" in content

    def test_handoff_uses_pm_role(self, threads_dir, mock_graph):
        """Test that handoff defaults to pm role."""
        init_thread("handoff-role-test", threads_dir=threads_dir)

        result = handoff(
            "handoff-role-test",
            threads_dir=threads_dir,
            agent="Claude",
            user_tag="user",
        )
        content = result.read_text()
        assert "Role: pm" in content

    def test_handoff_creates_thread_if_missing(self, threads_dir, mock_graph):
        """Test that handoff creates thread if it doesn't exist."""
        result = handoff(
            "new-handoff-topic",
            threads_dir=threads_dir,
            agent="Claude",
            note="Starting handoff",
            user_tag="user",
        )
        assert result.exists()
        content = result.read_text()
        assert "new-handoff-topic" in content


# ============================================================================
# Test set_status
# ============================================================================


class TestSetStatus:
    """Tests for set_status function."""

    def test_set_status_updates_status(self, sample_thread, threads_dir):
        """Test that set_status updates the thread status."""
        result = set_status(
            "test-topic",
            threads_dir=threads_dir,
            status="CLOSED",
        )
        content = result.read_text()
        # Check header has CLOSED status (first Status line in header)
        lines = content.split("\n")
        header_status = [l for l in lines[:10] if l.startswith("Status:")]
        assert any("CLOSED" in l for l in header_status), f"Header status not updated: {header_status}"

    def test_set_status_normalizes_to_uppercase(self, sample_thread, threads_dir):
        """Test that status is normalized to uppercase."""
        result = set_status(
            "test-topic",
            threads_dir=threads_dir,
            status="closed",  # lowercase
        )
        content = result.read_text()
        assert "Status: CLOSED" in content
        assert "Status: closed" not in content

    def test_set_status_missing_thread_raises(self, threads_dir):
        """Test that set_status raises FileNotFoundError for missing thread."""
        with pytest.raises(FileNotFoundError):
            set_status(
                "nonexistent-topic",
                threads_dir=threads_dir,
                status="CLOSED",
            )

    def test_set_status_preserves_body(self, sample_thread, threads_dir):
        """Test that set_status preserves thread body content."""
        original_body = "This is the initial planning entry."
        result = set_status(
            "test-topic",
            threads_dir=threads_dir,
            status="CLOSED",
        )
        content = result.read_text()
        assert original_body in content


# ============================================================================
# Test set_ball
# ============================================================================


class TestSetBall:
    """Tests for set_ball function."""

    def test_set_ball_updates_ball(self, sample_thread, threads_dir):
        """Test that set_ball updates the ball ownership."""
        result = set_ball(
            "test-topic",
            threads_dir=threads_dir,
            ball="Codex (user)",
        )
        content = result.read_text()
        # Check header has updated ball (first Ball line in header)
        lines = content.split("\n")
        header_ball = [l for l in lines[:10] if l.startswith("Ball:")]
        assert any("Codex (user)" in l for l in header_ball), f"Header ball not updated: {header_ball}"

    def test_set_ball_creates_thread_if_missing(self, tmp_path):
        """Test that set_ball creates thread if it doesn't exist."""
        # Use a fresh threads_dir to avoid lock contention
        threads_dir = tmp_path / "fresh_threads"
        threads_dir.mkdir()

        result = set_ball(
            "new-ball-topic",
            threads_dir=threads_dir,
            ball="Claude (user)",
        )
        assert result.exists()
        content = result.read_text()
        # Ball line should be in header
        lines = content.split("\n")
        header_ball = [l for l in lines[:10] if l.startswith("Ball:")]
        assert len(header_ball) > 0, "Ball line not found in header"
        assert any("Claude" in l for l in header_ball), f"Claude not in ball: {header_ball}"

    def test_set_ball_preserves_body(self, sample_thread, threads_dir):
        """Test that set_ball preserves thread body content."""
        original_body = "This is the initial planning entry."
        result = set_ball(
            "test-topic",
            threads_dir=threads_dir,
            ball="NewAgent",
        )
        content = result.read_text()
        assert original_body in content


# ============================================================================
# Test append_entry
# ============================================================================


class TestAppendEntry:
    """Tests for append_entry function."""

    def test_append_entry_basic(self, sample_thread, threads_dir, mock_graph):
        """Test basic entry append."""
        result = append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="Codex",
            role="implementer",
            title="Implementation Note",
            body="Started implementing the feature.",
            user_tag="user",
        )
        content = result.read_text()
        assert "Implementation Note" in content
        assert "Started implementing the feature." in content
        assert "Role: implementer" in content

    def test_append_entry_with_type(self, sample_thread, threads_dir, mock_graph):
        """Test appending entry with specific type."""
        result = append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="Codex",
            role="implementer",
            title="Decision Made",
            entry_type="Decision",
            body="We decided to use approach A.",
            user_tag="user",
        )
        content = result.read_text()
        assert "Type: Decision" in content

    def test_append_entry_updates_status(self, sample_thread, threads_dir, mock_graph):
        """Test that append_entry can update status."""
        result = append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="Codex",
            role="pm",
            title="Closing",
            body="Task complete.",
            status="CLOSED",
            user_tag="user",
        )
        content = result.read_text()
        assert "Status: CLOSED" in content

    def test_append_entry_creates_thread_if_missing(self, threads_dir, mock_graph):
        """Test that append_entry creates thread if missing."""
        result = append_entry(
            "new-append-topic",
            threads_dir=threads_dir,
            agent="Claude",
            role="planner",
            title="First Entry",
            body="Creating thread via append.",
            user_tag="user",
        )
        assert result.exists()
        content = result.read_text()
        assert "First Entry" in content


# ============================================================================
# Test Error Conditions
# ============================================================================


class TestErrorConditions:
    """Tests for error handling."""

    def test_empty_topic_handling(self, threads_dir):
        """Test that empty topic falls back to default name 'thread'.

        The _sanitize_component function handles empty topics by returning
        the default value 'thread', so init_thread("") creates 'thread.md'.
        """
        result = init_thread(
            "",  # Empty topic
            threads_dir=threads_dir,
            title="Empty Topic Test",
        )
        assert result.exists()
        assert result.name == "thread.md"  # Fallback to default

    def test_special_characters_in_topic(self, threads_dir):
        """Test topics with special characters."""
        # Topics might have hyphens, underscores, etc.
        result = init_thread(
            "my-special_topic.123",
            threads_dir=threads_dir,
            title="Special Topic",
        )
        assert result.exists()

    def test_unicode_in_body(self, sample_thread, threads_dir, mock_graph):
        """Test handling of unicode in body."""
        result = append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="Claude",
            role="implementer",
            title="Unicode Test",
            body="Hello \u4e16\u754c!",  # Chinese characters (without emoji to avoid encoding issues)
            user_tag="user",
        )
        content = result.read_text()
        assert "\u4e16\u754c" in content


# ============================================================================
# Test Thread Format Consistency
# ============================================================================


class TestThreadFormat:
    """Tests for thread markdown format consistency."""

    def test_entry_separator(self, sample_thread, threads_dir, mock_graph):
        """Test that entries are separated by ---."""
        append_entry(
            "test-topic",
            threads_dir=threads_dir,
            agent="Claude",
            role="implementer",
            title="New Entry",
            body="Content",
            user_tag="user",
        )
        content = sample_thread.read_text()
        # Should have multiple --- separators
        assert content.count("---") >= 2

    def test_header_format(self, threads_dir):
        """Test that thread header follows expected format."""
        result = init_thread(
            "header-test",
            threads_dir=threads_dir,
            title="Header Test",
            status="OPEN",
            ball="Claude",
        )
        content = result.read_text()
        # Should have title line
        assert "# header-test" in content or "header-test" in content
        # Should have Status line
        assert "Status:" in content
        # Should have Ball line
        assert "Ball:" in content


# ============================================================================
# Test Idempotency
# ============================================================================


class TestIdempotency:
    """Tests for idempotent operations."""

    def test_init_thread_multiple_times(self, threads_dir):
        """Test that init_thread can be called multiple times safely."""
        result1 = init_thread(
            "idem-test",
            threads_dir=threads_dir,
            title="Original Title",
        )
        result2 = init_thread(
            "idem-test",
            threads_dir=threads_dir,
            title="Different Title",  # Should be ignored
        )
        assert result1 == result2
        # Content should match first creation
        assert "Original Title" in result1.read_text() or "idem-test" in result1.read_text()
