"""Daemon management MCP tools.

Tools:
- watercooler_daemon_status: View daemon health and status
- watercooler_daemon_findings: Query daemon findings with filters
- watercooler_pulse_snapshot: Read the cached Project Pulse snapshot
- watercooler_acknowledge_finding: Mark a daemon finding as acknowledged
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Context

log = logging.getLogger(__name__)

# Module-level references to registered tools (populated by register_daemon_tools)
daemon_status = None
daemon_findings = None
pulse_snapshot_tool = None


def _authority_labels(runtime: Any) -> dict[str, str]:
    """Return truthful ``authority_scope`` + ``execution_mode`` labels.

    Plan v20 Phase 2 — labels the actual runtime ownership so operators
    are not misled about what ``watercooler_daemon_status`` /
    ``watercooler_daemon_findings`` are reporting in each supported
    hybrid configuration. Does not change daemon placement; only
    describes it.

    Three runtime-observable cases:

    - ``HostedDaemonCoordinator``:
      ``{"authority_scope": "hosted_premium_daemons", "execution_mode": "hosted"}``.
      This is the default hybrid routing model (and all hosted/proxy surfaces).

    - Local ``DaemonManager`` in ``stdio`` / ``local_full``:
      ``{"authority_scope": "local_daemons", "execution_mode": "local"}``.

    - Local ``DaemonManager`` in ``local_hybrid``:
      ``{"authority_scope": "local_daemons_hybrid_override", "execution_mode": "local"}``.
      Reached when either the operator set ``[mcp.capability_routes]
      daemon_observe = "local"`` or a premium daemon is pinned
      ``[mcp.daemons.<name>] route = "local"`` (the latter suppresses
      the proxy daemon-tool mount per the comment at
      ``server_factory.py:424-436``). In this case the tool reports
      the local daemon surface; hosted daemons are intentionally NOT
      surfaced. Merging both views under one tool is explicitly out
      of scope — see that code comment for rationale.
    """
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    if isinstance(runtime, HostedDaemonCoordinator):
        return {
            "authority_scope": "hosted_premium_daemons",
            "execution_mode": "hosted",
        }

    # Local DaemonManager branch — distinguish plain local from hybrid
    # override. PR #654 in-PR review round 5 (MEDIUM §4): the prior form
    # read the STATIC config transport key, which is wrong when a
    # hybrid-configured server starts but ``premium_client`` fails to
    # initialize (static config still says "hybrid" but the live runtime
    # surface is effectively local_full). Prefer the live runtime
    # surface as observed by :mod:`memory_sync` — the same source
    # server_factory.build_mcp_server uses to set
    # ``_HYBRID_T2_HANDOFF_ACTIVE`` — and only fall back to the static
    # config when the runtime hasn't been installed (e.g., in tests that
    # only construct a DaemonManager).
    effective_surface: str | None = None
    try:
        from ..memory_sync import get_runtime as _get_sync_runtime
        sync_runtime = _get_sync_runtime()
        if sync_runtime is not None:
            effective_surface = getattr(sync_runtime, "surface", None)
    except Exception:
        effective_surface = None

    if effective_surface is None:
        try:
            from ..config import get_watercooler_config
            transport = get_watercooler_config().mcp.transport
        except Exception:
            transport = "unknown"
        hybrid = transport == "hybrid"
    else:
        hybrid = effective_surface == "local_hybrid"

    if hybrid:
        return {
            "authority_scope": "local_daemons_hybrid_override",
            "execution_mode": "local",
            "note": (
                "Local daemon tool surface selected under a hybrid "
                "exception (daemon_observe=local or a premium daemon "
                "pinned route=local). Hosted daemons are intentionally "
                "not surfaced here; merging both views is a deferred "
                "refactor (server_factory.py:424-436)."
            ),
        }

    return {
        "authority_scope": "local_daemons",
        "execution_mode": "local",
    }


def _attach_authority(payload: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """Merge ``_authority_labels(runtime)`` into ``payload`` at top level.

    Safe against key collisions with real daemon names, because
    ``authority_scope`` / ``execution_mode`` / ``note`` are namespaced
    and not valid daemon identifiers.
    """
    labels = _authority_labels(runtime)
    # Preserve any existing fields; authority fields take precedence only
    # for their exact keys (which shouldn't collide with daemon names).
    for k, v in labels.items():
        payload[k] = v
    return payload


def _sanitize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Strip absolute paths and add enrichment_status before returning to MCP clients.

    - Removes ``latest_report_path`` (absolute filesystem path) and replaces
      it with ``report_found: bool``.
    - Adds ``enrichment_status`` to help consumers distinguish enrichment
      states without key-existence checks.
    - Removes internal ``_llm_configured`` flag (not for consumers).
    """
    s = copy.deepcopy(snapshot)
    a = s.get("analysis", {})
    a["report_found"] = a.get("latest_report_path") is not None
    a.pop("latest_report_path", None)
    if "llm_enrichment" in s:
        s["enrichment_status"] = "available"
    elif "llm_enrichment_error" in s:
        s["enrichment_status"] = "error"
    elif s.get("_llm_configured"):
        s["enrichment_status"] = "pending"
    else:
        s["enrichment_status"] = "not_configured"
    s.pop("_llm_configured", None)
    return s


