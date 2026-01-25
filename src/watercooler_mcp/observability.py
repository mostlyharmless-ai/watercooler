from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from watercooler.config_facade import config


LOGGER_NAME = "watercooler_mcp"

# All logger namespaces that should share the same configuration
# This ensures consistent logging across all watercooler modules
LOGGER_NAMESPACES = [
    "watercooler_mcp",    # MCP server and tools
    "watercooler",        # Core library (baseline_graph, config, etc.)
    "watercooler_memory", # Memory backends (graphiti, leanrag, etc.)
]

# Environment variables for configuration (env vars override config file)
ENV_LOG_DIR = "WATERCOOLER_LOG_DIR"
ENV_LOG_LEVEL = "WATERCOOLER_LOG_LEVEL"
ENV_LOG_MAX_BYTES = "WATERCOOLER_LOG_MAX_BYTES"
ENV_LOG_BACKUP_COUNT = "WATERCOOLER_LOG_BACKUP_COUNT"
ENV_LOG_DISABLE_FILE = "WATERCOOLER_LOG_DISABLE_FILE"

# Defaults (used when config system unavailable)
DEFAULT_LOG_DIR = Path.home() / ".watercooler" / "logs"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUP_COUNT = 5

# Thread-safe initialization state
_logger_initialized = False
_logger_lock = threading.Lock()
_session_lock = threading.Lock()  # Separate lock for session timestamp to avoid deadlock
_session_start: Optional[str] = None  # Lazy initialization to avoid import-time side effects
_cached_logging_config: Optional[Dict[str, Any]] = None  # Cache to avoid repeated config lookups


def _reset_logging_state() -> None:
    """Reset logging state for testing.

    This clears cached config and resets the logger initialization flag,
    allowing tests to reconfigure logging with different settings.

    WARNING: Only use this in tests - not thread-safe during normal operation.
    """
    global _logger_initialized, _cached_logging_config, _session_start

    _cached_logging_config = None
    _session_start = None

    # Reset logger handlers for ALL namespaces if already initialized
    if _logger_initialized:
        _logger_initialized = False
        for namespace in LOGGER_NAMESPACES:
            ns_logger = logging.getLogger(namespace)
            ns_logger.handlers.clear()


def _get_logging_config_safe() -> Dict[str, Any]:
    """Get logging config from config system with fallback to defaults.

    Uses lazy import to avoid circular imports (config.py imports log_debug).
    Caches result to avoid repeated file I/O.

    Returns:
        Dict with keys: level, dir, max_bytes, backup_count, disable_file
    """
    global _cached_logging_config

    if _cached_logging_config is not None:
        return _cached_logging_config

    # Try to load from config system (lazy import to avoid circular import)
    try:
        from watercooler_mcp.config import get_logging_config
        _cached_logging_config = get_logging_config()
    except Exception as e:
        # Config system not available or failed, use defaults with env var overrides
        # Use standard logging directly (not log_debug) to avoid circular dependency
        logging.getLogger(LOGGER_NAME).debug(
            "Config system unavailable, using env defaults: %s", e
        )
        _cached_logging_config = {
            "level": config.env.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL),
            "dir": config.env.get(ENV_LOG_DIR, ""),
            "max_bytes": config.env.get_int(ENV_LOG_MAX_BYTES, DEFAULT_MAX_BYTES),
            "backup_count": config.env.get_int(ENV_LOG_BACKUP_COUNT, DEFAULT_BACKUP_COUNT),
            "disable_file": config.env.get_bool(ENV_LOG_DISABLE_FILE, False),
        }

    return _cached_logging_config


def _get_log_level() -> int:
    """Get log level from config system, with env var override.

    Resolution order:
    1. WATERCOOLER_LOG_LEVEL environment variable
    2. Config file (mcp.logging.level)
    3. Default (INFO)

    Warns to stderr if an invalid log level is specified.
    """
    config = _get_logging_config_safe()
    level_name = config.get("level", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, None)
    if level is None:
        print(f"Warning: Invalid log level '{level_name}', using INFO", file=sys.stderr)
        return logging.INFO
    return level


