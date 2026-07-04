"""Synthetic T2 (Graphiti) channel liveness probe.

Actively exercises the remote T2 path the way an interactive caller would — a
cheap ``facts`` query through the live ``premium_client`` — and classifies the
result against a latency SLA. In hybrid mode it ALSO runs the same query through
a *fresh* ``PremiumToolClient`` and compares, so it can tell a **wedged
long-lived client** apart from a genuine **Railway-side T2 outage**.

Why this exists: the long-lived hybrid server's shared ``premium_client`` could
wedge such that every remote T2 call hangs ~50s while a fresh client returns in
~1s — and it stayed undetected for 11 days because ``/health`` only checks app
liveness, never the T2 channel (thread
``list-decisions-supersession-hosted-premium-unmounted``). This probe is the
detector: it reproduces Jay's "fresh-vs-long-lived" discriminator on a schedule
or on demand.

The probe is read-only (a ``limit=1`` facts query) and bounded by a per-check
timeout well under the ~50s middleware ceiling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Verdict tiers.
GREEN = "green"
AMBER = "amber"
RED = "red"

# Channel diagnoses — the actionable "which side to fix" signal.
CH_HEALTHY = "healthy"
CH_CLIENT_WEDGED = "client_wedged"
CH_BACKEND_UNAVAILABLE = "backend_unavailable"
CH_AUTH_ERROR = "auth_error"
CH_NOT_APPLICABLE = "not_applicable"

# Defaults. amber_ms: a healthy T2 facts read returns in ~1s; flag slowness
# above this. timeout_s: bound each canary well under the ~50s middleware
# ceiling so a wedged channel is detected as a fast-failing probe, not a hang.
DEFAULT_AMBER_MS = 3000.0
DEFAULT_TIMEOUT_S = 8.0

_PROBE_QUERY = "watercooler health probe"


class _T2BackendUnavailable(Exception):
    """A configured in-process T2 backend exists but could not be acquired.

    Distinguishes "no T2 on this surface" (→ ``not_applicable`` / green) from
    "T2 is configured but the backend will not initialize" — the latter is the
    same condition real ``watercooler_search`` raises ``Graphiti backend
    unavailable`` on, so the probe must surface it red, never a false green.
    """


def _looks_like_auth_error(reason: str) -> bool:
    r = reason.lower()
    return "401" in r or "403" in r or "unauthor" in r or "forbidden" in r


def _classify_call_result(text: str) -> tuple[bool, str]:
    """Interpret a ``call_tool_text`` payload as (ok, reason).

    ``call_tool_text`` never raises — on a remote failure it returns a JSON
    string carrying an ``error`` key (and often ``remote_error`` /
    ``status_code``). A successful facts query returns a JSON payload with
    ``count`` / ``results`` (or any non-error shape).
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Non-JSON text is an unexpected-but-non-error payload — treat as ok.
        return True, "ok (non-json payload)"
    if isinstance(parsed, dict) and parsed.get("error"):
        detail = parsed.get("remote_error") or parsed.get("message") or parsed["error"]
        status = parsed.get("status_code")
        reason = f"{parsed['error']}: {detail}" + (f" (status {status})" if status else "")
        return False, reason
    return True, "ok"


