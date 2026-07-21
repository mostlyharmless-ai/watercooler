from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from importlib import metadata as importlib_metadata  # type: ignore
except ImportError:  # pragma: no cover - Python <3.8 fallback
    import importlib_metadata  # type: ignore

from watercooler.agents import _canonical_agent, _load_agents_registry
from watercooler.lock import AdvisoryLock

from .observability import log_debug, log_warning

# Import shared git discovery and path helpers from path_resolver (consolidates logic)
from watercooler.path_resolver import (
    discover_git_info as _discover_git_shared,
    _expand_path,
    _resolve_path,
    _extract_repo_path,
)

__all__ = [
    "ThreadContext",
    "resolve_thread_context",
    "get_threads_dir",
    "get_threads_dir_for",
    "get_code_context",
    "get_agent_name",
    "get_version",
    "get_slack_config",
    "is_slack_enabled",
]


ORPHAN_BRANCH_NAME = "watercooler/threads"
WORKTREE_BASE = Path("~/.watercooler/worktrees").expanduser()


@dataclass(frozen=True)
class ThreadContext:
    """Resolved configuration for operating on watercooler threads."""

    code_root: Optional[Path]
    threads_dir: Path
    code_repo: Optional[str]
    code_branch: Optional[str]
    code_commit: Optional[str]
    code_remote: Optional[str]
    explicit_dir: bool


@dataclass(frozen=True)
class _GitDetails:
    root: Optional[Path]
    branch: Optional[str]
    commit: Optional[str]
    remote: Optional[str]


def _normalize_code_root(code_root: Optional[Path]) -> Optional[Path]:
    if code_root is None:
        return None
    if not isinstance(code_root, Path):
        code_root = Path(code_root)
    try:
        code_root = code_root.expanduser()
    except Exception:
        pass
    return _resolve_path(code_root)


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    cmd = " ".join(args)
    log_debug(f"CONFIG_GIT_START: git {cmd} (cwd={cwd})")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            # Prevent git from inheriting open file descriptors (sockets,
            # pipes) from the MCP server process. On Windows this is
            # critical — without it, subprocess.run hangs forever because
            # git inherits Claude Code's stdin pipe. On POSIX, Python
            # defaults close_fds=True but we set it explicitly to be safe.
            close_fds=True,
        )
        log_debug(f"CONFIG_GIT_END: git {cmd} (returned {len(result.stdout)} chars)")
        return result.stdout.strip()
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as e:
        log_debug(f"CONFIG_GIT_FAIL: git {cmd} (error: {type(e).__name__})")
        return None


def _discover_git(code_root: Optional[Path]) -> _GitDetails:
    """Discover git repository info using shared path_resolver.

    Delegates to watercooler.path_resolver.discover_git_info to consolidate
    git discovery logic and eliminate duplication.
    """
    log_debug(f"CONFIG: Discovering git info for {code_root}")

    # Use shared git discovery from path_resolver
    git_info = _discover_git_shared(code_root)

    log_debug(
        f"CONFIG: Git discovery complete (root={git_info.root}, branch={git_info.branch})"
    )

    return _GitDetails(
        root=git_info.root,
        branch=git_info.branch,
        commit=git_info.commit,
        remote=git_info.remote,
    )


def _worktree_path_for(code_root: Path) -> Path:
    """Compute the worktree path for a code repo."""
    return WORKTREE_BASE / code_root.name


def _orphan_branch_exists(code_root: Path) -> bool:
    """Check if the orphan branch exists (local or remote)."""
    result = _run_git(["branch", "-a", "--list", f"*{ORPHAN_BRANCH_NAME}*"], code_root)
    return bool(result and ORPHAN_BRANCH_NAME in result)


def _select_push_remote(code_root: Path) -> Optional[str]:
    """Pick the remote to publish the orphan branch to.

    Prefers ``origin``. With no ``origin`` a lone remote is unambiguous and
    is used; multiple non-``origin`` remotes are ambiguous, so None is
    returned (the caller proceeds local-only rather than guessing). None is
    also returned when the repo has no remotes at all.
    """
    remotes_out = _run_git(["remote"], code_root)
    remotes = [r.strip() for r in (remotes_out or "").splitlines() if r.strip()]
    if not remotes:
        return None
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    log_warning(
        f"CONFIG: no 'origin' remote and {len(remotes)} remotes configured "
        f"({', '.join(remotes)}) — cannot pick a push target unambiguously; "
        f"orphan branch will be created local-only"
    )
    return None


