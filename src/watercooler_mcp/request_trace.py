"""Request-scoped tracing for hosted MCP tool calls.

Records named stages with duration and outcome for each request flowing
through the HTTP middleware pipeline.  Stages map 1-to-1 to the pipeline
steps in ``server_http.py`` (authenticate, rate-limit, tool dispatch, …)
but can also be used inside individual tool implementations.

Usage in middleware::

    from .request_trace import RequestTrace, set_request_trace, trace_stage

    trace = RequestTrace(request_id=rid, tool_name="watercooler_say")
    token = set_request_trace(trace)
    try:
        with trace_stage("auth.resolve_api_key"):
            ...
        with trace_stage("tool.execute", backend="baseline"):
            ...
        trace.emit_log()
    finally:
        clear_request_trace(token)

Usage in tool code::

    from .request_trace import trace_stage

    with trace_stage("graphiti.init", tier="t2"):
        graph = await init_graph()
"""

from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Literal

from .observability import log_action

# ---------------------------------------------------------------------------
# Metadata safety
# ---------------------------------------------------------------------------

ALLOWED_METADATA_KEYS: frozenset[str] = frozenset({
    "count",
    "backend",
    "route",
    "cache_hit",
    "profile",
    "surface",
    "capability",
    "tier",
    "warm_state",
    "fallback_used",
    "source",
})