def _daemon_status_impl(
    ctx: Context,
    daemon: str = "",
    trigger: bool = False,
) -> str:
    """Check daemon status and health.

    Returns status, last run time, interval, and error/findings counts
    for all registered daemons (or a specific one).

    When ``trigger=True`` the named daemon (or ``t2_indexer`` when
    ``daemon`` is empty) is woken immediately so its next tick runs
    without waiting for the scheduled interval.  The wake is
    **asynchronous** — the tick completes in the background.  Call this
    tool again after a short wait to see updated
    ``last_tick_*`` metrics.  The response shape changes when
    ``trigger=True``: status is nested under a ``"daemons"`` key and a
    ``"triggered"`` boolean (plus optional ``"trigger_error"``) is added
    at the top level.

    Args:
        daemon: Optional daemon name. Empty returns all daemons.
        trigger: If True, wake the target daemon before returning status.
    """
    from ..daemons import get_daemon_runtime, ensure_hosted_scope_for_current_context
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    # Ensure daemon scope exists for hosted/premium callers
    ensure_hosted_scope_for_current_context(reason="daemon_status")

    runtime = get_daemon_runtime()
    if runtime is None:
        return json.dumps(
            {
                "status": "not_initialized",
                "message": "Daemon manager not initialized",
            },
            indent=2,
        )

    # Hosted coordinator: aggregate status across scopes
    if isinstance(runtime, HostedDaemonCoordinator):
        from ..context import get_effective_context
        from ..daemons.telemetry import get_telemetry

        eff_ctx = get_effective_context()
        scope_id = eff_ctx.scope_id if eff_ctx else None
        result = runtime.status(
            scope_id=scope_id,
            user_id=eff_ctx.user_id if eff_ctx and not scope_id else None,
        )
        telemetry = get_telemetry()
        if telemetry:
            result["service_telemetry"] = telemetry
        _attach_authority(result, runtime)
        return json.dumps(result, indent=2)

    # Local mode: use the DaemonManager
    manager = runtime

    # 1. Validate named daemon up front so d is never None downstream.
    d = None
    if daemon:
        d = manager.get_daemon(daemon)
        if d is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Daemon '{daemon}' not found",
                    "available": manager.daemon_names,
                },
                indent=2,
            )

    # 2. Handle trigger — runs after validation so d is safe to use.
    triggered = False
    trigger_error: str | None = None
    if trigger:
        target_name = daemon or "t2_indexer"
        wake_target = d if daemon else manager.get_daemon(target_name)
        if wake_target is not None:
            wake_target.wake()
            triggered = True
        else:
            trigger_error = f"daemon '{target_name}' not found"

    # 3. Build status payload (same logic as before).
    status_payload = d.status_summary() if d is not None else manager.status_all()

    # 3a. Attach all-daemons-view sibling fields. Three additions land
    #     here — they're conceptually independent so they're listed
    #     under one ``if d is None`` guard:
    #
    #     - ``registration_errors``: structured per-daemon registration
    #       failures (PR #789 / F1; mirrors hosted-side PR #755).
    #     - ``sibling_fleets``: other live MCP-server fleets on this
    #       machine, each watching a different repo (PR #791 / L1
    #       per-repo PID locks; cloud Design (local) entry
    #       ``01KR5RCWK0F0EM1YVKWRJPD239``).
    #     - ``repo_key``: this fleet's repo identity (PR #791 / L1).
    #
    #     Per-daemon queries return their own scope of data and don't
    #     get any of these fleet-wide additions.
    if d is None:
        if manager.registration_errors:
            status_payload["registration_errors"] = list(
                manager.registration_errors
            )

        from ..daemons import _list_sibling_fleets

        siblings = _list_sibling_fleets()
        if siblings:
            status_payload["sibling_fleets"] = siblings
        repo_key = getattr(manager, "repo_key", "")
        if repo_key:
            status_payload["repo_key"] = repo_key

    # 4. Attach service telemetry (call counts, tokens, cache stats).
    from ..daemons.telemetry import get_telemetry

    telemetry = get_telemetry()

    # 5. When trigger was requested, wrap with trigger metadata so
    #    "triggered"/"trigger_error" never collide with daemon-name keys.
    if trigger:
        result: dict = {"daemons": status_payload, "triggered": triggered}
        if trigger_error:
            result["trigger_error"] = trigger_error
        if telemetry:
            result["service_telemetry"] = telemetry
        _attach_authority(result, runtime)
        return json.dumps(result, indent=2)

    # Non-trigger path: return status_payload at top level (backward-compatible).
    # Telemetry added as a sibling key — safe because daemon names never collide.
    if telemetry:
        status_payload["service_telemetry"] = telemetry
    _attach_authority(status_payload, runtime)
    return json.dumps(status_payload, indent=2)


