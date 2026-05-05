import json
import logging
import importlib.util
import os
import sys
from pathlib import Path
from io import StringIO
from unittest import mock

import pytest

# Import module directly from file to avoid importing package __init__
# (which pulls fastmcp). Setting ``__package__`` to ``watercooler_mcp``
# is load-bearing — without it the relative import
# ``from .secrets.gateway import redact_value`` inside observability.py
# fails with "attempted relative import with no known parent package",
# which silently makes ``obs._redact_value = None`` and turns the
# RedactingFilter into a no-op for every test in this file. PR #727
# round 3 MED #1 caught the bug. Using the canonical dotted name
# (``watercooler_mcp.observability``) also makes the relative import
# resolve correctly via ``importlib`` machinery.
# PR #727 round 9 L2 + round 11 LOW: resolve relative to this test
# file rather than CWD so a CI runner / IDE invocation that cd's
# elsewhere still finds observability.py. With CWD-relative
# resolution and the round-3 ``__package__`` annotation, a
# wrong-CWD invocation would fail at ``spec.loader.exec_module``
# with an opaque AttributeError instead of a clear path error.
# ``parents[2]`` (== ``.parent.parent.parent``) anchors at repo
# root for the current ``tests/unit/test_observability.py``
# location; the explicit ``assert exists()`` below catches a
# silent path-resolution drift if this test file is ever moved.
_OBS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "watercooler_mcp" / "observability.py"
)
assert _OBS_PATH.exists(), (
    f"observability.py not found at expected path {_OBS_PATH}. "
    "If this test file moved, update the parents[N] index."
)
spec = importlib.util.spec_from_file_location(
    "watercooler_mcp.observability", _OBS_PATH
)
obs = importlib.util.module_from_spec(spec)
obs.__package__ = "watercooler_mcp"
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

# PR #727 round 3 MED #2: round 1 RedactingFilter tests imported
# the real ``watercooler_mcp.observability`` module via the package
# (a ``from watercooler_mcp import observability`` style import),
# which produced a separate object from ``obs`` above. The two had
# distinct ``_logger_initialized`` / ``_logger_lock`` /
# ``_warned_keys`` globals; the autouse ``reset_logger`` fixture
# only reset the importlib-loaded ``obs`` instance, so
# test-isolation could leak between classes depending on execution
# order. Pull every RedactingFilter symbol off the SAME ``obs``
# reference to keep state coherent across the file.
_RedactingFilter = obs._RedactingFilter
_emit_redact_import_failure_warning = obs._emit_redact_import_failure_warning
LOGGER_NAMESPACES = obs.LOGGER_NAMESPACES


@pytest.fixture(autouse=True)
def reset_logger(monkeypatch):
    """Reset logger state between tests.

    Disables file logging so propagation is enabled for pytest caplog.

    PR #727 round 9 M2: also snapshot/restore ``obs._redact_value``.
    Tests in ``TestRedactingFilterImportFailureSignal`` patch this
    module global to ``None`` to simulate the import failure. If a
    test crashes between mutation and pytest's monkeypatch teardown
    (or if a future test uses ``setattr`` directly), the
    process-wide global stays clobbered and every subsequent test
    in the session runs with redaction silently disabled. Restoring
    on every fixture exit guarantees a clean slate regardless of how
    a prior test exited.
    """
    # Disable file logging to enable log propagation (for caplog to work)
    monkeypatch.setenv("WATERCOOLER_LOG_DISABLE_FILE", "1")
    # Snapshot the redact_value binding before each test runs.
    _saved_redact_value = obs._redact_value
    # Use the module's reset function to clear all cached state
    _reset_logging_state()
    yield
    # Clean up after test
    _reset_logging_state()
    # Restore the redact_value binding even if the test crashed or
    # used ``setattr`` directly (bypassing monkeypatch's auto-undo).
    obs._redact_value = _saved_redact_value


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


# ---------------------------------------------------------------------- #
# Move 4 / Move 6: RedactingFilter tests
# ---------------------------------------------------------------------- #


import logging
import pytest

# PR #727 round 3 MED #1: the previous ``from
# watercooler_mcp.observability import _RedactingFilter`` here
# shadowed the module-level ``_RedactingFilter`` alias bound at
# line 39 from the importlib-loaded ``obs``. That defeated the
# round-2 test-isolation fix — tests then ran against the package-
# loaded module instance whose state didn't get reset by the
# autouse ``reset_logger`` fixture. The alias above is the
# canonical reference for the rest of this file.


