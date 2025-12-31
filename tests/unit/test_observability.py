import json
import logging
import importlib.util
import os
import sys
from pathlib import Path
from io import StringIO
from unittest import mock

import pytest

# Import module directly from file to avoid importing package __init__ (which pulls fastmcp)
_OBS_PATH = Path("src/watercooler_mcp/observability.py").resolve()
spec = importlib.util.spec_from_file_location("watercooler_mcp_observability", _OBS_PATH)
obs = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(obs)  # type: ignore[attr-defined]

log_action = obs.log_action
log_debug = obs.log_debug
log_warning = obs.log_warning
log_error = obs.log_error
timeit = obs.timeit
LOGGER_NAME = obs.LOGGER_NAME
_get_log_level = obs._get_log_level
_get_log_file_path = obs._get_log_file_path
_reset_logging_state = obs._reset_logging_state


@pytest.fixture(autouse=True)
def reset_logger():
    """Reset logger state between tests."""
    # Use the module's reset function to clear all cached state
    _reset_logging_state()
    yield
    # Clean up after test
    _reset_logging_state()


def test_log_action_emits_json(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    log_action("git.pull", outcome="ok", duration_ms=123, topic="t1", agent="Codex")
    assert caplog.records
    msg = caplog.records[-1].message
    data = json.loads(msg)
    assert data["action"] == "git.pull"
    assert data["outcome"] == "ok"
    assert data["duration_ms"] == 123
    assert data["topic"] == "t1"
    assert data["agent"] == "Codex"


def test_timeit_success_logs(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with timeit("test.block", topic="t2"):
        pass
    msg = caplog.records[-1].message
    data = json.loads(msg)
    assert data["action"] == "test.block"
    assert data["outcome"] == "ok"
    assert data["topic"] == "t2"
    assert isinstance(data["duration_ms"], (int, float))


def test_timeit_error_logs(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    try:
        with timeit("test.err", topic="t3"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    msg = caplog.records[-1].message
    data = json.loads(msg)
    assert data["action"] == "test.err"
    assert data["outcome"] == "error"
    assert data["topic"] == "t3"


def test_timeit_with_output_chars(caplog):
    """Test that timeit properly captures output_chars from result_info dict."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with timeit("test.metrics", input_chars=100) as result_info:
        result_info["output_chars"] = 200
    msg = caplog.records[-1].message
    data = json.loads(msg)
    assert data["in_chars"] == 100
    assert data["in_tokens_est"] == 25  # 100 // 4
    assert data["out_chars"] == 200
    assert data["out_tokens_est"] == 50  # 200 // 4


def test_log_action_with_tool_metrics(caplog):
    """Test log_action includes tool_name and token estimates."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    log_action("mcp.tool", tool_name="say", input_chars=400, output_chars=800)
    data = json.loads(caplog.records[-1].message)
    assert data["tool"] == "say"
    assert data["in_chars"] == 400
    assert data["in_tokens_est"] == 100  # 400 // 4
    assert data["out_chars"] == 800
    assert data["out_tokens_est"] == 200  # 800 // 4


def test_log_debug_basic(caplog, monkeypatch):
    """Test log_debug emits message at DEBUG level."""
    monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("WATERCOOLER_LOG_DISABLE_FILE", "1")
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    log_debug("test debug message")
    assert len(caplog.records) > 0
    assert "test debug message" in caplog.records[-1].message


def test_log_debug_with_fields(caplog, monkeypatch):
    """Test log_debug appends structured fields."""
    monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("WATERCOOLER_LOG_DISABLE_FILE", "1")
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    log_debug("Git operation", repo="watercooler", branch="main")
    msg = caplog.records[-1].message
    assert "Git operation" in msg
    assert '"branch":"main"' in msg
    assert '"repo":"watercooler"' in msg


def test_log_debug_not_emitted_at_info(caplog):
    """Test log_debug is suppressed when level is INFO."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    log_debug("should not appear")
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug_records) == 0


def test_log_warning_basic(caplog):
    """Test log_warning emits message at WARNING level."""
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    log_warning("test warning")
    assert len(caplog.records) > 0
    assert caplog.records[-1].levelno == logging.WARNING
    assert "test warning" in caplog.records[-1].message


def test_log_error_basic(caplog):
    """Test log_error emits message at ERROR level."""
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    log_error("test error")
    assert len(caplog.records) > 0
    assert caplog.records[-1].levelno == logging.ERROR
    assert "test error" in caplog.records[-1].message


def test_log_level_from_env(monkeypatch):
    """Test log level respects WATERCOOLER_LOG_LEVEL env var."""
    monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "DEBUG")
    level = _get_log_level()
    assert level == logging.DEBUG

    # Reset cached config before changing env var (config is cached on first read)
    _reset_logging_state()
    monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "WARNING")
    level = _get_log_level()
    assert level == logging.WARNING


def test_invalid_log_level_falls_back(monkeypatch, capsys):
    """Test invalid log level falls back to INFO with warning."""
    monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "INVALID_LEVEL")
    level = _get_log_level()
    assert level == logging.INFO
    captured = capsys.readouterr()
    assert "Invalid log level" in captured.err
    assert "INVALID_LEVEL" in captured.err


def test_disable_file_logging(monkeypatch):
    """Test WATERCOOLER_LOG_DISABLE_FILE=1 returns None for log path."""
    monkeypatch.setenv("WATERCOOLER_LOG_DISABLE_FILE", "1")
    path = _get_log_file_path()
    assert path is None


def test_custom_log_dir(monkeypatch, tmp_path):
    """Test WATERCOOLER_LOG_DIR sets custom log directory."""
    monkeypatch.delenv("WATERCOOLER_LOG_DISABLE_FILE", raising=False)
    custom_dir = tmp_path / "custom_logs"
    monkeypatch.setenv("WATERCOOLER_LOG_DIR", str(custom_dir))
    path = _get_log_file_path()
    assert path is not None
    assert path.parent == custom_dir
    assert custom_dir.exists()


def test_log_file_creation_failure(monkeypatch, capsys):
    """Test graceful fallback when log directory creation fails."""
    monkeypatch.delenv("WATERCOOLER_LOG_DISABLE_FILE", raising=False)
    # Use a path that can't be created
    monkeypatch.setenv("WATERCOOLER_LOG_DIR", "/root/nonexistent/path/logs")
    path = _get_log_file_path()
    # Should return None and warn to stderr
    assert path is None
    captured = capsys.readouterr()
    assert "Could not create log directory" in captured.err