# Non-daemon producers whose findings the ordinary listing must surface
# (rereview #1131 P1). Each writes via daemons.state.append_findings under the
# caller's auth-derived namespace (hosted) or unscoped (local single-tenant).
_AUX_FINDING_SOURCES: tuple[str, ...] = ("blessed_projection",)


def _resolve_aux_scope(*, hosted_hint: bool = False):
    """Resolve ``(namespace, allow_unscoped)`` for aux-findings access.

    Rereview #1131 P1 (round 4): the ABSENCE of auth context is not proof of
    local mode — a hosted request whose middleware failed to install or
    retain the contextvars must fail closed, never fall into the global
    unscoped store. The caller is treated as positively local only when
    neither the per-request signal (``hosted_hint``, e.g. from an already
    resolved ``is_hosted_context``) nor the process runtime (a live
    ``HostedDaemonCoordinator``) says hosted. In hosted mode the scope MUST
    resolve: ``resolve_scope()`` raises ``ScopeResolutionError`` on missing
    or incomplete context, and callers convert that into their fail-closed
    behavior (skip the read / refuse the ack / log the lost record).
    """
    from ..auth.scope import resolve_scope, resolve_scope_or_off_hosted

    hosted = hosted_hint
    if not hosted:
        from ..daemons import get_hosted_coordinator

        hosted = get_hosted_coordinator() is not None
    if hosted:
        scope = resolve_scope()  # raises when context is absent OR incomplete
        return scope.namespace, False, scope
    # Positively local: still prefer a resolvable scope if one exists.
    scope = resolve_scope_or_off_hosted()
    if scope is not None:
        return scope.namespace, False, scope
    return "", True, None


def _aux_source_findings(
    *,
    daemon_filter: str | None,
    severity: str | None,
    category: str | None,
    topic: str | None,
    limit: int,
    unacknowledged_only: bool,
) -> list:
    """Findings from auxiliary (non-daemon) sources, caller-scoped.

    Included when no daemon filter is set, or when the filter names an aux
    source. Reads use the request's auth-derived namespace in hosted mode —
    a tenant can only ever read its own aux findings — and the local
    unscoped store otherwise (mirroring the write-side contract in
    ``_persist_blessed_repair_finding``).
    """
    if daemon_filter and daemon_filter not in _AUX_FINDING_SOURCES:
        return []
    names = (daemon_filter,) if daemon_filter else _AUX_FINDING_SOURCES

    # Rereview #1131 P1 (rounds 2+4): a hosted caller with broken OR absent
    # auth context FAILS CLOSED — it must never read the global findings
    # file. Only a positively local caller takes the unscoped store.
    from ..auth.scope import ScopeResolutionError

    try:
        namespace, allow_unscoped, _ = _resolve_aux_scope()
    except ScopeResolutionError:
        log.warning(
            "aux findings: hosted scope resolution failed; "
            "refusing unscoped read",
            exc_info=True,
        )
        return []

    from ..daemons.state import load_findings

    out: list = []
    for name in names:
        try:
            out.extend(
                load_findings(
                    name,
                    limit=limit,
                    severity=severity,
                    category=category,
                    topic=topic,
                    unacknowledged_only=unacknowledged_only,
                    namespace=namespace,
                    _allow_unscoped=allow_unscoped,
                )
            )
        except Exception:  # noqa: BLE001 — a broken aux store must not 500 the listing
            log.warning("aux finding source %r read failed", name, exc_info=True)
    return out