def _create_orphan_branch(code_root: Path, push: bool = True) -> bool:
    """Create the orphan branch and bind its worktree.

    Returns True only when the worktree is a valid git worktree on the
    orphan branch. On a worktree/branch/commit failure the half-created
    scaffold is rolled back and False is returned — callers never observe
    a scaffold-only directory. A failed *push* is non-fatal (the worktree
    is valid locally and entries sync on a later push).

    Args:
        push: When False, bind the worktree locally but skip publishing the
            orphan branch to a remote. ``watercooler_init`` passes ``push=False``
            so a brand-new repo with a (possibly public) ``origin`` is never
            published without explicit, gated opt-in. The default ``True``
            preserves the auto-publish behavior of the first-write path.
    """
    log_debug(f"CONFIG: Creating orphan branch '{ORPHAN_BRANCH_NAME}' in {code_root}")
    wt_path = _worktree_path_for(code_root)

    def _rollback(reason: str) -> bool:
        log_warning(
            f"CONFIG: orphan-branch bootstrap failed — {reason}; "
            f"rolling back {wt_path}"
        )
        _run_git(["worktree", "remove", "--force", str(wt_path)], code_root)
        _run_git(["worktree", "prune"], code_root)
        shutil.rmtree(wt_path, ignore_errors=True)
        return False

    # `git worktree add` refuses a non-empty path. A stale scaffold from a
    # prior failed bootstrap (a directory tree with no .git) is the common
    # trigger for that refusal — clear it so `worktree add` starts clean.
    if wt_path.exists() and not (wt_path / ".git").exists():
        log_debug(f"CONFIG: clearing stale scaffold at {wt_path} before worktree add")
        shutil.rmtree(wt_path, ignore_errors=True)

    # Create the orphan branch + worktree. Prefer `worktree add --orphan`
    # (git >= 2.42); fall back to a detached worktree + `checkout --orphan`.
    created = _run_git(
        ["worktree", "add", "--orphan", "-b", ORPHAN_BRANCH_NAME, str(wt_path)],
        code_root,
    )
    if created is None:
        log_debug(
            "CONFIG: `worktree add --orphan` unavailable — using detached fallback"
        )
        shutil.rmtree(wt_path, ignore_errors=True)  # ensure a clean path
        if _run_git(["worktree", "add", "--detach", str(wt_path)], code_root) is None:
            return _rollback("could not create the worktree")
        if _run_git(["checkout", "--orphan", ORPHAN_BRANCH_NAME], wt_path) is None:
            return _rollback("could not create the orphan branch")
        _run_git(["rm", "-rf", "."], wt_path)

    # The worktree must actually be bound before we continue.
    if not (wt_path / ".git").exists():
        return _rollback("worktree directory was not bound to git")

    if (
        _run_git(
            ["commit", "--allow-empty", "-m", "Initialize watercooler threads"], wt_path
        )
        is None
    ):
        return _rollback("initial commit failed")

    # Structured directory layout for new repos, with .gitkeep placeholders.
    from watercooler.fs import ensure_directory_structure

    for d in ensure_directory_structure(wt_path):
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
    _run_git(["add", "."], wt_path)
    _run_git(["commit", "-m", "Add structured directory layout"], wt_path)
    log_debug(f"CONFIG: Created structured directory layout in {wt_path}")

    # Publish the orphan branch. Resolve the remote explicitly — repos
    # forked/migrated between hosts commonly have origin + upstream, and a
    # bare push is ambiguous. A failed push is non-fatal: the worktree is
    # valid locally and entries sync on a later push.
    if not push:
        log_debug(
            f"CONFIG: orphan branch '{ORPHAN_BRANCH_NAME}' created local-only "
            f"in {wt_path} — push suppressed (opt-in deferred to caller)"
        )
        return True
    remote = _select_push_remote(code_root)
    if remote is None:
        log_warning(
            f"CONFIG: orphan branch '{ORPHAN_BRANCH_NAME}' created local-only "
            f"in {wt_path} — no usable push remote"
        )
    elif _run_git(["push", "-u", remote, ORPHAN_BRANCH_NAME], wt_path) is None:
        log_warning(
            f"CONFIG: orphan branch '{ORPHAN_BRANCH_NAME}' created but the push "
            f"to '{remote}' failed — check auth/network/permissions for that "
            f"remote; entries will sync on a later push"
        )
    else:
        log_debug(f"CONFIG: orphan branch '{ORPHAN_BRANCH_NAME}' pushed to '{remote}'")

    log_debug(f"CONFIG: Orphan branch '{ORPHAN_BRANCH_NAME}' created")
    return True


