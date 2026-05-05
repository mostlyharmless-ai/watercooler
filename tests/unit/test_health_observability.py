"""Regression tests for health-surface observability fixes.

Covers two cosmetic but important visibility gaps:

1. **Defect #29** — `_run_hybrid` (hybrid stdio MCP) deliberately skips
   `ensure_falkordb_running()` because T2 is hosted on Railway, but
   never updates `_service_status["falkordb"]` from its initial
   `UNKNOWN` value. Health output then displayed `falkordb: unknown`
   instead of the more informative `falkordb: disabled`.

2. **Graphiti warmup observability** — the background warmup thread in
   `server_http` only logged `state` and `duration_ms`. When a warmup
   landed against the wrong database (e.g. the env-var-derived fallback
   firing pre-request) there was no breadcrumb. The state dict and log
   line now include `host`, `port`, and `database`.
"""

from __future__ import annotations

import importlib


def test_hybrid_path_marks_falkordb_disabled_in_source():
    """_run_hybrid explicitly marks falkordb DISABLED after T1-only auto-start.

    Source-inspection test: invoking _run_hybrid in a unit test is brittle
    because it builds a full MCP server. The cosmetic-defect-#29 contract
    is just that the explicit `_update_service_status` call exists in the
    hybrid path, between the LLM/embedding starters and the MCP server
    build. We assert that with a source check.
    """
    from watercooler_mcp import server as server_mod
    server_src = open(server_mod.__file__).read()
    # The disabled-mark must appear in the file. The "Hosted FalkorDB"
    # message is the stable assertion — it survives black reformatting
    # of the multi-arg _update_service_status call.
    assert "Hosted FalkorDB" in server_src


def test_hybrid_disabled_mark_actually_updates_service_status():
    """The underlying _update_service_status mechanism produces the right state."""
    from watercooler_mcp import startup as startup_mod

    original = startup_mod._service_status.get("falkordb")
    startup_mod._service_status["falkordb"] = startup_mod.ServiceStatus(
        name="falkordb",
    )
    try:
        assert startup_mod._service_status["falkordb"].state == startup_mod.ServiceState.UNKNOWN

        startup_mod._update_service_status(
            "falkordb",
            startup_mod.ServiceState.DISABLED,
            message="Hosted FalkorDB owns T1/T2 (transport=hybrid)",
        )

        final = startup_mod._service_status["falkordb"]
        assert final.state == startup_mod.ServiceState.DISABLED
        assert final.state.value == "disabled"
        assert "Hosted FalkorDB" in final.message
    finally:
        if original is not None:
            startup_mod._service_status["falkordb"] = original


def test_graphiti_warm_state_is_module_level_with_observability_keys():
    """_graphiti_warm_state must be module-level (not a closure-captured local).

    Regression guard for PR #670 review round 3 — the dict was a local
    inside create_http_app(), so the diagnostic display's
    `from ..server_http import _graphiti_warm_state` silently raised
    ImportError. Now hoisted; the import must resolve and the dict must
    carry the observability keys this PR added.
    """
    from watercooler_mcp.server_http import _graphiti_warm_state

    assert isinstance(_graphiti_warm_state, dict)
    # The keys this PR added must be present in the module-level shape.
    for key in ("state", "duration_ms", "error", "host", "port", "database"):
        assert key in _graphiti_warm_state, f"missing key: {key}"

    # And the warmup body still populates them from the resolved config.
    server_http = importlib.import_module("watercooler_mcp.server_http")
    source = open(server_http.__file__).read()
    assert '_graphiti_warm_state["host"] = config.falkordb_host' in source
    assert '_graphiti_warm_state["database"] = config.database' in source


def test_render_graphiti_warmup_line_includes_topology_when_set():
    """When host/database are set, the rendered line includes db=… @ host:port."""
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "ready",
        "duration_ms": 234,
        "error": None,
        "host": "falkordb.railway.internal",
        "port": 6379,
        "database": "mostlyharmless_ai_watercooler_cloud_t2",
    })

    assert "Graphiti Warmup:" in line
    assert "ready" in line
    assert "(234ms)" in line
    assert "db=mostlyharmless_ai_watercooler_cloud_t2" in line
    assert "@ falkordb.railway.internal:6379" in line
    assert "—" not in line  # no error → no "— err" suffix


