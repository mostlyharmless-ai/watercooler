"""Unit tests for the synthetic T2 channel liveness probe.

Covers the verdict tiers and — the point of the probe — the fresh-vs-long-lived
discriminator that tells a wedged client apart from a backend outage.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from watercooler_mcp import health_probe as hp


class _FakeClient:
    def __init__(self, *, payload: str = '{"count": 0, "results": []}', delay: float = 0.0):
        self._payload = payload
        self._delay = delay

    async def call_tool_text(self, name, arguments):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._payload


class _RT:
    def __init__(self, client, surface="local_hybrid"):
        self.surface = surface
        self.premium_client = client


def _probe(rt, **kw):
    kw.setdefault("compare_fresh", False)
    return asyncio.run(hp.probe_t2_channel(rt, **kw))


def test_green_when_live_path_fast():
    rt = _RT(_FakeClient())
    result = _probe(rt, amber_ms=5000.0)
    assert result["verdict"] == hp.GREEN
    assert result["channel_diagnosis"] == hp.CH_HEALTHY


def test_amber_when_live_path_slow():
    # ~20ms call against a 1ms amber threshold → working but "slow".
    rt = _RT(_FakeClient(delay=0.02))
    result = _probe(rt, amber_ms=1.0)
    assert result["verdict"] == hp.AMBER
    assert result["channel_diagnosis"] == hp.CH_HEALTHY


def test_red_client_wedged_when_live_hangs_but_fresh_ok(monkeypatch):
    monkeypatch.setattr(hp, "_build_fresh_client", lambda boot_cwd: _FakeClient())
    rt = _RT(_FakeClient(delay=10.0))  # long-lived hangs
    result = asyncio.run(
        hp.probe_t2_channel(rt, timeout_s=0.1, compare_fresh=True)
    )
    assert result["verdict"] == hp.RED
    assert result["channel_diagnosis"] == hp.CH_CLIENT_WEDGED
    assert result["checks"]["long_lived"]["ok"] is False
    assert result["checks"]["fresh"]["ok"] is True


def test_red_backend_unavailable_when_both_fail(monkeypatch):
    monkeypatch.setattr(hp, "_build_fresh_client", lambda boot_cwd: _FakeClient(delay=10.0))
    rt = _RT(_FakeClient(delay=10.0))
    result = asyncio.run(
        hp.probe_t2_channel(rt, timeout_s=0.1, compare_fresh=True)
    )
    assert result["verdict"] == hp.RED
    assert result["channel_diagnosis"] == hp.CH_BACKEND_UNAVAILABLE


def test_auth_error_classification():
    err = json.dumps({"error": "remote_call_failed", "status_code": 403, "message": "Forbidden"})
    rt = _RT(_FakeClient(payload=err))
    result = _probe(rt)
    assert result["verdict"] == hp.RED
    assert result["channel_diagnosis"] == hp.CH_AUTH_ERROR


def test_error_payload_is_not_ok():
    err = json.dumps({"error": "remote_call_failed", "message": "boom"})
    rt = _RT(_FakeClient(payload=err))
    result = _probe(rt)
    assert result["verdict"] == hp.RED
    assert result["checks"]["long_lived"]["ok"] is False


class _FakeBackend:
    def __init__(self, *, raises=None):
        self._raises = raises
        self.calls = []

    def search_memory_facts(
        self,
        query,
        max_facts=1,
        start_time="",
        end_time="",
        active_only=False,
        superseded_start="",
        superseded_end="",
    ):
        # Record the call so tests can assert the probe issues the same
        # explicit-kwarg facts query the real tool path uses (#1046).
        self.calls.append(
            {
                "query": query,
                "max_facts": max_facts,
                "start_time": start_time,
                "end_time": end_time,
                "active_only": active_only,
                "superseded_start": superseded_start,
                "superseded_end": superseded_end,
            }
        )
        if self._raises:
            raise self._raises
        return ([], {})


def test_not_applicable_when_no_channel_and_no_backend(monkeypatch):
    # No premium client AND no in-process T2 backend (open-core / baseline-only).
    monkeypatch.setattr(hp, "_acquire_t2_backend", lambda code_path: None)
    rt = _RT(None, surface="local_full")
    result = asyncio.run(hp.probe_t2_channel(rt))
    assert result["channel_diagnosis"] == hp.CH_NOT_APPLICABLE
    assert result["verdict"] == hp.GREEN


def test_inprocess_backend_healthy(monkeypatch):
    # Hosted surface: no premium client, but the in-process T2 backend answers.
    monkeypatch.setattr(hp, "_acquire_t2_backend", lambda code_path: _FakeBackend())
    rt = _RT(None, surface="hosted_full")
    result = asyncio.run(hp.probe_t2_channel(rt, amber_ms=5000.0))
    assert result["verdict"] == hp.GREEN
    assert result["channel_diagnosis"] == hp.CH_HEALTHY
    assert result["checks"]["backend"]["ok"] is True


def test_inprocess_probe_uses_faithful_facts_query(monkeypatch):
    # Regression for #1046: the probe must issue the same explicit-kwarg facts
    # query the real tool path uses, so it cannot diverge from live search.
    backend = _FakeBackend()
    monkeypatch.setattr(hp, "_acquire_t2_backend", lambda code_path: backend)
    rt = _RT(None, surface="hosted_full")
    asyncio.run(hp.probe_t2_channel(rt, amber_ms=5000.0))
    assert backend.calls == [
        {
            "query": hp._PROBE_QUERY,
            "max_facts": 1,
            "start_time": "",
            "end_time": "",
            "active_only": False,
            "superseded_start": "",
            "superseded_end": "",
        }
    ]


def test_acquire_t2_backend_uses_cached_singleton(monkeypatch):
    # #1046: the probe must reuse the warmed singleton the real search path
    # holds (tools.graph._get_or_create_graphiti_backend), not construct a fresh
    # per-probe backend (which diverged from live search and leaked a
    # search-loop thread + FalkorDB connection each probe).
    import watercooler_mcp.memory as mem
    import watercooler_mcp.tools.graph as graph

    sentinel = object()
    seen = {}

    monkeypatch.setattr(mem, "load_graphiti_config", lambda code_path=None: {"cfg": True})

    def _fake_cached(config):
        seen["config"] = config
        return sentinel

    monkeypatch.setattr(graph, "_get_or_create_graphiti_backend", _fake_cached)

    assert hp._acquire_t2_backend("/repo") is sentinel
    assert seen["config"] == {"cfg": True}


def test_acquire_t2_backend_none_when_no_config(monkeypatch):
    # No Graphiti config resolves (open-core baseline / hosted no-scope) → None
    # → not_applicable. This is the ONLY green-degradation path.
    import watercooler_mcp.memory as mem

    monkeypatch.setattr(mem, "load_graphiti_config", lambda code_path=None: None)
    assert hp._acquire_t2_backend("/repo") is None


def test_acquire_t2_backend_raises_on_configured_init_failure(monkeypatch):
    # #1046 review (P1): a config exists but the backend won't initialize — this
    # is a real configured-T2 outage (real search raises "Graphiti backend
    # unavailable"), so the probe must NOT collapse it to a green not_applicable.
    import watercooler_mcp.memory as mem
    import watercooler_mcp.tools.graph as graph

    monkeypatch.setattr(mem, "load_graphiti_config", lambda code_path=None: {"cfg": True})

    def _boom(config):
        raise RuntimeError("GraphitiBackend initialization failed")

    monkeypatch.setattr(graph, "_get_or_create_graphiti_backend", _boom)

    with pytest.raises(hp._T2BackendUnavailable):
        hp._acquire_t2_backend("/repo")


def test_inprocess_configured_init_failure_is_red_backend_unavailable(monkeypatch):
    # End-to-end: a configured backend that will not initialize surfaces as red
    # backend_unavailable, not a false green not_applicable (#1046 review P1).
    def _raise(code_path):
        raise hp._T2BackendUnavailable("falkor refused connection")

    monkeypatch.setattr(hp, "_acquire_t2_backend", _raise)
    rt = _RT(None, surface="hosted_full")
    result = asyncio.run(hp.probe_t2_channel(rt))
    assert result["verdict"] == hp.RED
    assert result["channel_diagnosis"] == hp.CH_BACKEND_UNAVAILABLE
    assert result["checks"]["backend"]["ok"] is False


def test_inprocess_backend_error_is_backend_unavailable(monkeypatch):
    monkeypatch.setattr(
        hp, "_acquire_t2_backend", lambda code_path: _FakeBackend(raises=RuntimeError("falkor down"))
    )
    rt = _RT(None, surface="hosted_full")
    result = asyncio.run(hp.probe_t2_channel(rt))
    assert result["verdict"] == hp.RED
    assert result["channel_diagnosis"] == hp.CH_BACKEND_UNAVAILABLE
    assert result["checks"]["backend"]["ok"] is False


def test_maybe_alert_respects_min_verdict(monkeypatch):
    sent = {}

    import watercooler_mcp.config as cfg
    import watercooler_mcp.slack.notify as notify

    def _fake_send(url, payload, **kw):
        sent["p"] = payload
        return True

    monkeypatch.setattr(cfg, "get_slack_config", lambda: {"webhook_url": "https://hook"})
    monkeypatch.setattr(notify, "send_webhook", _fake_send)

    assert hp.maybe_alert({"verdict": hp.GREEN}, min_verdict=hp.AMBER) is False
    assert "p" not in sent

    red = {"verdict": hp.RED, "channel_diagnosis": hp.CH_CLIENT_WEDGED, "surface": "local_hybrid", "checks": {"long_lived": {"ok": False, "latency_ms": 100, "reason": "timeout"}}}
    assert hp.maybe_alert(red, min_verdict=hp.AMBER) is True
    assert "T2 channel RED" in sent["p"]["text"]


def test_maybe_alert_no_webhook(monkeypatch):
    import watercooler_mcp.config as cfg

    monkeypatch.setattr(cfg, "get_slack_config", lambda: {"webhook_url": ""})
    red = {"verdict": hp.RED, "channel_diagnosis": hp.CH_BACKEND_UNAVAILABLE, "surface": "x", "checks": {}}
    assert hp.maybe_alert(red) is False


def test_alert_arg_escalates_authority():
    """alert=True dispatches a Slack webhook (a side effect) so it must NOT ride
    read-only L1 authority. The probe-only path stays L1. Regression guard for
    the #994 review finding."""
    from watercooler_mcp.capabilities import tool_authority

    assert tool_authority("watercooler_health_probe") == "L1"
    assert tool_authority("watercooler_health_probe", {"alert": False}) == "L1"
    assert tool_authority("watercooler_health_probe", {"alert": True}) == "L2"