def _find_existing_worktree_on_branch(
    code_root: Path,
    branch: str,
) -> Optional[Path]:
    """Return the path of an existing worktree on ``branch`` for ``code_root``,
    or None.

    Parses ``git worktree list --porcelain`` to find checkouts of ``branch``.
    Returns the first matching worktree path that is NOT ``code_root`` itself.

    Used by :func:`_ensure_worktree` to detect an orphan-branch worktree
    checked out at an unexpected (non-canonical) location.
    """
    out = _run_git(["worktree", "list", "--porcelain"], code_root)
    if not out:
        return None
    current_path: Optional[Path] = None
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and current_path is not None:
            # `branch` value is in the form `refs/heads/<name>`
            branch_name = line[len("branch ") :]
            if branch_name == f"refs/heads/{branch}":
                # Don't return the main worktree (code_root itself)
                try:
                    if current_path.resolve() != code_root.resolve():
                        return current_path
                except OSError:
                    pass
    return None


# Per-repo locks serializing orphan-worktree CREATION. One MCP server serves
# many repos concurrently (daemons + tool calls run in threads); without this,
# two concurrent first-writes to the SAME repo race in `git worktree add` /
# orphan-branch bootstrap — one fails, and the resolver falls back to
# `<repo>/_local` (the spurious local-only fallback operators hit in concurrent
# multi-repo use). Keyed by the canonical worktree path, so different repos take
# different locks and never contend.
#
# Two layers: an in-process threading.Lock (cheap, serializes this server's own
# threads) plus a best-effort cross-process file lock (AdvisoryLock) so a write
# from ANOTHER process — e.g. a `watercooler` CLI run alongside the server —
# can't race first-creation of the same repo either.
_WORKTREE_CREATE_LOCKS: Dict[str, threading.Lock] = {}
_WORKTREE_CREATE_LOCKS_GUARD = threading.Lock()

# Creation is a few git ops; a crashed creator's stale lock clears via TTL.
_WORKTREE_CREATE_LOCK_TTL_SECONDS = 120
_WORKTREE_CREATE_LOCK_TIMEOUT_SECONDS = 30


def _worktree_create_lock(code_root: Path) -> threading.Lock:
    """Return the in-process creation lock for ``code_root``'s worktree path."""
    key = str(_worktree_path_for(code_root))
    with _WORKTREE_CREATE_LOCKS_GUARD:
        lock = _WORKTREE_CREATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WORKTREE_CREATE_LOCKS[key] = lock
        return lock


def _acquire_create_filelock(wt_path: Path) -> Optional[AdvisoryLock]:
    """Best-effort cross-process lock around worktree creation.

    The lock file sits beside the worktree under ``WORKTREE_BASE`` (the worktree
    dir itself doesn't exist yet during creation). Returns the held lock, or
    ``None`` if it couldn't be acquired.

    NEVER raises and never blocks resolution indefinitely: on timeout, stale
    break failure, or any error it returns ``None`` and creation proceeds —
    git's own atomicity and the in-lock existence double-check still guard the
    common cases. Lock infrastructure must never turn a resolvable repo into a
    ``_local`` fallback.
    """
    try:
        lock_path = wt_path.parent / f"{wt_path.name}.create.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = AdvisoryLock(
            lock_path,
            ttl=_WORKTREE_CREATE_LOCK_TTL_SECONDS,
            timeout=_WORKTREE_CREATE_LOCK_TIMEOUT_SECONDS,
        )
        if lock.acquire():
            return lock
        log_debug(
            f"CONFIG: worktree-create file lock not acquired for {wt_path} "
            f"(another process creating?); proceeding best-effort"
        )
        return None
    except Exception as e:  # never let lock infra block resolution
        log_debug(
            f"CONFIG: worktree-create file lock unavailable ({e}); "
            f"proceeding best-effort"
        )
        return None


