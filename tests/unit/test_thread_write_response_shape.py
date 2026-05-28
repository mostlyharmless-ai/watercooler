"""Tests for Phase 1+2 workflow-topology changes.

Phase 1: Ball:/Next: advisory suffix on all write tool responses.
Phase 2: watercooler_write v1 — authority guardrails, control-transfer
         routing, body/title normalization.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp", reason="fastmcp required for MCP server tests")

from watercooler_mcp import validation
from watercooler_mcp.config import ThreadContext
from watercooler_mcp.tools.thread_write import (
    _next_signal,
    _write_impl,
    _say_impl,
    _ack_impl,
    _handoff_impl,
    _set_status_impl,
)


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_mcp_thread_write.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def mock_context(tmp_path, threads_dir):
    return ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        code_repo="test-org/test-repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=True,
    )


@pytest.fixture
def patched_context(mock_context, monkeypatch):
    monkeypatch.setattr(validation, "_require_context", lambda _: (None, mock_context))
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda _: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda _: None)
    monkeypatch.setattr(
        "watercooler_mcp.tools.thread_write.run_with_sync",
        lambda ctx, msg, op, **kw: op(),
    )
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_slack_bot_enabled", lambda: False)
    monkeypatch.setattr("watercooler_mcp.tools.thread_write.is_hosted_context", lambda _: False)
    return mock_context


@pytest.fixture
def sample_thread(patched_context, threads_dir):
    from watercooler.baseline_graph.writer import (
        init_thread_in_graph,
        upsert_entry_node,
        EntryData,
    )
    from watercooler.baseline_graph.projector import project_and_write_thread

    init_thread_in_graph(
        threads_dir, "test-topic", title="Test Thread", status="OPEN", ball="Human"
    )
    upsert_entry_node(
        threads_dir,
        EntryData(
            entry_id="01TEST00000000000000000001",
            thread_topic="test-topic",
            index=0,
            agent="Human",
            role="planner",
            entry_type="Plan",
            title="Initial planning",
            body="Spec: planner\nHello.",
            timestamp="2025-01-01T12:00:00Z",
            summary="",
        ),
    )
    project_and_write_thread(threads_dir, "test-topic")


# ---------------------------------------------------------------------------
# _next_signal unit tests
# ---------------------------------------------------------------------------


class TestNextSignal:
    def test_ordinary_note_returns_continue(self):
        result = _next_signal("Note", ball="Human")
        assert "Next: continue" in result
        assert "Ball: Human" in result

    def test_closure_returns_stop(self):
        result = _next_signal("Closure", ball="Human")
        assert "Next: stop" in result

    def test_handoff_with_target_agent(self):
        result = _next_signal("Note", ball="Codex", target_agent="Codex")
        assert "Next: handoff" in result
        assert "Codex" in result

    def test_default_args_return_continue(self):
        result = _next_signal()
        assert "Next: continue" in result


# ---------------------------------------------------------------------------
# Phase 1: Ball:/Next: suffix on existing write tools
# ---------------------------------------------------------------------------


class TestSayResponseShape:
    def test_local_say_contains_ball_and_next(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _say_impl(
            topic="test-topic",
            title="My entry",
            body="Spec: planner\nHello.",
            ctx=ctx,
            role="planner",
            entry_type="Note",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:planner",
        )
        assert "Ball:" in result
        assert "Next:" in result

    def test_closure_entry_returns_next_stop(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _say_impl(
            topic="test-topic",
            title="Closing",
            body="Spec: planner\nDone.",
            ctx=ctx,
            role="planner",
            entry_type="Closure",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:planner",
        )
        assert "Next: stop" in result


class TestAckResponseShape:
    def test_local_ack_when_caller_does_not_own_ball_emits_continue(
        self, sample_thread, threads_dir
    ):
        """When the caller acks on a thread whose ball is held by someone else,
        the response must reflect that — Next: continue with the real owner —
        not Next: keep-working (which would falsely tell the caller to proceed)."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        # sample_thread fixture sets ball="Human"; caller is "Claude Code".
        result = _ack_impl(
            topic="test-topic",
            ctx=ctx,
            title="Ack",
            body="Noted.",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:critic",
        )
        assert "Ball:" in result
        assert "Next: continue" in result
        assert "Next: keep-working" not in result

    def test_local_ack_when_caller_owns_ball_emits_keep_working(
        self, patched_context, threads_dir
    ):
        """When the caller actually holds the ball, ack should signal that the
        caller may keep working — Next: keep-working."""
        from watercooler.baseline_graph.writer import (
            init_thread_in_graph,
            upsert_entry_node,
            EntryData,
        )
        from watercooler.baseline_graph.projector import project_and_write_thread

        # Set the ball to the caller (agent_base = "Claude Code").
        init_thread_in_graph(
            threads_dir, "owned-topic", title="Owned", status="OPEN", ball="Claude Code"
        )
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id="01TEST00000000000000000002",
                thread_topic="owned-topic",
                index=0,
                agent="Claude Code",
                role="implementer",
                entry_type="Note",
                title="Initial",
                body="Spec: implementer\nStarting.",
                timestamp="2025-01-01T12:00:00Z",
                summary="",
            ),
        )
        project_and_write_thread(threads_dir, "owned-topic")

        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _ack_impl(
            topic="owned-topic",
            ctx=ctx,
            title="Ack",
            body="Continuing.",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:implementer",
        )
        assert "Next: keep-working" in result
        assert "Next: continue" not in result


