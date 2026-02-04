"""Tests for tool-level timeout in middleware instrumentation.

Validates that asyncio.wait_for() wrapping in setup_instrumentation()
correctly enforces per-tool timeouts and returns graceful errors instead
of crashing the server process.
"""

import asyncio
from unittest import mock

import pytest

from watercooler_mcp.middleware import (
    _DEFAULT_TOOL_TIMEOUT,
    _TOOL_TIMEOUTS,
    setup_instrumentation,
)
import watercooler_mcp.middleware as _mw


# ---------------------------------------------------------------------------
# Timeout constant tests
# ---------------------------------------------------------------------------


def test_default_timeout_is_under_60s():
    """Default timeout must be safely under MCP SDK's 60s hard limit."""
    assert _DEFAULT_TOOL_TIMEOUT < 60.0
    assert _DEFAULT_TOOL_TIMEOUT == 50.0


def test_tool_timeouts_dict_is_importable():
    """_TOOL_TIMEOUTS should be importable for inspection."""
    assert isinstance(_TOOL_TIMEOUTS, dict)
    assert len(_TOOL_TIMEOUTS) > 0


def test_known_tools_have_custom_timeouts():
    """Verify expected tools have custom timeout values."""
    assert _TOOL_TIMEOUTS["watercooler_graph_health"] == 180.0
    assert _TOOL_TIMEOUTS["watercooler_graph_enrich"] == 300.0
    assert _TOOL_TIMEOUTS["watercooler_graph_recover"] == 300.0
    assert _TOOL_TIMEOUTS["watercooler_leanrag_run_pipeline"] == 300.0
    assert _TOOL_TIMEOUTS["watercooler_smart_query"] == 120.0
    assert _TOOL_TIMEOUTS["watercooler_audit_branch_pairing"] == 120.0


def test_default_timeout_applies_to_unknown_tool():
    """Tools not in _TOOL_TIMEOUTS should get the default timeout."""
    assert "watercooler_health" not in _TOOL_TIMEOUTS
    timeout = _TOOL_TIMEOUTS.get("watercooler_health", _DEFAULT_TOOL_TIMEOUT)
    assert timeout == _DEFAULT_TOOL_TIMEOUT


# ---------------------------------------------------------------------------
# Instrumented run behavior tests
#
# Strategy: Create a fresh FakeFunctionTool class per test with the desired
# run() behavior, then let setup_instrumentation() capture and wrap it.
# This avoids recursive wrapping issues from multiple setup calls.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_orig_run():
    """Reset _orig_run between tests."""
    saved = _mw._orig_run
    _mw._orig_run = None
    yield
    _mw._orig_run = saved


def _make_tool_class(run_fn):
    """Create a fresh FakeFunctionTool class with run_fn as its run method."""
    class Tool:
        def __init__(self, name):
            self.name = name
        run = run_fn
    return Tool


def _instrument(tool_cls):
    """Instrument tool_cls via setup_instrumentation."""
    fake_module = type("Module", (), {"FunctionTool": tool_cls})()
    with mock.patch.dict("sys.modules", {"fastmcp.tools.tool": fake_module}):
        setup_instrumentation()


@pytest.mark.anyio
async def test_fast_tool_completes_normally():
    """Tools that finish quickly should return results normally."""

    async def run(self, arguments):
        return {"status": "healthy"}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")
    result = await Tool.run(tool, {"check": True})
    assert result == {"status": "healthy"}


@pytest.mark.anyio
async def test_timeout_raises_descriptive_error():
    """Tool exceeding timeout should raise TimeoutError with descriptive message."""

    async def run(self, arguments):
        await asyncio.sleep(999)
        return {}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")

    original_default = _mw._DEFAULT_TOOL_TIMEOUT
    _mw._DEFAULT_TOOL_TIMEOUT = 0.05
    try:
        with pytest.raises(TimeoutError, match=r"Tool 'watercooler_health' timed out"):
            await Tool.run(tool, {})
    finally:
        _mw._DEFAULT_TOOL_TIMEOUT = original_default


@pytest.mark.anyio
async def test_timeout_error_message_includes_server_alive():
    """Timeout error message should mention server is still running."""

    async def run(self, arguments):
        await asyncio.sleep(999)
        return {}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_graph_health")

    original_timeout = _TOOL_TIMEOUTS.get("watercooler_graph_health")
    _TOOL_TIMEOUTS["watercooler_graph_health"] = 0.05
    try:
        with pytest.raises(TimeoutError) as exc_info:
            await Tool.run(tool, {})
        msg = str(exc_info.value)
        assert "watercooler_graph_health" in msg
        assert "server is still running" in msg
    finally:
        if original_timeout is not None:
            _TOOL_TIMEOUTS["watercooler_graph_health"] = original_timeout