def _ensure_worktree(code_root: Path, push: bool = True) -> Optional[Path]:
    """Ensure the orphan branch worktree exists, creating it if needed.

    Args:
        push: Forwarded to :func:`_create_orphan_branch` on first creation.
            ``False`` binds the worktree locally without publishing to a
            remote (used by ``watercooler_init``); the default ``True``
            preserves the first-write auto-publish behavior.

    Returns:
        Path to the worktree directory, or None if creation failed.

    Creation is serialized per repo so two concurrent first-writes to the same
    repo can't race ``git worktree add`` into a failure that degrades to
    ``<repo>/_local``: an in-process lock (``_worktree_create_lock``) serializes
    this server's threads, and a best-effort cross-process file lock
    (``_acquire_create_filelock``) serializes against other processes (e.g. a
    CLI write). The double-check inside the lock returns the worktree a sibling
    just created. The create locks are fully released before the write path
    acquires its own worktree lock — no lock-ordering hazard.
    """
    wt_path = _worktree_path_for(code_root)
    with _worktree_create_lock(code_root):
        fs_lock = _acquire_create_filelock(wt_path)
        try:
            return _ensure_worktree_locked(code_root, wt_path, push=push)
        finally:
            if fs_lock is not None:
                fs_lock.release()


def _ensure_worktree_locked(
    code_root: Path, wt_path: Path, push: bool = True
) -> Optional[Path]:
    """Worktree existence-check + creation, run under the per-repo lock."""
    # Check if worktree already exists and is valid
    if wt_path.exists() and (wt_path / ".git").exists():
        # Verify it's on the right branch
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt_path)
        if branch == ORPHAN_BRANCH_NAME:
            return wt_path
        # Worktree exists but wrong branch — remove and recreate
        log_debug(
            f"CONFIG: Worktree at {wt_path} on wrong branch '{branch}', recreating"
        )
        _run_git(["worktree", "remove", "--force", str(wt_path)], code_root)

    # Prune stale worktree registrations before any further git ops.
    # Git refuses to create a worktree at a path that is "missing but
    # already registered" (e.g. directory was deleted externally).
    prune_result = _run_git(["worktree", "prune"], code_root)
    if prune_result is None:
        log_debug("CONFIG: git worktree prune failed (permissions or old git version)")

    # ALWAYS check for an existing worktree on the orphan branch BEFORE
    # gating on _orphan_branch_exists. Two reasons (per PR #838 review):
    #
    # 1. On git >= 2.42, `worktree add --orphan` creates an UNBORN branch
    #    (no initial commit). `git branch --list` returns empty for unborn
    #    branches, so _orphan_branch_exists() falsely reports "no branch"
    #    — but `worktree list --porcelain` DOES report the worktree on
    #    `refs/heads/watercooler/threads`. Probing worktrees first detects
    #    the unborn-branch case correctly.
    # 2. Architecturally: an existing worktree on the branch is the
    #    higher-priority signal. If one exists, we must decide migrate /
    #    refuse / use-as-is BEFORE attempting create-or-add.
    existing = _find_existing_worktree_on_branch(code_root, ORPHAN_BRANCH_NAME)
    if existing is not None:
        # The orphan branch is checked out somewhere other than the
        # canonical path (the canonical fast-path above already returned).
        # Operator-attention case: don't silently fall back; the resolver's
        # Bug-C structural guard produces a warning-loud in-repo fallback
        # when this returns None.
        log_warning(
            f"CONFIG: orphan branch '{ORPHAN_BRANCH_NAME}' already "
            f"checked out at {existing} (expected canonical "
            f"{wt_path}). Refusing to silently fall back; manually "
            f"`git worktree move` or `git worktree remove` the "
            f"unexpected checkout."
        )
        return None

    # No existing worktree on the branch. Check if the branch itself
    # exists (as a born ref). If not, bootstrap it.
    if not _orphan_branch_exists(code_root):
        try:
            _create_orphan_branch(code_root, push=push)
        except Exception as e:
            log_debug(f"CONFIG: Failed to create orphan branch: {e}")
            return None
    else:
        # Branch exists as a born ref but no worktree on it — add one
        # at the canonical path.
        wt_path.mkdir(parents=True, exist_ok=True)
        result = _run_git(
            ["worktree", "add", str(wt_path), ORPHAN_BRANCH_NAME],
            code_root,
        )
        if result is None:
            log_debug(f"CONFIG: Failed to create worktree at {wt_path}")
            return None

    if wt_path.exists() and (wt_path / ".git").exists():
        return wt_path

    return None


