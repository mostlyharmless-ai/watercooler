"""Unit tests for stance read-path decoration.

Covers the salience half of ``resolve_stance_block``, the Phase 2 signal half
(route-aware fetch, ``_apply_signal`` overlay, degradation matrix, split-try
isolation), the markdown/JSON renderers, the never-raising ``thread_query``
wrappers, and C1/C2 render gating through ``_read_thread_impl``. Tests install no
``ToolRuntime``, so the signal fetch degrades to ``"unavailable"`` unless a
specific test injects one — no network is ever touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from watercooler.pulse_stance_lib import STANCE_ROLES
from watercooler_mcp import stance_read_decoration as srd
from watercooler_mcp.stance_read_decoration import (
    StanceBlock,
    render_stance_json,
    render_stance_markdown,
    resolve_stance_block,
)


@dataclass
class FakeContext:
    code_root: Optional[str]


def _write_roles(root: Path, body: str) -> None:
    cfg = root / ".watercooler"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "roles.toml").write_text(body)


@pytest.fixture(autouse=True)
def _no_leaked_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the signal fetch to no-runtime (-> "unavailable").

    A ToolRuntime installed by another test module leaks via the
    ``memory_sync`` module global; pin it to None so these tests are hermetic
    and never touch the network. Signal-specific tests override this by
    re-patching ``memory_sync.get_runtime`` in their own bodies.
    """
    from watercooler_mcp import memory_sync

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: None, raising=False)


# --------------------------------------------------------------------------- #
# resolve_stance_block — salience half
# --------------------------------------------------------------------------- #


def test_signal_unavailable_without_runtime(tmp_path: Path) -> None:
    # With no ToolRuntime installed (get_runtime() -> None), the signal fetch
    # degrades to "unavailable" and never touches the network.
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.stance_block_status == "unavailable"


def test_all_stance_roles_present(tmp_path: Path) -> None:
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert set(block.roles) == set(STANCE_ROLES)


def test_salience_loaded(tmp_path: Path) -> None:
    _write_roles(
        tmp_path,
        """
[roles.planner]
project_salience = ["watch for X", "notice Y"]
""",
    )
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.salience_status == "loaded"
    assert block.roles["planner"].salience == ["watch for X", "notice Y"]


def test_salience_absent_when_no_bullets(tmp_path: Path) -> None:
    # A roles.toml override that blanks salience on all stance roles.
    _write_roles(
        tmp_path,
        """
[roles.planner]
project_salience = []

[roles.critic]
project_salience = []

[roles.tester]
project_salience = []
""",
    )
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.salience_status == "absent"
    assert all(not rs.salience for rs in block.roles.values())


def test_salience_absent_when_code_root_none() -> None:
    block = resolve_stance_block(FakeContext(code_root=None))
    assert block.salience_status == "absent"
    assert all(not rs.salience for rs in block.roles.values())


def test_salience_malformed_does_not_raise(tmp_path: Path) -> None:
    _write_roles(tmp_path, "this is = = not valid toml [[[")
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.salience_status == "malformed"
    assert all(not rs.salience for rs in block.roles.values())