def _daemon_findings_impl(
    ctx: Context,
    daemon: str = "",
    severity: str = "",
    category: str = "",
    topic: str = "",
    limit: int = 50,
    unacknowledged_only: bool = False,
    enrich: bool = False,
    code_path: str = ".",
    action: str = "list",
    finding_id: str = "",
    finding_ids: list[str] | None = None,
) -> str:
    """Query daemon findings, or acknowledge them.

    With ``action="list"`` (default) returns findings in reverse chronological
    order (newest first). With ``action="acknowledge"`` marks one or more
    findings as acknowledged (folded-in ``watercooler_acknowledge_finding``) —
    ``daemon`` is the owning daemon and ``finding_id`` / ``finding_ids``
    select the findings. The acknowledge action is authority-gated
    (``daemon_control`` / L3); listing is an L1 ``daemon_observe`` read.

    Args:
        daemon: Filter by daemon name (empty = all daemons). For
            ``action="acknowledge"`` this is the daemon that owns the finding.
        severity: Filter by severity ("info", "warning", "error").
        category: Filter by category (e.g., "missing_status", "stale_thread").
        topic: Filter by thread topic.
        limit: Maximum findings to return (default 50).
        unacknowledged_only: Only return unacknowledged findings.
        enrich: When True, overlay S1/S2/S3 context onto coordinator_lead
            findings before returning (hygiene tags, decision candidates,
            pulse dimension scores).  Has no effect when no coordinator_lead
            findings are present in the result.  Defaults to False.
        code_path: Path to the code repository root (default: current
            directory).  Used to derive repo_key for S3 pulse-context
            enrichment.  Ignored when enrich=False.
        action: ``"list"`` (default) or ``"acknowledge"``.
        finding_id: A single finding ID to acknowledge (``action="acknowledge"``).
        finding_ids: A list of finding IDs to acknowledge in one call (bulk).
    """
    if str(action).strip().lower() == "acknowledge":
        return _acknowledge_finding_impl(
            ctx,
            daemon_name=daemon,
            finding_id=finding_id,
            finding_ids=finding_ids,
        )

    from ..daemons import get_daemon_runtime, ensure_hosted_scope_for_current_context
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    # Ensure daemon scope exists for hosted/premium callers
    ensure_hosted_scope_for_current_context(reason="daemon_findings")

    runtime = get_daemon_runtime()
    if runtime is None:
        return json.dumps(
            {
                "status": "not_initialized",
                "message": "Daemon manager not initialized",
                "findings": [],
            },
            indent=2,
        )

    # Clamp limit
    limit = max(1, min(limit, 500))

    try:
        if isinstance(runtime, HostedDaemonCoordinator):
            from ..context import get_effective_context

            eff_ctx = get_effective_context()
            scope_id = eff_ctx.scope_id if eff_ctx else None
            if not scope_id:
                # Rereview #1131 P1 (round 5): scope_id=None means "ALL
                # scopes" to the coordinator — a hosted caller whose auth
                # context is absent must fail closed, never aggregate other
                # tenants' registered-daemon findings.
                return json.dumps(
                    {
                        "status": "error",
                        "message": (
                            "Cannot list findings: hosted runtime without a "
                            "resolved caller scope (missing user identity)."
                        ),
                        "findings": [],
                    },
                    indent=2,
                )
            findings = runtime.get_findings(
                scope_id=scope_id,
                limit=limit,
                daemon=daemon or None,
                severity=severity or None,
                category=category or None,
                topic=topic or None,
                unacknowledged_only=unacknowledged_only,
            )
        else:
            findings = runtime.get_all_findings(
                limit=limit,
                daemon=daemon or None,
                severity=severity or None,
                category=category or None,
                topic=topic or None,
                unacknowledged_only=unacknowledged_only,
            )
    except Exception:
        log.warning("_daemon_findings_impl: findings load failed", exc_info=True)
        return json.dumps(
            {
                "status": "error",
                "message": "Failed to load findings. Check server logs for details.",
                "findings": [],
            },
            indent=2,
        )

    # Auxiliary (non-daemon) finding sources — rereview #1131 P1: the blessed
    # projection persists repair findings without being a registered daemon,
    # so the ordinary all-daemon listing must merge them in explicitly. Reads
    # use the CALLER's auth-derived namespace — no cross-tenant exposure.
    try:
        aux = _aux_source_findings(
            daemon_filter=daemon or None,
            severity=severity or None,
            category=category or None,
            topic=topic or None,
            limit=limit,
            unacknowledged_only=unacknowledged_only,
        )
        if aux:
            findings = list(findings) + aux
            findings.sort(key=lambda f: f.created_at, reverse=True)
            findings = findings[:limit]
    except Exception:  # noqa: BLE001 — aux sources must not break the listing
        log.warning("_daemon_findings_impl: aux-source read failed", exc_info=True)

    results = [f.to_dict() for f in findings]
    enrich_stats: dict[str, Any] | None = None

    _ENRICHABLE_CATEGORIES = {"coordinator_lead", "refined_coordinator_lead"}
    if enrich and any(r.get("category") in _ENRICHABLE_CATEGORIES for r in results):
        from ..config import _discover_git
        from watercooler.pulse_snapshot_lib import derive_repo_key
        from .coordinator_leads import enrich_leads

        # Resolve repo_key from the filesystem path (mirrors _pulse_snapshot_impl).
        # Guard against non-directory code_path before invoking git discovery.
        code_root = Path(code_path).resolve()
        if not code_root.is_dir():
            log.debug(
                "_daemon_findings_impl: code_path %r is not a directory; "
                "skipping enrichment",
                code_path,
            )
            _em = "hosted" if isinstance(runtime, HostedDaemonCoordinator) else "local"
            enrich_stats = {
                "attempted": 0,
                "succeeded": 0,
                "skipped": 3 if _em == "hosted" else 0,
                "mode": _em,
                "error": True,
            }
        else:
            git_info = _discover_git(code_root)
            if git_info.root:
                code_root = git_info.root
            rk = derive_repo_key(code_root)

            # Resolve namespace: use the in-process coordinator daemon's namespace
            # when available; fall back to scope_id derived from request context.
            # In hosted mode, a context resolution failure skips enrichment rather
            # than silently falling back to an empty namespace.
            namespace = ""
            enrich_ok = True
            if isinstance(runtime, HostedDaemonCoordinator):
                try:
                    from ..context import get_effective_context

                    eff_ctx = get_effective_context()
                    if eff_ctx and eff_ctx.user_id and eff_ctx.repo:
                        namespace = f"{eff_ctx.user_id}:{eff_ctx.repo}"
                    else:
                        log.warning(
                            "_daemon_findings_impl: hosted context missing "
                            "user_id/repo; skipping enrichment"
                        )
                        enrich_ok = False
                except Exception:
                    log.warning(
                        "_daemon_findings_impl: failed to resolve hosted context; "
                        "skipping enrichment",
                        exc_info=True,
                    )
                    enrich_ok = False

                if not enrich_ok:
                    enrich_stats = {
                        "attempted": 0,
                        "succeeded": 0,
                        "skipped": 3,
                        "mode": "hosted",
                        "error": True,
                    }
            else:
                coord = runtime.get_daemon("project_coordinator")
                if coord is not None:
                    namespace = getattr(coord, "state_namespace", "")

            enrich_mode = (
                "hosted" if isinstance(runtime, HostedDaemonCoordinator) else "local"
            )
            if enrich_ok:
                try:
                    results, enrich_stats = enrich_leads(
                        results,
                        namespace=namespace,
                        repo_key=rk,
                        runtime=runtime,
                    )
                except Exception:
                    # Enrichment failure is non-fatal; return raw results.
                    log.warning(
                        "_daemon_findings_impl: enrich_leads failed", exc_info=True
                    )
                    enrich_stats = {
                        "attempted": 0 if enrich_mode == "hosted" else 3,
                        "succeeded": 0,
                        "skipped": 3 if enrich_mode == "hosted" else 0,
                        "mode": enrich_mode,
                        "error": True,
                    }

    elif enrich:
        # enrich=True but no coordinator_lead findings — emit zero-count stats
        # so callers always receive enrichment_stats when they asked for enrichment.
        enrich_mode = (
            "hosted" if isinstance(runtime, HostedDaemonCoordinator) else "local"
        )
        enrich_stats = {
            "attempted": 0,
            "succeeded": 0,
            "skipped": 3 if enrich_mode == "hosted" else 0,
            "mode": enrich_mode,
        }

    response: dict[str, Any] = {
        "count": len(results),
        "findings": results,
    }
    if enrich and enrich_stats is not None:
        response["enrichment_stats"] = enrich_stats

    _attach_authority(response, runtime)
    return json.dumps(response, indent=2)