def _enforce_threads_dir_safety(threads_dir: Path, effective_root: Path) -> Path:
    """Defense-in-depth: refuse a ``threads_dir`` outside the allowed set.

    The allowed set for an ``effective_root``:

      - The canonical orphan worktree at ``WORKTREE_BASE / effective_root.name``
      - The in-repo legacy fallback at ``effective_root / "_local"``

    Anything else means a resolution regression silently landed in an
    unrelated location (e.g. a different repo's tree, ``Path.cwd()`` from
    elsewhere). Override to the in-repo legacy fallback and log loudly so
    operators see the regression. Per GH #837.

    Idempotent — safe to call on a known-good ``threads_dir``.
    """
    canonical = _worktree_path_for(effective_root)
    legacy_local = _resolve_path(effective_root / "_local")
    try:
        allowed_canonical = threads_dir.resolve() == canonical.resolve()
    except OSError:
        allowed_canonical = False
    try:
        allowed_legacy = threads_dir.resolve() == legacy_local.resolve()
    except OSError:
        allowed_legacy = False
    if allowed_canonical or allowed_legacy:
        return threads_dir
    log_warning(
        f"CONFIG: threads_dir resolved to {threads_dir} which is "
        f"NEITHER the canonical worktree ({canonical}) nor the "
        f"in-repo legacy fallback ({legacy_local}) for effective_root "
        f"{effective_root}. This indicates a cross-repo resolution "
        f"regression; overriding to {legacy_local} as a safe default."
    )
    return legacy_local


def resolve_thread_context(code_root: Optional[Path] = None) -> ThreadContext:
    normalized_root = _normalize_code_root(code_root)
    git_details = _discover_git(normalized_root)

    explicit_dir_env = os.getenv("WATERCOOLER_DIR")
    if explicit_dir_env:
        threads_dir = _resolve_path(_expand_path(explicit_dir_env))
    else:
        threads_dir = None

    code_repo_env = os.getenv("WATERCOOLER_CODE_REPO")

    code_remote = git_details.remote
    code_repo = code_repo_env or None

    if code_repo is None and code_remote:
        repo_path = _extract_repo_path(code_remote)
        if repo_path:
            parts = [p for p in repo_path.split("/") if p]
            if parts:
                code_repo = "/".join(parts)

    effective_root = git_details.root or normalized_root

    # =========================================================================
    # Explicit directory override (WATERCOOLER_DIR)
    # =========================================================================
    if explicit_dir_env and threads_dir is not None:
        return ThreadContext(
            code_root=effective_root,
            threads_dir=threads_dir,
            code_repo=code_repo,
            code_branch=git_details.branch,
            code_commit=git_details.commit,
            code_remote=code_remote,
            explicit_dir=True,
        )

    # =========================================================================
    # Orphan Branch Worktree (the architecture)
    # =========================================================================
    if effective_root is not None:
        wt_dir = _ensure_worktree(effective_root)
        if wt_dir is not None:
            log_debug(f"CONFIG: Orphan worktree active, threads_dir={wt_dir}")
            return ThreadContext(
                code_root=effective_root,
                threads_dir=wt_dir,
                code_repo=code_repo,
                code_branch=git_details.branch,
                code_commit=git_details.commit,
                code_remote=code_remote,
                explicit_dir=False,
            )
        else:
            # Worktree creation failed — fall back to <effective_root>/_local.
            # Bug B fix (GH #837): previously this used Path.cwd() which is
            # the MCP server's CWD, not the queried repo, causing writes to
            # land in a DIFFERENT repository's _local. Always use
            # effective_root so writes stay inside the repo they were meant
            # for.
            fallback_local = _resolve_path(effective_root / "_local")
            if git_details.root is not None:
                # effective_root IS a git repo whose orphan worktree could not be
                # created (auth / permissions / lock). This is a genuine degraded
                # write target — writes to THIS repo really won't sync — so warn
                # loudly and surface a startup notice.
                log_warning(
                    f"CONFIG: Orphan worktree creation failed for "
                    f"{effective_root}; threads will be stored at {fallback_local}. "
                    f"New writes will NOT be synced to the remote until the "
                    f"orphan branch is configured. Check git permissions and retry."
                )
                from .helpers import _add_startup_warning

                _add_startup_warning(
                    f"Worktree creation failed for {effective_root} — threads "
                    f"will be stored at {fallback_local} (local-only). New writes "
                    f"will NOT be synced to the remote."
                )
            else:
                # effective_root is NOT a git repository — e.g. a daemon resolving
                # a default/"primary" threads dir from the server's launch CWD when
                # that CWD is a parent directory holding several cloned repos. There
                # is no repo to back threads at this path, but this is NOT a write
                # target: every real write carries its own code_path and resolves to
                # WORKTREE_BASE/<repo> correctly. The old "writes will NOT be synced"
                # startup notice was therefore false and alarming here — downgrade to
                # a debug log. No _local directory is created unless something
                # actually writes through this context.
                log_debug(
                    f"CONFIG: {effective_root} is not a git repository — no default "
                    f"threads context here; real writes resolve per code_path "
                    f"(WORKTREE_BASE/<repo>). Not a sync failure."
                )
            threads_dir = fallback_local

    # =========================================================================
    # Fallback: no git context (or worktree failed AND effective_root is None)
    # =========================================================================
    if threads_dir is None:
        # effective_root is None here (no git context at all) — fall back
        # to MCP-server CWD's _local as a last-resort. This path should
        # rarely fire in practice; the typical "no git context" case is a
        # tool invoked from outside any repo with no code_path set.
        base = Path.cwd()
        threads_dir = _resolve_path(base / "_local")
        log_warning(
            f"CONFIG: no git context AND no effective_root — falling back to "
            f"Path.cwd() / _local = {threads_dir}. This is unusual; expected "
            f"callers to pass code_path or be inside a git repo."
        )

    # Bug C — structural guard: never leak writes across repos.
    # See _enforce_threads_dir_safety. Per GH #837 defense-in-depth.
    if effective_root is not None:
        threads_dir = _enforce_threads_dir_safety(threads_dir, effective_root)

    return ThreadContext(
        code_root=effective_root,
        threads_dir=threads_dir,
        code_repo=code_repo,
        code_branch=git_details.branch,
        code_commit=git_details.commit,
        code_remote=code_remote,
        explicit_dir=False,
    )