async def _run_canary(client: Any, code_path: str, timeout_s: float) -> dict:
    """Run one bounded facts-query canary through *client*.

    Returns ``{ok, latency_ms, reason}``. A timeout (the wedge/hang signature)
    is the dominant failure mode we care about and is reported distinctly.
    """
    start = time.monotonic()
    try:
        text = await asyncio.wait_for(
            client.call_tool_text(
                "watercooler_search",
                {
                    "mode": "facts",
                    "limit": 1,
                    "query": _PROBE_QUERY,
                    "code_path": code_path,
                },
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        latency_ms = (time.monotonic() - start) * 1000.0
        return {
            "ok": False,
            "latency_ms": round(latency_ms, 1),
            "reason": f"timeout after {timeout_s:.1f}s (channel did not respond)",
        }
    except Exception as exc:  # defensive: call_tool_text shouldn't raise
        latency_ms = (time.monotonic() - start) * 1000.0
        return {
            "ok": False,
            "latency_ms": round(latency_ms, 1),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    latency_ms = (time.monotonic() - start) * 1000.0
    ok, reason = _classify_call_result(text)
    return {"ok": ok, "latency_ms": round(latency_ms, 1), "reason": reason}


def _build_fresh_client(boot_cwd: Optional[Path]) -> Any:
    """Build a brand-new ``PremiumToolClient`` from current transport config.

    This is the "fresh client" half of the discriminator — it shares no session
    state with the long-lived ``runtime.premium_client``, so if it succeeds
    while the long-lived one hangs, the long-lived client is wedged.
    """
    from .config import get_mcp_transport_config
    from .premium_client import PremiumToolClient

    transport_config = get_mcp_transport_config()
    return PremiumToolClient.from_transport_config(
        transport_config, boot_cwd=boot_cwd or Path.cwd()
    )


def _acquire_t2_backend(code_path: str):
    """Resiliently acquire the in-process T2 (Graphiti) backend, or ``None``.

    Returns the **same cached, warmed ``GraphitiBackend`` singleton the real
    search path uses** (``tools.graph._get_or_create_graphiti_backend``, keyed
    by host:port:database), not a fresh instance. This is what makes the probe
    faithful: the probe must exercise the exact object real ``watercooler_search``
    traffic holds, so a green probe means real search works and a red probe means
    it is genuinely broken (#1046). Constructing a fresh ``GraphitiBackend`` per
    probe — as the old path did via ``mem.get_graphiti_backend`` — diverged from
    the real path (false ``backend_unavailable`` while tenant search was healthy)
    and leaked a search-loop thread + FalkorDB connection on every probe.

    Returns ``None`` only when **no T2 is configured on this surface** — no
    Graphiti config resolves (open-core baseline / hosted request without scope)
    — which the probe reports as ``not_applicable`` (green), never a false
    outage. When a config *does* resolve but the backend cannot be acquired
    (``_get_or_create_graphiti_backend`` raises), that is a real configured-T2
    outage — the same condition real ``watercooler_search`` raises ``Graphiti
    backend unavailable`` on — so it is re-raised as ``_T2BackendUnavailable``
    for the caller to surface red rather than swallowed to a green
    ``not_applicable`` (#1046 review).
    """
    from . import memory as mem

    try:
        config = mem.load_graphiti_config(code_path=code_path or None)
    except Exception:
        # Config resolution itself failed (or the open-core config layer is
        # absent) — treat as "no T2 here", never a false outage.
        return None
    if not config:
        return None

    from .tools.graph import _get_or_create_graphiti_backend

    try:
        return _get_or_create_graphiti_backend(config)
    except Exception as exc:
        # Config exists but the backend will not initialize — a real outage.
        raise _T2BackendUnavailable(str(exc)) from exc


async def _probe_inprocess_backend(
    *, code_path: str, timeout_s: float, amber_ms: float, surface: str
) -> dict:
    """Probe the in-process T2 backend (hosted / self-hosted local_full).

    Used when there is no remote ``premium_client`` — i.e. the surface *is* the
    backend (hosted Railway) or a self-hosted T2. Runs the **same
    ``search_memory_facts`` call the real tool path issues** (cached warmed
    backend, explicit facts-query kwargs — see ``tools.graph._search_graphiti_impl``)
    so the probe is a faithful reproduction of live search rather than a
    divergent code path (#1046). Returns ``not_applicable`` when T2 is not
    configured on this surface (e.g. open-core baseline-only) — never a false
    "down" — but a *configured* backend that will not initialize is surfaced red
    (``backend_unavailable``), mirroring what real search would raise.
    """
    try:
        backend = _acquire_t2_backend(code_path)
    except _T2BackendUnavailable as exc:
        # A configured T2 backend that will not initialize — the same condition
        # real ``watercooler_search`` raises on. Surface red, not a false green.
        reason = f"backend initialization failed: {exc}"
        return {
            "verdict": RED,
            "channel_diagnosis": (
                CH_AUTH_ERROR if _looks_like_auth_error(reason) else CH_BACKEND_UNAVAILABLE
            ),
            "surface": surface,
            "checks": {"backend": {"ok": False, "latency_ms": None, "reason": reason}},
            "thresholds": {"amber_ms": amber_ms, "timeout_s": timeout_s},
        }
    if backend is None:
        return {
            "verdict": GREEN,
            "channel_diagnosis": CH_NOT_APPLICABLE,
            "surface": surface,
            "checks": {},
            "thresholds": {"amber_ms": amber_ms, "timeout_s": timeout_s},
            "note": "No remote channel and no in-process T2 backend configured "
            "on this surface (baseline-only / T2 unavailable).",
        }

    # Mirror the exact facts-query signature the tool path uses
    # (tools.graph._search_graphiti_impl) so the probe cannot diverge from real
    # search on parameter defaults. All temporal filters empty / active_only
    # False → no SearchFilters, same Cypher a real facts read runs.
    def _probe_search():
        return backend.search_memory_facts(
            query=_PROBE_QUERY,
            max_facts=1,
            start_time="",
            end_time="",
            active_only=False,
            superseded_start="",
            superseded_end="",
        )

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_probe_search),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        latency_ms = (time.monotonic() - start) * 1000.0
        check = {
            "ok": False,
            "latency_ms": round(latency_ms, 1),
            "reason": f"timeout after {timeout_s:.1f}s (backend did not respond)",
        }
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000.0
        check = {
            "ok": False,
            "latency_ms": round(latency_ms, 1),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    else:
        latency_ms = (time.monotonic() - start) * 1000.0
        check = {"ok": True, "latency_ms": round(latency_ms, 1), "reason": "ok"}

    if check["ok"]:
        verdict = GREEN if check["latency_ms"] <= amber_ms else AMBER
        channel = CH_HEALTHY
    else:
        verdict = RED
        channel = (
            CH_AUTH_ERROR if _looks_like_auth_error(check["reason"]) else CH_BACKEND_UNAVAILABLE
        )

    return {
        "verdict": verdict,
        "channel_diagnosis": channel,
        "surface": surface,
        "checks": {"backend": check},
        "thresholds": {"amber_ms": amber_ms, "timeout_s": timeout_s},
    }


async def probe_t2_channel(
    runtime: Any,
    *,
    code_path: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    amber_ms: float = DEFAULT_AMBER_MS,
    boot_cwd: Optional[Path] = None,
    compare_fresh: bool = True,
) -> dict:
    """Probe the remote T2 channel and classify health.

    Args:
        runtime: ToolRuntime; ``runtime.premium_client`` is the long-lived
            client under test (None on non-hybrid surfaces).
        code_path: Repo root for the facts query context.
        timeout_s: Per-canary timeout (bound under the middleware ceiling).
        amber_ms: Latency above which a working channel is flagged ``amber``.
        boot_cwd: cwd for fresh-client header resolution (defaults to cwd()).
        compare_fresh: Run the fresh-client comparison to discriminate a wedged
            client from a backend outage. Disable to probe only the live path.

    Returns:
        ``{verdict, channel_diagnosis, checks: {...}, thresholds, surface}``.
    """
    surface = getattr(runtime, "surface", "unknown")
    premium_client = getattr(runtime, "premium_client", None)

    if premium_client is None:
        # No remote channel on this surface (hosted_* / local_full). The surface
        # IS the backend here — probe the in-process T2 backend directly. This is
        # what makes watercooler_health_probe meaningful on the hosted service
        # (the dashboard's T2 card); not_applicable where T2 isn't configured.
        return await _probe_inprocess_backend(
            code_path=code_path,
            timeout_s=timeout_s,
            amber_ms=amber_ms,
            surface=surface,
        )

    long_lived = await _run_canary(premium_client, code_path, timeout_s)

    fresh: Optional[dict] = None
    if compare_fresh:
        try:
            fresh_client = _build_fresh_client(boot_cwd)
            fresh = await _run_canary(fresh_client, code_path, timeout_s)
        except Exception as exc:
            fresh = {"ok": False, "latency_ms": None, "reason": f"fresh-client build failed: {exc}"}

    # Verdict is driven by the LIVE (long-lived) path — that's what real calls use.
    if long_lived["ok"]:
        verdict = GREEN if long_lived["latency_ms"] <= amber_ms else AMBER
    else:
        verdict = RED

    # Channel diagnosis — the "which side to fix" signal.
    if long_lived["ok"]:
        channel = CH_HEALTHY
    elif _looks_like_auth_error(long_lived["reason"]):
        channel = CH_AUTH_ERROR
    elif fresh is not None and fresh["ok"]:
        # Live path broken, fresh client fine → the long-lived client is wedged.
        channel = CH_CLIENT_WEDGED
    elif fresh is not None and not fresh["ok"]:
        channel = (
            CH_AUTH_ERROR if _looks_like_auth_error(fresh["reason"]) else CH_BACKEND_UNAVAILABLE
        )
    else:
        # No fresh comparison available; can't attribute the side.
        channel = CH_BACKEND_UNAVAILABLE

    checks: dict[str, Any] = {"long_lived": long_lived}
    if fresh is not None:
        checks["fresh"] = fresh

    return {
        "verdict": verdict,
        "channel_diagnosis": channel,
        "surface": surface,
        "checks": checks,
        "thresholds": {"amber_ms": amber_ms, "timeout_s": timeout_s},
    }


async def probe_t2_backend(
    *,
    code_path: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    amber_ms: float = DEFAULT_AMBER_MS,
    boot_cwd: Optional[Path] = None,
) -> dict:
    """Probe T2 reachability with a FRESH client — safe to call from a scheduler.

    A scheduler (the ``t2_health_probe`` daemon) runs in its own thread with a
    per-tick event loop. Reusing the main server's long-lived ``premium_client``
    across event loops is unsafe — its session task is bound to the server's
    loop — so this builds a fresh client on the current loop instead. It detects
    backend / auth / total-channel failures autonomously; the in-loop
    long-lived *client wedge* is diagnosed by the ``watercooler_health_probe``
    tool, which runs on the server's own loop where the long-lived client lives.
    """
    from types import SimpleNamespace

    client = _build_fresh_client(boot_cwd)
    rt = SimpleNamespace(surface="local_hybrid", premium_client=client)
    return await probe_t2_channel(
        rt,
        code_path=code_path,
        timeout_s=timeout_s,
        amber_ms=amber_ms,
        compare_fresh=False,
        boot_cwd=boot_cwd,
    )


def _format_alert(result: dict, *, code_path: str) -> dict:
    """Build a Slack webhook payload for an amber/red probe result."""
    verdict = result["verdict"]
    channel = result["channel_diagnosis"]
    emoji = {GREEN: ":large_green_circle:", AMBER: ":large_yellow_circle:", RED: ":red_circle:"}.get(
        verdict, ":grey_question:"
    )
    ll = result.get("checks", {}).get("long_lived", {})
    fr = result.get("checks", {}).get("fresh")
    lines = [
        f"{emoji} *T2 channel {verdict.upper()}* — `{channel}`",
        f"surface: `{result.get('surface')}`" + (f" · repo: `{code_path}`" if code_path else ""),
        f"long-lived: ok={ll.get('ok')} {ll.get('latency_ms')}ms — {ll.get('reason')}",
    ]
    if fr is not None:
        lines.append(f"fresh: ok={fr.get('ok')} {fr.get('latency_ms')}ms — {fr.get('reason')}")
    if channel == CH_CLIENT_WEDGED:
        lines.append(
            "→ long-lived premium_client is wedged (fresh client is fine). "
            "Restart the MCP server or apply the evict-on-timeout fix."
        )
    elif channel == CH_BACKEND_UNAVAILABLE:
        lines.append("→ Railway-side T2 not responding (fresh client also failed).")
    return {"text": "\n".join(lines)}


def maybe_alert(
    result: dict, *, code_path: str = "", min_verdict: str = AMBER, force: bool = False
) -> bool:
    """Send a Slack alert if *result* meets ``min_verdict`` (or ``force``).

    ``force=True`` dispatches regardless of verdict — used by the scheduler to
    announce recovery (green after a degraded state). Returns True if an alert
    was dispatched. Fire-and-forget; failures are swallowed (a probe must never
    raise because alerting is unavailable).
    """
    order = {GREEN: 0, AMBER: 1, RED: 2}
    if not force and order.get(result.get("verdict", GREEN), 0) < order.get(min_verdict, 1):
        return False
    try:
        from .config import get_slack_config
        from .slack.notify import send_webhook

        webhook_url = get_slack_config().get("webhook_url", "")
        if not webhook_url:
            return False
        return send_webhook(webhook_url, _format_alert(result, code_path=code_path))
    except Exception as exc:
        logger.warning("health_probe: alert dispatch failed: %s", exc)
        return False