_MAX_METADATA_STR_LEN = 200


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip disallowed keys and truncate long string values.

    Only keys present in ``ALLOWED_METADATA_KEYS`` are kept.  String values
    longer than 200 characters are truncated with an ellipsis marker.

    Args:
        metadata: Raw metadata dict from caller.

    Returns:
        A new dict containing only safe, allowed entries.
    """
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in ALLOWED_METADATA_KEYS:
            continue
        if isinstance(value, str) and len(value) > _MAX_METADATA_STR_LEN:
            value = value[:_MAX_METADATA_STR_LEN] + "…"
        clean[key] = value
    return clean


# ---------------------------------------------------------------------------
# StageRecord
# ---------------------------------------------------------------------------

@dataclass
class StageRecord:
    """A single timed stage within a request trace.

    Attributes:
        name: Dot-delimited stage identifier (e.g. ``"auth.resolve_api_key"``).
        start_time: Monotonic timestamp captured at stage start.
        duration_ms: Elapsed wall-clock milliseconds for the stage.
        outcome: Terminal disposition — ``"ok"``, ``"error"``, ``"timeout"``,
            or ``"skipped"``.
        error_code: Machine-readable error tag when ``outcome`` is not ``"ok"``
            (e.g. ``"capability_not_enabled"``).
        metadata: Sanitized key/value pairs — only keys in
            ``ALLOWED_METADATA_KEYS`` are retained.
    """

    name: str
    start_time: float
    duration_ms: float
    outcome: Literal["ok", "error", "timeout", "skipped"]
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RequestTrace
# ---------------------------------------------------------------------------

@dataclass
class RequestTrace:
    """Accumulates stage records for a single HTTP request.

    Attributes:
        request_id: Correlation ID carried in ``X-Request-ID``.
        user_id: Resolved user identity (empty until auth completes).
        tool_name: MCP tool being invoked (empty for non-tool requests).
        cold_start: Whether this request triggered a cold-start path
            (first request after process boot, daemon init, etc.).
        stages: Ordered list of completed ``StageRecord`` instances.
    """

    request_id: str
    user_id: str = ""
    tool_name: str = ""
    cold_start: bool = False
    stages: list[StageRecord] = field(default_factory=list)

    # Private: tracks the monotonic start time of the currently open stage.
    _active_stage: str | None = field(default=None, repr=False)
    _active_start: float = field(default=0.0, repr=False)

    # ------------------------------------------------------------------
    # Stage lifecycle
    # ------------------------------------------------------------------

    def stage_start(self, name: str) -> None:
        """Mark the beginning of a named stage.

        Args:
            name: Dot-delimited stage identifier.
        """
        self._active_stage = name
        self._active_start = time.monotonic()

    def stage_end(
        self,
        name: str,
        outcome: str = "ok",
        **metadata: Any,
    ) -> StageRecord:
        """Finalise a stage with a successful (or explicit) outcome.

        Calculates duration from the matching ``stage_start`` call, sanitizes
        metadata, appends the record, and clears the active stage.

        Args:
            name: Must match the name passed to ``stage_start``.
            outcome: Disposition string (default ``"ok"``).
            **metadata: Arbitrary key/value pairs; filtered through
                ``ALLOWED_METADATA_KEYS``.

        Returns:
            The completed ``StageRecord``.
        """
        duration_ms = (time.monotonic() - self._active_start) * 1000.0
        record = StageRecord(
            name=name,
            start_time=self._active_start,
            duration_ms=round(duration_ms, 2),
            outcome=outcome,  # type: ignore[arg-type]
            metadata=_sanitize_metadata(metadata),
        )
        self.stages.append(record)
        self._active_stage = None
        return record

    def stage_error(
        self,
        name: str,
        error_code: str = "",
        **metadata: Any,
    ) -> StageRecord:
        """Convenience wrapper to finalise a stage with ``outcome="error"``.

        Args:
            name: Must match the name passed to ``stage_start``.
            error_code: Machine-readable error tag.
            **metadata: Extra metadata (filtered).

        Returns:
            The completed ``StageRecord``.
        """
        record = self.stage_end(name, outcome="error", **metadata)
        record.error_code = error_code
        return record

    # ------------------------------------------------------------------
    # Context manager for automatic start/end
    # ------------------------------------------------------------------

    @contextmanager
    def stage(self, name: str, **initial_metadata: Any) -> Generator[None, None, None]:
        """Context manager that brackets a stage with start/end calls.

        On normal exit the stage is closed with ``outcome="ok"``.  If an
        exception propagates, the stage is closed with ``outcome="error"``
        and the exception's class name is recorded as ``error_code``.

        Args:
            name: Dot-delimited stage identifier.
            **initial_metadata: Metadata attached to the resulting record.

        Yields:
            Nothing — the block runs inside the timed stage.
        """
        self.stage_start(name)
        try:
            yield
        except Exception as exc:
            self.stage_error(name, error_code=type(exc).__name__, **initial_metadata)
            raise
        else:
            self.stage_end(name, **initial_metadata)

    # ------------------------------------------------------------------
    # Summaries and logging
    # ------------------------------------------------------------------

    def total_duration_ms(self) -> float:
        """Sum of all completed stage durations in milliseconds."""
        return round(sum(s.duration_ms for s in self.stages), 2)

    def to_summary(self) -> dict[str, Any]:
        """Serialisable summary suitable for structured logging or metrics.

        Returns:
            Dict with ``request_id``, ``tool_name``, ``total_ms``,
            ``cold_start``, and a ``stages`` list of per-stage dicts
            (``name``, ``duration_ms``, ``outcome``).
        """
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "total_ms": self.total_duration_ms(),
            "cold_start": self.cold_start,
            "stages": [
                {
                    "name": s.name,
                    "duration_ms": s.duration_ms,
                    "outcome": s.outcome,
                }
                for s in self.stages
            ],
        }

    def emit_log(self) -> None:
        """Emit the trace summary via ``observability.log_action`` at INFO.

        The action name is ``"request_trace"`` and the summary dict is
        spread into the structured fields.
        """
        summary = self.to_summary()
        log_action(
            "request_trace",
            tool_name=self.tool_name,
            duration_ms=self.total_duration_ms(),
            request_id=self.request_id,
            cold_start=self.cold_start,
            stages=summary["stages"],
        )


# ---------------------------------------------------------------------------
# ContextVar helpers
# ---------------------------------------------------------------------------

_request_trace: contextvars.ContextVar[RequestTrace | None] = contextvars.ContextVar(
    "request_trace", default=None
)


def set_request_trace(trace: RequestTrace) -> contextvars.Token[RequestTrace | None]:
    """Bind *trace* to the current async context.

    Args:
        trace: The ``RequestTrace`` instance for this request.

    Returns:
        A token that can be passed to ``clear_request_trace`` to restore the
        previous value.
    """
    return _request_trace.set(trace)


def get_request_trace() -> RequestTrace | None:
    """Return the active ``RequestTrace``, or ``None`` outside a request."""
    return _request_trace.get()


def clear_request_trace(token: contextvars.Token[RequestTrace | None]) -> None:
    """Reset the context variable to its previous value.

    Args:
        token: The token returned by ``set_request_trace``.
    """
    _request_trace.reset(token)


# ---------------------------------------------------------------------------
# Standalone helper — works even when no trace is active
# ---------------------------------------------------------------------------

@contextmanager
def trace_stage(name: str, **initial_metadata: Any) -> Generator[None, None, None]:
    """Instrument a code section with the active request trace.

    If no ``RequestTrace`` is bound to the current context (e.g. in stdio
    mode or unit tests), the block executes without any tracing overhead.

    Args:
        name: Dot-delimited stage identifier.
        **initial_metadata: Metadata attached to the resulting record
            (filtered through ``ALLOWED_METADATA_KEYS``).

    Yields:
        Nothing — the wrapped block runs inside the timed stage.
    """
    trace = get_request_trace()
    if trace is None:
        yield
        return
    trace.stage_start(name)
    try:
        yield
    except Exception as exc:
        trace.stage_error(name, error_code=type(exc).__name__, **initial_metadata)
        raise
    else:
        trace.stage_end(name, **initial_metadata)
