"""Tests for #1121 — watercooler_write can author the first entry on a new topic.

Two halves:
1. ``watercooler_write`` exposes ``create_if_missing`` and passes it through
   to say semantics on the ``next_actor="auto"`` branch (the hosted path is
   the one that enforces it — local ``commands_graph.say`` auto-creates).
2. The thread-not-found error is actionable: it names the
   ``create_if_missing=true`` remedy and suggests near-miss topic slugs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.errors import ThreadNotFoundError
from watercooler_mcp.tools.thread_write import _write_impl


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def hosted_context(tmp_path, threads_dir, monkeypatch):
    """Context patched to take the hosted path in _say_impl."""
    ctx = ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        code_repo="test-org/test-repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=True,
    )
    monkeypatch.setattr(validation, "_require_context", lambda _: (None, ctx))
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda _: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda _: None)
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.is_hosted_context", lambda _: True
    )
    monkeypatch.setattr(
        "watercooler_mcp.daemons.ensure_hosted_scope_for_current_context",
        lambda reason: None,
    )
    return ctx


class _SayHostedRecorder:
    """Stands in for say_hosted; records kwargs, returns a canned result."""

    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            return (self._error, {})
        return (None, {"status": "OPEN", "ball": "Agent"})


_WRITE_ARGS = dict(
    body="Spec: implementer\nFirst entry.",
    ctx=None,
    role="implementer",
    agent_func="Claude Code:test-model:implementer",
    code_path="/tmp/repo",
)


class TestCreateIfMissingPassthrough:
    def test_write_passes_create_if_missing_true(self, hosted_context, monkeypatch):
        recorder = _SayHostedRecorder()
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.say_hosted", recorder
        )
        result = _write_impl(
            topic="brand-new-topic", create_if_missing=True, **_WRITE_ARGS
        )
        assert "✅" in result
        assert recorder.calls[0]["create_if_missing"] is True

    def test_write_defaults_to_no_create(self, hosted_context, monkeypatch):
        recorder = _SayHostedRecorder()
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.say_hosted", recorder
        )
        _write_impl(topic="existing-topic", **_WRITE_ARGS)
        assert recorder.calls[0]["create_if_missing"] is False


class TestActionableNotFoundError:
    def _not_found_write(self, monkeypatch, suggestions):
        recorder = _SayHostedRecorder(
            error="Thread 'brand-new-topic' not found and create_if_missing=False"
        )
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.say_hosted", recorder
        )
        monkeypatch.setattr(
            "watercooler_mcp.hosted_ops.nearest_topics_hosted",
            lambda topic, limit=3: suggestions,
        )
        with pytest.raises(ThreadNotFoundError) as exc_info:
            _write_impl(topic="brand-new-topic", **_WRITE_ARGS)
        return str(exc_info.value)

    def test_error_names_the_remedy(self, hosted_context, monkeypatch):
        message = self._not_found_write(monkeypatch, [])
        assert "create_if_missing=true" in message
        assert "check the topic slug" in message

    def test_error_suggests_near_miss_topics(self, hosted_context, monkeypatch):
        message = self._not_found_write(
            monkeypatch, ["brand-new-topics", "brand-old-topic"]
        )
        assert "Nearest existing topics" in message
        assert "brand-new-topics" in message
        assert "brand-old-topic" in message

    def test_suggestion_failure_never_masks_the_error(
        self, hosted_context, monkeypatch
    ):
        """nearest_topics_hosted is best-effort by contract; simulate the
        helper returning nothing (its own internal failure mode)."""
        message = self._not_found_write(monkeypatch, [])
        assert "not found" in message
        assert "Nearest existing topics" not in message


class TestThreadNotFoundErrorShape:
    def test_bare_error_message_unchanged(self):
        err = ThreadNotFoundError(topic="x", repo="org/repo")
        assert str(err) == "Thread 'x' not found in repository: org/repo"

    def test_hint_and_suggestions_folded_into_message(self):
        err = ThreadNotFoundError(
            topic="x",
            repo="org/repo",
            hint="Pass create_if_missing=true.",
            suggestions=["x-ray", "x-files"],
        )
        message = str(err)
        assert "Pass create_if_missing=true." in message
        assert "Nearest existing topics: x-ray, x-files." in message


class TestNearestTopicsHosted:
    def _fake_client(self, names):
        class _Item:
            def __init__(self, name):
                self.name = name
                self.type = "dir"

        class _Client:
            def list_files(self, path):
                return [_Item(n) for n in names]

        return _Client()

    def test_ranks_close_matches(self, monkeypatch):
        from watercooler_mcp import hosted_ops

        client = self._fake_client(
            ["proxy-transport-findings", "slack-integration", "unrelated"]
        )
        monkeypatch.setattr(
            hosted_ops, "_get_github_client", lambda: (None, client)
        )
        result = hosted_ops.nearest_topics_hosted("proxy-transport-finding")
        assert result[0] == "proxy-transport-findings"
        assert "unrelated" not in result

    def test_client_failure_returns_empty(self, monkeypatch):
        from watercooler_mcp import hosted_ops

        monkeypatch.setattr(
            hosted_ops, "_get_github_client", lambda: ("boom", None)
        )
        assert hosted_ops.nearest_topics_hosted("anything") == []

    def test_listing_exception_returns_empty(self, monkeypatch):
        from watercooler_mcp import hosted_ops

        class _Exploding:
            def list_files(self, path):
                raise RuntimeError("api down")

        monkeypatch.setattr(
            hosted_ops, "_get_github_client", lambda: (None, _Exploding())
        )
        assert hosted_ops.nearest_topics_hosted("anything") == []