def test_signal_fields_empty_without_runtime(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    planner = block.roles["planner"]
    assert planner.elevated is False
    assert planner.level is None
    assert planner.summary is None
    assert planner.source is None
    assert planner.produced_at is None


# --------------------------------------------------------------------------- #
# render_stance_markdown
# --------------------------------------------------------------------------- #


def test_markdown_renders_salience(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch for X"]\n')
    md = render_stance_markdown(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert md.startswith("## Project stance")
    assert "advisory only" in md
    assert "**planner**" in md
    assert "watch for X" in md


def test_markdown_empty_when_no_content() -> None:
    block = StanceBlock(stance_block_status="unavailable", salience_status="absent")
    assert render_stance_markdown(block) == ""


def test_markdown_malformed_shows_diagnostic() -> None:
    block = StanceBlock(stance_block_status="unavailable", salience_status="malformed")
    md = render_stance_markdown(block)
    assert md.startswith("## Project stance")
    assert "could not be" in md


def test_markdown_only_shows_roles_with_bullets(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.critic]\nproject_salience = ["notice Z"]\n')
    md = render_stance_markdown(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert "**critic**" in md
    assert "**planner**" not in md
    assert "**tester**" not in md


# --------------------------------------------------------------------------- #
# render_stance_json
# --------------------------------------------------------------------------- #


def test_json_shape(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    payload = render_stance_json(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert payload["stance_block_status"] == "unavailable"
    assert payload["salience_status"] == "loaded"
    assert payload["advisory_only"] is True
    assert set(payload["roles"]) == set(STANCE_ROLES)
    planner = payload["roles"]["planner"]
    assert planner["salience"] == ["watch X"]
    assert planner["elevated"] is False
    assert planner["level"] is None


# --------------------------------------------------------------------------- #
# thread_query wrappers — must never raise
# --------------------------------------------------------------------------- #


def test_wrappers_swallow_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from watercooler_mcp.tools import thread_query

    def _boom(_context):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(srd, "resolve_stance_block", _boom)
    ctx = FakeContext(code_root="/whatever")
    assert thread_query._stance_block_markdown(ctx) == ""
    assert thread_query._stance_block_json(ctx) is None


def test_wrappers_return_content_on_success(tmp_path: Path) -> None:
    from watercooler_mcp.tools import thread_query

    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    ctx = FakeContext(code_root=str(tmp_path))
    assert "## Project stance" in thread_query._stance_block_markdown(ctx)
    payload = thread_query._stance_block_json(ctx)
    assert payload is not None
    assert payload["salience_status"] == "loaded"


# --------------------------------------------------------------------------- #
# Integration: _read_thread_impl render gating (C1 / C2)
# --------------------------------------------------------------------------- #


@dataclass
class _RichContext:
    """Stand-in for ThreadContext across the local read path."""

    code_root: str
    threads_dir: Path = Path("/tmp/fake-threads")
    code_branch: Optional[str] = None
    code_repo: str = "example/repo"
    code_remote: str = "https://github.com/example/repo.git"


def _stub_local_read(monkeypatch: pytest.MonkeyPatch, roles_root: Path):
    """Patch the local read path so _read_thread_impl runs without I/O.

    The context's ``code_root`` points at ``roles_root`` so salience resolves
    from a controlled ``.watercooler/roles.toml``.
    """
    from watercooler_mcp.tools import thread_query as tq

    class _FakeThread:
        pass

    ctx = _RichContext(code_root=str(roles_root))
    monkeypatch.setattr(tq.validation, "_require_context", lambda _cp: (None, ctx))
    monkeypatch.setattr(tq, "is_hosted_context", lambda _c: False)
    monkeypatch.setattr(tq.validation, "_dynamic_context_missing", lambda *_a, **_k: False)
    monkeypatch.setattr(tq, "ensure_readable", lambda *_a, **_k: (True, [], "clean", False))
    monkeypatch.setattr(tq, "format_parity_warning", lambda *_a, **_k: "")
    monkeypatch.setattr(tq.validation, "_refresh_threads", lambda *_a, **_k: None)
    monkeypatch.setattr(tq, "read_thread_from_graph", lambda *_a, **_k: (_FakeThread(), []))
    monkeypatch.setattr(tq, "_track_access", lambda *_a, **_k: None)
    monkeypatch.setattr(tq, "get_branches_with_entries", lambda *_a, **_k: set())
    monkeypatch.setattr(tq, "_load_entries", lambda *_a, **_k: (None, [], {}))
    monkeypatch.setattr(
        tq, "_get_thread_metadata", lambda *_a, **_k: ("Title", "OPEN", "human", "2026-01-01")
    )
    monkeypatch.setattr(tq, "_get_thread_summary", lambda *_a, **_k: "summary")
    monkeypatch.setattr(tq, "_get_startup_warnings", lambda: [])
    monkeypatch.setattr(tq, "format_thread_markdown", lambda *_a, **_k: "# Thread (stub)\n")
    from watercooler_mcp.tools.thread_query import _read_thread_impl

    return _read_thread_impl


def test_summary_only_markdown_appends_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    impl = _stub_local_read(monkeypatch, tmp_path)
    result = impl(topic="t1", format="markdown", summary_only=True, code_path=str(tmp_path))
    text = result if isinstance(result, str) else result.content[0].text
    assert "## Project stance" in text
    assert "watch X" in text


def test_summary_only_json_adds_peer_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    impl = _stub_local_read(monkeypatch, tmp_path)
    result = impl(topic="t1", format="json", summary_only=True, code_path=str(tmp_path))
    payload = _json.loads(result if isinstance(result, str) else result.content[0].text)
    assert "_stance_advisory" in payload
    assert payload["_stance_advisory"]["salience_status"] == "loaded"


def test_full_read_markdown_has_no_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    impl = _stub_local_read(monkeypatch, tmp_path)
    result = impl(topic="t1", format="markdown", summary_only=False, code_path=str(tmp_path))
    text = result if isinstance(result, str) else result.content[0].text
    assert "## Project stance" not in text


def test_full_read_json_has_no_peer_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    impl = _stub_local_read(monkeypatch, tmp_path)
    result = impl(topic="t1", format="json", summary_only=False, code_path=str(tmp_path))
    payload = _json.loads(result if isinstance(result, str) else result.content[0].text)
    assert "_stance_advisory" not in payload


def test_hosted_summary_only_emits_no_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """C2: hosted-context read never decorates and never calls the helper."""
    import json as _json

    from watercooler_mcp.tools import thread_query as tq

    ctx = _RichContext(code_root="/unused")
    monkeypatch.setattr(tq.validation, "_require_context", lambda _cp: (None, ctx))
    monkeypatch.setattr(tq, "is_hosted_context", lambda _c: True)
    monkeypatch.setattr(tq, "read_thread_hosted", lambda _t: (None, "# hosted content\n"))
    monkeypatch.setattr(tq, "load_thread_entries_hosted", lambda _t: (None, []))
    from watercooler_mcp import hosted_ops as _hosted_ops

    monkeypatch.setattr(
        _hosted_ops,
        "load_thread_metadata_hosted",
        lambda _t: (None, {"title": "T", "status": "OPEN", "ball": "", "last_updated": "", "summary": ""}),
        raising=False,
    )
    monkeypatch.setattr(tq, "_get_startup_warnings", lambda: [])

    called = {"n": 0}

    def _tripwire(_context):
        called["n"] += 1
        return None

    monkeypatch.setattr(tq, "_stance_block_json", _tripwire)
    monkeypatch.setattr(tq, "_stance_block_markdown", lambda _c: "SHOULD_NOT_APPEAR")

    from watercooler_mcp.tools.thread_query import _read_thread_impl

    result = _read_thread_impl(topic="t1", format="json", summary_only=True, code_path="/unused")
    payload = _json.loads(result if isinstance(result, str) else result.content[0].text)
    assert "_stance_advisory" not in payload
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Phase 2: signal fetch (_apply_signal / _fetch_remote / _resolve_signal)
# --------------------------------------------------------------------------- #


def _finding(role, level, *, summary="watch it", daemon="decision_stance", created_at=1000.0):
    return {
        "category": "stance_advisory",
        "daemon_name": daemon,
        "created_at": created_at,
        "details": {
            "advisory": {
                "role": role,
                "level": level,
                "summary": summary,
                "advisory_only": True,
            }
        },
    }


def _roles():
    return {r: srd.RoleStance() for r in STANCE_ROLES}


def test_apply_signal_overlays_elevated() -> None:
    roles = _roles()
    srd._apply_signal(roles, [_finding("critic", 2, summary="hidden authority")])
    critic = roles["critic"]
    assert critic.elevated is True
    assert critic.level == 2
    assert critic.summary == "hidden authority"
    assert critic.source == "decision_stance"
    assert critic.produced_at is not None
    # untouched roles stay quiet
    assert roles["planner"].elevated is False


def test_apply_signal_skips_level_zero_tombstone() -> None:
    roles = _roles()
    srd._apply_signal(roles, [_finding("planner", 0)])
    assert roles["planner"].elevated is False
    assert roles["planner"].level is None


def test_apply_signal_keeps_newest_per_role() -> None:
    roles = _roles()
    # newest-first ordering: the first elevated finding for a role wins
    srd._apply_signal(
        roles,
        [
            _finding("tester", 2, summary="newest", created_at=2000.0),
            _finding("tester", 1, summary="older", created_at=1000.0),
        ],
    )
    assert roles["tester"].level == 2
    assert roles["tester"].summary == "newest"


def test_apply_signal_ignores_unknown_role_and_malformed() -> None:
    roles = _roles()
    srd._apply_signal(
        roles,
        [
            {"category": "stance_advisory", "details": {"advisory": {"role": "pm", "level": 3}}},
            {"category": "other", "details": {"advisory": {"role": "planner", "level": 2}}},
            "not-a-dict",
            {"category": "stance_advisory", "details": "not-a-dict"},
        ],
    )
    assert all(not rs.elevated for rs in roles.values())


def test_iso_formatting() -> None:
    assert srd._iso(0) is None
    assert srd._iso(None) is None
    assert srd._iso(True) is None
    out = srd._iso(1000.0)
    assert out is not None and out.startswith("1970-01-01")


class _FakePremium:
    def __init__(self, *, text=None, delay=0.0, raises=None):
        self._text, self._delay, self._raises = text, delay, raises

    async def call_tool_text(self, name, args):
        if self._delay:
            import asyncio as _a

            await _a.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._text


def test_fetch_remote_success() -> None:
    payload = json.dumps({"findings": [_finding("planner", 1)]})
    status, findings = srd._fetch_remote(_FakePremium(text=payload), 0.5)
    assert status == "success"
    assert isinstance(findings, list) and len(findings) == 1


def test_fetch_remote_error_string_payload() -> None:
    payload = json.dumps({"error": "remote_call_failed", "tool": "watercooler_daemon_findings"})
    status, findings = srd._fetch_remote(_FakePremium(text=payload), 0.5)
    assert status == "error"
    assert findings is None


def test_fetch_remote_non_json() -> None:
    status, findings = srd._fetch_remote(_FakePremium(text="boom"), 0.5)
    assert status == "error" and findings is None


def test_fetch_remote_missing_findings_key() -> None:
    status, findings = srd._fetch_remote(_FakePremium(text=json.dumps({"status": "ok"})), 0.5)
    assert status == "error" and findings is None


def test_fetch_remote_timeout() -> None:
    status, findings = srd._fetch_remote(_FakePremium(text="{}", delay=0.3), 0.01)
    assert status == "timeout" and findings is None


def test_fetch_findings_no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from watercooler_mcp import memory_sync

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: None)
    assert srd._fetch_findings(0.5) == ("unavailable", None)


def test_fetch_findings_remote_premium_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from watercooler_mcp import memory_sync

    class _RT:
        premium_client = None

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: _RT())
    monkeypatch.setattr(srd, "_routes_remote", lambda _rt: True)
    assert srd._fetch_findings(0.5) == ("unavailable", None)


def test_fetch_findings_local_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from watercooler_mcp import daemons, memory_sync

    class _RT:
        surface = "local_full"  # -> _routes_remote False

    class _Finding:
        def to_dict(self):
            return _finding("planner", 2)

    class _Mgr:
        def get_all_findings(self, **_kw):
            return [_Finding()]

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: _RT())
    monkeypatch.setattr(daemons, "get_daemon_runtime", lambda: _Mgr())
    status, findings = srd._fetch_findings(0.5)
    assert status == "success"
    assert findings and findings[0]["details"]["advisory"]["role"] == "planner"


def test_resolve_signal_isolation_preserves_salience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A signal-fetch blow-up must NOT suppress salience (independent try blocks).
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')

    def _boom(_deadline):
        raise RuntimeError("signal exploded")

    monkeypatch.setattr(srd, "_fetch_findings", _boom)
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.stance_block_status == "error"
    assert block.salience_status == "loaded"
    assert block.roles["planner"].salience == ["watch X"]


def test_resolve_stance_block_success_overlays_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_roles(tmp_path, '[roles.critic]\nproject_salience = ["notice Z"]\n')
    monkeypatch.setattr(
        srd, "_fetch_findings", lambda _d: ("success", [_finding("critic", 2, summary="risk")])
    )
    block = resolve_stance_block(FakeContext(code_root=str(tmp_path)))
    assert block.stance_block_status == "success"
    assert block.roles["critic"].elevated is True
    assert block.roles["critic"].level == 2
    assert block.roles["critic"].salience == ["notice Z"]


def test_markdown_renders_elevated_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_roles(tmp_path, '[roles.critic]\nproject_salience = ["notice Z"]\n')
    monkeypatch.setattr(
        srd, "_fetch_findings", lambda _d: ("success", [_finding("critic", 2, summary="risk")])
    )
    md = render_stance_markdown(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert "## Project stance" in md
    assert "(signal:" not in md  # success -> no degraded suffix
    assert "L2: risk" in md
    assert "source: decision_stance" in md
    assert "advisory only" in md


def test_markdown_shows_degraded_signal_suffix() -> None:
    block = StanceBlock(stance_block_status="timeout", salience_status="loaded")
    block.roles["planner"] = srd.RoleStance(salience=["watch X"])
    md = render_stance_markdown(block)
    assert "## Project stance (signal: timeout)" in md


def test_apply_signal_sanitizes_terminal_escapes() -> None:
    # Finding-derived summary/source must be stripped of terminal-escape and
    # control bytes (parity with stop_hook), since they reach a terminal/LLM sink.
    roles = _roles()
    finding = _finding("critic", 2, summary="risk\x1b[31m\x07 here", daemon="dae\x1bmon")
    srd._apply_signal(roles, [finding])
    critic = roles["critic"]
    assert critic.summary == "risk here"
    assert critic.source == "daemon"
    assert "\x1b" not in critic.summary and "\x07" not in critic.summary


def test_fetch_remote_offload_path_under_running_loop() -> None:
    # When the calling thread already owns a running loop, run_coro_in_fresh_loop
    # offloads to a worker; exercise that branch end-to-end (the FastMCP-async case).
    import asyncio as _a

    payload = json.dumps({"findings": [_finding("planner", 1)]})

    async def _driver():
        return srd._fetch_remote(_FakePremium(text=payload), 0.5)

    status, findings = _a.run(_driver())
    assert status == "success"
    assert findings and len(findings) == 1


# --------------------------------------------------------------------------- #
# Codex review fixes: tombstone precedence, status envelope, route suppression
# --------------------------------------------------------------------------- #


def test_apply_signal_tombstone_clears_over_older_elevated() -> None:
    # Newest finding is an L0 tombstone (clearance); an older L2 for the same role
    # is still in the list. The role must stay CLEARED, not resurrect the L2.
    roles = _roles()
    srd._apply_signal(
        roles,
        [
            _finding("critic", 0, summary="cleared", created_at=2000.0),
            _finding("critic", 2, summary="stale elevated", created_at=1000.0),
        ],
    )
    assert roles["critic"].elevated is False
    assert roles["critic"].level is None


def test_apply_signal_malformed_level_does_not_decide_role() -> None:
    # A malformed newest finding must not block a real older elevated finding.
    roles = _roles()
    bad = _finding("tester", 2)
    bad["details"]["advisory"]["level"] = "high"  # malformed
    srd._apply_signal(roles, [bad, _finding("tester", 1, summary="real")])
    assert roles["tester"].elevated is True
    assert roles["tester"].summary == "real"


def test_fetch_remote_daemon_status_error() -> None:
    payload = json.dumps({"status": "error", "message": "boom", "findings": []})
    status, findings = srd._fetch_remote(_FakePremium(text=payload), 0.5)
    assert status == "error" and findings is None


def test_fetch_remote_daemon_not_initialized() -> None:
    payload = json.dumps({"status": "not_initialized", "message": "x", "findings": []})
    status, findings = srd._fetch_remote(_FakePremium(text=payload), 0.5)
    assert status == "unavailable" and findings is None


def test_fetch_remote_success_envelope_with_count() -> None:
    payload = json.dumps({"count": 1, "findings": [_finding("planner", 1)]})
    status, findings = srd._fetch_remote(_FakePremium(text=payload), 0.5)
    assert status == "success" and findings is not None and len(findings) == 1


def test_routes_remote_false_when_daemon_pinned_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import watercooler_mcp.server_factory as sf

    monkeypatch.setattr(sf, "mountable_remote_tools_for_hybrid", lambda _rt: {"watercooler_daemon_findings"})
    monkeypatch.setattr(sf, "_premium_daemon_pinned_local", lambda: True)
    assert srd._routes_remote(object()) is False


def test_routes_remote_true_when_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    import watercooler_mcp.server_factory as sf

    monkeypatch.setattr(sf, "mountable_remote_tools_for_hybrid", lambda _rt: {"watercooler_daemon_findings"})
    monkeypatch.setattr(sf, "_premium_daemon_pinned_local", lambda: False)
    assert srd._routes_remote(object()) is True


def test_routes_remote_false_when_not_mountable(monkeypatch: pytest.MonkeyPatch) -> None:
    import watercooler_mcp.server_factory as sf

    monkeypatch.setattr(sf, "mountable_remote_tools_for_hybrid", lambda _rt: set())
    assert srd._routes_remote(object()) is False


def test_fetch_findings_disabled_route_does_not_fetch_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # daemon_observe explicitly disabled: even with a live local manager, the
    # read must report "unavailable" and never overlay stance (C5 contract).
    from watercooler_mcp import daemons, memory_sync

    class _Profile:
        def resolve_execution_target(self, cap, *, local_available, remote_available):
            return "disabled"

    class _RT:
        surface = "local_full"
        premium_client = None
        capability_profile = _Profile()

    consulted = {"n": 0}

    class _Mgr:
        def get_all_findings(self, **_kw):
            consulted["n"] += 1
            return []

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: _RT())
    monkeypatch.setattr(daemons, "get_daemon_runtime", lambda: _Mgr())
    status, findings = srd._fetch_findings(0.5)
    assert status == "unavailable"
    assert findings is None
    assert consulted["n"] == 0  # _fetch_local never reached


def test_fetch_findings_local_route_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contrast: daemon_observe resolves "local" -> local fetch happens.
    from watercooler_mcp import daemons, memory_sync

    class _Profile:
        def resolve_execution_target(self, cap, *, local_available, remote_available):
            return "local"

    class _RT:
        surface = "local_full"
        premium_client = None
        capability_profile = _Profile()

    class _Finding:
        def to_dict(self):
            return _finding("planner", 2)

    class _Mgr:
        def get_all_findings(self, **_kw):
            return [_Finding()]

    monkeypatch.setattr(memory_sync, "get_runtime", lambda: _RT())
    monkeypatch.setattr(daemons, "get_daemon_runtime", lambda: _Mgr())
    status, findings = srd._fetch_findings(0.5)
    assert status == "success"
    assert findings and findings[0]["details"]["advisory"]["role"] == "planner"


# --------------------------------------------------------------------------- #
# Phase 3 polish: elevated-first ordering, quiet marker, staleness
# --------------------------------------------------------------------------- #


def test_apply_signal_marks_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srd, "_now", lambda: 1_000_000.0)
    roles = _roles()
    srd._apply_signal(roles, [_finding("critic", 2, created_at=1_000_000.0 - 8 * 86400)])
    assert roles["critic"].stale is True

    fresh = _roles()
    srd._apply_signal(fresh, [_finding("planner", 2, created_at=1_000_000.0 - 3600)])
    assert fresh["planner"].stale is False


def test_markdown_elevated_first_ordering() -> None:
    roles = {r: srd.RoleStance() for r in STANCE_ROLES}
    roles["planner"] = srd.RoleStance(salience=["plan cue"])  # quiet, salience-only
    roles["tester"] = srd.RoleStance(
        elevated=True, level=2, summary="risk", source="decision_stance"
    )
    block = StanceBlock(stance_block_status="success", salience_status="loaded", roles=roles)
    md = render_stance_markdown(block)
    # elevated tester rendered before the quiet planner
    assert md.index("**tester**") < md.index("**planner**")
    assert "**planner** (quiet)" in md


def test_markdown_no_quiet_marker_when_none_elevated(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    md = render_stance_markdown(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert "(quiet)" not in md  # no elevated contrast -> no clutter
    assert "**planner**" in md


def test_markdown_stale_marker() -> None:
    roles = {r: srd.RoleStance() for r in STANCE_ROLES}
    roles["critic"] = srd.RoleStance(
        elevated=True,
        level=2,
        summary="old risk",
        source="decision_stance",
        produced_at="2020-01-01T00:00:00+00:00",
        stale=True,
    )
    block = StanceBlock(stance_block_status="success", salience_status="loaded", roles=roles)
    md = render_stance_markdown(block)
    assert "L2: old risk" in md
    assert "stale" in md


def test_json_includes_stale(tmp_path: Path) -> None:
    _write_roles(tmp_path, '[roles.planner]\nproject_salience = ["watch X"]\n')
    payload = render_stance_json(resolve_stance_block(FakeContext(code_root=str(tmp_path))))
    assert payload["roles"]["planner"]["stale"] is False
