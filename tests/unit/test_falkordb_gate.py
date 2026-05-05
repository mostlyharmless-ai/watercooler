"""Unit tests for the local FalkorDB auto-start gate in
``watercooler_mcp.startup.ensure_falkordb_running``.

Covers the T1-aware gate fix for the
``bug-falkordb-startup-gate-t1-2026-05-04`` thread (Plan v3 entry
``01KQTGHGPYXQ51Z1S94BKVZFZJ``): FalkorDB must be auto-started when
*either* T1 baseline semantic (``mcp.graph.generate_embeddings``) *or*
T2 graphiti (``memory.backend == "graphiti"``) is enabled in stdio
mode. Pre-fix, the gate only honored T2.

The 7-case matrix below is the authoritative description of the gate's
intended behavior; future changes to the gate must keep this green.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.startup import ServiceState, ensure_falkordb_running


def _build_cfg(*, transport: str, generate_embeddings: bool):
    """Construct the minimal cfg shape that ``ensure_falkordb_running``
    reads — only ``mcp.transport`` and ``mcp.graph.generate_embeddings``."""
    return SimpleNamespace(
        mcp=SimpleNamespace(
            transport=transport,
            graph=SimpleNamespace(generate_embeddings=generate_embeddings),
        )
    )


def _resolve_db(host: str = "localhost", port: int = 6379):
    return SimpleNamespace(host=host, port=port)


@pytest.fixture
def gate_env(monkeypatch):
    """Common fixture: patches every dependency of ``ensure_falkordb_running``
    so each test can dial the inputs (transport / generate_embeddings /
    backend / host) and assert the resulting status without touching
    Docker, the real config, or any background thread."""
    status_updates: list[tuple[str, ServiceState, dict]] = []
    threads_started: list[str] = []

    def fake_update(name, state, **kwargs):
        status_updates.append((name, state, kwargs))

    def fake_thread(*, target, args, daemon, name):
        threads_started.append(name)
        # Don't actually start — just record intent.
        return MagicMock(start=lambda: None)

    monkeypatch.setattr(
        "watercooler_mcp.startup._update_service_status", fake_update
    )
    monkeypatch.setattr(
        "watercooler_mcp.startup._check_falkordb_health", lambda *a, **kw: False
    )
    monkeypatch.setattr(
        "watercooler_mcp.startup.threading.Thread", fake_thread
    )

    return SimpleNamespace(
        status_updates=status_updates, threads_started=threads_started
    )


def _set_config(monkeypatch, *, transport: str, generate_embeddings: bool):
    cfg = _build_cfg(transport=transport, generate_embeddings=generate_embeddings)
    monkeypatch.setattr(
        "watercooler_mcp.config.get_watercooler_config", lambda: cfg
    )


def _set_backend(monkeypatch, backend: str):
    monkeypatch.setattr(
        "watercooler.memory_config.get_memory_backend", lambda: backend
    )


def _set_db(monkeypatch, *, host: str = "localhost", port: int = 6379):
    monkeypatch.setattr(
        "watercooler.memory_config.resolve_database_config",
        lambda: _resolve_db(host=host, port=port),
    )


def _last_falkordb_update(updates):
    """Return the most recent (state, kwargs) update for the falkordb service."""
    for name, state, kwargs in reversed(updates):
        if name == "falkordb":
            return state, kwargs
    return None, None


# ---------------------------------------------------------------------------
# 7-case gate matrix (Plan v3 `01KQTGHGPYXQ51Z1S94BKVZFZJ`)
# ---------------------------------------------------------------------------


class TestFalkorDBGateMatrix:
    """The seven cases from Plan v3's test matrix."""

    def test_hybrid_transport_skips(self, monkeypatch, gate_env):
        """hybrid + true + graphiti + localhost → DISABLED (transport)."""
        _set_config(monkeypatch, transport="hybrid", generate_embeddings=True)
        _set_backend(monkeypatch, "graphiti")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        state, kwargs = _last_falkordb_update(gate_env.status_updates)
        assert state == ServiceState.DISABLED
        assert "hybrid" in kwargs.get("message", "")
        assert gate_env.threads_started == []

    def test_proxy_transport_skips(self, monkeypatch, gate_env):
        """proxy + true + graphiti + localhost → DISABLED (transport)."""
        _set_config(monkeypatch, transport="proxy", generate_embeddings=True)
        _set_backend(monkeypatch, "graphiti")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        state, kwargs = _last_falkordb_update(gate_env.status_updates)
        assert state == ServiceState.DISABLED
        assert "proxy" in kwargs.get("message", "")
        assert gate_env.threads_started == []

    def test_stdio_t1_and_t2_starts(self, monkeypatch, gate_env):
        """stdio + true + graphiti + localhost → AUTO-START."""
        _set_config(monkeypatch, transport="stdio", generate_embeddings=True)
        _set_backend(monkeypatch, "graphiti")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        assert gate_env.threads_started == ["falkordb-startup"]

    def test_stdio_t1_only_starts(self, monkeypatch, gate_env):
        """stdio + true + null + localhost → AUTO-START (T1).

        This is the bug fix: pre-fix, this case was DISABLED because the
        gate only honored T2 (``memory.backend == "graphiti"``). T1
        semantic was ignored despite ``FalkorDBEntryStore`` needing
        FalkorDB to satisfy ``find_similar`` / embedding writes.
        """
        _set_config(monkeypatch, transport="stdio", generate_embeddings=True)
        _set_backend(monkeypatch, "null")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        assert gate_env.threads_started == ["falkordb-startup"], (
            "T1-only stdio user must auto-start FalkorDB (this is the bug fix)"
        )

    def test_stdio_t2_only_starts(self, monkeypatch, gate_env):
        """stdio + false + graphiti + localhost → AUTO-START (T2)."""
        _set_config(monkeypatch, transport="stdio", generate_embeddings=False)
        _set_backend(monkeypatch, "graphiti")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        assert gate_env.threads_started == ["falkordb-startup"]

    def test_stdio_no_tier_skips(self, monkeypatch, gate_env):
        """stdio + false + null + localhost → DISABLED (no tier needs it)."""
        _set_config(monkeypatch, transport="stdio", generate_embeddings=False)
        _set_backend(monkeypatch, "null")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        state, kwargs = _last_falkordb_update(gate_env.status_updates)
        assert state == ServiceState.DISABLED
        msg = kwargs.get("message", "")
        assert "neither" in msg.lower() or "no tier" in msg.lower(), (
            f"Expected explanation referencing both tiers; got: {msg!r}"
        )
        assert gate_env.threads_started == []

    def test_stdio_remote_host_not_configured(self, monkeypatch, gate_env):
        """stdio + true + graphiti + remote → NOT_CONFIGURED (host)."""
        _set_config(monkeypatch, transport="stdio", generate_embeddings=True)
        _set_backend(monkeypatch, "graphiti")
        _set_db(monkeypatch, host="some-remote-host")

        ensure_falkordb_running()

        state, kwargs = _last_falkordb_update(gate_env.status_updates)
        assert state == ServiceState.NOT_CONFIGURED
        assert "some-remote-host" in kwargs.get("message", "")
        assert gate_env.threads_started == []


