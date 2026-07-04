"""Shared setup-readiness report for ``watercooler_init`` and ``watercooler_health``.

This is the single source of the readiness contract. ``watercooler_init``
mutates (scaffolds roles, binds the worktree, optionally pushes) and then asks
this module to describe the resulting *current state*; ``watercooler_health
detail="setup"`` asks for the same description with **no mutation at all**.

Layering: this lives in ``watercooler_mcp`` because it composes the
parity/worktree/remote primitives in :mod:`watercooler_mcp.config`. Only the
packaged-assets check is library-side (:mod:`watercooler.packaged_assets`), so
there is no ``watercooler`` → ``watercooler_mcp`` import.

Every field here is **preference-neutral** — there is no stored
``setup_complete`` boolean and no ``allow_local_only`` field. Whether
"local-only" is acceptable is a judgment the caller makes; ``watercooler_init``
expresses it only in the human-facing ``summary``/``next_actions`` wording, so
``init`` and read-only ``health`` can never disagree about a structured fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from watercooler.packaged_assets import check_packaged_assets

from .config import (
    ORPHAN_BRANCH_NAME,
    _discover_git,
    _run_git,
    _select_push_remote,
    _worktree_path_for,
)

# ── Repo resolution (read-only) ──────────────────────────────────────────────


@dataclass(frozen=True)
class RepoResolution:
    """Read-only resolution of a ``code_path`` to its git toplevel."""

    input_path: Optional[Path]
    resolved_code_root: Optional[Path]
    is_git: bool
    needs_code_path: bool = False
    ambiguous: bool = False
    ambiguity_reason: Optional[str] = None


def resolve_repo(code_path: str) -> RepoResolution:
    """Resolve ``code_path`` to its git toplevel without mutating anything.

    Mirrors the safety the worktree path needs: a uvx-launched server's cwd is
    an ephemeral env dir, so an empty ``code_path`` is *not* inferred from cwd —
    it returns ``needs_code_path``.
    """
    if not code_path or not str(code_path).strip():
        return RepoResolution(
            input_path=None,
            resolved_code_root=None,
            is_git=False,
            needs_code_path=True,
        )

    path = Path(code_path).expanduser()
    git = _discover_git(path)
    if git.root is None:
        return RepoResolution(
            input_path=path, resolved_code_root=None, is_git=False
        )

    root = Path(git.root)
    # Genuine ambiguity: a nested .git boundary *between* the passed path and the
    # resolved toplevel means the path likely belongs to a different repo than
    # the user thinks. A plain subdirectory of its enclosing repo is NOT
    # ambiguous — that is the normal case; just report resolved_code_root.
    ambiguous, reason = _detect_nested_boundary(path, root)
    return RepoResolution(
        input_path=path,
        resolved_code_root=root,
        is_git=True,
        ambiguous=ambiguous,
        ambiguity_reason=reason,
    )


def _detect_nested_boundary(path: Path, root: Path) -> tuple[bool, Optional[str]]:
    """True when a ``.git`` exists strictly between ``path`` and ``root``.

    That signals a nested repo / submodule boundary the caller crossed — the
    ambiguous case worth a confirmation gate. A path equal to or a plain
    descendant of ``root`` with no intervening ``.git`` is unambiguous.
    """
    try:
        path = path.resolve()
        root = root.resolve()
    except OSError:
        return (False, None)
    if path == root:
        return (False, None)
    try:
        path.relative_to(root)
    except ValueError:
        # path is not under root (e.g. symlinked) — let git's answer stand.
        return (False, None)
    cursor = path
    # Walk up from path toward root; a .git below root means a nested boundary.
    while cursor != root and cursor != cursor.parent:
        if cursor != path and (cursor / ".git").exists():
            return (
                True,
                f"a nested git boundary at {cursor} sits between the path you "
                f"passed and the resolved repo root {root}",
            )
        cursor = cursor.parent
    return (False, None)


# ── Deployment context ───────────────────────────────────────────────────────


def deployment_context() -> tuple[str, str, bool]:
    """Return ``(transport, mode, local_init_applies)``.

    ``local_init_applies`` is False for checkout-less hosted/proxy deployments
    where the worktree model does not apply.
    """
    from .auth import is_hosted_mode
    from .config import get_mcp_transport_config
    from .observability import log_warning

    try:
        transport = str(get_mcp_transport_config().get("transport", "stdio"))
    except Exception as exc:
        # Defaulting to stdio is the safe report fallback, but a genuinely
        # broken transport config should be diagnosable, not silent.
        log_warning(f"setup_report: transport config unreadable, assuming stdio: {exc}")
        transport = "stdio"
    hosted = False
    try:
        hosted = is_hosted_mode()
    except Exception as exc:
        log_warning(f"setup_report: hosted-mode probe failed, assuming local: {exc}")
        hosted = False
    mode = "hosted" if hosted else "local"
    # Local init applies whenever there is a local checkout to bind — that is
    # any non-hosted deployment except the checkout-less "proxy" transport
    # (which forwards to a remote server). Transport is independent of the
    # hosted-vs-local distribution axis, so self-hosted HTTP (transport="http"
    # with is_hosted_mode()=False) DOES have a checkout and must be allowed.
    # INVARIANT (see server_factory.build_mcp_server): watercooler_init is
    # registered on local_full/local_hybrid, which must remain a superset of the
    # surfaces where this is True — otherwise a setup next_action could name an
    # unregistered tool. Keep the two gates in sync if either changes.
    local_init_applies = not hosted and transport != "proxy"
    return transport, mode, local_init_applies


# ── State probes (all read-only) ─────────────────────────────────────────────


def worktree_present(code_root: Path) -> bool:
    """True only when the canonical worktree exists AND is on the orphan branch.

    A path that exists but is checked out on some other branch (a stale/manual
    worktree, or a basename collision under ~/.watercooler/worktrees) is NOT a
    valid threads worktree — ``_ensure_worktree_locked`` treats that state as
    invalid and recreates it, so the read-only report must not count it as
    present or it would yield a false-positive ``usable_now``.
    """
    wt = _worktree_path_for(code_root)
    if not (wt.exists() and (wt / ".git").exists()):
        return False
    head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    return head == ORPHAN_BRANCH_NAME


def roles_present(code_root: Path) -> bool:
    return (code_root / ".watercooler" / "roles.toml").is_file()


def config_state(code_root: Optional[Path]) -> tuple[bool, List[str], Optional[str]]:
    """Return ``(effective_config_ok, config_sources, error)`` read-only.

    ``config_sources`` lists the precedence inputs that actually exist, so an
    agent can verify parity rather than just trust a boolean. ``error`` is the
    underlying cause when the effective config does not load.

    Validation is scoped to ``code_root`` via :func:`config_loader.load_config`
    into a throwaway result — this validates the *inspected repo's* effective
    config (including its ``.watercooler/config.toml``) without touching the
    process-wide cached config, so the read-only ``health detail="setup"``
    contract is preserved. (``get_watercooler_config`` is module-cached and
    would return the already-loaded server config, never the project file.)
    """
    sources: List[str] = ["built-in defaults"]
    user_cfg = Path("~/.watercooler/config.toml").expanduser()
    if user_cfg.is_file():
        sources.append(str(user_cfg))
    if code_root is not None:
        project_cfg = code_root / ".watercooler" / "config.toml"
        if project_cfg.is_file():
            sources.append(str(project_cfg))
    try:
        from watercooler.config_loader import ConfigError, load_config

        load_config(code_root)
        return True, sources, None
    except (ConfigError, OSError, ImportError) as exc:
        # ConfigError/OSError are the documented config failure modes;
        # ImportError (e.g. config_loader unavailable in a broken install) is
        # caught too so this read-only report stays total — surfacing the cause
        # instead of propagating out of watercooler_health detail="setup".
        return False, sources, f"{type(exc).__name__}: {exc}"


def sync_state(
    code_root: Path, *, worktree_ok: bool
) -> tuple[str, Optional[str]]:
    """Read-only remote-parity classification.

    Returns ``(sync_status, remote_descriptor)``.

    No fetch and no "is local ahead?" probe (a worktree just born from a remote
    ref would mis-report). Remote public/private visibility is intentionally not
    determined here — the push consent gate is always conservative (it requires
    an explicit ``confirm_public``) rather than relying on a visibility guess.
    """
    if not worktree_ok:
        return "unknown", None
    remote = _select_push_remote(code_root)
    if remote is None:
        return "no_remote", None
    url = _run_git(["remote", "get-url", remote], code_root) or None
    descriptor = f"{remote} ({url})" if url else remote
    # The orphan branch's remote-tracking ref existing locally is our
    # best-effort "has been published" signal without a network fetch.
    ref = _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{ORPHAN_BRANCH_NAME}"],
        code_root,
    )
    sync_status = "synced" if ref else "local_only"
    return sync_status, descriptor


# ── Report assembly ──────────────────────────────────────────────────────────


@dataclass
class InitActions:
    """What ``watercooler_init`` did this call — flavors the report."""

    worktree: str = "exists"  # created | exists | failed
    roles_file: str = "exists"  # created | exists | skipped_readonly
    roles_backup: Optional[str] = None
    push_attempt: str = "not_requested"  # not_requested | pushed | failed | not_applicable
    allow_local_only: bool = False
    extra_warnings: List[str] = field(default_factory=list)
    extra_next_actions: List[Dict[str, Any]] = field(default_factory=list)
    # When init performed a push, it owns the resulting sync_status.
    sync_status_override: Optional[str] = None


def _action(
    label: str, *, tool: Optional[str], instruction: str
) -> Dict[str, Any]:
    """One machine-actionable next step.

    ``tool`` names an MCP verb the agent can dispatch, or is ``None`` when no
    agent-callable remediation exists yet (the gap is then explicit, not punted).
    """
    return {
        "label": label,
        "tool": tool,
        "instruction": instruction,
        "agent_actionable": tool is not None,
    }


def needs_code_path_report(detail_for: str) -> Dict[str, Any]:
    """Report returned when ``code_path`` is missing/unresolvable."""
    transport, mode, _ = deployment_context()
    return {
        "summary": (
            "I need the path to your project's repo to check or set up "
            "watercooler — please pass code_path."
        ),
        "needs_code_path": True,
        "usable_now": False,
        "roles_customizable": False,
        "sync_status": "unknown",
        "remote": None,
        "resolved_code_root": None,
        "transport": transport,
        "mode": mode,
        "push_attempt": "not_applicable",
        "warnings": [
            f"{detail_for} could not resolve a git repository from the given "
            f"code_path (a uvx-launched server's working directory is not your "
            f"repo, so it is never assumed)."
        ],
        "next_actions": [
            _action(
                "supply code_path",
                tool=None,
                instruction=(
                    "Call again with code_path set to your project's repository "
                    "root (the directory containing its .git)."
                ),
            )
        ],
        "details": {},
    }


def hosted_na_report(transport: str, mode: str) -> Dict[str, Any]:
    """Report for checkout-less hosted/proxy deployments (local init N/A)."""
    return {
        "summary": (
            "This is a hosted/checkout-less deployment, so local repo "
            "initialization does not apply — the hosted control plane "
            "provisions storage."
        ),
        "usable_now": True,
        "roles_customizable": False,
        "sync_status": "unknown",
        "remote": None,
        "resolved_code_root": None,
        "transport": transport,
        "mode": mode,
        "push_attempt": "not_applicable",
        "warnings": [],
        "next_actions": [
            _action(
                "use hosted onboarding",
                tool=None,
                instruction=(
                    "Configure your hosted API key; storage is provisioned "
                    "server-side. The local worktree model is not used here."
                ),
            )
        ],
        "details": {"deployment": "hosted_or_proxy"},
    }


def non_git_report(resolution: RepoResolution) -> Dict[str, Any]:
    """Report when ``code_path`` resolves to a non-git directory.

    Shared by ``watercooler_init`` and read-only ``health detail="setup"`` so
    both give the same prerequisite ("run git init first") instead of the
    read-only path falsely reporting "uninitialized — call watercooler_init".
    """
    transport, mode, _ = deployment_context()
    return {
        "summary": (
            "That path isn't a git repository yet — run `git init` there first, "
            "then ask me to set up watercooler again."
        ),
        "usable_now": False,
        "roles_customizable": False,
        "sync_status": "unknown",
        "remote": None,
        "resolved_code_root": None,
        "transport": transport,
        "mode": mode,
        "push_attempt": "not_applicable",
        "warnings": [
            f"{resolution.input_path} is not inside a git repository; watercooler "
            f"stores threads on a git branch, so a repo is required."
        ],
        "next_actions": [
            _action(
                "initialize a git repo",
                tool=None,
                instruction="Run `git init` in your project, then call watercooler_init again.",
            )
        ],
        "details": {},
    }


def ambiguous_report(resolution: RepoResolution) -> Dict[str, Any]:
    """Report when a nested-repo boundary makes the resolved root ambiguous.

    Shared by ``watercooler_init`` and read-only ``health detail="setup"``.
    """
    transport, mode, _ = deployment_context()
    root = resolution.resolved_code_root
    return {
        "summary": (
            "Before I report anything, please confirm the repo: "
            f"I resolved your path to {root}. If that's the project you mean, "
            "call again with code_path set to it."
        ),
        "usable_now": False,
        "roles_customizable": False,
        "sync_status": "unknown",
        "remote": None,
        "resolved_code_root": str(root) if root else None,
        "transport": transport,
        "mode": mode,
        "push_attempt": "not_applicable",
        "warnings": [resolution.ambiguity_reason or "ambiguous repository resolution"],
        "next_actions": [
            _action(
                "confirm the repo root",
                tool="watercooler_init",
                instruction=(
                    f"If {root} is correct, call watercooler_init with "
                    f"code_path={root!s}. Otherwise pass the intended repo root."
                ),
            )
        ],
        "details": {"resolved_code_root": str(root) if root else None},
    }


def build_setup_report(
    resolution: RepoResolution,
    *,
    actions: Optional[InitActions] = None,
) -> Dict[str, Any]:
    """Assemble the readiness contract from read-only probes (+ optional init actions).

    ``actions is None`` → the read-only ``health detail="setup"`` report.
    ``actions`` set → the ``watercooler_init`` report (same fields, plus what
    this call did).
    """
    transport, mode, _ = deployment_context()
    root = resolution.resolved_code_root
    assets_ok, missing_assets = check_packaged_assets()
    cfg_ok, cfg_sources, cfg_error = config_state(root)

    wt_ok = bool(root and worktree_present(root))
    roles_ok = bool(root and roles_present(root))
    if actions is not None:
        # Init just acted: trust its action outcomes for these.
        wt_ok = wt_ok or actions.worktree == "created"
        roles_ok = roles_ok or actions.roles_file == "created"

    sync_status, remote = (
        sync_state(root, worktree_ok=wt_ok) if root else ("unknown", None)
    )
    if actions and actions.sync_status_override:
        sync_status = actions.sync_status_override

    # The setup contract requires the effective config to resolve: a malformed
    # .watercooler/config.toml means the server can't run as configured, so the
    # repo is not "usable now" even if the worktree and assets are fine.
    usable_now = bool(wt_ok and assets_ok and cfg_ok)
    roles_customizable = bool(roles_ok)

    warnings: List[str] = []
    if missing_assets:
        warnings.append(
            "packaged assets did not resolve: "
            + ", ".join(missing_assets)
            + " — reinstall the package."
        )
    if not cfg_ok:
        detail = f" ({cfg_error})" if cfg_error else ""
        warnings.append(
            "configuration failed to load; check the config files listed in "
            f"config_sources{detail}."
        )

    details: Dict[str, Any] = {
        # Read-only health: an uninitialized repo is "absent", not "failed"
        # (which an agent reads as an error). "failed" is reserved for an init
        # that tried to bind and could not.
        "worktree": (actions.worktree if actions else ("exists" if wt_ok else "absent")),
        "roles_file": (actions.roles_file if actions else ("exists" if roles_ok else "missing")),
        "effective_config_ok": cfg_ok,
        "config_sources": cfg_sources,
        "packaged_assets_ok": assets_ok,
    }
    if root is not None:
        details["worktree_path"] = str(_worktree_path_for(root))
    if actions and actions.roles_backup:
        details["roles_backup"] = actions.roles_backup

    push_attempt = actions.push_attempt if actions else "not_applicable"
    allow_local_only = bool(actions and actions.allow_local_only)

    next_actions = _build_next_actions(
        wt_ok=wt_ok,
        roles_ok=roles_ok,
        sync_status=sync_status,
        allow_local_only=allow_local_only,
    )
    if actions:
        next_actions = actions.extra_next_actions + next_actions
        warnings = actions.extra_warnings + warnings

    # Dedupe next_actions by (label, tool) — init and the generic builder can
    # independently surface the same remediation (e.g. repair sync).
    seen: set[tuple[str, Optional[str]]] = set()
    deduped: List[Dict[str, Any]] = []
    for a in next_actions:
        key = (a["label"], a["tool"])
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    next_actions = deduped

    summary = _build_summary(
        usable_now=usable_now,
        roles_ok=roles_ok,
        sync_status=sync_status,
        is_init=actions is not None,
        allow_local_only=allow_local_only,
        warnings=warnings,
    )
    if actions and actions.roles_backup:
        # The agent relays summary verbatim — a force re-scaffold that moved an
        # edited roles.toml aside must say so, not just bury it in details.
        summary += (
            f" Your previous roles.toml was backed up to {actions.roles_backup}."
        )

    return {
        "summary": summary,
        "usable_now": usable_now,
        "roles_customizable": roles_customizable,
        "sync_status": sync_status,
        "remote": remote,
        "resolved_code_root": str(root) if root else None,
        "transport": transport,
        "mode": mode,
        "push_attempt": push_attempt,
        "warnings": warnings,
        "next_actions": next_actions,
        "details": details,
    }


def _build_next_actions(
    *,
    wt_ok: bool,
    roles_ok: bool,
    sync_status: str,
    allow_local_only: bool,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not wt_ok:
        out.append(
            _action(
                "initialize this repo",
                tool="watercooler_init",
                instruction="Call watercooler_init with this code_path to bind thread storage.",
            )
        )
    if not roles_ok:
        out.append(
            _action(
                "scaffold roles",
                tool="watercooler_init",
                instruction=(
                    "watercooler_init writes an editable, fully-commented "
                    ".watercooler/roles.toml; uncomment a [roles.<name>] block to "
                    "tailor a role for this project."
                ),
            )
        )
    elif roles_ok:
        out.append(
            _action(
                "customize roles (optional)",
                tool=None,
                instruction=(
                    "Edit .watercooler/roles.toml — uncomment a [roles.<name>] "
                    "block to tailor it. Commit the file so teammates inherit it."
                ),
            )
        )
    if sync_status == "local_only" and not allow_local_only:
        out.append(
            _action(
                "publish to teammates",
                tool="watercooler_init",
                instruction=(
                    "When you want teammates to see threads, call watercooler_init "
                    "with push=true and confirm_public=true (optionally remote=<url> "
                    "to target a specific remote)."
                ),
            )
        )
        out.append(
            _action(
                "keep local-only (if intentional)",
                tool=None,
                instruction=(
                    "If solo/local-only is intentional, pass allow_local_only=true "
                    "to watercooler_init to silence the unsynced notice."
                ),
            )
        )
    if sync_status == "no_remote":
        out.append(
            _action(
                "add a git remote",
                tool=None,
                instruction=(
                    "This repo has no usable remote, so threads stay local. Add a "
                    "remote (git remote add origin <url>) if teammates should see them."
                ),
            )
        )
    if sync_status in ("auth_failed",):
        out.append(
            _action(
                "repair sync",
                tool="watercooler_sync_repair",
                instruction="The push failed (auth/network). Run watercooler_sync_repair.",
            )
        )
    return out


def _build_summary(
    *,
    usable_now: bool,
    roles_ok: bool,
    sync_status: str,
    is_init: bool,
    allow_local_only: bool,
    warnings: List[str],
) -> str:
    if not usable_now:
        if warnings:
            return (
                "Watercooler isn't fully set up here yet — "
                + warnings[0].rstrip(".")
                + "."
            )
        return (
            "Watercooler isn't initialized in this repo yet — "
            "call watercooler_init to set it up."
        )
    lead = "You're set up" if is_init else "Watercooler is set up here"
    persist = "your notes persist in this repo"
    if sync_status == "synced":
        tail = "and are shared with your team."
    elif sync_status == "local_only":
        if allow_local_only:
            tail = "(local-only, as you intended)."
        else:
            tail = "— ask me to push when you want teammates to see them."
    elif sync_status == "no_remote":
        if allow_local_only:
            tail = "(local-only, as you intended)."
        else:
            tail = "locally — add a git remote when you want teammates to see them."
    else:
        tail = "locally."
    roles_note = (
        "" if roles_ok else " Roles aren't customizable yet — re-run init to scaffold them."
    )
    return f"{lead} — {persist} {tail}{roles_note}"