class TestHandoffResponseShape:
    def test_explicit_target_returns_next_handoff(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _handoff_impl(
            topic="test-topic",
            ctx=ctx,
            note="Ready for review",
            target_agent="Codex",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:planner",
        )
        assert "Next: handoff" in result

    def test_implicit_handoff_returns_continue(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _handoff_impl(
            topic="test-topic",
            ctx=ctx,
            note="",
            target_agent=None,
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:planner",
        )
        assert "Ball:" in result
        assert "Next:" in result


class TestSetStatusResponseShape:
    def test_set_status_contains_next(self, patched_context, threads_dir, monkeypatch):
        from watercooler.fs import thread_path as _tp

        monkeypatch.setattr(
            "watercooler.commands_graph.set_status",
            lambda topic, *, threads_dir, status: _tp(topic, threads_dir),
        )
        result = _set_status_impl(
            topic="test-topic",
            status="IN_REVIEW",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:pm",
        )
        assert "Next:" in result


# ---------------------------------------------------------------------------
# Phase 2: watercooler_write v1
# ---------------------------------------------------------------------------


class TestWriteImplAuthorityGuardrails:
    def test_decision_without_auth_text_returns_error(self, patched_context, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Spec: planner\nDecision body.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="decision",
            authorization_text=None,
            downgrade_to_note=False,
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result
        assert "authorization_text" in result

    def test_decision_without_auth_text_with_downgrade_writes_note(
        self, sample_thread, threads_dir
    ):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Spec: planner\nDecision body.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="decision",
            authorization_text=None,
            downgrade_to_note=True,
            code_path=str(threads_dir.parent),
        )
        # Should succeed as Note, not error
        assert "❌" not in result
        assert "✅" in result

    def test_decision_with_auth_text_succeeds(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Spec: planner\nWe chose Postgres.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="decision",
            authorization_text="Human approved via thread entry 01ABC.",
            downgrade_to_note=False,
            code_path=str(threads_dir.parent),
        )
        assert "✅" in result

    def test_invalid_authority_mode_returns_error(self, patched_context, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Body",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="supersede",
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result
        assert "invalid authority_mode" in result

    def test_closure_without_auth_text_returns_error(self, patched_context, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Done.",
            ctx=ctx,
            role="pm",
            agent_func="Claude Code:claude-sonnet-4-6:pm",
            authority_mode="closure",
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result


class TestWriteImplControlTransfer:
    def test_auto_routes_to_say(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Hello.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            next_actor="auto",
            code_path=str(threads_dir.parent),
        )
        assert "Ball flipped to" in result

    def test_self_routes_to_ack(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Noted.",
            ctx=ctx,
            role="critic",
            agent_func="Claude Code:claude-sonnet-4-6:critic",
            next_actor="self",
            code_path=str(threads_dir.parent),
        )
        assert "Ball remains with" in result

    def test_explicit_agent_routes_to_handoff(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Handing off.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            next_actor="Codex",
            code_path=str(threads_dir.parent),
        )
        assert "Codex" in result
        assert "Next: handoff" in result


class TestWriteImplBodyNormalization:
    def test_spec_line_prepended_when_absent(self, sample_thread, threads_dir):
        """watercooler_write prepends Spec: role when body lacks it."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        # We verify indirectly: if the entry is written successfully the
        # role validation passed, which requires a valid role in the body.
        result = _write_impl(
            topic="test-topic",
            body="No spec prefix here.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            code_path=str(threads_dir.parent),
        )
        assert "✅" in result

    def test_existing_spec_line_preserved(self, sample_thread, threads_dir):
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Spec: critic\n\nHello.",
            ctx=ctx,
            role="critic",
            agent_func="Claude Code:claude-sonnet-4-6:critic",
            code_path=str(threads_dir.parent),
        )
        assert "✅" in result


class TestReviewRegressions:
    """Regressions for issues found during review of commit a91268f9."""

    def test_decision_with_self_next_actor_is_rejected(self, patched_context, threads_dir):
        """P1: authority_mode=decision must not silently downgrade to ack-Note."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Body.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="decision",
            authorization_text="Authorized by human.",
            next_actor="self",
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result
        assert "next_actor='auto'" in result

    def test_decision_with_explicit_handoff_is_rejected(self, patched_context, threads_dir):
        """P1: same bypass via explicit handoff target."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Body.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            authority_mode="closure",
            authorization_text="Authorized.",
            next_actor="Codex",
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result
        assert "next_actor='auto'" in result

    def test_downgrade_warning_preserves_original_mode(self, sample_thread, threads_dir):
        """P2: downgrade warning must name decision/closure, not 'ordinary'."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        # We can't easily inspect the written body from the response string, so
        # patch _say_impl to capture the body it receives.
        captured = {}
        from watercooler_mcp.tools import thread_write as tw

        original_say = tw._say_impl

        def capture(**kwargs):
            captured["body"] = kwargs.get("body", "")
            return "✅ stub"

        tw._say_impl = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Decision body.",
                ctx=ctx,
                role="planner",
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                authority_mode="decision",
                authorization_text=None,
                downgrade_to_note=True,
                code_path=str(threads_dir.parent),
            )
        finally:
            tw._say_impl = original_say

        assert "downgraded from decision" in captured["body"]
        assert "downgraded from ordinary" not in captured["body"]

    def test_invalid_role_returns_error(self, patched_context, threads_dir):
        """role must be in the project catalog (bundled defaults or
        .watercooler/roles.toml override). Validation delegates to
        watercooler.role_loader.validate_role."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Body",
            ctx=ctx,
            role="reviewer",  # not in bundled defaults
            agent_func="Claude Code:claude-sonnet-4-6:reviewer",
            code_path=str(threads_dir.parent),
        )
        assert "❌" in result
        assert "invalid role" in result.lower()

    def test_next_signal_terminal_status_emits_stop(self):
        """P3: set_status to terminal status should emit Next: stop."""
        assert "Next: stop" in _next_signal(status="CLOSED")
        assert "Next: stop" in _next_signal(status="resolved")
        assert "Next: continue" in _next_signal(status="OPEN")
        assert "Next: continue" in _next_signal(status=None)

    def test_next_signal_handoff_includes_target(self):
        """P2: handoff branch must report target_agent and Next: handoff."""
        sig = _next_signal(ball="Codex", target_agent="Codex")
        assert "Next: handoff" in sig
        assert "Codex" in sig


class TestCodexReviewRegressions:
    """Regressions for Codex review of commit a91268f9 follow-up."""

    def test_implicit_handoff_emits_next_handoff(self, sample_thread, threads_dir):
        """Implicit handoff (no target_agent) still changes the ball, so the
        response must signal Next: handoff, not Next: continue."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _handoff_impl(
            topic="test-topic",
            ctx=ctx,
            note="ping",
            target_agent=None,
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:planner",
        )
        assert "Next: handoff" in result
        assert "Next: continue" not in result

    def test_ack_impl_accepts_role_kwarg(self):
        """_ack_impl signature must accept role= so _write_impl can plumb it."""
        import inspect
        sig = inspect.signature(_ack_impl)
        assert "role" in sig.parameters

    def test_write_impl_passes_role_to_ack(self, sample_thread, threads_dir):
        """next_actor='self' with role='critic' must reach commands_graph.ack
        with role='critic' rather than defaulting to implementer."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.ack

        def capture(*args, **kwargs):
            captured["role"] = kwargs.get("role")
            return original(*args, **kwargs)

        tw.commands_graph.ack = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Some critique.",
                ctx=ctx,
                role="critic",
                agent_func="Claude Code:claude-sonnet-4-6:critic",
                next_actor="self",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.ack = original

        assert captured["role"] == "critic"

    def test_write_impl_signature_drops_unused_code_branch(self):
        """code_branch was a no-op param; signature should not advertise it."""
        import inspect
        sig = inspect.signature(_write_impl)
        assert "code_branch" not in sig.parameters


class TestCodexReviewRegressionsRound2:
    """Regressions for Codex second-round review."""

    def test_set_status_local_reports_actual_ball(self, sample_thread, threads_dir):
        """Status updates must surface the real ball owner, not 'counterpart'."""
        result = _set_status_impl(
            topic="test-topic",
            status="IN_REVIEW",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:pm",
        )
        # The sample thread fixture starts with a known ball owner (not literal
        # "counterpart"); the advisory must contain a real name from the thread.
        assert "Ball: counterpart" not in result
        # And the signal line must still appear.
        assert "Ball:" in result

    def test_set_status_terminal_emits_stop_with_owner(self, sample_thread, threads_dir):
        """Terminal status still emits Next: stop and a non-placeholder Ball."""
        result = _set_status_impl(
            topic="test-topic",
            status="CLOSED",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:pm",
        )
        assert "Next: stop" in result
        assert "Ball: counterpart" not in result

    def test_hosted_implicit_handoff_emits_next_handoff(self):
        """Hosted handoff with target_agent=None must still emit Next: handoff
        because the ball was transferred to the resolved counterpart."""
        # _next_signal is the contract surface; the hosted call site now passes
        # `target_agent=target_agent or new_ball`. Verify the helper categorizes
        # that input as a handoff.
        sig = _next_signal(ball="Codex", target_agent="Codex")
        assert "Next: handoff" in sig
        assert "Next: continue" not in sig


class TestCodexReviewRegressionsRound3:
    """Regressions for Codex third-round review (role plumbing + Spec conflict)."""

    def test_hosted_ack_accepts_role_kwarg(self):
        """ack_hosted signature must accept role= so canonical role flows
        through to graph metadata on hosted writes."""
        import inspect
        from watercooler_mcp.hosted_ops import ack_hosted
        assert "role" in inspect.signature(ack_hosted).parameters

    def test_hosted_handoff_accepts_role_kwarg(self):
        """handoff_hosted signature must accept role= for the same reason."""
        import inspect
        from watercooler_mcp.hosted_ops import handoff_hosted
        assert "role" in inspect.signature(handoff_hosted).parameters

    def test_handoff_impl_accepts_role_kwarg(self):
        """_handoff_impl must accept role= so _write_impl can plumb canonical
        role into both local and hosted handoff entries."""
        import inspect
        from watercooler_mcp.tools.thread_write import _handoff_impl
        assert "role" in inspect.signature(_handoff_impl).parameters

    def test_explicit_handoff_passes_role_to_commands_graph(
        self, sample_thread, threads_dir
    ):
        """next_actor=<agent> with role='critic' must reach
        commands_graph.append_entry with role='critic', not hardcoded 'pm'."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["role"] = kwargs.get("role")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Please review.",
                ctx=ctx,
                role="critic",
                agent_func="Claude Code:claude-sonnet-4-6:critic",
                next_actor="Codex",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        assert captured["role"] == "critic"

    def test_implicit_handoff_passes_role_to_commands_graph(
        self, sample_thread, threads_dir
    ):
        """next_actor='auto' is not the implicit-handoff path (that's say) —
        the implicit handoff is _handoff_impl with target_agent=None. Verify
        role flows through commands_graph.handoff."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler_mcp.tools.thread_write import _handoff_impl
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.handoff

        def capture(*args, **kwargs):
            captured["role"] = kwargs.get("role")
            return original(*args, **kwargs)

        tw.commands_graph.handoff = capture
        try:
            _handoff_impl(
                topic="test-topic",
                ctx=ctx,
                note="moving on",
                target_agent=None,
                code_path=str(threads_dir.parent),
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                role="planner",
            )
        finally:
            tw.commands_graph.handoff = original

        assert captured["role"] == "planner"

    def test_write_impl_preserves_caller_spec_line_verbatim(
        self, sample_thread, threads_dir
    ):
        """The Spec line is body documentation; the structural role lives in
        graph metadata. A caller-supplied Spec line — even one that differs
        from the canonical role — must be preserved verbatim (per CLAUDE.md,
        cross-cutting specs like 'security-audit', 'docs', 'ops' are valid)."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            captured["role"] = kwargs.get("role")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Spec: security-audit\n\nThis is a security review.",
                ctx=ctx,
                role="critic",
                agent_func="Claude Code:claude-sonnet-4-6:critic",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        assert captured["body"].startswith("Spec: security-audit")
        assert captured["role"] == "critic"


class TestRound5Regressions:
    """Regressions for Round 5 multi-agent review (banner ordering, sub-spec,
    authorization_text sanitization)."""

    def test_downgrade_preserves_spec_first_line_when_body_has_spec(
        self, sample_thread, threads_dir
    ):
        """Downgrade banner must not displace `Spec: <role>` from line 1 when
        the caller pre-declared a matching Spec: line."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Spec: planner\n\nRecommendation deferred.",
                ctx=ctx,
                role="planner",
                authority_mode="decision",
                downgrade_to_note=True,
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        assert captured["body"] is not None
        assert captured["body"].startswith("Spec: planner")
        assert "[watercooler_write: downgraded from decision" in captured["body"]

    def test_downgrade_preserves_spec_first_line_when_body_lacks_spec(
        self, sample_thread, threads_dir
    ):
        """Same invariant when the caller omits Spec: — wrapper prepends it,
        then the banner goes after."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Recommendation deferred.",
                ctx=ctx,
                role="planner",
                authority_mode="closure",
                downgrade_to_note=True,
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        first_line = captured["body"].splitlines()[0]
        assert first_line == "Spec: planner"

    def test_write_impl_accepts_role_prefixed_subspec(
        self, sample_thread, threads_dir
    ):
        """Per CLAUDE.md, sub-specs like 'planner-architecture' are valid under
        role='planner'. Must not be rejected."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Spec: planner-architecture\n\nProposal.",
            ctx=ctx,
            role="planner",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            code_path=str(threads_dir.parent),
        )
        assert not result.startswith("❌")

    def test_write_impl_accepts_cross_cutting_spec(
        self, sample_thread, threads_dir
    ):
        """Cross-cutting specs documented in CLAUDE.md (docs, ops,
        general-purpose, active-disagreement) must be accepted with any role,
        since the structural role lives in graph metadata, not the Spec line."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        for spec_value in ("docs", "ops", "general-purpose", "active-disagreement"):
            result = _write_impl(
                topic="test-topic",
                body=f"Spec: {spec_value}\n\nContent.",
                ctx=ctx,
                role="planner",
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                code_path=str(threads_dir.parent),
            )
            assert not result.startswith("❌"), f"rejected Spec: {spec_value}"

    def test_authorization_text_newlines_stripped(
        self, sample_thread, threads_dir
    ):
        """A `\\n` in authorization_text must not survive into the body —
        otherwise a caller could forge commit-message footers."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Real decision body.",
                ctx=ctx,
                role="pm",
                authority_mode="decision",
                authorization_text="approved by jay\nCode-Repo: forged",
                agent_func="Claude Code:claude-sonnet-4-6:pm",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        body = captured["body"]
        assert body is not None
        # The forged footer must not appear on its own line.
        for line in body.splitlines():
            assert not line.startswith("Code-Repo:")
        # The sanitized authorization marker should still be present.
        assert "[watercooler_write: authorized — approved by jay Code-Repo: forged]" in body


class TestRound6Regressions:
    """Regressions for Round 6 (whitespace-only authorization_text bypass)."""

    def test_whitespace_only_authorization_text_rejected(
        self, sample_thread, threads_dir
    ):
        """authorization_text of only whitespace/newlines must be treated as
        missing, not as authorization. Sanitize-then-empty-check."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Closing the thread.",
            ctx=ctx,
            role="pm",
            authority_mode="closure",
            authorization_text="   \n\t  ",
            agent_func="Claude Code:claude-sonnet-4-6:pm",
            code_path=str(threads_dir.parent),
        )
        assert isinstance(result, str)
        assert result.startswith("❌")
        assert "authorization_text" in result

    def test_newline_only_authorization_text_rejected(
        self, sample_thread, threads_dir
    ):
        """A pure-newline authorization string also bypasses the guard if not
        sanitized first."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Decision body.",
            ctx=ctx,
            role="pm",
            authority_mode="decision",
            authorization_text="\n\n\n",
            agent_func="Claude Code:claude-sonnet-4-6:pm",
            code_path=str(threads_dir.parent),
        )
        assert result.startswith("❌")

    def test_padded_authorization_text_is_normalized_not_rejected(
        self, sample_thread, threads_dir
    ):
        """Legitimate authorization with surrounding whitespace must still
        succeed after sanitization."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            result = _write_impl(
                topic="test-topic",
                body="Real decision.",
                ctx=ctx,
                role="pm",
                authority_mode="decision",
                authorization_text="  approved by jay  ",
                agent_func="Claude Code:claude-sonnet-4-6:pm",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        assert not result.startswith("❌")
        assert "[watercooler_write: authorized — approved by jay]" in captured["body"]


class TestRound7Regressions:
    """Regressions for Round 7 (blank next_actor routing)."""

    def test_empty_next_actor_rejected(self, sample_thread, threads_dir):
        """next_actor='' would silently route to implicit handoff via the
        target=falsy branch. Reject explicitly."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Some update.",
            ctx=ctx,
            role="pm",
            next_actor="",
            agent_func="Claude Code:claude-sonnet-4-6:pm",
            code_path=str(threads_dir.parent),
        )
        assert result.startswith("❌")
        assert "next_actor" in result

    def test_whitespace_next_actor_rejected(self, sample_thread, threads_dir):
        """next_actor='  ' would flip the ball to literal whitespace if it
        reached _handoff_impl. Reject after strip."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Some update.",
            ctx=ctx,
            role="pm",
            next_actor="   \t  ",
            agent_func="Claude Code:claude-sonnet-4-6:pm",
            code_path=str(threads_dir.parent),
        )
        assert result.startswith("❌")
        assert "next_actor" in result

    def test_padded_next_actor_accepted_after_strip(
        self, sample_thread, threads_dir
    ):
        """A legitimately named target with surrounding whitespace should
        succeed after normalization, not route to the empty-string branch."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Please pick this up.",
            ctx=ctx,
            role="planner",
            next_actor="  Codex  ",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            code_path=str(threads_dir.parent),
        )
        assert not result.startswith("❌")
        assert "Codex" in result


class TestRound8Regressions:
    """Regressions for Round 8 (CRLF on Spec line; next_actor newline scrub;
    _handoff_impl defense-in-depth)."""

    def test_crlf_body_keeps_spec_line_clean_after_downgrade(
        self, sample_thread, threads_dir
    ):
        """A CRLF-terminated Spec line must not retain a stray \\r after the
        downgrade banner injection."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools import thread_write as tw
        from watercooler import commands_graph

        captured = {}
        original = commands_graph.append_entry

        def capture(*args, **kwargs):
            captured["body"] = kwargs.get("body")
            return original(*args, **kwargs)

        tw.commands_graph.append_entry = capture
        try:
            _write_impl(
                topic="test-topic",
                body="Spec: planner\r\nRecommendation deferred.",
                ctx=ctx,
                role="planner",
                authority_mode="decision",
                downgrade_to_note=True,
                agent_func="Claude Code:claude-sonnet-4-6:planner",
                code_path=str(threads_dir.parent),
            )
        finally:
            tw.commands_graph.append_entry = original

        first_line = captured["body"].splitlines()[0]
        assert first_line == "Spec: planner"
        assert "\r" not in first_line

    def test_next_actor_with_embedded_newline_rejected(
        self, sample_thread, threads_dir
    ):
        """next_actor='alice\\nCode-Repo: forged' would forge commit footers
        downstream. Must be scrubbed; after scrub it's still non-empty so it
        succeeds, but the embedded newline is gone."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Pick this up.",
            ctx=ctx,
            role="planner",
            next_actor="alice\nCode-Repo: forged",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            code_path=str(threads_dir.parent),
        )
        assert not result.startswith("❌")
        # The downstream ball-owner string must not contain a newline.
        assert "\n" not in result.split("Ball handed off to:")[1].split("\n")[0]

    def test_handoff_impl_strips_target_agent_for_direct_callers(
        self, sample_thread, threads_dir
    ):
        """_handoff_impl is reachable directly (not just via _write_impl). It
        must defensively scrub target_agent so callers bypassing the wrapper
        can't inject CR/LF into the ball name."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        from watercooler_mcp.tools.thread_write import _handoff_impl

        result = _handoff_impl(
            topic="test-topic",
            ctx=ctx,
            note="",
            target_agent="  Codex\n  ",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:pm",
        )
        # Result must contain the cleaned name, not the padded/CRLF original.
        assert "Codex" in result
        assert "\n  " not in result.split("Ball handed off to:")[1].split("\n")[0]