# ---------------------------------------------------------------------------
# Resilience cases (config-resolution failures must not lock out stdio)
# ---------------------------------------------------------------------------


class TestFalkorDBGateResilience:
    """Resolution-failure paths in the gate must default to safe behavior."""

    def test_transport_config_failure_does_not_mark_failed(
        self, monkeypatch, gate_env
    ):
        """When the outer transport-config resolve fails, the function must
        log the failure with a real f-string (not ``"%s"`` + positional
        arg, which raises TypeError under ``log_error``'s actual signature
        ``log_error(message: str, **fields)``). Pre-existing-bug-fix
        regression: previously the buggy ``log_error(..., exc)`` raised,
        propagated to the outer ``except Exception`` block, and marked
        FalkorDB as ``FAILED`` instead of executing the intended
        ``transport = "stdio"`` graceful fallback. CR observation on
        PR #758."""
        # Force config-resolution to fail.
        def boom():
            raise RuntimeError("config.toml is malformed")

        monkeypatch.setattr(
            "watercooler_mcp.config.get_watercooler_config", boom
        )
        # Backend resolves to null → with default-true T1 fallback, the
        # gate should AUTO-START (T1 conservative path).
        monkeypatch.setattr(
            "watercooler.memory_config.get_memory_backend", lambda: "null"
        )
        _set_db(monkeypatch)

        ensure_falkordb_running()

        # The log_error f-string must format cleanly (no TypeError raised
        # to the outer except block). FalkorDB must NOT be marked FAILED.
        falkordb_states = [
            state for name, state, _ in gate_env.status_updates
            if name == "falkordb"
        ]
        assert ServiceState.FAILED not in falkordb_states, (
            f"FalkorDB was marked FAILED, suggesting the log_error TypeError "
            f"regressed. States seen: {falkordb_states}"
        )
        # Conservative T1 fallback should auto-start.
        assert gate_env.threads_started == ["falkordb-startup"]

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("memory backend config is wrong"),
            ImportError("watercooler.memory_config not found"),
            AttributeError("'NoneType' object has no attribute 'backend'"),
            RuntimeError("graphiti backend module crashed at import"),
        ],
        ids=["ValueError", "ImportError", "AttributeError", "RuntimeError"],
    )
    def test_memory_backend_failure_does_not_mark_failed(
        self, monkeypatch, gate_env, exc
    ):
        """When ``get_memory_backend`` raises ANY exception (malformed
        memory config, missing import, partial config object, runtime
        crash), the function must log it and return cleanly — NOT escape
        to the outer ``except Exception`` and mark FalkorDB as FAILED.

        The catch must be broad (``except Exception``), matching the
        transport-config and graph-config catches. CR finding on PR #758
        flagged that the prior ``except ValueError`` was too narrow and
        let ``ImportError`` / ``AttributeError`` etc. escape, defeating
        this PR's stated resilience intent. Parametrized over the
        exception types most likely to arise from a malformed
        ``memory_config`` module."""
        _set_config(monkeypatch, transport="stdio", generate_embeddings=True)

        def boom():
            raise exc

        monkeypatch.setattr(
            "watercooler.memory_config.get_memory_backend", boom
        )
        _set_db(monkeypatch)

        ensure_falkordb_running()

        falkordb_states = [
            state for name, state, _ in gate_env.status_updates
            if name == "falkordb"
        ]
        assert ServiceState.FAILED not in falkordb_states, (
            f"FalkorDB was marked FAILED on {type(exc).__name__} — the "
            f"backend catch is still too narrow. States seen: {falkordb_states}"
        )
        # ``return`` after log_error → no thread started, no DISABLED update.
        assert gate_env.threads_started == []

    def test_graph_config_failure_falls_back_to_default_true(
        self, monkeypatch, gate_env
    ):
        """If ``mcp.graph.generate_embeddings`` cannot be resolved, the gate
        treats T1 as enabled (matches schema default) so the user is not
        locked out of stdio mode by a malformed config."""
        # Transport resolves cleanly to "stdio"; the graph attribute lookup
        # fails because the mcp object has no .graph (mimics a partial /
        # malformed config that survives transport read but breaks the
        # nested-config read).
        broken_mcp = SimpleNamespace(transport="stdio")
        cfg = SimpleNamespace(mcp=broken_mcp)
        monkeypatch.setattr(
            "watercooler_mcp.config.get_watercooler_config", lambda: cfg
        )
        _set_backend(monkeypatch, "null")
        _set_db(monkeypatch)

        ensure_falkordb_running()

        # T1 defaulted to True → AUTO-START
        assert gate_env.threads_started == ["falkordb-startup"]