def _get_log_file_path() -> Optional[Path]:
    """Get the log file path, creating directories if needed.

    Resolution order for log directory:
    1. WATERCOOLER_LOG_DIR environment variable
    2. Config file (mcp.logging.dir)
    3. Default (~/.watercooler/logs)

    Returns None if file logging is disabled via config or env var,
    or if the log directory cannot be created (falls back to stderr-only).
    """
    global _session_start

    config = _get_logging_config_safe()

    # Check if file logging is disabled
    if config.get("disable_file", False):
        return None

    # Thread-safe lazy initialization of session start timestamp
    # Uses separate lock to avoid deadlock when called from _get_logger()
    if _session_start is None:
        with _session_lock:
            if _session_start is None:  # Double-check pattern
                _session_start = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")

    # Get log directory from config (already has env var override applied)
    config_dir = config.get("dir", "")
    if config_dir:
        log_dir = Path(config_dir).expanduser()
    else:
        log_dir = DEFAULT_LOG_DIR

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        # Fall back to stderr-only logging if directory creation fails
        print(f"Warning: Could not create log directory {log_dir}: {e}", file=sys.stderr)
        return None

    # Session-based filename: watercooler_2024-01-15_143022.log
    log_file = log_dir / f"watercooler_{_session_start}.log"
    return log_file


def _get_logger() -> logging.Logger:
    """Get or initialize the watercooler logger.

    By default, logs to ~/.watercooler/logs/watercooler_<session>.log

    Configuration via config file (mcp.logging section) or environment variables:
    - WATERCOOLER_LOG_DIR: Directory for log files (default: ~/.watercooler/logs/)
    - WATERCOOLER_LOG_LEVEL: DEBUG, INFO, WARNING, ERROR (default: INFO)
    - WATERCOOLER_LOG_MAX_BYTES: Max log file size before rotation (default: 10MB)
    - WATERCOOLER_LOG_BACKUP_COUNT: Number of backup files to keep (default: 5)
    - WATERCOOLER_LOG_DISABLE_FILE: Set to 1 to disable file logging (stderr only)

    Environment variables override config file values.

    Note: This configures ALL watercooler logger namespaces (watercooler_mcp,
    watercooler, watercooler_memory) to share the same handlers and format.
    This ensures consistent logging across the entire codebase.
    """
    global _logger_initialized
    logger = logging.getLogger(LOGGER_NAME)

    if not _logger_initialized:
        with _logger_lock:
            # Double-check pattern for thread safety
            if not _logger_initialized:
                _logger_initialized = True

                log_level = _get_log_level()

                # Human-readable formatter with logger name for traceability
                formatter = logging.Formatter(
                    "[%(levelname)s %(asctime)s] [%(name)s] %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S"
                )

                # Create handlers (shared across all loggers)
                handlers = []

                # File handler (enabled by default)
                log_file = _get_log_file_path()
                if log_file:
                    # Get rotation settings from config (already has env var override)
                    config = _get_logging_config_safe()
                    max_bytes = config.get("max_bytes", DEFAULT_MAX_BYTES)
                    backup_count = config.get("backup_count", DEFAULT_BACKUP_COUNT)

                    file_handler = RotatingFileHandler(
                        str(log_file),
                        maxBytes=max_bytes,
                        backupCount=backup_count,
                    )
                    file_handler.setFormatter(formatter)
                    file_handler.setLevel(log_level)
                    handlers.append(file_handler)

                # Also log to stderr for visibility (only warnings and above by default)
                stream_handler = logging.StreamHandler()
                stream_handler.setFormatter(formatter)
                stream_handler.setLevel(max(log_level, logging.WARNING))
                handlers.append(stream_handler)

                # Configure ALL watercooler logger namespaces with shared handlers
                # This ensures logging from watercooler, watercooler_mcp, and
                # watercooler_memory all go to the same place with the same format
                for namespace in LOGGER_NAMESPACES:
                    ns_logger = logging.getLogger(namespace)
                    ns_logger.handlers.clear()  # Remove any existing handlers
                    ns_logger.setLevel(log_level)
                    ns_logger.propagate = False  # Don't propagate to root logger
                    for handler in handlers:
                        ns_logger.addHandler(handler)

    return logger