def _pulse_snapshot_impl(
    ctx: Context,
    code_path: str = ".",
) -> str:
    """Read the pulse snapshot daemon's cached state.

    Returns the most recent Project Pulse snapshot for the repo at code_path,
    or a status object if the daemon is disabled or has not yet run.

    The snapshot is computed by the PulseSnapshotDaemon in the background
    and does not trigger a fresh computation.

    **Fallback chain:** (1) in-process daemon (freshest), (2) checkpoint on disk
    (cross-process access — other Claude Code sessions can read without an active
    daemon in the current process).

    **Status/reason codes returned:**

    - ``{"status": "ok", "snapshot": {...}}`` — Fresh snapshot available.
      When sourced from the on-disk checkpoint, ``source: "checkpoint"`` and
      ``age_seconds`` (derived from ``snapshot.generated_at``) are also present.
    - ``{"status": "error", "reason": "invalid_code_path"}`` —
      ``code_path`` does not exist or is not a directory.
    - ``{"status": "unavailable", "reason": "disabled"}`` —
      ``[mcp.daemons.pulse_snapshot] enabled = false`` in config.
    - ``{"status": "unavailable", "reason": "no_snapshot"}`` —
      Feature enabled but no snapshot yet (daemon hasn't ticked, or
      ``code_path`` doesn't match the daemon's tracked repo).
    - ``{"status": "unavailable", "reason": "daemon_not_running"}`` —
      MCP server started without the daemon manager.

    **To trigger a fresh snapshot:**

    .. code-block::

        watercooler_daemon_status(daemon="pulse_snapshot", trigger=True)

    Then poll again after a short wait (not the full interval).

    Args:
        code_path: Path to the code repository root (default: current directory).
    """
    from ..daemons import get_daemon_manager, ensure_hosted_scope_for_current_context
    from ..daemons.state import load_checkpoint
    from ..config import _discover_git
    from watercooler.pulse_snapshot_lib import derive_repo_key

    # Ensure daemon scope exists for hosted/premium callers
    ensure_hosted_scope_for_current_context(reason="pulse_snapshot")

    code_root = Path(code_path).resolve()
    if not code_root.is_dir():
        return json.dumps({"status": "error", "reason": "invalid_code_path"}, indent=2)

    # Use _discover_git() (read-only, no worktree side effects) to resolve the
    # git repo root so repo_key matches what the daemon stored.
    git_info = _discover_git(code_root)
    if git_info.root:
        code_root = git_info.root

    repo_key = derive_repo_key(code_root)

    manager = get_daemon_manager()
    daemon = manager.get_daemon("pulse_snapshot") if manager is not None else None

    # Check config (non-caching load_config so repo-specific lookups don't
    # poison the process-wide config singleton).
    config_enabled: bool | None = None
    try:
        from watercooler.config_loader import load_config

        ps_config = load_config(project_path=code_root).mcp.daemons.pulse_snapshot
        config_enabled = ps_config.enabled
    except Exception:
        # Do not infer "disabled" if config lookup fails.
        pass

    # Resolve checkpoint namespace.  The PulseSnapshotDaemon has state_namespace set
    # to "" (local) or scope_id (hosted) by HostedCoordinator.  Use it directly when
    # the daemon is in-process; derive it from the request context otherwise so that
    # cross-process checkpoint reads are scoped correctly in hosted mode.
    namespace = ""
    if daemon is not None:
        namespace = getattr(daemon, "state_namespace", "")
    else:
        try:
            from ..context import get_effective_context

            ctx = get_effective_context()
            if ctx and ctx.user_id and ctx.repo:
                namespace = f"{ctx.user_id}:{ctx.repo}"
        except Exception:
            pass

    # 1. Primary: in-process daemon (freshest)
    snapshot: dict[str, Any] | None = (
        daemon.get_snapshot(repo_key) if daemon is not None else None
    )
    dimension_scores: dict[str, Any] | None = (
        daemon.get_dimension_scores(repo_key) if daemon is not None else None
    )
    source = "daemon"

    # 2. Fallback: on-disk checkpoint (cross-process access)
    # Skip when config is explicitly disabled to avoid presenting orphaned data.
    if snapshot is None and config_enabled is not False:
        cp = load_checkpoint("pulse_snapshot", namespace=namespace)
        project_state = cp.extras.get("projects", {}).get(repo_key, {})
        cp_snapshot = project_state.get("pulse_snapshot")
        if cp_snapshot is not None:
            snapshot = cp_snapshot
            dimension_scores = project_state.get("dimension_scores")
            source = "checkpoint"

    # 3. No snapshot available — return appropriate status
    if snapshot is None:
        if config_enabled is False:
            return json.dumps({"status": "unavailable", "reason": "disabled"}, indent=2)
        if manager is None:
            return json.dumps(
                {"status": "unavailable", "reason": "daemon_not_running"}, indent=2
            )
        if daemon is None:
            # Daemon not in this process but feature not disabled — cross-process case
            # with no checkpoint data yet.
            return json.dumps(
                {"status": "unavailable", "reason": "no_snapshot"}, indent=2
            )
        # Daemon running but hasn't ticked or wrong repo_key
        daemon_summary = daemon.status_summary()
        return json.dumps(
            {
                "status": "unavailable",
                "reason": "no_snapshot",
                "daemon_repo_key": daemon_summary.get("repo_key", ""),
                "requested_repo_key": repo_key,
            },
            indent=2,
        )

    # 4. Return snapshot (sanitized to strip absolute paths)
    sanitized = _sanitize_snapshot(snapshot)
    result: dict[str, Any] = {"status": "ok", "snapshot": sanitized}
    if dimension_scores is not None:
        result["dimension_scores"] = dimension_scores
    if source == "checkpoint":
        result["source"] = "checkpoint"
        try:
            gen = datetime.fromisoformat(snapshot["generated_at"])
            result["age_seconds"] = round(
                (datetime.now(timezone.utc) - gen).total_seconds(), 1
            )
        except (KeyError, ValueError):
            pass  # omit age_seconds if generated_at is missing/unparseable
    return json.dumps(result, indent=2)


