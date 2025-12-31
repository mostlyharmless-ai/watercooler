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
    result = health.fn(mock_context, code_path=str(temp_project_dir))

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
    result = list_threads.fn(mock_context, code_path=str(temp_project_dir))
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
    from watercooler_mcp.server import read_thread

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call read_thread with required code_path parameter
    result = read_thread.fn("test-topic", code_path=str(temp_project_dir))

    # Verify directory was created
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()
    assert "not found" in result  # Thread doesn't exist, but directory was created


def test_reindex_creates_directory_if_missing(temp_project_dir, mock_context, monkeypatch):
    """Test that reindex() creates the .watercooler directory if it doesn't exist."""
    from watercooler_mcp.server import reindex

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call reindex - access the underlying function
    result = reindex.fn(mock_context)

    # Verify directory was created
    assert watercooler_dir.exists()
    assert watercooler_dir.is_dir()
    assert "Threads directory created" in result


def test_say_creates_directory_via_init_thread(temp_project_dir, mock_context, monkeypatch):
    """Test that say() creates the .watercooler directory through init_thread()."""
    from watercooler_mcp.server import say

    # Set the watercooler directory to a non-existent path
    watercooler_dir = temp_project_dir / ".watercooler"
    assert not watercooler_dir.exists()

    # Mock get_threads_dir to return our test directory
    monkeypatch.setenv("WATERCOOLER_DIR", str(watercooler_dir))

    # Call say to create a new thread with required code_path parameter
    result = say.fn(
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