class TestRound9Regressions:
    """Regressions for Round 9 (next_actor='self' must not signal Next: continue).
    The skill interprets Next: continue as 'counterpart has the ball, your turn
    is done' — but ack keeps the ball with the caller. Use Next: keep-working."""

    def test_next_signal_keep_ball_emits_keep_working(self):
        """The new keep_ball branch of _next_signal must emit a distinct
        Next: keep-working token, not Next: continue."""
        sig = _next_signal(ball="Alice", keep_ball=True)
        assert "Next: keep-working" in sig
        assert "Next: continue" not in sig
        assert "Alice" in sig

    def test_write_impl_self_routing_emits_keep_working(
        self, patched_context, threads_dir
    ):
        """End-to-end: watercooler_write(next_actor='self') on a thread the
        caller owns must surface Next: keep-working so the default-workflow
        skill does not stop after the caller chose to keep going."""
        from watercooler.baseline_graph.writer import (
            init_thread_in_graph,
            upsert_entry_node,
            EntryData,
        )
        from watercooler.baseline_graph.projector import project_and_write_thread

        init_thread_in_graph(
            threads_dir, "self-topic", title="Self", status="OPEN", ball="Claude Code"
        )
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id="01TEST00000000000000000003",
                thread_topic="self-topic",
                index=0,
                agent="Claude Code",
                role="implementer",
                entry_type="Note",
                title="Start",
                body="Spec: implementer\nStarting.",
                timestamp="2025-01-01T12:00:00Z",
                summary="",
            ),
        )
        project_and_write_thread(threads_dir, "self-topic")

        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="self-topic",
            body="Interim note, more coming.",
            ctx=ctx,
            role="implementer",
            next_actor="self",
            agent_func="Claude Code:claude-sonnet-4-6:implementer",
            code_path=str(threads_dir.parent),
        )
        assert "Next: keep-working" in result
        assert "Next: continue" not in result

    def test_write_impl_self_routing_when_caller_lacks_ball_emits_continue(
        self, sample_thread, threads_dir
    ):
        """When watercooler_write(next_actor='self') is used on a thread whose
        ball is held by someone else, the response must NOT claim the caller
        can keep working. The skill would otherwise loop out of turn."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        # sample_thread fixture has ball='Human'; caller agent is 'Claude Code'.
        result = _write_impl(
            topic="test-topic",
            body="Trying to write out of turn.",
            ctx=ctx,
            role="implementer",
            next_actor="self",
            agent_func="Claude Code:claude-sonnet-4-6:implementer",
            code_path=str(threads_dir.parent),
        )
        assert "Next: keep-working" not in result
        assert "Next: continue" in result
        # The actual ball owner must be surfaced.
        assert "Human" in result

    def test_handoff_still_emits_handoff_not_keep_working(
        self, sample_thread, threads_dir
    ):
        """Sanity check: keep_ball must not leak into the handoff path."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Over to you.",
            ctx=ctx,
            role="planner",
            next_actor="Codex",
            agent_func="Claude Code:claude-sonnet-4-6:planner",
            code_path=str(threads_dir.parent),
        )
        assert "Next: handoff" in result
        assert "Next: keep-working" not in result


