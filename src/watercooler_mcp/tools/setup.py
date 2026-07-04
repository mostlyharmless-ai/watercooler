"""MCP-native repository setup: ``watercooler_init``.

A user who installs only the MCP server (the ``watercooler-mcp`` entry point via
``uvx``) has no PATH-visible ``watercooler`` CLI, so init must be an MCP tool.
``watercooler_init`` is idempotent: it verifies packaged assets,
scaffolds an editable ``.watercooler/roles.toml``, binds the threads worktree
*locally*, optionally (opt-in, gated) publishes it, and returns a structured
readiness report.

Authority: registered ``ToolSpec("diagnostics", "L2")`` — it mutates durable
state, so it is never auto-invoked; an agent calls it only after the user
signals setup intent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import Context

from watercooler.roles_scaffold import scaffold_roles_file

from ..config import (
    ORPHAN_BRANCH_NAME,
    _ensure_worktree,
    _run_git,
    _select_push_remote,
    _worktree_path_for,
)
from ..setup_report import (
    InitActions,
    _action,
    ambiguous_report,
    build_setup_report,
    deployment_context,
    hosted_na_report,
    needs_code_path_report,
    non_git_report,
    resolve_repo,
    worktree_present,
)

CREDENTIALS_IGNORE_LINE = ".watercooler/credentials.toml"


def _ensure_credentials_gitignored(code_root: Path) -> str:
    """Idempotently ensure ``.watercooler/credentials.toml`` is gitignored.

    Returns ``"present"`` / ``"added"`` / ``"skipped"`` (write failure). Only
    ever appends the single secret-safety line; never blanket-ignores
    ``.watercooler/`` (roles.toml/config.toml are shareable).
    """
    gitignore = code_root / ".gitignore"
    try:
        existing = gitignore.read_text() if gitignore.is_file() else ""
    except OSError:
        return "skipped"
    lines = {ln.strip() for ln in existing.splitlines()}
    if CREDENTIALS_IGNORE_LINE in lines or "credentials.toml" in lines:
        return "present"
    try:
        prefix = "" if (existing == "" or existing.endswith("\n")) else "\n"
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(
                f"{prefix}# watercooler secrets — never commit\n"
                f"{CREDENTIALS_IGNORE_LINE}\n"
            )
        return "added"
    except OSError:
        return "skipped"


def _credentials_tracked(code_root: Path) -> bool:
    """True when ``.watercooler/credentials.toml`` is already tracked by git.

    A gitignore line does nothing for an already-tracked file, so this is the
    real "are my secrets about to be published?" check.
    """
    out = _run_git(
        ["ls-files", "--error-unmatch", CREDENTIALS_IGNORE_LINE], code_root
    )
    return out is not None


def _is_named_remote(code_root: Path, target: str) -> bool:
    """True when ``target`` is a configured git remote *name* (not a bare URL)."""
    remotes_out = _run_git(["remote"], code_root)
    remotes = [r.strip() for r in (remotes_out or "").splitlines() if r.strip()]
    return target in remotes


def _do_push(
    code_root: Path,
    *,
    remote: Optional[str],
    confirm_public: bool,
) -> tuple[str, Optional[str], List[str], List[Dict[str, Any]]]:
    """Perform the opt-in publish behind the consent gate.

    Returns ``(push_attempt, sync_status_override, warnings, next_actions)``.

    ``remote`` only *targets* a destination — it is never treated as consent.
    The remote's public/private visibility cannot be determined reliably here,
    so publishing thread bodies always requires an explicit ``confirm_public``
    (otherwise an agent could satisfy the gate by echoing the refusal's own
    ``remote=`` suggestion). On success this returns ``None`` for the
    sync-status override so the report recomputes it from git state — keeping
    ``init`` and a later ``health detail="setup"`` in agreement.
    """
    warnings: List[str] = []
    actions: List[Dict[str, Any]] = []

    target = remote or _select_push_remote(code_root)
    if target is None:
        warnings.append(
            "push requested but no usable remote is configured — add one "
            "(git remote add origin <url>) then retry."
        )
        actions.append(
            _action(
                "add a git remote",
                tool=None,
                instruction="git remote add origin <url>, then call watercooler_init push=true.",
            )
        )
        return "failed", "no_remote", warnings, actions

    url = _run_git(["remote", "get-url", target], code_root) or target

    # Consent gate: visibility is unknown, so publishing thread bodies always
    # requires confirm_public. Naming remote= chooses the destination only.
    if not confirm_public:
        warnings.append(
            f"heads up — I can't confirm whether '{target}' ({url}) is private; "
            f"thread bodies (agent reasoning, decisions) become world-readable if "
            f"it's public. I did NOT push. Re-call with confirm_public=true once "
            f"you've confirmed the remote is one your team should see."
        )
        actions.append(
            _action(
                "confirm and publish",
                tool="watercooler_init",
                instruction=(
                    "Re-call watercooler_init with push=true and confirm_public=true to "
                    f"publish the threads branch to '{target}'"
                    + (f" (remote={remote})" if remote else "")
                    + "."
                ),
            )
        )
        return "failed", None, warnings, actions

    wt_path = _worktree_path_for(code_root)
    pushed = _run_git(["push", "-u", target, ORPHAN_BRANCH_NAME], wt_path)
    if pushed is None:
        warnings.append(
            f"push to '{target}' ({url}) failed — check auth/network/permissions."
        )
        actions.append(
            _action(
                "repair sync",
                tool="watercooler_sync_repair",
                instruction="Run watercooler_sync_repair to diagnose and retry the push.",
            )
        )
        return "failed", "auth_failed", warnings, actions

    # Pushing to a bare URL leaves no named remote, so no remote-tracking ref is
    # recorded and durable team sync is not configured — surface that honestly
    # rather than claim "synced" (which a later health check would contradict).
    if not _is_named_remote(code_root, target):
        warnings.append(
            f"pushed to {target}, but it is not a configured named remote, so future "
            f"syncs won't continue automatically — add one with "
            f"`git remote add origin {target}` for ongoing team sync."
        )
    return "pushed", None, warnings, actions


def _init_impl(
    ctx: Context,
    code_path: str = "",
    push: bool = False,
    remote: Optional[str] = None,
    confirm_public: bool = False,
    allow_local_only: bool = False,
    force: bool = False,
) -> str:
    """Initialize watercooler in a repository and report readiness (JSON).

    Relay the ``summary`` field to the user verbatim — it is one plain-language
    sentence answering "did setup work?". Do NOT dump the raw JSON at the user;
    use the structured fields (``usable_now``, ``sync_status``, ``next_actions``)
    to decide follow-ups.

    Mutating (L2): scaffolds ``.watercooler/roles.toml``, binds the threads
    worktree, and — only when ``push=true`` and the public-remote gate is
    satisfied — publishes the threads branch. Never auto-invoke; call only after
    the user asks to set up / initialize watercooler.

    Args:
        code_path: Required. The project repo root (the directory with its .git).
            Not inferred from the server's working directory.
        push: Opt-in publish of the threads branch. Default False binds locally
            only and reports the resolved remote so the user can opt in.
        remote: Explicit remote name or URL to *target* for the push. This only
            chooses the destination — it does NOT bypass the consent gate.
        confirm_public: Required to publish. Affirms the remote is one your team
            should see; the remote's public/private visibility can't be
            auto-detected, so every push needs this regardless of remote=.
        allow_local_only: Solo use — silences the "unsynced" notice in the summary.
        force: Re-scaffold roles.toml even if present (backs up the old file).

    Returns:
        JSON readiness report (see ``summary``, ``usable_now``,
        ``roles_customizable``, ``sync_status``, ``push_attempt``,
        ``next_actions``, ``details``).
    """
    resolution = resolve_repo(code_path)
    if resolution.needs_code_path:
        return json.dumps(needs_code_path_report("watercooler_init"), indent=2)

    transport, mode, local_applies = deployment_context()
    if not local_applies:
        return json.dumps(hosted_na_report(transport, mode), indent=2)

    if not resolution.is_git:
        return json.dumps(non_git_report(resolution), indent=2)
    if resolution.ambiguous:
        return json.dumps(ambiguous_report(resolution), indent=2)

    root = resolution.resolved_code_root
    assert root is not None  # is_git guarantees this

    # 1. Scaffold the editable roles override (create-only; backup on force).
    roles_result = scaffold_roles_file(root, force=force)
    roles_status = roles_result.status
    roles_backup = str(roles_result.backup_path) if roles_result.backup_path else None

    # 2. Bind the worktree LOCALLY — never publish at bind time (push is opt-in).
    existed_before = worktree_present(root)
    wt = _ensure_worktree(root, push=False)
    if wt is None:
        worktree_status = "failed"
    elif existed_before:
        worktree_status = "exists"
    else:
        worktree_status = "created"

    extra_warnings: List[str] = []
    extra_next_actions: List[Dict[str, Any]] = []
    push_attempt = "not_requested"
    sync_override: Optional[str] = None

    # 3. Secret-safety: ensure credentials.toml is gitignored (file-specific).
    cred_state = _ensure_credentials_gitignored(root)
    cred_tracked = _credentials_tracked(root)
    if cred_tracked:
        extra_warnings.append(
            "⚠️ .watercooler/credentials.toml is already tracked by git — "
            "gitignoring it now has NO effect. Run "
            "`git rm --cached .watercooler/credentials.toml` before pushing, or "
            "your API keys/tokens will be published."
        )
        extra_next_actions.append(
            _action(
                "untrack credentials before pushing",
                tool=None,
                instruction=(
                    "git rm --cached .watercooler/credentials.toml "
                    "(keeps the local file, stops tracking it), then commit."
                ),
            )
        )

    # 4. Opt-in publish with the conservative public-remote gate.
    if push:
        if worktree_status == "failed":
            push_attempt = "failed"
            extra_warnings.append("cannot push: the threads worktree is not bound.")
        else:
            push_attempt, sync_override, pw, pa = _do_push(
                root, remote=remote, confirm_public=confirm_public
            )
            extra_warnings.extend(pw)
            extra_next_actions.extend(pa)

    actions = InitActions(
        worktree=worktree_status,
        roles_file=roles_status,
        roles_backup=roles_backup,
        push_attempt=push_attempt,
        allow_local_only=allow_local_only,
        extra_warnings=extra_warnings,
        extra_next_actions=extra_next_actions,
        sync_status_override=sync_override,
    )
    report = build_setup_report(resolution, actions=actions)
    report["details"]["credentials_gitignored"] = cred_state
    report["details"]["credentials_tracked"] = cred_tracked
    if worktree_status == "failed":
        report["warnings"].insert(
            0,
            "the threads worktree could not be bound — threads will not persist; "
            "run watercooler_health detail=setup to diagnose.",
        )
    return json.dumps(report, indent=2)


def register_setup_tools(mcp) -> None:
    """Register setup tools with the MCP server."""
    mcp.tool(name="watercooler_init")(_init_impl)
