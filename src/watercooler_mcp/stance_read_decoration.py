"""Stance-advisory decoration for the recall read path.

Decorates ``watercooler_read_thread(summary_only=true)`` — the canonical
recall-entry read — with a **stance block** that joins two things the disk-less
hosted coordinator cannot join itself: the hosted ``stance_advisory`` signal and
the local ``project_salience`` attention bullets from ``.watercooler/roles.toml``.

Reads carry *stance* (pre-write attention); writes keep carrying *coordination*
(``Ball:``/``Next:``). The block is advisory-only (authority ladder L1),
best-effort, and must never block or fail the core read.

The two halves are resolved **independently** so a signal-fetch failure can never
suppress salience (and vice versa): salience is loaded from local disk first, then
the hosted/local signal is fetched under a hard deadline. Every failure degrades
to an explicit status — ``salience_status`` for the salience half,
``stance_block_status`` for the signal half. See
``dev_docs/plans/2026-07-01-feat-stance-advisory-read-decoration-plan.md``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from watercooler.pulse_stance_lib import STANCE_ROLES
from watercooler.role_loader import load_roles

# Shared terminal-escape / control-byte sanitizer — the single source of truth
# with the Stop hook, which applies it to these same finding-derived fields
# (summary, source) before they reach a terminal/LLM sink (stop_hook.py:320-336).
# Importing the one function (rather than re-implementing the regex) keeps the two
# render paths from drifting. stop_hook has stdlib-only module imports and guards
# its execution under __main__, so importing it here is side-effect-free.
from watercooler.stop_hook import _strip_unsafe_terminal_content

logger = logging.getLogger(__name__)

# Hard deadline for the best-effort signal fetch on the hot read path. Bounded so
# a slow/unreachable hosted coordinator can only ever add this much latency. Kept
# far below the read path's existing 15s git-fetch budget (sync.ensure_readable):
# a healthy proxy answers a 20-item findings list in tens of ms, so 0.25s covers
# the healthy case ~10x over while halving the per-read stall when the proxy is
# degraded-but-responding. Overridable per call via ``resolve_stance_block(
# deadline_s=...)``.
DEFAULT_SIGNAL_DEADLINE_S = 0.25
# Findings pulled per fetch — a handful covers the three stance roles newest-first.
SIGNAL_LIMIT = 20
_STANCE_ADVISORY_CATEGORY = "stance_advisory"
# An elevated advisory older than this is marked "stale" in the render so a
# week-old finding doesn't read as a live, current signal.
STALE_AFTER_S = 7 * 24 * 3600

# Closed status vocabularies. The full StanceBlockStatus set is the ratified
# degradation matrix (see plan); a signal fetch resolves to exactly one of them.
StanceBlockStatus = Literal["success", "timeout", "unavailable", "error"]
SalienceStatus = Literal["loaded", "absent", "malformed"]


@dataclass
class RoleStance:
    """Per-role stance state: local salience bullets + hosted signal overlay."""

    salience: list[str] = field(default_factory=list)
    elevated: bool = False
    level: int | None = None
    summary: str | None = None
    source: str | None = None
    produced_at: str | None = None
    stale: bool = False


@dataclass
class StanceBlock:
    """Structured stance data for one recall read (rendered by the caller)."""

    stance_block_status: StanceBlockStatus
    salience_status: SalienceStatus
    roles: dict[str, RoleStance] = field(default_factory=dict)


def resolve_stance_block(
    context: Any, *, deadline_s: float = DEFAULT_SIGNAL_DEADLINE_S
) -> StanceBlock:
    """Resolve the stance block for a local ``summary_only`` read.

    The salience half is resolved first and on its own; the signal half is then
    fetched independently and can never raise (it returns a status). So a signal
    failure leaves salience intact — the two halves are not entangled in one
    try/except.

    Args:
        context: The resolved read context (duck-typed; only ``code_root`` is
            read). ``code_root`` is ``None`` in hosted mode, but hosted reads
            never reach this helper.
        deadline_s: Hard timeout for the best-effort signal fetch.

    Returns:
        A :class:`StanceBlock`. Expected degradation (malformed/absent roles.toml,
        unreachable signal) is encoded in the status fields, not raised.
    """
    # Salience half — always resolved first, independently of the signal fetch.
    salience_status, roles = _resolve_salience(context)
    # Signal half — never raises; overlays onto ``roles`` and returns a status.
    signal_status = _resolve_signal(roles, deadline_s=deadline_s)
    return StanceBlock(
        stance_block_status=signal_status,
        salience_status=salience_status,
        roles=roles,
    )


# --------------------------------------------------------------------------- #
# Salience half (local disk)
# --------------------------------------------------------------------------- #


def _resolve_salience(context: Any) -> tuple[SalienceStatus, dict[str, RoleStance]]:
    """Load per-role salience bullets from the project roles.toml.

    Returns ``(salience_status, roles)`` where every stance role is present.
    Never raises: a malformed roles.toml degrades to ``"malformed"`` with empty
    bullets, so a broken file is diagnosable rather than silently empty.
    """
    roles: dict[str, RoleStance] = {role: RoleStance() for role in STANCE_ROLES}

    # code_root is None only in hosted mode, which never reaches this helper
    # (the hosted branch of _read_thread_impl returns before decoration). Treat
    # it as "absent" defensively — same visible outcome as a missing roles.toml.
    code_root = getattr(context, "code_root", None)
    if code_root is None:
        return "absent", roles

    try:
        definitions = load_roles(code_root)
    except ValueError:
        # roles.toml present but unparseable — surface, don't crash the read.
        return "malformed", roles

    any_bullets = False
    for role in STANCE_ROLES:
        definition = definitions.get(role)
        bullets = list(definition.project_salience) if definition else []
        roles[role] = RoleStance(salience=bullets)
        if bullets:
            any_bullets = True

    return ("loaded" if any_bullets else "absent"), roles


# --------------------------------------------------------------------------- #
# Signal half (hosted or local daemon findings)
# --------------------------------------------------------------------------- #


def _resolve_signal(
    roles: dict[str, RoleStance], *, deadline_s: float
) -> StanceBlockStatus:
    """Fetch stance_advisory findings and overlay them onto ``roles``.

    Never raises — any unexpected failure degrades to ``"error"``. This is the
    load-bearing isolation boundary: it is called *after* salience is already
    resolved, and it swallows everything, so a signal failure cannot suppress
    salience.
    """
    try:
        status, findings = _fetch_findings(deadline_s)
        if findings is None:
            return status
        _apply_signal(roles, findings)
        return "success"
    except Exception as exc:  # signal is best-effort; never break the read
        logger.debug("stance signal fetch failed: %s", exc)
        return "error"


def _fetch_findings(
    deadline_s: float,
) -> tuple[StanceBlockStatus, list[dict[str, Any]] | None]:
    """Route-aware fetch of stance_advisory findings.

    Returns ``(status, findings)``. ``findings`` is ``None`` for every
    non-``success`` status. A ``success`` with an empty list is a legitimately
    quiet result (no elevated advisories), not a failure.
    """
    from .memory_sync import get_runtime

    runtime = get_runtime()
    if runtime is None:
        return "unavailable", None

    if _routes_remote(runtime):
        premium = getattr(runtime, "premium_client", None)
        if premium is None:
            return "unavailable", None
        return _fetch_remote(premium, deadline_s)

    # Not remote — but "not remote" is not the same as "local". If the operator
    # explicitly disabled daemon observation, the read must NOT silently fall
    # through to a local fetch and overlay stance (C5: disabled -> unavailable).
    if _observe_disabled(runtime):
        return "unavailable", None

    return _fetch_local()


def _routes_remote(runtime: Any) -> bool:
    """Whether ``watercooler_daemon_findings`` is mounted from the premium proxy.

    Imports ``server_factory`` lazily inside the function to keep the mcp-layer
    ``server_factory`` a strict layer *above* this decoration module.
    ``server_factory`` imports ``thread_query`` (which lazily imports this module)
    only at call time, so a module-level import here would not close a hard cycle
    today — but hoisting it would entangle this leaf decoration module with the
    server builder, which the lazy import deliberately avoids.
    """
    try:
        from .server_factory import (
            _premium_daemon_pinned_local,
            mountable_remote_tools_for_hybrid,
        )

        if "watercooler_daemon_findings" not in mountable_remote_tools_for_hybrid(runtime):
            return False
        # Match the actual mounted surface, not just the capability route. When a
        # premium daemon is pinned route="local", build_mcp_server suppresses the
        # proxy daemon mount and registers the LOCAL daemon tools instead
        # (server_factory.py:488-538), so watercooler_daemon_findings is served
        # locally even though the capability resolved "remote". Route local to
        # match, or we'd query the proxy and miss the locally-pinned producer.
        if _premium_daemon_pinned_local():
            return False
        return True
    except Exception as exc:
        logger.debug("stance route check failed, treating as local: %s", exc)
        return False


def _fetch_remote(
    premium: Any, deadline_s: float
) -> tuple[StanceBlockStatus, list[dict[str, Any]] | None]:
    """Fetch findings via the premium proxy under a hard deadline.

    Bridges the async ``call_tool_text`` to this sync read path with
    ``run_coro_in_fresh_loop`` (the #937 helper — safe even when the calling
    thread already owns a running loop). ``call_tool_text`` itself never raises
    (it returns a JSON error string), but the outer ``asyncio.wait_for`` may
    raise ``TimeoutError`` before it returns.
    """
    import asyncio

    from ._async_utils import run_coro_in_fresh_loop

    # No scope arg: the hosted server scopes findings by the premium client's
    # X-Repo/X-Branch headers (set at client construction) and rejects a
    # mismatched repo with 403 — scoping is enforced server-side, not here.
    args = {
        "category": _STANCE_ADVISORY_CATEGORY,
        "action": "list",
        "enrich": False,
        "limit": SIGNAL_LIMIT,
    }
    try:
        # _read_thread_impl is a *sync* tool, so FastMCP runs it on an
        # anyio.to_thread worker that owns no running loop — run_coro_in_fresh_loop
        # therefore takes its inline private-loop branch (microseconds of loop
        # setup), not the ThreadPoolExecutor offload. The bridge stays correct if
        # that ever changes (a future async read tool) — it would just offload.
        text = run_coro_in_fresh_loop(
            asyncio.wait_for(
                premium.call_tool_text("watercooler_daemon_findings", args),
                deadline_s,
            )
        )
    except (asyncio.TimeoutError, TimeoutError):
        # Both names are required: on the Python 3.10 floor asyncio.TimeoutError
        # is a *distinct* class from builtin TimeoutError (they were unified in
        # 3.11). Dropping either would let a 3.10 timeout fall through to the
        # catch-all and be mislabeled "error" instead of "timeout".
        return "timeout", None

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return "error", None

    if not isinstance(parsed, dict):
        return "error", None

    # Two distinct failure shapes must not be mistaken for a quiet success:
    #  - call_tool_text transport failure: {"error": ...} (no findings)
    #  - the daemon tool's own failure envelope: {"status": "error"|
    #    "not_initialized", "findings": []} — the success envelope has no status
    #    key, so status=None passes. A daemon that isn't initialized is a
    #    (recoverable) unavailability, not a hard error.
    status_val = parsed.get("status")
    if status_val == "not_initialized":
        return "unavailable", None
    if "error" in parsed or (status_val is not None and status_val != "ok"):
        return "error", None
    if not isinstance(parsed.get("findings"), list):
        return "error", None

    return "success", parsed["findings"]


def _local_findings_available() -> bool:
    """Whether an in-process local daemon manager can serve findings."""
    from .daemons import get_daemon_runtime

    runtime = get_daemon_runtime()
    return runtime is not None and hasattr(runtime, "get_all_findings")


def _observe_disabled(runtime: Any) -> bool:
    """True iff the ``daemon_observe`` capability resolves to ``"disabled"``.

    An operator can turn daemon observation off explicitly; in that case
    ``mountable_remote_tools_for_hybrid`` omits the tool (so ``_routes_remote``
    is False) *and* there is no local surface either — the read must report
    ``unavailable``, not quietly fetch local findings. Resolving the capability
    target tri-state is the only way to tell ``"disabled"`` apart from
    ``"local"`` at this point.
    """
    try:
        from .capabilities import tool_capability

        cap = tool_capability("watercooler_daemon_findings")
        premium = getattr(runtime, "premium_client", None)
        target = runtime.capability_profile.resolve_execution_target(
            cap,
            local_available=_local_findings_available(),
            remote_available=premium is not None,
        )
        return target == "disabled"
    except Exception as exc:
        # Fail open to the local fetch (prior behavior) rather than blanking the
        # signal on an unexpected resolution error.
        logger.debug("stance observe-disabled check failed: %s", exc)
        return False


def _fetch_local() -> tuple[StanceBlockStatus, list[dict[str, Any]] | None]:
    """Fetch findings from the in-process local daemon manager."""
    from .daemons import get_daemon_runtime

    runtime = get_daemon_runtime()
    if runtime is None or not hasattr(runtime, "get_all_findings"):
        return "unavailable", None

    findings = runtime.get_all_findings(
        category=_STANCE_ADVISORY_CATEGORY, limit=SIGNAL_LIMIT
    )
    return "success", [f.to_dict() for f in findings]


def _apply_signal(
    roles: dict[str, RoleStance], findings: list[dict[str, Any]]
) -> None:
    """Overlay the newest elevated (level>=1) finding per role onto ``roles``.

    ``findings`` are newest-first (both fetch paths sort reverse-chronologically),
    so the **newest valid finding per role is authoritative** and no older finding
    for that role is considered. This is what makes clearances stick: if the
    newest finding is a level-0 tombstone ("cleared" stance), the role stays quiet
    even when an older level>=1 finding is still within ``SIGNAL_LIMIT`` — without
    this, a cleared stance would be resurrected until the old record aged out.
    Malformed findings (bad level type) don't decide a role — they're ignored so
    the search continues to the newest *valid* record.
    """
    decided: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("category") != _STANCE_ADVISORY_CATEGORY:
            continue
        details = finding.get("details") or {}
        if not isinstance(details, dict):
            continue
        advisory = details.get("advisory") or {}
        if not isinstance(advisory, dict):
            continue

        role = advisory.get("role")
        if not isinstance(role, str):
            continue
        role_stance = roles.get(role)
        if role_stance is None or role in decided:
            continue  # unknown role, or already decided by a newer finding

        level = advisory.get("level", 0)
        if not isinstance(level, int) or isinstance(level, bool):
            continue  # malformed level: not a valid state — keep looking

        # Newest valid finding for this role — it decides the role's state.
        decided.add(role)
        if level < 1:
            continue  # level-0 tombstone: role is cleared, ignore older records

        role_stance.elevated = True
        role_stance.level = level
        # summary/source originate from a remote payload (or local daemon) and
        # flow into markdown/JSON that can reach a terminal or LLM context —
        # sanitize here (once, for both render paths) exactly as the Stop hook
        # does for these same fields. produced_at is our own ISO string and level
        # is an int, so neither needs it. Salience bullets are already sanitized
        # at load by role_loader._validate_project_salience.
        summary = advisory.get("summary")
        role_stance.summary = (
            _strip_unsafe_terminal_content(summary) if isinstance(summary, str) else None
        )
        source = finding.get("daemon_name")
        role_stance.source = (
            _strip_unsafe_terminal_content(source)
            if isinstance(source, str) and source
            else None
        )
        role_stance.produced_at = _iso(finding.get("created_at"))
        role_stance.stale = _is_stale(finding.get("created_at"))


def _iso(created_at: Any) -> str | None:
    """Format a finding ``created_at`` epoch as an ISO-8601 UTC string."""
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        return None
    if created_at <= 0:
        return None
    try:
        return datetime.fromtimestamp(created_at, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _now() -> float:
    """Current wall-clock epoch (indirection so tests can pin staleness)."""
    return time.time()


def _is_stale(created_at: Any) -> bool:
    """Whether an elevated finding is older than ``STALE_AFTER_S``."""
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        return False
    if created_at <= 0:
        return False
    return (_now() - created_at) > STALE_AFTER_S


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_stance_markdown(block: StanceBlock) -> str:
    """Render the stance block as a markdown ``## Project stance`` section.

    Returns an empty string when there is nothing worth showing to a human (no
    salience and no elevated signal), so the caller can skip appending an empty
    block. A degraded signal status is shown as a heading suffix only when the
    block is already being rendered for other content — markdown stays quiet on
    a fully-empty read; the JSON peer key carries the machine-readable status.
    """
    if block.salience_status == "malformed":
        return (
            "## Project stance\n\n"
            "_Role salience unavailable: `.watercooler/roles.toml` could not be "
            "parsed._"
        )

    # Elevated roles render first (the live, actionable signal); salience-only
    # ("quiet") roles follow. A quiet role is marked "(quiet)" only when there is
    # an elevated role to contrast against — otherwise every role is quiet and the
    # "(signal: ...)" heading suffix already conveys that.
    any_elevated = any(rs.elevated for rs in block.roles.values() if rs is not None)
    elevated_lines: list[str] = []
    quiet_lines: list[str] = []
    for role in STANCE_ROLES:
        role_stance = block.roles.get(role)
        if role_stance is None or (
            not role_stance.elevated and not role_stance.salience
        ):
            continue
        if role_stance.elevated:
            src = f", source: {role_stance.source}" if role_stance.source else ""
            when = f", {role_stance.produced_at}" if role_stance.produced_at else ""
            stale = ", stale" if role_stance.stale else ""
            elevated_lines.append(
                f"- **{role}** — L{role_stance.level}: {role_stance.summary} "
                f"_(advisory only{src}{when}{stale})_"
            )
            bucket = elevated_lines
        else:
            marker = " (quiet)" if any_elevated else ""
            quiet_lines.append(f"- **{role}**{marker}")
            bucket = quiet_lines
        for bullet in role_stance.salience:
            bucket.append(f"  - {bullet}")

    sections = elevated_lines + quiet_lines
    if not sections:
        return ""

    suffix = (
        ""
        if block.stance_block_status == "success"
        else f" (signal: {block.stance_block_status})"
    )
    note = "_Attention cues (advisory only)._"
    return f"## Project stance{suffix}\n\n" + note + "\n\n" + "\n".join(sections)


def render_stance_json(block: StanceBlock) -> dict[str, Any]:
    """Render the stance block as the ``_stance_advisory`` JSON peer key."""
    return {
        "stance_block_status": block.stance_block_status,
        "salience_status": block.salience_status,
        "advisory_only": True,
        "roles": {
            role: {
                "salience": role_stance.salience,
                "elevated": role_stance.elevated,
                "level": role_stance.level,
                "summary": role_stance.summary,
                "source": role_stance.source,
                "produced_at": role_stance.produced_at,
                "stale": role_stance.stale,
            }
            for role, role_stance in block.roles.items()
        },
    }