def log_action(
    action: str,
    *,
    outcome: str = "ok",
    duration_ms: Optional[float] = None,
    tool_name: Optional[str] = None,
    input_chars: Optional[int] = None,
    output_chars: Optional[int] = None,
    **fields: Any,
) -> None:
    """Emit a structured log line for an action.

    Fields are serialized to JSON for safety. Keep schema lightweight.

    Args:
        action: Name of the action being logged
        outcome: Result status ("ok", "error", etc.)
        duration_ms: How long the action took in milliseconds
        tool_name: MCP tool name (for per-tool metrics)
        input_chars: Size of input in characters (for token estimation)
        output_chars: Size of output in characters (for token estimation)
        **fields: Additional fields to include
    """
    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": action,
        "outcome": outcome,
    }
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 2)
    if tool_name is not None:
        payload["tool"] = tool_name
    if input_chars is not None:
        payload["in_chars"] = input_chars
        payload["in_tokens_est"] = input_chars // 4  # Rough estimate
    if output_chars is not None:
        payload["out_chars"] = output_chars
        payload["out_tokens_est"] = output_chars // 4  # Rough estimate
    if fields:
        payload.update(fields)

    _get_logger().info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def log_debug(message: str, **fields: Any) -> None:
    """Log a debug message with optional structured fields.

    Use this for detailed diagnostic output (replaces _diag()).
    Only emitted when log level is DEBUG.

    Args:
        message: Human-readable debug message
        **fields: Optional structured fields to append
    """
    logger = _get_logger()
    if logger.isEnabledFor(logging.DEBUG):
        if fields:
            field_str = " " + json.dumps(fields, separators=(",", ":"), sort_keys=True)
            logger.debug(f"{message}{field_str}")
        else:
            logger.debug(message)


def log_warning(message: str, **fields: Any) -> None:
    """Log a warning message with optional structured fields."""
    logger = _get_logger()
    if fields:
        field_str = " " + json.dumps(fields, separators=(",", ":"), sort_keys=True)
        logger.warning(f"{message}{field_str}")
    else:
        logger.warning(message)


def log_error(message: str, **fields: Any) -> None:
    """Log an error message with optional structured fields."""
    logger = _get_logger()
    if fields:
        field_str = " " + json.dumps(fields, separators=(",", ":"), sort_keys=True)
        logger.error(f"{message}{field_str}")
    else:
        logger.error(message)


@contextmanager
def timeit(
    action: str,
    *,
    tool_name: Optional[str] = None,
    input_chars: Optional[int] = None,
    **fields: Any,
):
    """Time a block and emit a structured log on exit.

    On exception, logs outcome="error" and re-raises.

    Args:
        action: Name of the action being timed
        tool_name: MCP tool name (for per-tool metrics)
        input_chars: Size of input in characters
        **fields: Additional fields to include

    Yields:
        A dict that can be updated with output_chars after the operation
    """
    start = time.perf_counter()
    result_info: Dict[str, Any] = {}
    try:
        yield result_info
        duration_ms = (time.perf_counter() - start) * 1000.0
        log_action(
            action,
            outcome="ok",
            duration_ms=duration_ms,
            tool_name=tool_name,
            input_chars=input_chars,
            output_chars=result_info.get("output_chars"),
            **fields,
        )
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000.0
        log_action(
            action,
            outcome="error",
            duration_ms=duration_ms,
            tool_name=tool_name,
            input_chars=input_chars,
            **fields,
        )
        raise