class TestRedactingFilter:
    """The filter is the egress chokepoint that catches any
    secret-shaped string that leaked into a log line — defense-in-
    depth over per-call-site redaction (Secret wrappers, explicit
    redact_value calls). Tests pin: tokens get redacted, non-secret
    text passes through, exceptions during redact never drop a
    log line."""

    def _make_record(self, msg: str, args: tuple = ()) -> logging.LogRecord:
        return logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_passes_non_secret_message_unchanged(self) -> None:
        f = _RedactingFilter()
        record = self._make_record("Hello world, no secrets here")
        assert f.filter(record) is True
        assert record.getMessage() == "Hello world, no secrets here"

    def test_redacts_github_pat_in_message(self) -> None:
        f = _RedactingFilter()
        record = self._make_record(
            "Auth failed with token ghp_aaaaaaaaaaaaaaaaaaaaaaa for user alice"
        )
        f.filter(record)
        out = record.getMessage()
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in out
        assert "[REDACTED:ghp_*]" in out
        assert "alice" in out  # context preserved

    def test_redacts_slack_xoxb_in_message(self) -> None:
        f = _RedactingFilter()
        record = self._make_record(
            "Slack call failed: xoxb-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
        )
        f.filter(record)
        out = record.getMessage()
        assert "xoxb-1234567890123" not in out
        assert "[REDACTED:xoxb_*]" in out

    def test_redacts_after_args_interpolation(self) -> None:
        """The filter operates on the formatted message, so a token
        smuggled in via ``args`` still gets redacted. This is the
        common log-call shape: ``logger.info('user=%s tok=%s', u, t)``.
        """
        f = _RedactingFilter()
        record = self._make_record(
            "user=%s token=%s",
            ("alice", "ghp_aaaaaaaaaaaaaaaaaaaaaaa"),
        )
        f.filter(record)
        out = record.getMessage()
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in out
        assert "[REDACTED:ghp_*]" in out
        assert "alice" in out

    def test_filter_never_drops_records(self) -> None:
        """The filter must always return True even on internal errors
        — dropping a log line is worse than missing a redaction
        (the latter is caught by other layers; the former loses
        signal entirely)."""
        f = _RedactingFilter()
        record = self._make_record("normal msg")
        # Forcibly break getMessage by setting a non-format-string
        # msg with mismatched args. The redact path falls through to
        # the bare-return-True branch.
        record.args = ("oops",)  # No %s in msg → TypeError on getMessage
        assert f.filter(record) is True

    def test_round15_section1_failure_clears_args_with_token(self) -> None:
        """PR #727 round 15 M1: when ``getMessage()`` raises (mis-
        matched %s/args), the previous failure path was ``pass``
        — leaving ``record.args`` intact. If ``args`` itself
        contained a token (``logger.info("tok=%s", "ghp_<token>")``
        with a malformed format string that broke interpolation),
        a handler reading ``record.args`` or ``record.__dict__``
        directly (structlog, python-json-logger, Sentry shims)
        bypassed the formatted-message path and re-surfaced the
        raw token.

        The fix clears ``record.args`` in the failure path, matching
        the successful-redaction path's behaviour. Args info is
        lost but on a record whose ``getMessage`` already raised
        the args wouldn't have rendered correctly anyway.
        """
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="no format specifier",  # No %s
            args=("ghp_aaaaaaaaaaaaaaaaaaaaaaa",),  # Mismatch → TypeError
            exc_info=None,
        )
        # Sanity: getMessage actually raises on this record.
        with pytest.raises(TypeError):
            record.getMessage()
        # Pre-condition: token visible in args.
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" in record.args
        f.filter(record)
        # Post-condition: args cleared. A handler reading
        # ``record.args`` directly cannot leak the token.
        assert record.args == ()

    def test_section1_failure_still_runs_exc_info_redaction(self) -> None:
        """PR #727 round 5 MED: when ``getMessage()`` raises (mis-
        matched %s/args) the filter previously returned early,
        skipping sections 2 and 3 — so a token in ``exc_info`` would
        leak whenever the bad-format-string condition coincided with
        an exception payload. Sections 2 and 3 carry independent
        secret surfaces; section 1's failure must NOT short-circuit
        them.
        """
        f = _RedactingFilter()
        try:
            raise ValueError(
                "auth failed for token ghp_aaaaaaaaaaaaaaaaaaaaaaa"
            )
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        # Build a record whose msg deliberately breaks getMessage
        # (no %s but args provided → TypeError on interpolation),
        # AND carries a real exception with a token.
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="no format specifier here",
            args=("oops_arg",),  # mismatched → TypeError
            exc_info=exc_info,
        )
        # Sanity: getMessage actually does raise on this record.
        with pytest.raises(TypeError):
            record.getMessage()
        f.filter(record)
        # Section 2 must still run despite section 1 failure.
        assert record.exc_text is not None
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.exc_text
        assert "[REDACTED:ghp_*]" in record.exc_text
        assert record.exc_info is None  # round 4 invariant preserved

    def test_section1_failure_still_runs_stack_info_redaction(self) -> None:
        """Same invariant for section 3 (stack_info)."""
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="no format specifier here",
            args=("oops_arg",),  # mismatched → TypeError on getMessage
            exc_info=None,
        )
        record.stack_info = (
            'File "x.py", line 1, in foo\n'
            '    token = "ghp_aaaaaaaaaaaaaaaaaaaaaaa"'
        )
        f.filter(record)
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.stack_info
        assert "[REDACTED:ghp_*]" in record.stack_info

    def test_short_lookalike_not_redacted(self) -> None:
        # SECRET_PATTERN requires 20+ chars after the prefix.
        f = _RedactingFilter()
        record = self._make_record("debug trace: ghp_short")
        f.filter(record)
        # Short string passes through.
        assert "ghp_short" in record.getMessage()

    def test_no_args_after_redaction(self) -> None:
        """When the filter rewrites msg, it must clear args so the
        formatter doesn't try to re-interpolate (which would either
        TypeError or strip the redaction)."""
        f = _RedactingFilter()
        record = self._make_record(
            "tok=%s", ("ghp_aaaaaaaaaaaaaaaaaaaaaaa",)
        )
        f.filter(record)
        # Either args is empty (mutated to ()) or msg already contains
        # the redacted form. Both shapes prevent re-interpolation.
        assert record.args == ()
        assert "[REDACTED:ghp_*]" in record.msg