class TestRound10Regressions:
    """Regressions for Round 10 (role catalog delegation + spec liberalization)."""

    def test_custom_role_via_roles_toml_accepted(self, sample_thread, threads_dir, tmp_path):
        """A project that declares a custom role in .watercooler/roles.toml
        must be able to use that role through watercooler_write."""
        # Place roles.toml at the repo root walked by validate_role.
        repo_root = threads_dir.parent
        watercooler_dir = repo_root / ".watercooler"
        watercooler_dir.mkdir(exist_ok=True)
        (watercooler_dir / "roles.toml").write_text(
            '[roles.auditor]\n'
            'description = "Custom audit role"\n'
            'canonical_role = "critic"\n'
        )

        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Audit finding: nothing wrong.",
            ctx=ctx,
            role="auditor",
            agent_func="Claude Code:claude-sonnet-4-6:auditor",
            code_path=str(repo_root),
        )
        assert not result.startswith("❌"), result

    def test_role_validation_still_rejects_garbage(self, sample_thread, threads_dir):
        """validate_role delegation must still reject unknown roles when no
        custom roles.toml is present (bundled defaults only)."""
        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _write_impl(
            topic="test-topic",
            body="Body.",
            ctx=ctx,
            role="banana",
            agent_func="Claude Code:claude-sonnet-4-6:banana",
            code_path=str(threads_dir.parent),
        )
        assert result.startswith("❌")
        assert "banana" in result.lower() or "invalid role" in result.lower()