def get_threads_dir() -> Path:
    return resolve_thread_context().threads_dir


def get_threads_dir_for(code_root: Optional[Path]) -> Path:
    return resolve_thread_context(code_root).threads_dir


def get_code_context(code_root: Optional[Path]) -> Dict[str, str]:
    ctx = resolve_thread_context(code_root)
    return {
        "code_root": str(ctx.code_root) if ctx.code_root else "",
        "code_repo": ctx.code_repo or "",
        "code_branch": ctx.code_branch or "",
        "code_commit": ctx.code_commit or "",
        "threads_dir": str(ctx.threads_dir),
    }


def get_agent_name(client_id: Optional[str] = None) -> str:
    agent_env = os.getenv("WATERCOOLER_AGENT")
    if agent_env:
        base_agent = agent_env
    else:
        base_agent = _infer_agent_from_client(client_id)
    registry_path = os.getenv("WATERCOOLER_AGENT_REGISTRY")
    registry = _load_agents_registry(registry_path)
    explicit_tag = os.getenv("WATERCOOLER_AGENT_TAG")
    return _canonical_agent(base_agent, registry, user_tag=explicit_tag)


def _infer_agent_from_client(client_id: Optional[str]) -> str:
    if not client_id:
        return "Agent"
    lowered = client_id.strip().lower()
    if not lowered:
        return "Agent"
    if lowered.startswith("claude"):
        return "Claude"
    if lowered.startswith("codex"):
        return "Codex"
    if lowered.startswith("gpt"):
        return "GPT"
    return client_id.split()[0]


def get_version() -> str:
    for dist_name in ("watercooler", "watercooler-mcp"):
        try:
            return importlib_metadata.version(dist_name)
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:
            break
    return os.getenv("WATERCOOLER_MCP_VERSION", "0.0.0")


# =============================================================================
# Config System Integration (TOML-based configuration)
# =============================================================================

# Lazy-loaded config to avoid import-time file I/O
_loaded_config: Optional["WatercoolerConfig"] = None