@pytest.mark.anyio
async def test_custom_timeout_applies_to_mapped_tool():
    """Tools in _TOOL_TIMEOUTS should use their custom timeout, not the default."""
    timeout_seen = None
    original_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, *, timeout=None):
        nonlocal timeout_seen
        timeout_seen = timeout
        return await original_wait_for(coro, timeout=timeout)

    async def run(self, arguments):
        return {"ok": True}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_graph_health")

    with mock.patch.object(_mw.asyncio, "wait_for", side_effect=spy_wait_for):
        await Tool.run(tool, {})

    assert timeout_seen == 180.0


@pytest.mark.anyio
async def test_default_timeout_applies_for_unmapped_tool():
    """Tools not in _TOOL_TIMEOUTS should use _DEFAULT_TOOL_TIMEOUT."""
    timeout_seen = None
    original_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, *, timeout=None):
        nonlocal timeout_seen
        timeout_seen = timeout
        return await original_wait_for(coro, timeout=timeout)

    async def run(self, arguments):
        return {"ok": True}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")

    with mock.patch.object(_mw.asyncio, "wait_for", side_effect=spy_wait_for):
        await Tool.run(tool, {})

    assert timeout_seen == _DEFAULT_TOOL_TIMEOUT


@pytest.mark.anyio
async def test_timeout_logged_via_log_action():
    """Timeout events should be logged with outcome='timeout' and timeout_s."""

    async def run(self, arguments):
        await asyncio.sleep(999)
        return {}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")

    original_default = _mw._DEFAULT_TOOL_TIMEOUT
    _mw._DEFAULT_TOOL_TIMEOUT = 0.05

    logged_calls = []
    original_log_action = _mw.log_action

    def capture_log_action(*args, **kwargs):
        logged_calls.append((args, kwargs))

    _mw.log_action = capture_log_action
    try:
        with pytest.raises(TimeoutError):
            await Tool.run(tool, {})

        tool_logs = [
            (a, kw) for a, kw in logged_calls if a and a[0] == "mcp.tool"
        ]
        assert len(tool_logs) == 1
        _, kwargs = tool_logs[0]
        assert kwargs["outcome"] == "timeout"
        assert kwargs["timeout_s"] == 0.05
        assert kwargs["tool_name"] == "watercooler_health"
    finally:
        _mw._DEFAULT_TOOL_TIMEOUT = original_default
        _mw.log_action = original_log_action


@pytest.mark.anyio
async def test_normal_error_still_has_error_outcome():
    """Non-timeout exceptions should still be logged with outcome='error'."""

    async def run(self, arguments):
        raise ValueError("something broke")

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")

    logged_calls = []
    original_log_action = _mw.log_action

    def capture_log_action(*args, **kwargs):
        logged_calls.append((args, kwargs))

    _mw.log_action = capture_log_action
    try:
        with pytest.raises(ValueError, match="something broke"):
            await Tool.run(tool, {})

        tool_logs = [
            (a, kw) for a, kw in logged_calls if a and a[0] == "mcp.tool"
        ]
        assert len(tool_logs) == 1
        _, kwargs = tool_logs[0]
        assert kwargs["outcome"] == "error"
        assert "timeout_s" in kwargs
    finally:
        _mw.log_action = original_log_action


@pytest.mark.anyio
async def test_ok_outcome_includes_timeout_s():
    """Successful tool calls should also log timeout_s for observability."""

    async def run(self, arguments):
        return {"ok": True}

    Tool = _make_tool_class(run)
    _instrument(Tool)
    tool = Tool("watercooler_health")

    logged_calls = []
    original_log_action = _mw.log_action

    def capture_log_action(*args, **kwargs):
        logged_calls.append((args, kwargs))

    _mw.log_action = capture_log_action
    try:
        await Tool.run(tool, {"check": True})

        tool_logs = [
            (a, kw) for a, kw in logged_calls if a and a[0] == "mcp.tool"
        ]
        assert len(tool_logs) == 1
        _, kwargs = tool_logs[0]
        assert kwargs["outcome"] == "ok"
        assert kwargs["timeout_s"] == _DEFAULT_TOOL_TIMEOUT
    finally:
        _mw.log_action = original_log_action
