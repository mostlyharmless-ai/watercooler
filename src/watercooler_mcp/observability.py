"""Observability utilities for watercooler MCP server.

Logging Level Guidelines
------------------------
Use the following guidelines when choosing log levels:

DEBUG (log_debug):
    - Detailed diagnostic information for troubleshooting
    - Service startup/shutdown steps and timing
    - Configuration resolution details
    - Network request/response details
    - Model download progress
    - Only visible when WATERCOOLER_LOG_LEVEL=DEBUG

INFO (log_action):
    - Structured action records (tool calls, operations)
    - Successful service transitions
    - Key milestones in workflows
    - Default level - visible in normal operation

WARNING (log_warning):
    - Recoverable issues that may need attention
    - Configuration mismatches (e.g., EMBEDDING_DIM mismatch)
    - Deprecated features being used
    - Fallback behavior activated
    - Security policy violations (e.g., checksum mismatch)

ERROR (log_error):
    - Operation failures that affect functionality
    - Unrecoverable errors
    - Service startup failures
    - Data corruption or integrity issues

Best Practices:
    - Always include context: what was happening, what failed, what to do
    - Use structured fields for machine-parseable data
    - Avoid logging sensitive data (credentials, tokens, user content)
    - For long-running operations, log start at DEBUG, completion at INFO
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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
ENV_LOG_STREAM_LEVEL = "WATERCOOLER_LOG_STREAM_LEVEL"

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


# ---------------------------------------------------------------------- #
# Move 4 / Move 6: RedactingFilter — final defense for any token-shaped
# string that escapes into a log line. Applies ``redact_value`` from the
# secrets gateway to:
#
#   - the formatted message (``record.getMessage()`` covering both
#     ``record.msg`` and ``record.args`` interpolation)
#   - ``record.exc_info`` formatted to text (so exception messages
#     and tracebacks containing tokens are also redacted — PR #727
#     round 1 MED #1)
#   - ``record.stack_info`` (frame stacks captured via
#     ``logger.foo(..., stack_info=True)``)
#
# Attached at the LOGGER level (not handler level) so it runs exactly
# once per record before dispatch to any handler — eliminates the
# cross-handler-mutation aliasing concern (PR #727 round 1 MED #3).
# Filters that raise kill the log pipeline; every redact path is
# wrapped in try/except and falls through to the original record so
# a redaction failure can never drop a log line.
# ---------------------------------------------------------------------- #


# PR #727 round 1 MED #2: import ``redact_value`` once at module load
# rather than per-filter-call. If the import fails (test isolation,
# circular import edge case, packaging error), warn LOUDLY once and
# operate as a no-op filter — silently failing forever with no
# observable signal would defeat the "last-line defense" claim.
def _emit_redact_import_failure_warning(
    exc: BaseException, *, logger_name: str = LOGGER_NAME
) -> None:
    """Emit the loud-warning side of the redact-import failure path.

    Extracted to a helper so PR #727 round 2 LOW #3's testability
    concern can be addressed: the warning is normally emitted at
    module-import time before any handler is attached (so caplog
    can't reliably observe it). This helper lets a test assert
    the warning message + level by invoking it directly with a
    synthetic exception, decoupling test coverage from import
    timing.

    PR #727 round 12 L1: ALSO write to ``sys.stderr`` directly.
    The logger.warning path is best-effort — at module-import time
    no handlers are attached, so the record propagates to the root
    logger which silently discards it (default behaviour without a
    ``basicConfig`` call). The stderr write guarantees an
    operator-visible signal regardless of the logging configuration.
    Duplication is acceptable: the dead-defense state is bad enough
    that we want both surfaces to fire.
    """
    message = (
        f"RedactingFilter: failed to import secrets.gateway.redact_value "
        f"({type(exc).__name__}: {exc}); the log-egress redaction filter "
        f"will be a no-op for this process. Token-shaped strings in log "
        f"lines WILL leak. Investigate the import path and restart."
    )
    # Logger surface (testable via caplog when handlers are attached).
    logging.getLogger(logger_name).warning(
        "%s",
        message,
    )
    # Stderr surface (guaranteed visibility at import time before any
    # logging configuration is applied). Mirrors the existing
    # ``_get_log_file_path`` print-to-stderr pattern for log-dir
    # creation failures.
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


try:
    from .secrets.gateway import redact_value as _redact_value
except Exception as _redact_import_exc:  # noqa: BLE001 — re-emit explicitly
    _redact_value = None  # type: ignore[assignment]
    _emit_redact_import_failure_warning(_redact_import_exc)
    del _redact_import_exc


# Stateless module-private formatter used to render exc_info /
# stack_info into text so we can run redact_value over it. Safe to
# share across calls because we don't mutate it.
_EXC_FORMATTER = logging.Formatter()


def _exc_chain_reveals_secret(
    root_exc: BaseException,
    redact_fn: Callable[[str], str],
) -> bool:
    """Walk an exception's ``__cause__`` / ``__context__`` chain and
    return True if ``str()`` of any node contains a secret-shaped
    string, OR if any step of the walk cannot be verified (a
    ``str()`` call raises, ``redact_fn`` raises, or a malformed
    chain object is encountered).

    Used by ``_RedactingFilter`` to cross-check the live exception
    surface when the cached ``exc_text`` may not faithfully
    represent the exception object's payload — a pre-existing
    ``record.exc_text`` could be a sanitised form, while the live
    object's ``args`` (or chained exceptions) still carry the raw
    token. Downstream handlers (Sentry, GCP CloudLoggingHandler,
    structlog JSON renderer) walk the cause chain and would
    re-surface tokens this filter previously redacted from
    ``exc_text``.

    Fail-safe semantics: any uncertainty resolves to ``True``
    (assume secret present, conservatively null the surface).
    Confidentiality is preferred over preserving exc_info on a
    pathological exception whose ``__str__`` raises or whose chain
    we cannot traverse.
    """
    seen: set[int] = set()
    exc: Optional[BaseException] = root_exc
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        try:
            node_str = str(exc)
        except Exception:
            return True  # cannot verify → conservative null
        if node_str:
            try:
                if redact_fn(node_str) != node_str:
                    return True
            except Exception:
                return True  # cannot verify → conservative null
        # Walk the chain. ``__cause__`` is set by ``raise X from Y``;
        # ``__context__`` is set automatically by an exception
        # raised inside an except handler.
        #
        # PR #727 round 15 M2: match CPython's
        # ``TracebackException.from_exception`` chain-iteration
        # logic — when ``__cause__`` is set or ``__suppress_context__``
        # is True (the latter is auto-set by ``raise X from <expr>``
        # for any expr including ``None``), the implicit
        # ``__context__`` is suppressed from rendered output. Walking
        # it anyway produces false positives: a clean exception
        # raised inside an except block that previously caught a
        # token-bearing exception has the token-bearing one as
        # ``__context__``, but stdlib / structlog / Sentry render
        # only the new exception. Wrongly nulling ``exc_info`` for
        # those cases breaks Sentry/GCP grouping for benign
        # exceptions (I5 violation).
        try:
            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                exc = cause
            elif getattr(exc, "__suppress_context__", False):
                # Explicit suppression: ``raise X from Y`` or
                # ``raise X from None``. Stop the walk here.
                exc = None
            else:
                exc = getattr(exc, "__context__", None)
        except Exception:
            return True  # malformed chain → conservative null
    return False


class _RedactingFilter(logging.Filter):
    """Apply pattern-based secret redaction at log egress.

    Design contract (six invariants pinned by tests):
      I1. Never drop a record. ``filter()`` always returns ``True``.
      I2. No token-bearing surface reaches a downstream handler.
          The four mutable record surfaces — ``msg``, ``exc_text``,
          ``exc_info``, ``stack_info`` — are each independently
          inspected and redacted or nullified.
      I3. Sections are independent. A failure in section 1 (msg)
          must not skip section 2 (exc) or section 3 (stack). Each
          section has its own try/except boundary.
      I4. Redact failures fall SAFE, not OPEN. When ``redact_fn``
          raises on a value we'd have written, replace with a
          ``[REDACTING-FILTER FAILURE: ...]`` placeholder and null
          the matching surface (no silent leak of the un-redacted
          input).
      I5. Preserve ``exc_info`` for non-secret exceptions so
          downstream routing (Sentry exception-type, GCP grouping,
          structlog chained-cause walking) keeps working. Null
          ``exc_info`` only when redaction actually mutated the
          formatted text OR the live exception chain reveals a
          secret OR verification was impossible.
      I6. Don't pre-populate ``record.exc_text`` for non-secret
          exceptions. CPython's ``Formatter.format`` short-circuits
          its own ``formatException`` call when ``exc_text`` is
          set; pre-populating defeats downstream Formatter
          subclasses that override ``formatException``
          (structured-JSON, Sentry breadcrumb, colorized handlers).

    Section map:
      Section 1 — ``record.msg`` / ``record.args``.
      Section 2 — ``record.exc_info`` is real: format (or use
          cached) ``exc_text``, redact, gate the write on actual
          mutation (I6), gate the ``exc_info`` null on actual
          mutation (I5), cross-check the live exception chain via
          ``_exc_chain_reveals_secret`` to catch tokens hidden by
          a pre-existing sanitised ``exc_text`` (e.g., chained
          causes invisible to the cached form).
      Section 2.5 — ``record.exc_info`` is None but
          ``record.exc_text`` is set (orphan). CPython renders
          ``exc_text`` independently of ``exc_info``; redact in
          place.
      Section 3 — ``record.stack_info`` (from
          ``logger.foo(..., stack_info=True)``).

    Documented scope boundaries (NOT mitigated):
      - ``exc_info[2]`` frame locals are not walked. A handler that
        prints frame locals (``cgitb``, ``rich.traceback`` with
        ``show_locals=True``) on a non-secret exception that has
        a token bound in a local would leak. Stdlib
        ``traceback.format_exception`` does not print locals; the
        deployment surface does not include locals-printing
        handlers. Documented for any future change that adds one.
      - The filter is logger-level, not handler-level. A custom
        logger that bypasses the standard hierarchy
        (``logging.Logger.callHandlers`` override, etc.) would
        skip filters entirely.

    Architectural note: attached at the LOGGER level via
    ``Logger.addFilter`` (not at handler level). Logger filters run
    exactly once per record before any handler is invoked, so we
    can safely mutate ``record`` once and have every downstream
    handler see the redacted form. Handler-level mutation would
    work today but would alias across multiple handlers, breaking
    if a third-party handler is added later without the filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if _redact_value is None:
            # Module-load import failed; warning was already emitted
            # once. Fail open — the filter must never drop a record.
            return True

        # 1. Redact the formatted message (covers msg + args).
        # PR #727 round 5 MED: never short-circuit out of the filter
        # on a section-1 failure — sections 2 (exc_info) and 3
        # (stack_info) carry independent secret surfaces and must
        # still be redacted even when ``getMessage`` raises (mis-
        # matched %s/args) or ``redact_value`` raises on an exotic
        # input. Scope the try/except to ONLY the msg-mutation path:
        # on failure leave ``record.msg`` / ``record.args`` alone
        # and fall through to sections 2 and 3.
        try:
            formatted = record.getMessage()
            redacted = _redact_value(formatted)
            if redacted != formatted:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Section 1 failed — getMessage() or redact_value() raised.
            # PR #727 round 15 M1: ``record.args`` may itself contain
            # token-bearing strings (e.g.,
            # ``logger.info("tok=%s", "ghp_…")`` with a malformed
            # format string that breaks ``getMessage``). The
            # successful-redaction path already clears ``args`` after
            # interpolating into ``msg``; the failure path must do
            # the same — handlers that read ``record.args`` or
            # ``record.__dict__`` directly (structlog,
            # python-json-logger, Sentry shims, custom JSON
            # renderers) bypass the formatted-message path and would
            # otherwise re-surface the raw token. Clearing args
            # loses the values, but on a record where ``getMessage``
            # already raised the args wouldn't have rendered
            # correctly anyway. Sections 2 and 3 still run below;
            # their try/except boundaries are independent.
            try:
                record.args = ()
            except Exception:
                # Pathologically read-only attribute — nothing more
                # we can do. The Formatter.format call may also
                # raise on this record; that surfaces the failure.
                pass

        # 2. Redact exception text (PR #727 round 1 MED #1).
        # Pre-format and redact so the standard ``Formatter.format``
        # sees ``record.exc_text`` already populated and skips its
        # own ``formatException`` call (per Python logging docs).
        #
        # PR #727 round 2 MED #1: the bare ``if record.exc_info:``
        # guard accepted the zero-exception sentinel
        # ``(None, None, None)``, which ``formatException`` rejects.
        # Match CPython's own ``Formatter.format`` guard:
        # ``record.exc_info[0] is not None``.
        if record.exc_info and record.exc_info[0] is not None:
            try:
                # PR #727 round 5 LOW (informational): if
                # ``record.exc_text`` is already populated by an
                # earlier filter or by application code, we trust
                # it as the rendered exception text and run
                # ``redact_value`` over it. The output is still
                # redacted, so a pre-poisoned ``exc_text`` cannot
                # leak via the ``exc_text`` surface itself — though
                # the live ``exc_info`` is cross-checked separately
                # below (round 7 LOW).
                #
                # PR #727 round 9 M1 (scope boundary, not a bug):
                # this filter mutates ``record.msg``, ``record.args``,
                # ``record.exc_text``, ``record.exc_info``, and
                # ``record.stack_info`` — every text surface the
                # stdlib Formatter renders. It does NOT walk
                # ``exc_info[2]`` frame locals. When ``exc_info``
                # survives (non-secret-bearing exception per the
                # round-6 gate AND round-7 cross-check), a future
                # downstream handler that walks frame locals
                # (``rich.traceback`` with ``show_locals=True``,
                # Sentry ``before_send`` hooks, ``cgitb``) could
                # surface a token bound to a local in the failing
                # frame. Stdlib ``traceback.format_exception`` does
                # not print locals, so neither the cached-text path
                # nor the formatException fallback expose them
                # today. The deployment surface does not include
                # locals-printing handlers; introducing one without
                # adding frame-walk redaction here would break the
                # claim. Documented as a known scope boundary, not
                # mitigated.
                exc_text = (
                    record.exc_text
                    if record.exc_text
                    else _EXC_FORMATTER.formatException(record.exc_info)
                )
                redacted_exc = _redact_value(exc_text)
                exc_text_changed = redacted_exc != exc_text
                # I6: only write ``exc_text`` when redaction actually
                # changed it. Pre-populating defeats downstream
                # Formatter subclasses that override ``formatException``
                # (round 12 M1).
                if exc_text_changed:
                    record.exc_text = redacted_exc
                # I5: null ``exc_info`` only when we have positive
                # evidence of a secret. Two oracles:
                #   (a) the cached/formatted ``exc_text`` mutated
                #       under redact (round 6 MED — primary signal);
                #   (b) the live exception chain reveals a secret
                #       via ``str()`` of any node in
                #       ``__cause__`` / ``__context__`` (round 7
                #       LOW + round 13 HIGH — catches tokens hidden
                #       by a pre-existing sanitised ``exc_text``,
                #       including chained causes the cached form
                #       omits). The chain helper fails CLOSED:
                #       any uncertainty (str raises, redact raises,
                #       malformed chain) returns True.
                should_null_exc_info = exc_text_changed
                if not should_null_exc_info:
                    if _exc_chain_reveals_secret(
                        record.exc_info[1], _redact_value
                    ):
                        should_null_exc_info = True
                if should_null_exc_info:
                    record.exc_info = None
            except Exception:
                # PR #727 round 3 LOW #3: fail SAFE, not fail OPEN.
                # The earlier "leave the record alone" path emitted
                # the original (potentially-secret-containing)
                # ``exc_text`` if redact_value raised. Since the
                # record will still be written by the Formatter,
                # silent-swallow turns a redaction failure into a
                # silent secret leak. Replace ``exc_text`` with an
                # operator-visible placeholder so the failure is
                # surfaced AND no secret survives. Suppressing
                # ``exc_info`` likewise prevents the Formatter from
                # re-rendering the unredacted version.
                record.exc_text = (
                    "[REDACTING-FILTER FAILURE: exception text "
                    "suppressed; investigate redact_value]"
                )
                record.exc_info = None
        elif record.exc_text:
            # PR #727 round 10 MED: orphan exc_text. CPython's
            # ``Formatter.format`` appends ``record.exc_text``
            # **independently** of ``record.exc_info`` — the
            # ``if record.exc_text:`` branch is checked uncondition-
            # ally even when ``exc_info`` is None. Section 2 above
            # only runs when ``exc_info`` is set, so a record with
            # ``exc_info=None`` and ``exc_text="auth failed: ghp_…"``
            # passed straight through the filter unredacted. Reachable
            # shapes: application code that sets ``exc_text`` directly
            # without populating ``exc_info``; a prior filter that
            # nulled ``exc_info`` (e.g., this filter on a previous
            # record copied/re-emitted) but left ``exc_text`` intact;
            # any pipeline that pre-renders the exception text.
            #
            # Redact in place. There is no ``exc_info`` to null and
            # no fast-path ambiguity here — only the cached text.
            try:
                record.exc_text = _redact_value(record.exc_text)
            except Exception:
                record.exc_text = (
                    "[REDACTING-FILTER FAILURE: orphan exc_text "
                    "suppressed; investigate redact_value]"
                )

        # 3. Redact stack_info (``logger.foo(..., stack_info=True)``).
        if record.stack_info:
            try:
                record.stack_info = _redact_value(record.stack_info)
            except Exception:
                # PR #727 round 3 LOW #3: same fail-safe rationale.
                record.stack_info = (
                    "[REDACTING-FILTER FAILURE: stack_info suppressed; "
                    "investigate redact_value]"
                )

        return True

# Process-global set of keys that have already emitted a WARNING.
# Never cleared — once a key is warned, subsequent calls log at DEBUG.
_warned_keys: set[str] = set()
_warned_keys_lock = threading.Lock()


def log_warning_once(key: str, message: str, **fields: Any) -> None:
    """Warn the first time *key* is seen, then log at DEBUG on repeats.

    Use for conditions that recur every write (e.g. missing optional
    backend) where repeated WARNING-level output is noise.
    """
    with _warned_keys_lock:
        seen = key in _warned_keys
        if not seen:
            _warned_keys.add(key)

    if seen:
        log_debug(message, **fields)
    else:
        log_warning(message, **fields)


def _reset_logging_state() -> None:
    """Reset logging state for testing.

    This clears cached config and resets the logger initialization flag,
    allowing tests to reconfigure logging with different settings.

    WARNING: Only use this in tests - not thread-safe during normal operation.
    """
    global _logger_initialized, _cached_logging_config, _session_start

    _cached_logging_config = None
    _session_start = None
    with _warned_keys_lock:
        _warned_keys.clear()

    # Reset logger handlers for ALL namespaces if already initialized
    if _logger_initialized:
        _logger_initialized = False
        for namespace in LOGGER_NAMESPACES:
            ns_logger = logging.getLogger(namespace)
            ns_logger.handlers.clear()
            # PR #727 round 2 MED #2: also clear filters. The
            # RedactingFilter is now attached at logger level (not
            # handler level), so a stale filter survives a
            # handlers-only reset and silently mutates records the
            # next test produces. Clearing both keeps the reset's
            # contract — a clean-slate logger.
            ns_logger.filters.clear()
            ns_logger.propagate = True  # Re-enable propagation for pytest caplog


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

                # Also log to stderr for visibility.
                # In hosted mode, default stream level to INFO so that
                # request traces are visible in Railway/container stderr.
                # Override with WATERCOOLER_LOG_STREAM_LEVEL env var.
                stream_handler = logging.StreamHandler()
                stream_handler.setFormatter(formatter)
                stream_level_name = os.environ.get(ENV_LOG_STREAM_LEVEL, "")
                if stream_level_name:
                    stream_level = getattr(logging, stream_level_name.upper(), None)
                    if stream_level is None:
                        stream_level = max(log_level, logging.WARNING)
                else:
                    is_hosted = os.environ.get("WATERCOOLER_MODE", "") == "hosted"
                    if is_hosted:
                        stream_level = max(log_level, logging.INFO)
                    else:
                        stream_level = max(log_level, logging.WARNING)
                stream_handler.setLevel(stream_level)
                handlers.append(stream_handler)

                # PR #727 round 1 MED #3: attach the RedactingFilter at
                # the LOGGER level (one filter instance per namespace),
                # not the handler level. Logger filters run exactly
                # once per record before dispatch to any handler — so
                # a third-party / late-registered handler will see the
                # already-redacted record without needing the filter
                # itself. Handler-level mutation would silently alias
                # across handlers and break if such a handler appeared.
                redacting_filter = _RedactingFilter()

                # Configure ALL watercooler logger namespaces with shared handlers
                # This ensures logging from watercooler, watercooler_mcp, and
                # watercooler_memory all go to the same place with the same format
                #
                # When file logging is enabled, we disable propagation to prevent
                # duplicate logs. When file logging is disabled (test mode), we
                # allow propagation so pytest's caplog fixture can capture logs.
                disable_propagation = log_file is not None
                for namespace in LOGGER_NAMESPACES:
                    ns_logger = logging.getLogger(namespace)
                    ns_logger.handlers.clear()  # Remove any existing handlers
                    # PR #727 round 2 MED #1 + round 3 MED #1 / LOW #1:
                    # repeated ``_get_logger`` calls (e.g., test resets)
                    # would stack duplicate ``_RedactingFilter`` instances
                    # without dedup; list-replacement
                    # (``ns_logger.filters = [...]``) is not thread-safe.
                    # The security gap is asymmetric: a missed handler is
                    # observable (no log line written) but a missed filter
                    # pass leaks a secret silently.
                    #
                    # Order is load-bearing: ADD first, then REMOVE the
                    # stale instances. After ``addFilter`` there is at
                    # least one ``_RedactingFilter`` on the logger; after
                    # each ``removeFilter`` of a stale instance, the new
                    # one is still attached. A concurrent log call at any
                    # point during this loop sees ≥1 filter and gets
                    # redacted. The reverse order (remove-then-add) had a
                    # window between the last ``removeFilter`` and
                    # ``addFilter`` where a concurrent log emitted
                    # unredacted.
                    ns_logger.addFilter(redacting_filter)
                    for stale in [
                        f for f in ns_logger.filters
                        if isinstance(f, _RedactingFilter) and f is not redacting_filter
                    ]:
                        ns_logger.removeFilter(stale)
                    ns_logger.setLevel(log_level)
                    ns_logger.propagate = not disable_propagation
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