def get_watercooler_config(project_path: Optional[Path] = None) -> "WatercoolerConfig":
    """Get the loaded Watercooler configuration.

    Lazy-loads config from TOML files on first access.
    Uses cached config for subsequent calls.

    Args:
        project_path: Project directory for config discovery

    Returns:
        WatercoolerConfig instance
    """
    global _loaded_config

    if _loaded_config is None:
        try:
            from watercooler.config_loader import load_config

            _loaded_config = load_config(project_path)
        except ImportError:
            # Config system not available, use defaults
            from watercooler.config_schema import WatercoolerConfig

            _loaded_config = WatercoolerConfig()
        except Exception as e:
            # Promoted from log_debug per bug-falkordb-startup-gate-hybrid-2026-05-12
            # entry 01KRDMK58S59A2WRPHCBY46XPS: a silent fallback to schema
            # defaults can flip transport from an operator's explicit value to
            # the proxy default in transient subprocesses, bypassing the FalkorDB
            # hybrid skip in startup.ensure_falkordb_running (the proxy default
            # itself falls back to local stdio without hosted credentials). Make
            # this visible at the operator's default log level so
            # config-resolution failures are diagnosable.
            log_warning(
                f"CONFIG: load_config failed; falling back to schema defaults "
                f"(transport=proxy, capability_routes={{}}). This may cause "
                f"unwanted local-service auto-start if operator intent was "
                f"hybrid or an authenticated proxy. Underlying error: "
                f"{type(e).__name__}: {e}"
            )
            from watercooler.config_schema import WatercoolerConfig

            _loaded_config = WatercoolerConfig()

    return _loaded_config


def reload_config(project_path: Optional[Path] = None) -> "WatercoolerConfig":
    """Force reload of configuration from disk.

    Args:
        project_path: Project directory for config discovery

    Returns:
        Freshly loaded WatercoolerConfig instance
    """
    global _loaded_config
    _loaded_config = None
    return get_watercooler_config(project_path)


def get_mcp_transport_config() -> Dict[str, Any]:
    """Get MCP transport configuration.

    Returns dict with keys: transport, host, port
    Environment variables override config file values.
    """
    config = get_watercooler_config()

    return {
        "transport": os.getenv("WATERCOOLER_MCP_TRANSPORT", config.mcp.transport),
        "host": os.getenv("WATERCOOLER_MCP_HOST", config.mcp.host),
        "port": int(os.getenv("WATERCOOLER_MCP_PORT", str(config.mcp.port))),
        "url": os.getenv("WATERCOOLER_MCP_URL", config.mcp.url),
        "proxy_repo": os.getenv("WATERCOOLER_CODE_REPO", config.mcp.proxy_repo),
        "proxy_branch": os.getenv("WATERCOOLER_CODE_BRANCH", config.mcp.proxy_branch),
        "capability_routes": config.mcp.capability_routes,
    }


def effective_transport(transport: str, url: str, api_key: str) -> str:
    """Resolve the transport that will actually run.

    ``proxy`` has no local fallback of its own — it forwards to a remote
    endpoint and needs both a URL and an API key. Without either, the runtime
    (``server._resolve_effective_transport``) and the setup report treat it as
    local ``stdio``. All other transports pass through unchanged.

    Args:
        transport: The configured transport.
        url: The remote endpoint URL (may be empty).
        api_key: The hosted API key (empty when not authenticated).

    Returns:
        ``"stdio"`` for a credential-less proxy, else ``transport`` unchanged.
    """
    if transport == "proxy" and not (url and api_key):
        return "stdio"
    return transport