def test_render_graphiti_warmup_line_omits_topology_when_unset():
    """Disabled / pre-warmup states omit the db=… branch entirely."""
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "disabled",
        "duration_ms": 0,
        "error": None,
        "host": None,
        "port": None,
        "database": None,
    })

    assert "disabled" in line
    assert "db=" not in line
    assert "@" not in line


def test_render_graphiti_warmup_line_appends_error_suffix():
    """When error is set, the suffix '— <err>' appears after topology."""
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "failed",
        "duration_ms": 87,
        "error": "Connection refused",
        "host": "falkordb.railway.internal",
        "port": 6379,
        "database": "test_db_t2",
    })

    assert "failed" in line
    assert "— Connection refused" in line
    assert "db=test_db_t2 @ falkordb.railway.internal:6379" in line


def test_render_graphiti_warmup_line_skipped_includes_reason():
    """Hosted multi-tenant skip surfaces the reason instead of a misleading 'failed'.

    Issue #734 fix: PR #660's startup warmup probe set ``state: failed``
    in hosted mode because no ``http_ctx`` is set at startup. The new
    behaviour explicitly initialises the state to ``skipped`` with a
    human-readable ``reason`` so operators reading ``/health`` see the
    real cause and don't think the backend is broken.
    """
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "skipped",
        "duration_ms": 0,
        "error": None,
        "reason": "multi-tenant scope-bound; warmup deferred to first per-scope request",
        "host": None,
        "port": None,
        "database": None,
    })

    assert "skipped" in line
    assert "— multi-tenant scope-bound" in line
    # Topology block omitted because host/database aren't set.
    assert "db=" not in line
    assert "@" not in line


def test_render_graphiti_warmup_line_failed_prefers_raw_error_over_reason():
    """When both reason and error are set, the raw error wins on /health.

    Review #737 round 1 LOW: the prior implementation preferred the
    curated ``reason`` string and silently dropped the raw exception
    when both were present. That hides the diagnostic signal an operator
    needs during an outage. The renderer now prefers ``error`` and falls
    back to ``reason`` (so ``"skipped"`` — which has no error — still
    surfaces its multi-tenant explanation).
    """
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "failed",
        "duration_ms": 12,
        "error": "ConnectionError: redis timed out",
        "reason": "load_graphiti_config returned None",
        "host": None,
        "port": None,
        "database": None,
    })

    assert "failed" in line
    assert "— ConnectionError: redis timed out" in line
    # Curated reason hidden when raw error is more informative.
    assert "load_graphiti_config returned None" not in line


def test_render_graphiti_warmup_line_skipped_falls_back_to_reason():
    """For 'skipped' there is no error; the renderer falls back to reason.

    Companion to the ``failed``-prefers-error test: the new precedence
    must NOT regress the hosted-skipped path, which only has ``reason``
    populated.
    """
    from watercooler_mcp.tools.diagnostic import _render_graphiti_warmup_line

    line = _render_graphiti_warmup_line({
        "state": "skipped",
        "duration_ms": 0,
        "error": None,
        "reason": "multi-tenant scope-bound; warmup deferred to first per-scope request",
        "host": None,
        "port": None,
        "database": None,
    })

    assert "skipped" in line
    assert "— multi-tenant scope-bound" in line


def test_health_endpoint_redacts_falkordb_topology_and_error():
    """/health exposes only state/duration_ms/has_error, not topology or err string.

    Defense against PR #670 round 2 (host/port/database leak) and round 3
    (error strings embed host:port via redis/socket exception messages).
    The /health endpoint is unauthenticated; only the auth-gated MCP
    diagnostic tool may surface infrastructure topology or raw error
    strings.
    """
    from watercooler_mcp import server_http as sh_mod
    source = open(sh_mod.__file__).read()
    # The redaction shape must include exactly the safe public keys.
    assert '"state": _graphiti_warm_state.get("state")' in source
    assert '"duration_ms": _graphiti_warm_state.get("duration_ms")' in source
    assert '"has_error": _graphiti_warm_state.get("error") is not None' in source