class TestRound12Regressions:
    """Regressions for Round 12 (case-insensitive ball ownership check)."""

    def test_caller_holds_ball_case_insensitive(self, patched_context, threads_dir):
        """ball='claude code' vs agent='Claude Code' must still resolve to
        keep-working — case drift across human edits, defaults, or platform
        names should not break the stop-naturally contract."""
        from watercooler.baseline_graph.writer import (
            init_thread_in_graph,
            upsert_entry_node,
            EntryData,
        )
        from watercooler.baseline_graph.projector import project_and_write_thread

        init_thread_in_graph(
            threads_dir, "case-topic", title="Case", status="OPEN", ball="claude code"
        )
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id="01TEST00000000000000000004",
                thread_topic="case-topic",
                index=0,
                agent="claude code",
                role="implementer",
                entry_type="Note",
                title="Start",
                body="Spec: implementer\nGo.",
                timestamp="2025-01-01T12:00:00Z",
                summary="",
            ),
        )
        project_and_write_thread(threads_dir, "case-topic")

        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _ack_impl(
            topic="case-topic",
            ctx=ctx,
            title="Ack",
            body="Continuing.",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:implementer",
        )
        assert "Next: keep-working" in result
        assert "Next: continue" not in result

    def test_caller_holds_ball_whitespace_insensitive(self, patched_context, threads_dir):
        """Trailing/leading whitespace on either side must not produce a false
        Next: continue."""
        from watercooler.baseline_graph.writer import (
            init_thread_in_graph,
            upsert_entry_node,
            EntryData,
        )
        from watercooler.baseline_graph.projector import project_and_write_thread

        init_thread_in_graph(
            threads_dir, "ws-topic", title="WS", status="OPEN", ball="  Claude Code  "
        )
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id="01TEST00000000000000000005",
                thread_topic="ws-topic",
                index=0,
                agent="  Claude Code  ",
                role="implementer",
                entry_type="Note",
                title="Start",
                body="Spec: implementer\nGo.",
                timestamp="2025-01-01T12:00:00Z",
                summary="",
            ),
        )
        project_and_write_thread(threads_dir, "ws-topic")

        ctx = type("Ctx", (), {"client_id": "test"})()
        result = _ack_impl(
            topic="ws-topic",
            ctx=ctx,
            title="Ack",
            body="Continuing.",
            code_path=str(threads_dir.parent),
            agent_func="Claude Code:claude-sonnet-4-6:implementer",
        )
        assert "Next: keep-working" in result