class TestRedactingFilterExceptionPaths:
    """PR #727 round 1 MED #1: exc_info / stack_info paths must also
    redact, not just ``record.msg``. Standard Formatter renders
    ``record.exc_info`` and ``record.stack_info`` AFTER the filter
    returns, so a token in an exception body or stack frame
    bypasses message-only redaction. The filter pre-formats and
    redacts both before the Formatter sees them.
    """

    def _make_record_with_exc(
        self, msg: str, exc_info: tuple
    ) -> logging.LogRecord:
        return logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=exc_info,
        )

    def _make_record_with_stack(
        self, msg: str, stack_info: str
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        record.stack_info = stack_info
        return record

    def test_redacts_token_in_exception_message(self) -> None:
        f = _RedactingFilter()
        try:
            raise ValueError(
                "auth failed for token ghp_aaaaaaaaaaaaaaaaaaaaaaa"
            )
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        f.filter(record)
        assert record.exc_text is not None
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.exc_text
        assert "[REDACTED:ghp_*]" in record.exc_text

    def test_preserves_exc_info_when_no_secret_present(self) -> None:
        """PR #727 round 6 MED + round 12 M2: nulling
        ``record.exc_info`` must be SCOPED to the case where
        redaction actually changed the exception text. Otherwise
        every non-secret exception loses its exc_info — Sentry,
        GCP CloudLoggingHandler, structlog JSON renderers, etc.
        that read ``record.exc_info[1]`` for exception-type routing
        and chained-cause walking would receive ``None`` for
        entirely benign exceptions.

        Round 12 M2 also asserts ``record.exc_text`` remains
        ``None`` for a benign exception — the round-12 M1 gate
        prevents the filter from pre-populating ``exc_text`` for
        non-secret exceptions, which would otherwise short-circuit
        any downstream Formatter subclass that overrides
        ``formatException`` (structured-JSON formatters, Sentry
        breadcrumb formatters, colorized handlers).
        """
        f = _RedactingFilter()
        try:
            raise ValueError("ordinary failure with no token here")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("benign error", exc_info)
        original_exc_info = record.exc_info
        assert original_exc_info is not None
        # Pre-condition: no exc_text yet (records start clean).
        assert record.exc_text is None
        f.filter(record)
        # No secret → no redaction-mutation → exc_info preserved.
        assert record.exc_info is original_exc_info
        assert record.exc_info[0] is ValueError
        # Round 12 M1: filter must NOT pre-populate exc_text for a
        # benign exception. Downstream Formatter subclasses retain
        # their ability to render the exception via custom
        # formatException.
        assert record.exc_text is None

    def test_clears_exc_info_after_successful_redaction(self) -> None:
        """PR #727 round 4 HIGH: after pre-formatting and redacting
        the exception, ``record.exc_info`` must be nulled. Otherwise
        any handler that reads ``record.exc_info[1]`` directly
        (structured JSON handlers, GCP CloudLoggingHandler, structlog,
        custom Sentry shims) re-surfaces the raw exception object,
        which still carries the unredacted token in its message and
        in traceback frame locals. Stdlib Formatter honours the
        cached ``exc_text`` so this is invisible there, but the
        defense-in-depth claim must hold for non-stdlib handlers too.
        """
        f = _RedactingFilter()
        try:
            raise ValueError(
                "auth failed for token ghp_aaaaaaaaaaaaaaaaaaaaaaa"
            )
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        # Pre-condition: filter receives a populated exc_info.
        assert record.exc_info is not None
        assert record.exc_info[0] is ValueError
        f.filter(record)
        # Post-condition: exc_info is nulled, exc_text holds the
        # redacted form. A handler that does
        # ``str(record.exc_info[1])`` cannot leak the token.
        assert record.exc_info is None
        assert record.exc_text is not None
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.exc_text

    def test_nulls_exc_info_when_pre_existing_exc_text_omits_token(self) -> None:
        """PR #727 round 7 LOW: the round-6 gate compared
        ``redacted_exc != exc_text``, but ``exc_text`` could come
        from the fast-path (pre-existing ``record.exc_text``). If a
        prior filter or application code populated ``exc_text`` with
        text that omits a token still present in
        ``record.exc_info[1].args``, redact_value returns the cached
        text unchanged, the gate is False, and exc_info survives —
        which lets a downstream handler reading
        ``str(record.exc_info[1])`` re-surface the raw token.
        Round 7 cross-checks the live exception's ``str()`` to close
        this gap while preserving the round-6 win for non-secret
        exceptions.
        """
        f = _RedactingFilter()
        try:
            raise ValueError(
                "auth: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
            )
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        # Simulate a prior filter that populated exc_text with a
        # sanitised form omitting the token. The token is still in
        # the live exception's args.
        record.exc_text = "auth: [no token here]"
        assert "ghp_" in str(record.exc_info[1])
        f.filter(record)
        # Live exception object would leak the token if exc_info
        # survived. Round 7 must null it.
        assert record.exc_info is None

    def test_round8_redact_failure_on_live_repr_preserves_exc_text(
        self, monkeypatch
    ) -> None:
        """PR #727 round 8 LOW: the live-exception cross-check must
        have its own try/except. If ``_redact_value`` raises only on
        the live ``str(exc_info[1])`` (not on the cached exc_text
        already redacted), the outer except would otherwise overwrite
        a correctly-redacted ``record.exc_text`` with the
        ``REDACTING-FILTER FAILURE`` placeholder — for a benign
        non-secret exception, that's data loss without signal.

        Fail-safe: keep the already-redacted exc_text, null exc_info
        (the unverifiable surface).
        """
        # Build a synthetic ``redact_value`` that succeeds on the
        # exc_text input but raises on str(exc_info[1]).
        original = obs._redact_value

        def selective_redact(value: str) -> str:
            if value == "BENIGN_LIVE_REPR_RAISE":
                raise RuntimeError("synthetic failure on live_repr")
            return original(value)

        monkeypatch.setattr(obs, "_redact_value", selective_redact)
        # Reload the alias used inside the filter via the module-level
        # patch (the filter dereferences ``_redact_value`` from the
        # module each call, so monkeypatching obs._redact_value
        # propagates).

        class BadException(Exception):
            def __str__(self) -> str:  # noqa: D401
                return "BENIGN_LIVE_REPR_RAISE"

        try:
            raise BadException("benign no-secret payload")
        except BadException:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        f = _RedactingFilter()
        f.filter(record)
        # The round-8 invariant: a failure on the live_repr
        # cross-check must NOT cascade into the outer except (which
        # would overwrite ``record.exc_text`` with the failure
        # placeholder). PR #727 round 12 M1 added a gate that skips
        # the exc_text write when redaction didn't change anything,
        # so in this benign-payload scenario ``record.exc_text``
        # stays ``None`` rather than being the redacted form. Either
        # state is correct — the assertion that matters is that the
        # failure placeholder did NOT appear (which would indicate
        # the outer except fired and wiped a correctly-handled
        # record).
        assert "REDACTING-FILTER FAILURE" not in (record.exc_text or "")
        # exc_info must be nulled (live surface unverifiable).
        assert record.exc_info is None

    def test_round13_str_failure_on_exc_info_nulls_unverifiable_surface(
        self,
    ) -> None:
        """PR #727 round 13 HIGH: when ``str(record.exc_info[1])``
        raises (custom ``__str__`` that errors), we cannot verify
        whether the live exception object contains a token. Round 7
        set ``live_repr = ""`` in the except branch; that empty
        string short-circuits the redact-equality guard and leaves
        ``should_null_exc_info`` False — so ``exc_info[1]`` with
        full ``.args`` survives into the record and a downstream
        handler that reads it directly (Sentry, GCP, structlog)
        re-surfaces the raw token. That is fail-OPEN.

        Round 13 makes the str() failure conservatively null
        ``exc_info``: cannot verify → cannot trust the surface.

        Drives the path with a custom exception whose ``__str__``
        raises AND whose ``.args`` contain a token. The pre-existing
        ``record.exc_text`` is set to a sanitised form (no token),
        so the round-6 gate would say "no change" and preserve
        ``exc_info`` without the round-13 fix.
        """
        class StrRaises(Exception):
            def __str__(self) -> str:  # noqa: D401
                raise RuntimeError("synthetic __str__ failure")

        f = _RedactingFilter()
        try:
            raise StrRaises("auth: ghp_aaaaaaaaaaaaaaaaaaaaaaa")
        except StrRaises:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        # Pre-set a sanitised exc_text so the round-6 gate doesn't
        # trigger — without round 13, the live cross-check sets
        # live_repr="" and the surface survives.
        record.exc_text = "auth: [no token here]"
        # Sanity: the live exception's args still have the token,
        # accessible to anyone who reads exc_info[1].args[0]
        # without going through __str__.
        assert "ghp_" in record.exc_info[1].args[0]
        f.filter(record)
        # Round 13: unverifiable surface → null it.
        assert record.exc_info is None

    def test_round14_chained_cause_with_token_nulls_exc_info(self) -> None:
        """PR #727 round 14 (audit-driven): the live-exception
        cross-check must walk ``__cause__`` / ``__context__``, not
        just ``str()`` of the outer exception. ``raise X from Y``
        where Y carries a token would otherwise survive when a
        pre-existing sanitised ``exc_text`` is present — Sentry
        and structlog walk the chain and would re-surface the
        chained token. Previously a gap (round 7 only checked
        outer ``str()``).

        Drives: outer exception with no token, ``raise from``
        causal exception with a token in args. Pre-set sanitised
        ``exc_text`` so the round-6 gate doesn't trigger. Without
        the chain walk, ``exc_info`` would survive with the chain
        intact.
        """
        f = _RedactingFilter()
        try:
            try:
                raise ValueError(
                    "inner: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
                )
            except ValueError as inner:
                raise RuntimeError("outer: nothing here") from inner
        except RuntimeError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        # Sanitised pre-existing exc_text — round-6 gate sees no
        # change. Outer str() reveals no token. Without the chain
        # walk, exc_info[1].__cause__ still has the token.
        record.exc_text = "outer: [no token here]"
        f.filter(record)
        assert record.exc_info is None  # chain-walk caught it

    def test_round14_helper_returns_true_for_chained_secret(self) -> None:
        """Direct unit test for ``_exc_chain_reveals_secret``:
        a chained cause with a secret must return True regardless
        of whether the outer exception's ``str()`` reveals it.
        """
        try:
            try:
                raise ValueError("ghp_aaaaaaaaaaaaaaaaaaaaaaa")
            except ValueError as inner:
                raise RuntimeError("outer-clean") from inner
        except RuntimeError as outer:
            assert obs._exc_chain_reveals_secret(outer, obs._redact_value) is True

    def test_round14_helper_returns_false_for_clean_chain(self) -> None:
        """A chain with no secret in any node returns False so
        ``exc_info`` is preserved (I5 — Sentry/GCP routing).
        """
        try:
            try:
                raise ValueError("inner-clean")
            except ValueError as inner:
                raise RuntimeError("outer-clean") from inner
        except RuntimeError as outer:
            assert obs._exc_chain_reveals_secret(outer, obs._redact_value) is False

    def test_round14_helper_returns_true_when_str_raises(self) -> None:
        """Cannot verify → conservatively assume secret (round 13
        invariant preserved through the helper)."""
        class StrRaises(Exception):
            def __str__(self) -> str:  # noqa: D401
                raise RuntimeError("synthetic")

        exc = StrRaises("anything")
        assert obs._exc_chain_reveals_secret(exc, obs._redact_value) is True

    def test_round14_helper_returns_true_when_redact_raises(
        self, monkeypatch
    ) -> None:
        """Same conservative semantics if ``redact_fn`` itself
        raises on a node — uncertainty resolves to True.
        """
        def boom(_value: str) -> str:
            raise RuntimeError("synthetic redact failure")

        try:
            raise ValueError("anything")
        except ValueError as exc:
            assert obs._exc_chain_reveals_secret(exc, boom) is True

    def test_round15_helper_skips_context_when_suppressed_explicit_cause(
        self,
    ) -> None:
        """PR #727 round 15 M2: ``raise NewException() from cause``
        sets ``__suppress_context__ = True``. CPython's
        ``formatException`` skips ``__context__`` in this case;
        the helper must too. A clean outer exception with a
        suppressed-context inner that contains a token must NOT
        report a secret — the inner is invisible to every
        downstream renderer.
        """
        try:
            try:
                raise ValueError(
                    "secret-bearing inner: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
                )
            except ValueError:
                # ``raise X from None`` suppresses __context__.
                raise RuntimeError("clean outer") from None
        except RuntimeError as outer:
            # The inner ValueError is the __context__ of outer
            # (auto-attached) but __suppress_context__ is True.
            assert outer.__suppress_context__ is True
            assert outer.__context__ is not None
            assert "ghp_" in str(outer.__context__)
            # Helper must respect the suppression — outer is clean,
            # context is invisible to formatters.
            assert obs._exc_chain_reveals_secret(
                outer, obs._redact_value
            ) is False

    def test_round15_helper_walks_implicit_context_when_not_suppressed(
        self,
    ) -> None:
        """When ``__suppress_context__`` is False (no ``from``
        clause used), the implicit ``__context__`` IS rendered by
        stdlib formatException ("During handling of the above
        exception, another exception occurred:"). The helper must
        walk it for parity, catching tokens that downstream
        chain-walkers would render.
        """
        try:
            try:
                raise ValueError(
                    "implicit-context with token: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
                )
            except ValueError:
                # No ``from`` clause → implicit context, NOT
                # suppressed. CPython renders this in tracebacks.
                raise RuntimeError("clean outer")
        except RuntimeError as outer:
            assert outer.__suppress_context__ is False
            assert outer.__context__ is not None
            # Helper walks the visible context → True.
            assert obs._exc_chain_reveals_secret(
                outer, obs._redact_value
            ) is True

    def test_round15_helper_walks_explicit_cause_with_token(self) -> None:
        """``raise X from Y`` where Y has a token. Cause is
        explicit and rendered by formatException; helper must walk
        it. Pin alongside the suppression test so the
        cause/context branching is fully covered.
        """
        try:
            try:
                raise ValueError(
                    "cause with token: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
                )
            except ValueError as inner:
                raise RuntimeError("clean outer") from inner
        except RuntimeError as outer:
            assert outer.__cause__ is not None
            assert obs._exc_chain_reveals_secret(
                outer, obs._redact_value
            ) is True

    def test_round15_section2_preserves_exc_info_for_suppressed_context_secret(
        self,
    ) -> None:
        """End-to-end (round 15 M2): a benign exception whose
        suppressed-context contains a token must NOT have its
        ``exc_info`` nulled. Sentry/GCP routing for the legitimate
        outer exception keeps working; the suppressed-context token
        is invisible to renderers anyway.
        """
        f = _RedactingFilter()
        try:
            try:
                raise ValueError(
                    "leaked: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
                )
            except ValueError:
                raise RuntimeError("clean outer") from None
        except RuntimeError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("clean log", exc_info)
        f.filter(record)
        # exc_info preserved — outer is clean, suppressed context
        # is invisible.
        assert record.exc_info is exc_info

    def test_round14_helper_handles_self_referential_chain(self) -> None:
        """A pathological exception whose ``__cause__`` points back
        to itself must not loop forever. The helper deduplicates by
        ``id()``.
        """
        exc = RuntimeError("loop me")
        exc.__cause__ = exc  # self-loop
        # Should terminate; no secret in the message → False.
        assert obs._exc_chain_reveals_secret(exc, obs._redact_value) is False

    def test_round13_str_succeeds_with_no_secret_preserves_exc_info(
        self,
    ) -> None:
        """Companion to the round-13 test: when ``str()`` succeeds
        and reveals no secret, the round-6 + round-7 invariant
        still holds — exc_info is preserved. Round 13's fix
        scoped to the failure path only.
        """
        f = _RedactingFilter()
        try:
            raise ValueError("ordinary failure with no token")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("benign", exc_info)
        f.filter(record)
        assert record.exc_info is exc_info  # preserved

    def test_round10_orphan_exc_text_with_no_exc_info_redacted(self) -> None:
        """PR #727 round 10 MED: CPython's ``Formatter.format``
        appends ``record.exc_text`` independently of
        ``record.exc_info``. A record with ``exc_info=None`` but
        ``exc_text="auth: ghp_<token>"`` would render the token
        unredacted — the section-2 branch only runs when ``exc_info``
        is set. Section 2.5 covers the orphan case.
        """
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="see exc_text",
            args=(),
            exc_info=None,  # ← orphan case: no exc_info
        )
        record.exc_text = "auth failed: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
        f.filter(record)
        assert record.exc_text is not None
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.exc_text
        assert "[REDACTED:ghp_*]" in record.exc_text

    def test_round10_orphan_exc_text_redact_failure_falls_back_safe(
        self, monkeypatch
    ) -> None:
        """The orphan-exc_text branch must fail safe if
        ``_redact_value`` raises: replace ``exc_text`` with the
        operator-visible placeholder so no secret survives. (The
        record will still be emitted; silent-swallow would leak.)
        """
        def boom(value: str) -> str:
            raise RuntimeError("synthetic redact failure")

        monkeypatch.setattr(obs, "_redact_value", boom)
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="see exc_text",
            args=(),
            exc_info=None,
        )
        record.exc_text = "auth failed: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
        f.filter(record)
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in (record.exc_text or "")
        assert "REDACTING-FILTER FAILURE" in (record.exc_text or "")
        assert "orphan exc_text" in (record.exc_text or "")

    def test_round10_no_orphan_path_when_exc_info_set(self) -> None:
        """The orphan branch must NOT fire when ``exc_info`` is
        already populated — section 2 owns that case (with
        round-7 cross-check). Regression guard against accidentally
        making both branches run.
        """
        f = _RedactingFilter()
        try:
            raise ValueError(
                "auth: ghp_aaaaaaaaaaaaaaaaaaaaaaa"
            )
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = self._make_record_with_exc("see below", exc_info)
        # exc_info is set → section 2 path; exc_text not yet
        assert record.exc_text is None
        f.filter(record)
        # Section 2 ran: exc_text populated and redacted, exc_info
        # nulled (token was present).
        assert record.exc_text is not None
        assert "[REDACTED:ghp_*]" in record.exc_text
        assert record.exc_info is None

    def test_redacts_token_in_stack_info(self) -> None:
        f = _RedactingFilter()
        record = self._make_record_with_stack(
            "stack capture",
            'File "x.py", line 1, in foo\n    token = "ghp_aaaaaaaaaaaaaaaaaaaaaaa"',
        )
        f.filter(record)
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in record.stack_info
        assert "[REDACTED:ghp_*]" in record.stack_info

    def test_no_exc_info_does_not_create_exc_text(self) -> None:
        # Sanity: a record without exc_info should not have exc_text
        # populated by the filter (would mislead Formatter into
        # rendering an empty exception block).
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="normal",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert record.exc_text is None

    def test_zero_exception_sentinel_does_not_create_exc_text(self) -> None:
        """PR #727 round 2 MED #1: ``record.exc_info`` of
        ``(None, None, None)`` is the zero-exception sentinel and
        is truthy — but ``Formatter.formatException`` rejects it,
        and a bare ``except Exception:`` then leaves ``exc_text``
        in an indeterminate state. The fix matches CPython's own
        ``record.exc_info[0] is not None`` guard.
        """
        f = _RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="normal",
            args=(),
            exc_info=(None, None, None),
        )
        f.filter(record)
        # The filter must NOT have populated exc_text from the
        # sentinel. A non-None exc_text here would cause the
        # Formatter to emit a stray "None\nNone\nNone" or similar
        # garbage on the next render.
        assert record.exc_text is None


