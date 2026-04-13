"""Daemon management MCP tools.

Tools:
- watercooler_daemon_status: View daemon health and status
- watercooler_daemon_findings: Query daemon findings with filters
- watercooler_pulse_snapshot: Read the cached Project Pulse snapshot
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Context

# Module-level references to registered tools (populated by register_daemon_tools)
daemon_status = None
daemon_findings = None
pulse_snapshot_tool = None


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
    from ..daemons import get_daemon_runtime, get_daemon_manager, ensure_hosted_scope_for_current_context
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    # Ensure daemon scope exists for hosted/premium callers
    ensure_hosted_scope_for_current_context(reason="daemon_status")

    runtime = get_daemon_runtime()
    if runtime is None:
        return json.dumps({
            "status": "not_initialized",
            "message": "Daemon manager not initialized",
        }, indent=2)

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
        return json.dumps(result, indent=2)

    # Local mode: use the DaemonManager
    manager = runtime

    # 1. Validate named daemon up front so d is never None downstream.
    d = None
    if daemon:
        d = manager.get_daemon(daemon)
        if d is None:
            return json.dumps({
                "status": "error",
                "message": f"Daemon '{daemon}' not found",
                "available": manager.daemon_names,
            }, indent=2)

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
        return json.dumps(result, indent=2)

    # Non-trigger path: return status_payload at top level (backward-compatible).
    # Telemetry added as a sibling key — safe because daemon names never collide.
    if telemetry:
        status_payload["service_telemetry"] = telemetry
    return json.dumps(status_payload, indent=2)


def _daemon_findings_impl(
    ctx: Context,
    daemon: str = "",
    severity: str = "",
    category: str = "",
    topic: str = "",
    limit: int = 50,
    unacknowledged_only: bool = False,
) -> str:
    """Query daemon findings with optional filters.

    Returns findings in reverse chronological order (newest first).

    Args:
        daemon: Filter by daemon name (empty = all daemons).
        severity: Filter by severity ("info", "warning", "error").
        category: Filter by category (e.g., "missing_status", "stale_thread").
        topic: Filter by thread topic.
        limit: Maximum findings to return (default 50).
        unacknowledged_only: Only return unacknowledged findings.
    """
    from ..daemons import get_daemon_runtime, ensure_hosted_scope_for_current_context
    from ..daemons.hosted_coordinator import HostedDaemonCoordinator

    # Ensure daemon scope exists for hosted/premium callers
    ensure_hosted_scope_for_current_context(reason="daemon_findings")

    runtime = get_daemon_runtime()
    if runtime is None:
        return json.dumps({
            "status": "not_initialized",
            "message": "Daemon manager not initialized",
            "findings": [],
        }, indent=2)

    # Clamp limit
    limit = max(1, min(limit, 500))

    try:
        if isinstance(runtime, HostedDaemonCoordinator):
            from ..context import get_effective_context
            eff_ctx = get_effective_context()
            scope_id = eff_ctx.scope_id if eff_ctx else None
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
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": str(exc),
            "findings": [],
        }, indent=2)

    return json.dumps({
        "count": len(findings),
        "findings": [f.to_dict() for f in findings],
    }, indent=2)


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
    snapshot: dict[str, Any] | None = daemon.get_snapshot(repo_key) if daemon is not None else None
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
            return json.dumps({"status": "unavailable", "reason": "daemon_not_running"}, indent=2)
        if daemon is None:
            # Daemon not in this process but feature not disabled — cross-process case
            # with no checkpoint data yet.
            return json.dumps({"status": "unavailable", "reason": "no_snapshot"}, indent=2)
        # Daemon running but hasn't ticked or wrong repo_key
        daemon_summary = daemon.status_summary()
        return json.dumps({
            "status": "unavailable",
            "reason": "no_snapshot",
            "daemon_repo_key": daemon_summary.get("repo_key", ""),
            "requested_repo_key": repo_key,
        }, indent=2)

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


def register_daemon_tools(mcp) -> None:
    """Register daemon tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global daemon_status, daemon_findings, pulse_snapshot_tool

    daemon_status = mcp.tool(name="watercooler_daemon_status")(_daemon_status_impl)
    daemon_findings = mcp.tool(name="watercooler_daemon_findings")(_daemon_findings_impl)
    pulse_snapshot_tool = mcp.tool(name="watercooler_pulse_snapshot")(_pulse_snapshot_impl)
