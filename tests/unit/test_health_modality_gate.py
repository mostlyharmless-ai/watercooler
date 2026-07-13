"""Unit tests for the modality-gated health diagnostics
(audit-transport-modes-hosted-db-2026-07 plan v3, entry
``01KWZBMM7XS4TXANENWC88KZ10``).

Covers:

- ``watercooler_mcp.startup.local_falkordb_in_use`` — the shared
  "does this configuration use local FalkorDB?" decision (§A), including
  the Codex-required five-capability regression case (``memory_query``).
- The diagnostic Memory Sync block probing 127.0.0.1:6379 ONLY when the
  configuration uses local FalkorDB (§A) — a configuration that does not
  use local FalkorDB must not look for one.
- The structural pin replacing the deleted reachability "Mismatch"
  warning: hybrid never constructs a local Graphiti backend (§B).
- ``phase_indicator`` honesty in ``diagnose_memory`` (§C): probe failure
  reports "indeterminate" + reason, and a ``phase_1_local_only`` computed
  where the handoff signals can never be set carries an explicit
  conflict note.
- The hosted state-root persistence alarm (§D).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from watercooler_mcp.startup import local_falkordb_in_use


ALL_FIVE_CAPS = (
    "memory_ingest",
    "memory_query",
    "memory_observe",
    "daemon_observe",
    "semantic_similarity",
)


def _cfg(
    *,
    transport: str = "stdio",
    generate_embeddings: bool = False,
    capability_routes: dict | None = None,
):
    return SimpleNamespace(
        mcp=SimpleNamespace(
            transport=transport,
            graph=SimpleNamespace(generate_embeddings=generate_embeddings),
            capability_routes=dict(capability_routes or {}),
        )
    )


def _set_cfg(monkeypatch, cfg):
    monkeypatch.setattr(
        "watercooler_mcp.config.get_watercooler_config", lambda: cfg
    )


def _set_backend(monkeypatch, backend: str):
    monkeypatch.setattr(
        "watercooler.memory_config.get_memory_backend", lambda: backend
    )


# ---------------------------------------------------------------------------
# local_falkordb_in_use — the shared modality decision (§A)
# ---------------------------------------------------------------------------


class TestLocalFalkordbInUse:
    def test_hybrid_transport_not_in_use(self, monkeypatch):
        _set_cfg(monkeypatch, _cfg(transport="hybrid", generate_embeddings=True))
        _set_backend(monkeypatch, "graphiti")

        in_use, reason, detail = local_falkordb_in_use()

        assert in_use is False
        assert reason == "transport_hosted"
        assert "hybrid" in detail

    def test_proxy_transport_not_in_use(self, monkeypatch):
        _set_cfg(monkeypatch, _cfg(transport="proxy", generate_embeddings=True))
        _set_backend(monkeypatch, "graphiti")

        in_use, reason, _ = local_falkordb_in_use()

        assert in_use is False
        assert reason == "transport_hosted"

    def test_stdio_all_five_routes_remote_not_in_use(self, monkeypatch):
        routes = {c: "remote" for c in ALL_FIVE_CAPS}
        _set_cfg(
            monkeypatch,
            _cfg(transport="stdio", generate_embeddings=True,
                 capability_routes=routes),
        )
        _set_backend(monkeypatch, "graphiti")

        in_use, reason, _ = local_falkordb_in_use()

        assert in_use is False
        assert reason == "routes_all_remote"

    def test_memory_query_local_alone_keeps_falkordb_in_use(self, monkeypatch):
        """Codex review :27 blocking-finding regression pin.

        ``memory_query`` is the fifth FalkorDB-using capability
        (``falkordb_caps``, startup.py). With only ``memory_query`` local
        and T2 graphiti enabled, the all-remote shortcut must NOT fire:
        local graph reads still need FalkorDB. Under the four-route
        omission this returned ``routes_all_remote`` (wrong).
        """
        routes = {c: "remote" for c in ALL_FIVE_CAPS}
        routes["memory_query"] = "local"
        _set_cfg(
            monkeypatch,
            _cfg(transport="stdio", generate_embeddings=False,
                 capability_routes=routes),
        )
        _set_backend(monkeypatch, "graphiti")

        in_use, reason, _ = local_falkordb_in_use()

        assert in_use is True
        assert reason == "t2_graphiti"

    def test_stdio_t1_only_in_use(self, monkeypatch):
        _set_cfg(monkeypatch, _cfg(transport="stdio", generate_embeddings=True))
        _set_backend(monkeypatch, "null")

        in_use, reason, _ = local_falkordb_in_use()

        assert in_use is True
        assert reason == "t1_semantic"

    def test_stdio_no_tier_not_in_use(self, monkeypatch):
        _set_cfg(monkeypatch, _cfg(transport="stdio", generate_embeddings=False))
        _set_backend(monkeypatch, "null")

        in_use, reason, detail = local_falkordb_in_use()

        assert in_use is False
        assert reason == "no_local_tier"
        assert "neither" in detail.lower()

    def test_backend_unresolvable_not_in_use_and_tagged(self, monkeypatch):
        _set_cfg(monkeypatch, _cfg(transport="stdio", generate_embeddings=False))

        def boom():
            raise RuntimeError("backend config exploded")

        monkeypatch.setattr(
            "watercooler.memory_config.get_memory_backend", boom
        )

        in_use, reason, detail = local_falkordb_in_use()

        assert in_use is False
        assert reason == "backend_unresolvable"
        assert "backend config exploded" in detail


# ---------------------------------------------------------------------------
# Diagnostic Memory Sync block — probe only when local FalkorDB is used (§A)
# ---------------------------------------------------------------------------


def _run_memory_sync_block(monkeypatch, *, cfg, backend: str):
    """Run _append_memory_sync_block with a recording socket factory.

    Returns (rendered_output, socket_calls).
    """
    from watercooler_mcp.tools.diagnostic import _append_memory_sync_block

    _set_cfg(monkeypatch, cfg)
    _set_backend(monkeypatch, backend)

    socket_calls: list = []

    class _RecordingSocket:
        def __init__(self, *a, **kw):
            socket_calls.append((a, kw))

        def settimeout(self, *_a):
            pass

        def connect(self, *_a):
            raise OSError("nothing listening")

        def close(self):
            pass

    monkeypatch.setattr("socket.socket", _RecordingSocket)

    status_lines: list[str] = []
    context = SimpleNamespace(repo_slug="org/repo", code_repo_name="repo")
    _append_memory_sync_block(status_lines, context)
    return "\n".join(status_lines), socket_calls


class TestDiagnosticProbeGating:
    def test_hybrid_all_remote_makes_no_socket_connection(self, monkeypatch):
        """A configuration that does not use local FalkorDB must not look
        for one — zero socket use, explicit "not used" line, and no
        mismatch warning even though (in the old code) a listener would
        have triggered one."""
        out, socket_calls = _run_memory_sync_block(
            monkeypatch,
            cfg=_cfg(transport="hybrid", generate_embeddings=True),
            backend="graphiti",
        )

        assert socket_calls == [], "modality says unused: the port must not be touched"
        assert "Local FalkorDB: not used in this configuration" in out
        assert "Mismatch" not in out
        assert "⚠️" not in out

    def test_local_mode_still_probes_and_reports(self, monkeypatch):
        out, socket_calls = _run_memory_sync_block(
            monkeypatch,
            cfg=_cfg(transport="stdio", generate_embeddings=True),
            backend="graphiti",
        )

        assert len(socket_calls) == 1
        assert "Local FalkorDB (127.0.0.1:6379): not reachable" in out

    def test_backend_unresolvable_reports_indeterminate_not_unused(
        self, monkeypatch
    ):
        """PR #1086 review blocking finding: ``backend_unresolvable`` means
        the helper FAILED to establish the T2 backend setting — not that
        local FalkorDB is unused. Health must report indeterminate (and
        not probe), never a confident "not used"."""
        from watercooler_mcp.tools.diagnostic import _append_memory_sync_block

        _set_cfg(monkeypatch, _cfg(transport="stdio", generate_embeddings=False))

        def boom():
            raise RuntimeError("backend config exploded")

        monkeypatch.setattr(
            "watercooler.memory_config.get_memory_backend", boom
        )

        socket_calls: list = []

        class _RecordingSocket:
            def __init__(self, *a, **kw):
                socket_calls.append((a, kw))

        monkeypatch.setattr("socket.socket", _RecordingSocket)

        status_lines: list[str] = []
        context = SimpleNamespace(repo_slug="org/repo", code_repo_name="repo")
        _append_memory_sync_block(status_lines, context)
        out = "\n".join(status_lines)

        assert "Local FalkorDB: usage indeterminate" in out
        assert "backend config exploded" in out
        assert "not used in this configuration" not in out
        assert socket_calls == [], "unestablished usage must not be probed"

    def test_reachability_never_warns(self, monkeypatch):
        """Even in a probing (local) configuration, reachability is
        reported neutrally — the reachability-based Mismatch warning is
        deleted, replaced by the structural hybrid_refused pin below."""
        from watercooler_mcp.tools.diagnostic import _append_memory_sync_block

        _set_cfg(monkeypatch, _cfg(transport="stdio", generate_embeddings=True))
        _set_backend(monkeypatch, "graphiti")

        class _ReachableSocket:
            def __init__(self, *a, **kw):
                pass

            def settimeout(self, *_a):
                pass

            def connect(self, *_a):
                return None  # something answers

            def close(self):
                pass

        monkeypatch.setattr("socket.socket", _ReachableSocket)

        status_lines: list[str] = []
        context = SimpleNamespace(repo_slug="org/repo", code_repo_name="repo")
        _append_memory_sync_block(status_lines, context)
        out = "\n".join(status_lines)

        assert "Local FalkorDB (127.0.0.1:6379): reachable" in out
        assert "Mismatch" not in out


# ---------------------------------------------------------------------------
# Structural pin — hybrid never constructs a local Graphiti backend (§B)
# ---------------------------------------------------------------------------


class TestHybridRefusedGuardPin:
    def test_local_hybrid_surface_refuses_backend_construction(self, monkeypatch):
        """The regression the deleted port-sniff warning feared —
        in-process GraphitiBackend re-enabled under hybrid — is prevented
        here: get_graphiti_backend must refuse construction outright when
        the runtime surface is local_hybrid."""
        from watercooler_mcp import memory as wc_memory

        monkeypatch.setattr(wc_memory, "_graphiti_importable", lambda: True)
        monkeypatch.setattr(
            "watercooler_mcp.memory_sync.get_runtime",
            lambda: SimpleNamespace(surface="local_hybrid"),
        )

        result = wc_memory.get_graphiti_backend(MagicMock())

        assert isinstance(result, dict)
        assert result.get("error") == "hybrid_refused"


# ---------------------------------------------------------------------------
# phase_indicator honesty (§C)
# ---------------------------------------------------------------------------


def _run_identity_fields(monkeypatch, *, hosted: bool, transport: str = "stdio",
                         t1_signal=lambda: False, t2_signal=lambda: False):
    from watercooler_mcp.tools.memory import _add_canonical_identity_fields

    monkeypatch.setattr(
        "watercooler_mcp.context.get_effective_context",
        lambda: SimpleNamespace(repo="org/repo"),
    )
    monkeypatch.setattr("watercooler_mcp.auth.is_hosted_mode", lambda: hosted)
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync._t1_remote_upsert_enabled", t1_signal
    )
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.is_hybrid_t2_handoff_active", t2_signal
    )
    _set_cfg(monkeypatch, _cfg(transport=transport))

    diagnostics: dict = {}
    _add_canonical_identity_fields(diagnostics)
    return diagnostics


class TestPhaseIndicatorHonesty:
    def test_signal_probe_failure_reports_indeterminate(self, monkeypatch):
        """A diagnostic must not assert a state it failed to establish:
        signal-probe failure must never default to phase_1_local_only."""
        def boom():
            raise RuntimeError("signals unavailable")

        diagnostics = _run_identity_fields(
            monkeypatch, hosted=False, t1_signal=boom
        )

        assert diagnostics["phase_indicator"] == "indeterminate"
        assert "signals unavailable" in diagnostics["phase_indicator_error"]

    def test_active_handoff_reports_phase_5_without_note(self, monkeypatch):
        diagnostics = _run_identity_fields(
            monkeypatch, hosted=False, t2_signal=lambda: True
        )

        assert diagnostics["phase_indicator"] == "phase_5_hybrid_t2_handoff"
        assert "phase_indicator_note" not in diagnostics
        assert "phase_indicator_error" not in diagnostics

    def test_hosted_process_phase_1_carries_conflict_note(self, monkeypatch):
        """Jay's case: diagnose_memory executed in a hosted server process
        where the hybrid handoff signals are never set. The bare
        'phase_1_local_only' label mislabels the server process's state as
        the caller's; it must carry an explicit conflict note."""
        diagnostics = _run_identity_fields(monkeypatch, hosted=True)

        assert diagnostics["phase_indicator"] == "phase_1_local_only"
        note = diagnostics.get("phase_indicator_note", "")
        assert "conflicts with configured routes" in note

    def test_hybrid_transport_phase_1_carries_conflict_note(self, monkeypatch):
        diagnostics = _run_identity_fields(
            monkeypatch, hosted=False, transport="hybrid"
        )

        assert diagnostics["phase_indicator"] == "phase_1_local_only"
        note = diagnostics.get("phase_indicator_note", "")
        assert "conflicts with configured routes" in note
        assert "hybrid" in note

    def test_stdio_phase_1_is_clean(self, monkeypatch):
        """Genuine local-only deployment: the label is correct and carries
        neither note nor error."""
        diagnostics = _run_identity_fields(
            monkeypatch, hosted=False, transport="stdio"
        )

        assert diagnostics["phase_indicator"] == "phase_1_local_only"
        assert "phase_indicator_note" not in diagnostics
        assert "phase_indicator_error" not in diagnostics