class TestRedactingFilterImportFailureSignal:
    """PR #727 round 1 MED #2 + round 2 LOW #3: if the secrets gateway
    can't be imported, the filter must (a) operate as a no-op so log
    records aren't dropped, AND (b) emit an operator-visible WARNING
    so the dead-defense doesn't fail silently.

    The warning normally fires at module-import time before any
    handler is attached, so ``caplog`` can't reliably intercept the
    actual import-time emission. Round 2 LOW #3 split the emission
    into a testable helper so the contract is pinned by direct
    invocation rather than depending on import timing.
    """

    def test_filter_is_no_op_when_redact_unavailable(self, monkeypatch) -> None:
        # Simulate import failure by patching the module-level binding.
        

        monkeypatch.setattr(obs, "_redact_value", None)

        f = obs._RedactingFilter()
        record = logging.LogRecord(
            name="watercooler_mcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token ghp_aaaaaaaaaaaaaaaaaaaaaaa",
            args=(),
            exc_info=None,
        )
        # Filter still returns True (records are not dropped) but
        # leaves the record unmutated.
        assert f.filter(record) is True
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" in record.getMessage()

    def test_snapshot_restore_pattern_recovers_from_raw_setattr(
        self,
    ) -> None:
        """PR #727 round 9 M2 + round 10 LOW: the round-9 tests were
        a paired clobber+verify shape that depended on test ordering
        — under ``pytest-randomly`` the verify side could run on a
        never-clobbered global and pass vacuously, hiding a broken
        fixture.

        This single self-contained test exercises the same
        snapshot/restore pattern the autouse ``reset_logger``
        fixture implements. It clobbers ``obs._redact_value`` via
        raw setattr (bypassing monkeypatch's auto-undo), verifies
        the clobber took effect, restores via the fixture-equivalent
        pattern, and asserts post-condition behaviour. Independent
        of test order; cannot pass vacuously.
        """
        saved = obs._redact_value
        assert callable(saved), "pre-condition: callable redact_value"
        try:
            obs._redact_value = None
            assert obs._redact_value is None  # clobber took effect
        finally:
            # Fixture-equivalent restore step.
            obs._redact_value = saved
        # Post-condition: ordinary log lines redact correctly again.
        assert obs._redact_value is saved
        assert (
            obs._redact_value("ghp_aaaaaaaaaaaaaaaaaaaaaaa")
            == "[REDACTED:ghp_*]"
        )

    def test_warning_helper_emits_to_stderr_for_import_time_visibility(
        self, capsys
    ) -> None:
        """PR #727 round 12 L1: at module-import time no handlers
        are attached, so a ``logger.warning`` call propagates to the
        root logger which silently discards it (default Python
        behaviour without ``basicConfig``). The dead-defense state
        is bad enough that we want a guaranteed-visible signal —
        ``_emit_redact_import_failure_warning`` mirrors the warning
        to ``sys.stderr`` directly. Pin that contract.
        """
        obs._emit_redact_import_failure_warning(
            ImportError("synthetic for stderr-visibility test")
        )
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "RedactingFilter" in captured.err
        assert "ImportError" in captured.err
        assert "WILL leak" in captured.err
        assert "Investigate" in captured.err

    def test_warning_helper_emits_actionable_message(
        self, caplog
    ) -> None:
        """``_emit_redact_import_failure_warning`` produces a
        WARNING-level log entry that includes the exception type +
        message, the operator-actionable fix ("Investigate the
        import path and restart"), and the "WILL leak" phrase that
        documents the consequence of the dead-defense state.

        This is the test that round-2 LOW #3 specifically asked for
        — direct verification of the warning's contract rather than
        relying on caplog catching it at import time.
        """
        

        ns = obs.LOGGER_NAME
        ns_logger = logging.getLogger(ns)
        original_propagate = ns_logger.propagate
        ns_logger.propagate = True
        try:
            with caplog.at_level(logging.WARNING, logger=ns):
                obs._emit_redact_import_failure_warning(
                    ImportError("simulated import failure")
                )
        finally:
            ns_logger.propagate = original_propagate

        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(records) >= 1, (
            f"expected ≥1 WARNING record, got {len(records)}"
        )
        message = records[-1].getMessage()
        # Exception type + value surfaced.
        assert "ImportError" in message
        assert "simulated import failure" in message
        # Contract phrases — operators and docs reference these.
        assert "WILL leak" in message
        assert "Investigate" in message