def _acknowledge_finding_impl(
    ctx: Context,
    daemon_name: str,
    finding_id: str = "",
    finding_ids: list[str] | None = None,
) -> str:
    """Mark one or more daemon findings as acknowledged.

    Acknowledged findings are excluded from future
    ``watercooler_daemon_findings(unacknowledged_only=True)`` queries.

    This is a mutating operation (``daemon_control`` capability). In hosted
    and hybrid mode the call is routed to the Railway daemon service so that
    the acknowledgment lands in the correct namespace.

    Args:
        daemon_name: The daemon that owns the finding(s)
            (from ``findings[].daemon_name``, e.g. ``project_coordinator``).
        finding_id: A single finding ID to acknowledge.
        finding_ids: A list of finding IDs to acknowledge in one call (bulk).
            Combined with ``finding_id``; all must belong to ``daemon_name``.

    Returns:
        JSON with ``status`` (ok | partial | not_found | error), and the
        ``acknowledged`` / ``not_found`` / ``errors`` lists.
    """
    # Resolve the target id set — single + bulk, deduped, order-preserving.
    ids: list[str] = []
    for fid in [finding_id, *(finding_ids or [])]:
        fid = str(fid or "").strip()
        if fid and fid not in ids:
            ids.append(fid)
    if not ids:
        return json.dumps(
            {
                "status": "error",
                "message": "Provide finding_id or finding_ids",
            },
            indent=2,
        )

    from ..daemons import get_daemon_runtime, ensure_hosted_scope_for_current_context
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    ensure_hosted_scope_for_current_context(reason="acknowledge_finding")

    # Auxiliary (non-daemon) sources: rereview #1131 P2. These findings live
    # under the caller's auth-derived namespace (hosted) or the local
    # unscoped store — the SAME contract as their write/list paths. The
    # coordinator's daemon-derived namespace recovery cannot apply (there is
    # no registered daemon to recover ``state_namespace`` from), so route the
    # acknowledgement directly at the state layer with the caller's scope.
    if daemon_name in _AUX_FINDING_SOURCES:
        from ..auth.scope import ScopeResolutionError

        try:
            aux_namespace, aux_allow_unscoped, _ = _resolve_aux_scope()
        except ScopeResolutionError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "message": (
                        "Cannot acknowledge auxiliary finding: hosted scope "
                        f"resolution failed ({exc})."
                    ),
                },
                indent=2,
            )

        from ..daemons.state import acknowledge_finding as _ack_aux

        def _ack_one(fid: str) -> bool:
            return _ack_aux(
                daemon_name,
                fid,
                namespace=aux_namespace,
                _allow_unscoped=aux_allow_unscoped,
            )

        return _run_acknowledgements(daemon_name, ids, _ack_one)

    runtime = get_daemon_runtime()
    if runtime is None:
        return json.dumps(
            {
                "status": "error",
                "message": "Daemon manager not initialized",
            },
            indent=2,
        )

    # Bind a per-id ack callable for the active runtime (hosted vs local).
    if isinstance(runtime, HostedDaemonCoordinator):
        from ..context import get_effective_context

        eff_ctx = get_effective_context()
        scope_id = eff_ctx.scope_id if eff_ctx else None
        if not scope_id:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Cannot acknowledge finding: missing user identity.",
                },
                indent=2,
            )

        def _ack_one(fid: str) -> bool:
            return runtime.acknowledge_finding(
                scope_id=scope_id,
                daemon_name=daemon_name,
                finding_id=fid,
            )
    else:
        from ..daemons.state import acknowledge_finding as _ack

        def _ack_one(fid: str) -> bool:
            return _ack(daemon_name, fid)

    return _run_acknowledgements(daemon_name, ids, _ack_one)


