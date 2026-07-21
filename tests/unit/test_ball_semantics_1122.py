"""Tests for #1122 — no phantom "Agent" ball; non-participant ball advisory.

Hosted say/handoff previously fell back to a hardcoded ``"Agent"`` ball
(no counterpart registry exists server-side), stranding new threads in
nobody's waiting-on view. Contract now:

1. Hosted say keeps the ball with the author; hosted handoff without a
   target does the same. Directing the ball at another actor is
   handoff's job.
2. Handing off to a target that has never authored on the thread emits
   a ``ball_warning`` advisory (both transports, shared wording via
   ``helpers.ball_target_warning``) so a typo'd target is visible at
   write time instead of orphaning the thread silently.
3. Write responses tell the truth about ball state ("Ball remains
   with" / Next: keep-working when no flip happened).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import hosted_ops, validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.helpers import ball_target_warning
from watercooler_mcp.tools.thread_write import _handoff_impl, _say_impl


_AUTHOR = "Claude Code (jay)"
_HUMAN = "Claude Code (caleb)"


# ---------------------------------------------------------------------------
# helpers.ball_target_warning
# ---------------------------------------------------------------------------


class TestBallTargetWarning:
    def test_known_participant_no_warning(self):
        assert ball_target_warning(_HUMAN, {_HUMAN, _AUTHOR}, _AUTHOR) is None

    def test_membership_is_case_insensitive(self):
        assert (
            ball_target_warning("claude code (CALEB)", {_HUMAN}, _AUTHOR) is None
        )

    def test_author_always_counts_as_participant(self):
        assert ball_target_warning(_AUTHOR, set(), _AUTHOR) is None

    def test_unknown_target_warns_and_names_participants(self):
        warning = ball_target_warning("Claude Code (calbe)", {_HUMAN}, _AUTHOR)
        assert warning is not None
        assert "Claude Code (calbe)" in warning
        assert _HUMAN in warning
        assert _AUTHOR in warning
        assert "nobody's queue" in warning


# ---------------------------------------------------------------------------
# hosted_ops ball resolution (mocked GitHub client, real data assembly)
# ---------------------------------------------------------------------------


def _entry(agent: str, index: int) -> dict:
    return {
        "id": f"entry:e{index}",
        "type": "entry",
        "thread_topic": "demo",
        "index": index,
        "agent": agent,
    }


@pytest.fixture
def http_ctx():
    from watercooler_mcp.context import (
        HttpRequestContext,
        clear_http_context,
        set_http_context,
    )

    ctx = HttpRequestContext(
        user_id="u1", repo="org/repo", branch="main", github_token="ghp_test"
    )
    set_http_context(ctx)
    yield ctx
    clear_http_context()


def _patch_hosted(monkeypatch, meta, entries):
    """Patch the GitHub boundary; return the recorded write kwargs dict."""
    written = {}
    monkeypatch.setattr(
        hosted_ops, "_get_github_client", lambda: (None, MagicMock())
    )
    monkeypatch.setattr(
        hosted_ops,
        "_read_per_thread_graph",
        lambda client, topic: (meta, entries, [], "msha", "esha", "gsha"),
    )

    def _record_write(client, **kwargs):
        written.update(kwargs)
        return ("commit-sha", {"md_projected": True, "enriched": False})

    monkeypatch.setattr(hosted_ops, "_write_per_thread_atomic", _record_write)
    return written


class TestHostedSayBall:
    def test_new_thread_ball_stays_with_author(self, monkeypatch, http_ctx):
        _patch_hosted(monkeypatch, None, [])
        error, result = hosted_ops.say_hosted(
            topic="demo",
            title="First entry",
            body="Spec: implementer\nhello",
            agent=_AUTHOR,
            create_if_missing=True,
        )
        assert error is None
        assert result["ball"] == _AUTHOR

    def test_author_already_holding_keeps_ball(self, monkeypatch, http_ctx):
        meta = {"topic": "demo", "status": "OPEN", "ball": _AUTHOR, "title": "Demo"}
        _patch_hosted(monkeypatch, meta, [_entry(_AUTHOR, 0)])
        error, result = hosted_ops.say_hosted(
            topic="demo", title="More", body="x", agent=_AUTHOR
        )
        assert error is None
        assert result["ball"] == _AUTHOR

    def test_ball_never_lands_on_placeholder(self, monkeypatch, http_ctx):
        _patch_hosted(monkeypatch, None, [])
        _, result = hosted_ops.say_hosted(
            topic="demo", title="t", body="b", agent=_AUTHOR, create_if_missing=True
        )
        assert result["ball"] != "Agent"


class TestHostedHandoffBall:
    def test_no_target_keeps_ball_with_author(self, monkeypatch, http_ctx):
        meta = {"topic": "demo", "status": "OPEN", "ball": _HUMAN, "title": "Demo"}
        _patch_hosted(monkeypatch, meta, [_entry(_AUTHOR, 0)])
        error, result = hosted_ops.handoff_hosted(topic="demo", agent=_AUTHOR)
        assert error is None
        assert result["ball"] == _AUTHOR
        assert result["ball_warning"] is None

    def test_known_target_no_warning(self, monkeypatch, http_ctx):
        meta = {"topic": "demo", "status": "OPEN", "ball": _AUTHOR, "title": "Demo"}
        _patch_hosted(
            monkeypatch, meta, [_entry(_AUTHOR, 0), _entry(_HUMAN, 1)]
        )
        error, result = hosted_ops.handoff_hosted(
            topic="demo", agent=_AUTHOR, target_agent=_HUMAN
        )
        assert error is None
        assert result["ball"] == _HUMAN
        assert result["ball_warning"] is None

    def test_unknown_target_carries_warning(self, monkeypatch, http_ctx):
        meta = {"topic": "demo", "status": "OPEN", "ball": _AUTHOR, "title": "Demo"}
        _patch_hosted(monkeypatch, meta, [_entry(_AUTHOR, 0)])
        error, result = hosted_ops.handoff_hosted(
            topic="demo", agent=_AUTHOR, target_agent="Claude Code (calbe)"
        )
        assert error is None
        assert result["ball"] == "Claude Code (calbe)"
        assert "Claude Code (calbe)" in result["ball_warning"]
        assert _AUTHOR in result["ball_warning"]


# ---------------------------------------------------------------------------
# Response shape (thread_write) — prose must match ball state
# ---------------------------------------------------------------------------


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def hosted_context(tmp_path, threads_dir, monkeypatch):
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


_AGENT_FUNC = "Claude Code:test-model:implementer"


class TestHostedSayResponseTruth:
    def test_ball_kept_reported_as_remains_and_keep_working(
        self, hosted_context, monkeypatch
    ):
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.say_hosted",
            lambda **kw: (None, {"status": "OPEN", "ball": "Claude Code"}),
        )
        result = _say_impl(
            topic="demo",
            title="t",
            body="Spec: implementer\nx",
            ctx=None,
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert "Ball remains with: Claude Code" in result
        assert "Ball flipped to" not in result
        assert "keep-working" in result

    def test_real_flip_still_reported_as_flip(self, hosted_context, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.say_hosted",
            lambda **kw: (None, {"status": "OPEN", "ball": _HUMAN}),
        )
        result = _say_impl(
            topic="demo",
            title="t",
            body="Spec: implementer\nx",
            ctx=None,
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert f"Ball flipped to: {_HUMAN}" in result
        assert "Next: continue" in result


class TestHostedHandoffResponseWarning:
    def test_warning_from_result_is_surfaced(self, hosted_context, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.handoff_hosted",
            lambda **kw: (
                None,
                {
                    "status": "OPEN",
                    "ball": "Claude Code (calbe)",
                    "ball_warning": "ball handed to 'Claude Code (calbe)', "
                    "which has not authored any entry on this thread",
                },
            ),
        )
        result = _handoff_impl(
            topic="demo",
            ctx=None,
            target_agent="Claude Code (calbe)",
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert "⚠️" in result
        assert "Claude Code (calbe)" in result

    def test_no_target_reports_keep_working_not_self_handoff(
        self, hosted_context, monkeypatch
    ):
        """Review finding on this PR: a no-target hosted handoff keeps the
        ball with the author, so the response must say keep-working — not
        'Ball passed to <yourself>'."""
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.handoff_hosted",
            lambda **kw: (
                None,
                {"status": "OPEN", "ball": "Claude Code", "ball_warning": None},
            ),
        )
        result = _handoff_impl(
            topic="demo",
            ctx=None,
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert "Ball remains with: Claude Code" in result
        assert "keep-working" in result
        assert "Ball passed to" not in result
        assert "handed off" not in result

    def test_no_warning_line_when_target_known(self, hosted_context, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.tools.thread_write.handoff_hosted",
            lambda **kw: (
                None,
                {"status": "OPEN", "ball": _HUMAN, "ball_warning": None},
            ),
        )
        result = _handoff_impl(
            topic="demo",
            ctx=None,
            target_agent=_HUMAN,
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert "⚠️" not in result


# ---------------------------------------------------------------------------
# Local handoff advisory (real baseline graph in tmp_path)
# ---------------------------------------------------------------------------


@pytest.fixture
def local_context(tmp_path, threads_dir, monkeypatch):
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
        "watercooler_mcp.tools.thread_write.run_with_sync",
        lambda ctx, msg, op, **kw: op(),
    )
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.is_slack_enabled", lambda: False
    )
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.is_slack_bot_enabled", lambda: False
    )
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.is_hosted_context", lambda _: False
    )
    return ctx


@pytest.fixture
def local_thread(local_context, threads_dir):
    from watercooler.baseline_graph.writer import (
        EntryData,
        init_thread_in_graph,
        upsert_entry_node,
    )
    from watercooler.baseline_graph.projector import project_and_write_thread

    init_thread_in_graph(
        threads_dir, "demo", title="Demo", status="OPEN", ball=_HUMAN
    )
    upsert_entry_node(
        threads_dir,
        EntryData(
            entry_id="01TEST00000000000000000001",
            thread_topic="demo",
            index=0,
            agent=_HUMAN,
            role="planner",
            entry_type="Note",
            title="First",
            body="Spec: planner\nhi",
            timestamp="2025-01-01T12:00:00Z",
            summary="",
        ),
    )
    project_and_write_thread(threads_dir, "demo")


class TestLocalHandoffAdvisory:
    def test_unknown_target_gets_warning(self, local_thread):
        result = _handoff_impl(
            topic="demo",
            ctx=None,
            target_agent="Claude Code (calbe)",
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert "✅ Ball handed off to: Claude Code (calbe)" in result
        assert "⚠️" in result
        assert _HUMAN in result  # known participants listed

    def test_known_target_no_warning(self, local_thread):
        result = _handoff_impl(
            topic="demo",
            ctx=None,
            target_agent=_HUMAN,
            code_path="/tmp/repo",
            agent_func=_AGENT_FUNC,
        )
        assert f"✅ Ball handed off to: {_HUMAN}" in result
        assert "⚠️" not in result