class TestRedactingFilterAttachmentArchitecture:
    """PR #727 round 1 MED #3: the filter is attached at the LOGGER
    level, not the handler level, so it runs exactly once per
    record before dispatch to any handler. A late-registered or
    third-party handler will see the already-redacted record
    without itself needing the filter. This test pins the
    architectural decision against accidental refactors that
    move the attachment back to handler level.
    """

    def test_filter_attached_at_logger_level_after_initialisation(
        self, monkeypatch, tmp_path
    ) -> None:
        

        # Force re-init with a tmp log dir so this test doesn't
        # collide with other tests' logger state.
        monkeypatch.setenv("WATERCOOLER_LOG_DIR", str(tmp_path))
        obs._reset_logging_state()

        # Trigger init.
        obs._get_logger()

        for ns in obs.LOGGER_NAMESPACES:
            ns_logger = logging.getLogger(ns)
            redact_filters = [
                fl for fl in ns_logger.filters
                if isinstance(fl, obs._RedactingFilter)
            ]
            assert len(redact_filters) == 1, (
                f"{ns}: expected exactly one RedactingFilter at logger "
                f"level, found {len(redact_filters)}"
            )

            # And NOT at any handler level (would be redundant).
            for handler in ns_logger.handlers:
                handler_redact_filters = [
                    fl for fl in handler.filters
                    if isinstance(fl, obs._RedactingFilter)
                ]
                assert handler_redact_filters == [], (
                    f"{ns} handler {handler} unexpectedly has a "
                    f"RedactingFilter — should be logger-level only"
                )

        obs._reset_logging_state()

    def test_namespaces_are_sibling_loggers_not_parent_child(self) -> None:
        """PR #727 round 11 MED (push-back / regression guard):
        ``LOGGER_NAMESPACES`` are ``watercooler``, ``watercooler_mcp``,
        ``watercooler_memory``. A reviewer flagged that the same
        ``_RedactingFilter`` instance attached to all three would
        run twice per record because ``watercooler_mcp`` is a child
        of ``watercooler`` in the logger hierarchy. This is incorrect:
        Python's logger hierarchy uses **dots** (``.``) as the
        separator, not underscores (``_``). ``watercooler.mcp`` would
        be a child of ``watercooler``; ``watercooler_mcp`` is a
        sibling top-level logger whose parent is ``root``. No
        propagation occurs between siblings.

        Pin the invariant so a future namespace addition that DOES
        introduce a parent-child relationship trips this test rather
        than silently double-firing the filter.
        """
        for ns in obs.LOGGER_NAMESPACES:
            ns_logger = logging.getLogger(ns)
            assert ns_logger.parent is logging.getLogger(), (
                f"{ns}: parent is {ns_logger.parent.name!r}, expected "
                "root. If a child-namespace is intentional, the filter "
                "attachment loop must be updated to avoid double-firing "
                "via propagation (set propagate=False on the child, or "
                "attach only at the parent)."
            )

    def test_filter_runs_exactly_once_per_record_via_counting_filter(
        self, monkeypatch, tmp_path
    ) -> None:
        """PR #727 round 11 MED (push-back evidence): empirically
        verify that emitting a record from any of
        ``LOGGER_NAMESPACES`` triggers the filter chain exactly
        once — no propagation-induced double-fire between siblings.

        Uses a counting filter as an observability probe. The real
        ``_RedactingFilter`` is idempotent on already-redacted text
        so a double-fire wouldn't cause a leak, but this test
        catches the design-intent violation directly.
        """
        monkeypatch.setenv("WATERCOOLER_LOG_DIR", str(tmp_path))
        obs._reset_logging_state()
        obs._get_logger()  # trigger init so logger state is set up

        calls: list[str] = []

        class CountingFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                calls.append(record.name)
                return True

        probe = CountingFilter()
        try:
            for ns in obs.LOGGER_NAMESPACES:
                logging.getLogger(ns).addFilter(probe)
            for ns in obs.LOGGER_NAMESPACES:
                calls.clear()
                logging.getLogger(ns).info("probe message")
                # Filter chain on the EMITTING logger fires exactly
                # once. If a sibling logger were a parent, the
                # propagation step would re-run filters on it,
                # producing a second call.
                assert calls == [ns], (
                    f"emitted from {ns} but filter call list was "
                    f"{calls} — expected exactly one call from {ns}"
                )
        finally:
            for ns in obs.LOGGER_NAMESPACES:
                logging.getLogger(ns).removeFilter(probe)
            obs._reset_logging_state()

    def test_repeated_init_does_not_stack_filters(
        self, monkeypatch, tmp_path
    ) -> None:
        

        monkeypatch.setenv("WATERCOOLER_LOG_DIR", str(tmp_path))

        for _ in range(3):
            obs._reset_logging_state()
            obs._get_logger()

        ns_logger = logging.getLogger("watercooler_mcp")
        redact_filters = [
            fl for fl in ns_logger.filters
            if isinstance(fl, obs._RedactingFilter)
        ]
        assert len(redact_filters) == 1, (
            f"expected one RedactingFilter after 3 init cycles, "
            f"found {len(redact_filters)} (filter stacking regression)"
        )

        obs._reset_logging_state()

    def test_reset_logging_state_clears_filters(
        self, monkeypatch, tmp_path
    ) -> None:
        """PR #727 round 2 MED #2: after the round-1 fix moved
        ``_RedactingFilter`` from handler-level to logger-level,
        ``_reset_logging_state`` was clearing handlers but not
        filters — so a stale filter survived a reset and silently
        mutated records the next test produced. This pins the
        clean-slate contract."""
        

        monkeypatch.setenv("WATERCOOLER_LOG_DIR", str(tmp_path))
        obs._reset_logging_state()
        obs._get_logger()

        ns_logger = logging.getLogger("watercooler_mcp")
        # Pre-condition: a RedactingFilter is attached.
        pre_redact = [
            fl for fl in ns_logger.filters
            if isinstance(fl, obs._RedactingFilter)
        ]
        assert pre_redact, "init failed to attach RedactingFilter"

        # Reset and verify the filter is gone.
        obs._reset_logging_state()

        post_redact = [
            fl for fl in ns_logger.filters
            if isinstance(fl, obs._RedactingFilter)
        ]
        assert post_redact == [], (
            f"_reset_logging_state did not clear filters; found "
            f"{len(post_redact)} stale RedactingFilter(s) — round 2 "
            f"MED #2 regression"
        )