def get_sync_config() -> Dict[str, Any]:
    """Get git sync configuration.

    Returns dict with sync settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    sync = config.mcp.sync

    def _get_float(env_key: str, default: float) -> float:
        val = os.getenv(env_key)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return default

    def _get_int(env_key: str, default: int) -> int:
        val = os.getenv(env_key)
        if val:
            try:
                return int(val)
            except ValueError:
                pass
        return default

    def _get_bool(env_key: str, default: bool) -> bool:
        val = os.getenv(env_key)
        if val:
            return val.lower() in ("1", "true", "yes", "on")
        return default

    return {
        "async_sync": _get_bool("WATERCOOLER_ASYNC_SYNC", sync.async_sync),
        "batch_window": _get_float("WATERCOOLER_BATCH_WINDOW", sync.batch_window),
        "max_delay": sync.max_delay,
        "max_batch_size": sync.max_batch_size,
        "max_retries": _get_int("WATERCOOLER_SYNC_MAX_RETRIES", sync.max_retries),
        "max_backoff": _get_float("WATERCOOLER_SYNC_MAX_BACKOFF", sync.max_backoff),
        "interval": _get_float("WATERCOOLER_SYNC_INTERVAL", sync.interval),
        "stale_threshold": sync.stale_threshold,
    }


def get_logging_config() -> Dict[str, Any]:
    """Get logging configuration.

    Returns dict with logging settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    logging = config.mcp.logging

    return {
        "level": os.getenv("WATERCOOLER_LOG_LEVEL", logging.level),
        "dir": os.getenv("WATERCOOLER_LOG_DIR", logging.dir) or None,
        "max_bytes": int(
            os.getenv("WATERCOOLER_LOG_MAX_BYTES", str(logging.max_bytes))
        ),
        "backup_count": int(
            os.getenv("WATERCOOLER_LOG_BACKUP_COUNT", str(logging.backup_count))
        ),
        "disable_file": os.getenv("WATERCOOLER_LOG_DISABLE_FILE", "").lower()
        in ("1", "true", "yes")
        or logging.disable_file,
    }


def get_agent_for_platform(platform_slug: Optional[str] = None) -> Dict[str, str]:
    """Get agent configuration for a platform.

    Args:
        platform_slug: Platform identifier (e.g., "claude-code", "cursor")

    Returns:
        Dict with name and default_spec for the agent
    """
    config = get_watercooler_config()

    if platform_slug:
        agent_config = config.get_agent_config(platform_slug)
        if agent_config:
            return {
                "name": agent_config.name,
                "default_spec": agent_config.default_spec,
            }

    return {
        "name": config.mcp.default_agent,
        "default_spec": "general-purpose",
    }


def get_slack_config() -> Dict[str, Any]:
    """Get Slack integration configuration.

    Returns dict with Slack settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    slack = config.mcp.slack

    def _get_bool(env_key: str, default: bool) -> bool:
        val = os.getenv(env_key)
        if val:
            return val.lower() in ("1", "true", "yes", "on")
        return default

    def _get_float(env_key: str, default: float) -> float:
        val = os.getenv(env_key)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return default

    return {
        "webhook_url": os.getenv("WATERCOOLER_SLACK_WEBHOOK", slack.webhook_url),
        "bot_token": os.getenv("WATERCOOLER_SLACK_BOT_TOKEN", slack.bot_token),
        "app_token": os.getenv("WATERCOOLER_SLACK_APP_TOKEN", slack.app_token),
        "default_channel": os.getenv(
            "WATERCOOLER_SLACK_CHANNEL", slack.default_channel
        ),
        # Phase 2+ config
        "channel_prefix": os.getenv(
            "WATERCOOLER_SLACK_CHANNEL_PREFIX", slack.channel_prefix
        ),
        "auto_create_channels": _get_bool(
            "WATERCOOLER_SLACK_AUTO_CREATE_CHANNELS", slack.auto_create_channels
        ),
        # Notification toggles
        "notify_on_say": _get_bool("WATERCOOLER_SLACK_NOTIFY_SAY", slack.notify_on_say),
        "notify_on_ball_flip": _get_bool(
            "WATERCOOLER_SLACK_NOTIFY_BALL_FLIP", slack.notify_on_ball_flip
        ),
        "notify_on_status_change": _get_bool(
            "WATERCOOLER_SLACK_NOTIFY_STATUS", slack.notify_on_status_change
        ),
        "notify_on_handoff": _get_bool(
            "WATERCOOLER_SLACK_NOTIFY_HANDOFF", slack.notify_on_handoff
        ),
        "min_notification_interval": _get_float(
            "WATERCOOLER_SLACK_MIN_INTERVAL", slack.min_notification_interval
        ),
    }


def is_slack_enabled() -> bool:
    """Check if Slack notifications are enabled (webhook or bot token)."""
    slack_config = get_slack_config()
    return bool(slack_config.get("webhook_url")) or bool(slack_config.get("bot_token"))


def is_slack_bot_enabled() -> bool:
    """Check if Slack bot API is enabled (Phase 2+)."""
    slack_config = get_slack_config()
    return bool(slack_config.get("bot_token"))


# Type hint import (deferred to avoid circular imports)
if False:  # TYPE_CHECKING equivalent
    from watercooler.config_schema import WatercoolerConfig
