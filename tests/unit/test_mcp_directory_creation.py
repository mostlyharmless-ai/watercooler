"""Tests for automatic .watercooler directory creation in MCP server."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Skip all tests in this module if fastmcp is not available
pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory for testing."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)


@pytest.fixture
def mock_context():
    """Create a mock MCP context."""
    ctx = MagicMock()
    ctx.client_id = "Claude Code"
    return ctx


def test_health_creates_directory_if_missing(temp_project_dir, mock_context, monkeypatch):
    """Test that health() creates the .watercooler directory if it doesn't exist."""
    from watercooler_mcp.server import health

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call health - access the underlying function with code_path
    result = health(mock_context, code_path=str(temp_project_dir))

    # Verify directory was created
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()
    assert "Threads Dir Exists: True" in result


def test_list_threads_creates_directory_if_missing(temp_project_dir, mock_context, monkeypatch):
    """Test that list_threads() creates the .watercooler directory if it doesn't exist."""
    from watercooler_mcp.server import list_threads

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call list_threads with required code_path parameter
    result = list_threads(mock_context, code_path=str(temp_project_dir))
    # FastMCP tools now return ToolResult objects
    if hasattr(result, "content"):
        text = " ".join(
            getattr(part, "text", "")
            for part in result.content
        )
    else:
        text = str(result)

    # Verify directory was created
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()
    assert "Threads directory created" in text


def test_read_thread_creates_directory_if_missing(temp_project_dir, monkeypatch):
    """Test that read_thread() creates the .watercooler directory if it doesn't exist."""
    import pytest
    from watercooler_mcp.server import read_thread
    from watercooler_mcp.errors import ThreadNotFoundError

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call read_thread with required code_path parameter - expect ThreadNotFoundError
    with pytest.raises(ThreadNotFoundError) as exc_info:
        read_thread("test-topic", code_path=str(temp_project_dir))

    # Verify directory was created before the exception was raised
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()
    assert exc_info.value.topic == "test-topic"


# watercooler_reindex was retired in PR4b (superseded by the graph-first
# watercooler_list_threads); its directory-creation behaviour is covered by
# test_say_creates_directory_via_init_thread below.


def test_health_identity_does_not_create_threads_dir(temp_project_dir, mock_context):
    """PR4b review — health(detail="identity") / the whoami alias must stay a
    pure identity probe: it reports threads-dir state without creating it."""
    from unittest.mock import patch

    from watercooler_mcp.tools import diagnostic

    missing = temp_project_dir / "nested" / ".watercooler"
    fake_ctx = type("Ctx", (), {"threads_dir": missing})()
    with patch.object(
        diagnostic, "resolve_thread_context", return_value=fake_ctx
    ):
        result = diagnostic._health_identity_impl(
            mock_context, code_path=str(temp_project_dir)
        )

    # The directory must NOT have been created by an identity check.
    assert not missing.exists()
    assert "absent" in result


def test_say_creates_directory_via_init_thread(temp_project_dir, mock_context, monkeypatch):
    """Test that say() creates the .watercooler directory through init_thread()."""
    from watercooler_mcp.server import say

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call say to create a new thread with required code_path parameter
    result = say(
        topic="test-topic",
        title="Test Entry",
        body="This is a test",
        ctx=mock_context,
        code_path=str(temp_project_dir),
        agent_func="Claude:pm",
    )

    # Verify directory was created
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()

    # Verify thread file was created
    thread_file = watercooler_dir / "test-topic.md"
    assert thread_file.exists()
    assert "Entry added" in result