def _run_acknowledgements(daemon_name: str, ids: list[str], ack_one) -> str:
    """Apply one bound per-id ack callable to each id; JSON status result."""
    acknowledged: list[str] = []
    not_found: list[str] = []
    errors: list[dict] = []
    for fid in ids:
        try:
            ok = ack_one(fid)
        except ValueError as exc:
            errors.append({"finding_id": fid, "message": str(exc)})
            continue
        (acknowledged if ok else not_found).append(fid)

    if errors:
        status = "error"
    elif acknowledged and not_found:
        status = "partial"
    elif not_found:
        status = "not_found"
    else:
        status = "ok"

    return json.dumps(
        {
            "status": status,
            "daemon_name": daemon_name,
            "acknowledged": acknowledged,
            "not_found": not_found,
            "errors": errors,
        },
        indent=2,
    )


def _build_hybrid_pooled_daemon_wrapper(runtime, tool_name, impl_func):
    """Build a per-call hybrid routing wrapper for a daemon tool.

    R3 (completion plan v3, audit-transport-modes-hosted-db-2026-07:12;
    GitHub issue #1063): daemon tools were bare proxy-mounts in hybrid
    mode, freezing every call to the boot repo's ``X-Repo``. The wrapper
    selects the per-repo premium client from the call's ``code_path``
    (``daemon_findings`` / ``pulse_snapshot``); ``daemon_status`` has no
    ``code_path`` parameter and always forwards on the boot client — the
    tool-surface scope table in docs/AUTHENTICATION_HOSTED.md records
    this.

    The capability is re-resolved **per call** (PR #1081 review):
    ``daemon_findings`` is arg-sensitive — ``action="acknowledge"`` is
    the folded-in acknowledge_finding and resolves to ``daemon_control``
    (L3), not ``daemon_observe``. A registration-time-only resolution
    would forward acknowledge writes to the hosted endpoint even when an
    operator disabled or localized ``daemon_control``, reintroducing a
    capability bypass on the write path. A locally-pinned premium daemon
    never reaches this wrapper (the registration branch keeps the local
    impls instead).
    """
    import functools
    import inspect

    from ..capabilities import tool_capability

    @functools.wraps(impl_func)
    async def _hybrid_daemon_route(ctx, **kwargs):
        capability = tool_capability(tool_name, kwargs)
        target = runtime.capability_profile.resolve_execution_target(
            capability,
            local_available=True,
            remote_available=getattr(runtime, "premium_client", None)
            is not None,
        )
        if target == "disabled":
            return json.dumps(
                {"error": "capability_disabled", "capability": capability},
                indent=2,
            )
        if target == "remote":
            from ..premium_client import select_pool_client

            return await select_pool_client(
                runtime, kwargs.get("code_path")
            ).call_tool_text(tool_name, kwargs)
        result = impl_func(ctx, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return _hybrid_daemon_route


def register_daemon_tools(mcp, *, runtime=None, remote_route=False) -> None:
    """Register daemon tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
        runtime: ToolRuntime for the surface (required when
            ``remote_route`` is True).
        remote_route: When True (hybrid surface whose daemon_observe
            capability resolves remote, no local pin), register per-call
            routing wrappers instead of the local implementations. The
            wrappers re-resolve the capability per call, so arg-sensitive
            routes (daemon_findings action="acknowledge" → daemon_control)
            honor their own configuration.
    """
    global daemon_status, daemon_findings, pulse_snapshot_tool

    def _impl(tool_name, impl_func):
        if remote_route and runtime is not None:
            return _build_hybrid_pooled_daemon_wrapper(
                runtime, tool_name, impl_func
            )
        return impl_func

    daemon_status = mcp.tool(name="watercooler_daemon_status")(
        _impl("watercooler_daemon_status", _daemon_status_impl)
    )
    daemon_findings = mcp.tool(name="watercooler_daemon_findings")(
        _impl("watercooler_daemon_findings", _daemon_findings_impl)
    )
    pulse_snapshot_tool = mcp.tool(name="watercooler_pulse_snapshot")(
        _impl("watercooler_pulse_snapshot", _pulse_snapshot_impl)
    )