# ---------------------------------------------------------------------------
# Hosted state-root persistence alarm (§D)
# ---------------------------------------------------------------------------


def _run_hosted_health(monkeypatch, tmp_path, *, mounted: bool,
                       state_root_exists: bool = True):
    from watercooler_mcp.tools import diagnostic as diag

    home = tmp_path / "home"
    home.mkdir()
    if state_root_exists:
        (home / ".watercooler").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("os.path.ismount", lambda p: mounted)

    ctx = SimpleNamespace(client_id="test-client")
    return diag._health_hosted_impl(ctx)


class TestHostedStateRootAlarm:
    def test_volume_backed_state_root_is_quiet(self, monkeypatch, tmp_path):
        out = _run_hosted_health(monkeypatch, tmp_path, mounted=True)

        assert "State Root:" in out
        assert "volume-backed" in out
        assert "⚠️  State Root" not in out

    def test_unmounted_state_root_warns_with_consequence(self, monkeypatch, tmp_path):
        out = _run_hosted_health(monkeypatch, tmp_path, mounted=False)

        assert "⚠️  State Root" in out
        assert "not volume-backed" in out
        assert "redeploy" in out.lower()

    def test_missing_state_root_warns(self, monkeypatch, tmp_path):
        out = _run_hosted_health(
            monkeypatch, tmp_path, mounted=False, state_root_exists=False
        )

        assert "⚠️  State Root" in out
        assert "does not exist" in out
