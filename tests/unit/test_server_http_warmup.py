"""Tests for the Graphiti warmup helpers in ``server_http``.

Issue #734 (PR #660 collateral): the startup Graphiti warmup probe was a
closure nested inside ``create_http_app()``. In hosted multi-tenant mode
it ran before any HTTP request had set ``http_ctx``, so
``load_graphiti_config()`` returned ``None`` and ``/health`` displayed
``graphiti_warmup: failed`` even though Graphiti was actually fine
(per-scope cold-start latency is paid lazily on first per-tenant
request).

The fix extracts two module-level helpers:

* ``_initialize_warmup_state(is_hosted)`` — sets the warmup state dict.
  Hosted → ``"skipped"`` with a human-readable ``reason``. Self-hosted
  → ``"disabled"`` (the warmup thread will overwrite it as it progresses).
* ``_run_warmup_probe()`` — runs the actual probe; only invoked in
  self-hosted single-tenant deployments.

These tests pin the contract.
"""

from __future__ import annotations


def test_initialize_warmup_state_hosted_returns_skipped():
    """Hosted mode → state dict is 'skipped' with a multi-tenant reason."""
    from watercooler_mcp import server_http

    server_http._initialize_warmup_state(is_hosted=True)

    state = server_http._graphiti_warm_state
    assert state["state"] == "skipped"
    assert state["host"] is None
    assert state["port"] is None
    assert state["database"] is None
    assert state["error"] is None
    assert state["duration_ms"] == 0
    # The reason must mention the multi-tenant scope-bound nature so an
    # operator reading /health understands why nothing was warmed.
    assert "multi-tenant" in (state.get("reason") or "")


def test_initialize_warmup_state_self_hosted_returns_disabled():
    """Self-hosted mode → state dict starts at 'disabled' (warmup thread updates).

    The probe itself flips state to 'warming' → 'ready' / 'failed'. The
    initialiser just resets to the pre-probe baseline.
    """
    from watercooler_mcp import server_http

    server_http._initialize_warmup_state(is_hosted=False)

    state = server_http._graphiti_warm_state
    assert state["state"] == "disabled"
    assert state.get("reason") is None


def test_initialize_warmup_state_mutates_in_place():
    """The initialiser must mutate the existing dict, not rebind it.

    The diagnostic surface imports the module-level dict once and
    expects it to remain the same object. Rebinding would leave the
    diagnostic looking at a stale dict.
    """
    from watercooler_mcp import server_http

    original = server_http._graphiti_warm_state
    server_http._initialize_warmup_state(is_hosted=True)
    assert server_http._graphiti_warm_state is original

    server_http._initialize_warmup_state(is_hosted=False)
    assert server_http._graphiti_warm_state is original


def test_run_warmup_probe_handles_load_returns_none(monkeypatch):
    """When load_graphiti_config returns None, state goes to 'failed'.

    This is the legacy self-hosted failure path. The new ``reason`` field
    captures why so the diagnostic line is informative.
    """
    from watercooler_mcp import server_http

    # Reset state cleanly first.
    server_http._initialize_warmup_state(is_hosted=False)

    monkeypatch.setattr(
        "watercooler_mcp.memory.load_graphiti_config",
        lambda: None,
    )

    server_http._run_warmup_probe()

    state = server_http._graphiti_warm_state
    assert state["state"] == "failed"
    assert state["error"] == "load_graphiti_config returned None"
    assert state["reason"] == "load_graphiti_config returned None"


def test_run_warmup_probe_handles_exception(monkeypatch):
    """When the probe raises, state goes to 'failed' with the exception message."""
    from watercooler_mcp import server_http

    server_http._initialize_warmup_state(is_hosted=False)

    def _raise():
        raise RuntimeError("FalkorDB unreachable")

    monkeypatch.setattr(
        "watercooler_mcp.memory.load_graphiti_config",
        _raise,
    )

    server_http._run_warmup_probe()

    state = server_http._graphiti_warm_state
    assert state["state"] == "failed"
    assert "FalkorDB unreachable" in (state["error"] or "")
    assert "FalkorDB unreachable" in (state["reason"] or "")
