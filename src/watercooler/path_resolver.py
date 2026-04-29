"""Unified path resolution for threads and templates.

Consolidates git-aware path discovery logic used by both
the core library and MCP server. This eliminates duplication
between watercooler/config.py and watercooler_mcp/config.py.

Uses GitPython for git operations (avoids Windows subprocess stdio hangs).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# GitPython for in-process git operations (avoids Windows subprocess stdio hangs)
from git import Repo, InvalidGitRepositoryError, GitCommandError

WORKTREE_BASE = Path("~/.watercooler/worktrees").expanduser()


@dataclass(frozen=True)
class GitInfo:
    """Git repository information from subprocess git calls.

    Attributes:
        root: Repository root directory (resolved path)
        branch: Current branch name (None if detached HEAD)
        commit: Short commit hash (7 chars)
        remote: Origin remote URL
    """
    root: Optional[Path]
    branch: Optional[str]
    commit: Optional[str]
    remote: Optional[str]


def _expand_path(value: str) -> Path:
    """Expand environment variables and user home directory in path."""
    return Path(os.path.expanduser(os.path.expandvars(value)))


def _resolve_path(path: Path) -> Path:
    """Safely resolve path, handling errors gracefully."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return path


def discover_git_info(code_root: Optional[Path]) -> GitInfo:
    """Discover git repository information using GitPython.

    Consolidates logic from watercooler/config.py and watercooler_mcp/config.py.
    Uses GitPython library for in-process git operations, avoiding
    Windows subprocess stdio hanging issues.

    Args:
        code_root: Directory to search from (searches parent dirs)

    Returns:
        GitInfo with repository details (all None if not a git repo)
    """
    if code_root is None or not code_root.exists():
        return GitInfo(None, None, None, None)

    try:
        # Use GitPython to discover git info (no subprocess)
        repo = Repo(code_root, search_parent_directories=True)

        # Get repository root
        root = Path(repo.working_dir) if repo.working_dir else None

        # Get current branch (None if detached HEAD)
        try:
            branch = repo.active_branch.name
        except TypeError:
            # Detached HEAD state
            branch = None

        # Get short commit hash
        try:
            commit = repo.head.commit.hexsha[:7]
        except (ValueError, AttributeError):
            commit = None

        # Get origin remote URL
        try:
            remote_obj = repo.remote("origin")
            remote = next(iter(remote_obj.urls), None)
        except (ValueError, GitCommandError):
            remote = None

        return GitInfo(root=root, branch=branch, commit=commit, remote=remote)

    except InvalidGitRepositoryError:
        print(
            f"watercooler: '{code_root}' is not inside a git repository. "
            f"Thread resolution will fall back to a local directory. "
            f"To use watercooler, run from inside a git repo or set "
            f"WATERCOOLER_DIR explicitly.",
            file=sys.stderr,
        )
        return GitInfo(None, None, None, None)
    except GitCommandError as e:
        print(
            f"watercooler: git discovery failed for '{code_root}': {e}. "
            f"Thread resolution will fall back to a local directory.",
            file=sys.stderr,
        )
        return GitInfo(None, None, None, None)


def _default_threads_base(repo_root: Optional[Path]) -> Path:
    """Determine default base directory for threads repositories.

    Precedence:
    1. WATERCOOLER_THREADS_BASE env var
    2. Parent of repo_root (if available)
    3. Parent of current working directory

    Args:
        repo_root: Git repository root (if in a git repo)

    Returns:
        Resolved base directory path
    """
    base_env = os.getenv("WATERCOOLER_THREADS_BASE")
    if base_env:
        return _resolve_path(_expand_path(base_env))

    if repo_root is not None:
        try:
            parent = repo_root.parent
            if parent != repo_root:
                return _resolve_path(parent)
        except (OSError, RuntimeError, ValueError):
            pass

    try:
        cwd = Path.cwd().resolve()
        parent = cwd.parent if cwd.parent != cwd else cwd
        return _resolve_path(parent)
    except (OSError, RuntimeError, ValueError):
        # Fallback to current working directory if resolution fails
        return _resolve_path(Path.cwd())


def _strip_repo_suffix(value: str) -> str:
    """Strip .git suffix and trailing slashes from URL."""
    value = value.strip()
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def _extract_repo_path(remote: Optional[str]) -> Optional[str]:
    """Extract repository path from git remote URL.

    Handles various formats:
    - git@github.com:org/repo.git -> org/repo
    - https://github.com/org/repo.git -> org/repo
    - ssh://git@github.com/org/repo -> org/repo

    Args:
        remote: Git remote URL

    Returns:
        Extracted path (e.g., "org/repo") or None
    """
    if not remote:
        return None

    remote = _strip_repo_suffix(remote)

    # Handle git@ format (SSH)
    if remote.startswith("git@"):
        remote = remote.split(":", 1)[-1]
    # Handle URL format (https://, ssh://, etc.)
    elif "://" in remote:
        remote = remote.split("://", 1)[-1]
        if "/" in remote:
            remote = remote.split("/", 1)[-1]
        else:
            remote = ""

    remote = remote.lstrip("/")
    return remote or None


def _split_namespace_repo(slug: str) -> tuple[Optional[str], str]:
    """Split repository slug into namespace and repo name.

    Examples:
    - "repo" -> (None, "repo")
    - "org/repo" -> ("org", "repo")
    - "group/subgroup/repo" -> ("group/subgroup", "repo")

    Args:
        slug: Repository slug (e.g., "org/repo")

    Returns:
        Tuple of (namespace, repo_name)
    """
    parts = [p for p in slug.split("/") if p]
    if not parts:
        return None, slug
    if len(parts) == 1:
        return None, parts[0]
    namespace = "/".join(parts[:-1])
    return namespace, parts[-1]


# =============================================================================
# Unified Repo Name and group_id Derivation
# =============================================================================


def get_threads_suffix() -> str:
    """Get configured threads suffix, with fallback to default.

    Priority:
    1. WATERCOOLER_THREADS_SUFFIX env var
    2. config.common.threads_suffix from TOML
    3. Default: "-threads"

    Returns:
        The threads suffix string (e.g., "-threads")
    """
    env_suffix = os.getenv("WATERCOOLER_THREADS_SUFFIX")
    if env_suffix is not None:
        return env_suffix

    try:
        from .config_facade import config
        return config.full().common.threads_suffix
    except Exception:
        return "-threads"


def derive_code_repo_name(
    code_path: Optional[Path] = None,
    git_info: Optional[GitInfo] = None
) -> Optional[str]:
    """Derive code repository name from path or git info.

    Priority:
    1. Git repo root directory name (if in git repo)
    2. Resolved path basename (if path provided)
    3. None (if nothing available)

    Args:
        code_path: Path to the code repository
        git_info: Git repository information (from discover_git_info)

    Returns:
        Repository name (e.g., "watercooler-cloud") or None
    """
    if git_info and git_info.root:
        return git_info.root.name

    if code_path:
        path = Path(code_path) if isinstance(code_path, (str, Path)) else code_path
        try:
            return path.resolve().name
        except (OSError, RuntimeError):
            return path.name

    return None


def derive_threads_repo_name(
    code_repo_name: str,
    suffix: Optional[str] = None
) -> str:
    """Derive threads repository name from code repo name.

    Applies configured suffix (default: "-threads").

    Args:
        code_repo_name: The code repository name (e.g., "watercooler-cloud")
        suffix: Override suffix (uses get_threads_suffix() if None)

    Returns:
        Threads repository name (e.g., "watercooler-cloud-threads")
    """
    if suffix is None:
        suffix = get_threads_suffix()

    if code_repo_name.endswith(suffix):
        return code_repo_name  # Already has suffix

    return f"{code_repo_name}{suffix}"


def derive_code_repo_from_threads(
    threads_name: str,
    suffix: Optional[str] = None
) -> str:
    """Reverse derivation: extract code repo name from threads repo name.

    Strips configured suffix (default: "-threads").

    Args:
        threads_name: The threads repository name (e.g., "watercooler-cloud-threads")
        suffix: Override suffix (uses get_threads_suffix() if None)

    Returns:
        Code repository name (e.g., "watercooler-cloud")
    """
    if suffix is None:
        suffix = get_threads_suffix()

    if threads_name.endswith(suffix):
        return threads_name[:-len(suffix)]

    return threads_name  # No suffix to strip


def derive_group_id(
    code_repo_name: Optional[str] = None,
    code_path: Optional[Path] = None,
    threads_dir: Optional[Path] = None
) -> str:
    """Derive group_id (database name) from repo context.

    This is the canonical function for deriving FalkorDB database names
    and other identifiers that need consistent sanitization.

    For federation, use ``namespace_to_group_id()`` to convert namespace IDs
    to group_ids. Both functions apply identical sanitization:
    ``name.replace("-", "_").lower()``.

    Source priority:
    1. code_repo_name (if provided directly)
    2. Derived from code_path (if provided)
    3. Reverse-derived from threads_dir (strips threads suffix)

    Sanitization rules (preserves established behavior from Jan 30 fix):
    - Replace hyphens with underscores
    - Lowercase

    Note: Does NOT remove dots or other special chars to preserve
    compatibility with existing migrated FalkorDB data.

    Args:
        code_repo_name: Direct code repo name (highest priority)
        code_path: Path to code repository (derives name from path)
        threads_dir: Path to threads directory (reverse-derives code repo name)

    Returns:
        Sanitized group_id suitable for FalkorDB database name
        (e.g., "watercooler_cloud")
    """
    # Determine code_repo_name
    name = code_repo_name

    if not name and code_path:
        name = derive_code_repo_name(code_path)

    if not name and threads_dir:
        threads_path = Path(threads_dir) if isinstance(threads_dir, str) else threads_dir
        threads_name = threads_path.name
        name = derive_code_repo_from_threads(threads_name)

    if not name:
        return "watercooler"  # Default fallback

    # Sanitization: hyphens to underscores, lowercase
    # Preserves established behavior - does NOT strip dots/special chars
    return name.replace("-", "_").lower() or "watercooler"


def namespace_to_group_id(namespace_id: str) -> str:
    """Convert a federation namespace ID to a FalkorDB group_id.

    Namespace IDs are human-friendly (hyphens allowed, e.g., 'watercooler-cloud').
    Group IDs are FalkorDB-safe (underscores, lowercase, e.g., 'watercooler_cloud').

    Delegates to derive_group_id() to guarantee identical sanitization:
    namespace_to_group_id(ns_id) == derive_group_id(code_repo_name=ns_id).

    Args:
        namespace_id: Federation namespace identifier (e.g., 'watercooler-cloud')

    Returns:
        Sanitized group_id suitable for FalkorDB database name
    """
    return derive_group_id(code_repo_name=namespace_id)


# =============================================================================
# Slug-aware canonical identity helpers (Plan v20 Phase 1)
# =============================================================================
#
# These helpers introduce the canonical ``<org>_<repo>`` form for hosted
# identity while remaining backward-compatible with the repo-only form.
# Callers that pass ``repo_slug="<org>/<repo>"`` receive the full canonical
# form; callers that pass only ``code_repo_name`` / ``code_path`` /
# ``threads_dir`` continue to receive the repo-only form as before.
#
# Phase 1 adds the helpers and makes them available. Phase 6 is where the
# physical Falkor database names on hosted actually migrate to the
# ``<org>_<repo>_t1`` / ``<org>_<repo>_t2`` form and where in-code callers
# start preferring ``derive_project_group_id`` over ``derive_group_id``.


def _split_slug(repo_slug: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split ``<org>/<repo>`` into ``(org, repo)``. Returns ``(None, None)`` on invalid input."""
    if not repo_slug or "/" not in repo_slug:
        return None, None
    parts = repo_slug.split("/", 1)
    org = parts[0].strip() or None
    repo = parts[1].strip() or None
    return org, repo


def derive_repo_slug(
    code_path: Optional[Path] = None,
    threads_dir: Optional[Path] = None,
) -> Optional[str]:
    """Extract ``<org>/<repo>`` from the git remote of ``code_path``.

    Phase 1 scaffolding (Plan v20). The canonical project identity is
    ``<org>_<repo>``; to produce that from a bare path, we first need the
    ``<org>/<repo>`` slug. This helper reads ``remote.origin.url`` via
    ``git config`` and strips any trailing ``.git`` / trailing slash.

    Returns ``None`` if no code_path/threads_dir resolves to a git
    repository with an ``origin`` remote — callers should then fall back
    to the repo-only form via :func:`derive_project_group_id` without a
    ``repo_slug``.
    """
    import subprocess

    target: Optional[Path] = None
    if code_path is not None:
        target = Path(code_path)
    elif threads_dir is not None:
        td = Path(threads_dir)
        stem = td.name
        if stem.endswith("-threads"):
            stem = stem.removesuffix("-threads")
        candidate = td.parent / stem
        target = candidate if candidate.exists() else td.parent
    if target is None:
        return None

    try:
        result = subprocess.run(
            ["git", "-C", str(target), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    url = result.stdout.strip()
    if not url or result.returncode != 0:
        return None

    # Normalize SSH / HTTPS / scp-style URLs into ``org/repo``.
    # git@github.com:org/repo.git                 -> org/repo
    # https://github.com/org/repo.git             -> org/repo
    # ssh://git@gitserver/org/repo.git            -> org/repo
    # http://intra-server:2222/org/repo.git       -> org/repo
    #
    # PR #654 in-PR review round 5 (LOW §5): the prior host-strip
    # heuristic keyed on ``"." in segments[0]`` which silently preserved
    # the host on dotless intranet hostnames (``gitserver``, ``dev-box``),
    # producing slugs like ``gitserver/org`` and downgrading identity to
    # repo-only. Use ``urllib.parse`` on schemed URLs so the host always
    # comes off, regardless of whether it has a dot; fall back to the
    # scp-style split when the URL has no scheme.
    from urllib.parse import urlparse as _urlparse

    path_part: str
    if url.startswith("git@") and ":" in url and "://" not in url:
        # scp-style: "git@host:path"
        _, path_part = url.split(":", 1)
    else:
        parsed = _urlparse(url)
        if parsed.scheme and parsed.netloc:
            # Schemed URL — urlparse already stripped the host into netloc.
            path_part = parsed.path.lstrip("/")
        else:
            # Schemeless, non-scp form. PR #654 in-PR review round 9 (LOW):
            # a URL like ``gitserver/org/repo`` (dotless intranet host,
            # no scheme, no colon) used to keep ``gitserver`` as the
            # first segment because the host-strip heuristic required a
            # ``.`` or ``:`` — producing a slug of ``gitserver/org``
            # instead of ``org/repo``. There is no reliable way to
            # distinguish a dotless hostname from a two-segment path
            # (``org/repo``) in a schemeless form, so this form is
            # inherently ambiguous. The downstream two-segment
            # truncation below will only produce a plausible slug when
            # the input is already in ``org/repo`` shape; otherwise we
            # refuse.
            path_part = url.lstrip("/")
            segments = path_part.split("/")
            if len(segments) >= 2 and ("." in segments[0] or ":" in segments[0]):
                path_part = "/".join(segments[1:])
            elif len(segments) > 2:
                # Ambiguous schemeless URL with more than two segments.
                # We can't tell if segment 0 is a dotless host or an org.
                # Refuse rather than emit a wrong slug.
                return None

    if path_part.endswith(".git"):
        path_part = path_part[:-4]
    path_part = path_part.strip("/")
    # Collapse accidental double slashes or empty segments.
    path_part = "/".join(seg for seg in path_part.split("/") if seg)
    if "/" not in path_part:
        return None
    # Take only the first two segments — some servers nest deeper paths
    # (e.g., GitLab subgroups); the <org>/<repo> contract is fixed to
    # two.
    segments = path_part.split("/")
    if len(segments) > 2:
        path_part = "/".join(segments[:2])
    return path_part


def derive_project_group_id(
    repo_slug: Optional[str] = None,
    code_repo_name: Optional[str] = None,
    code_path: Optional[Path] = None,
    threads_dir: Optional[Path] = None,
) -> str:
    """Derive the canonical project group identifier.

    This is the slug-aware successor to :func:`derive_group_id`. When
    ``repo_slug`` is provided (format ``"<org>/<repo>"``), the canonical
    form is ``<org>_<repo>`` with identical sanitisation as
    :func:`derive_group_id` (hyphens to underscores, lowercase). When
    ``repo_slug`` is absent, falls back to the repo-only form via
    :func:`derive_group_id`.

    Example::

        derive_project_group_id(repo_slug="mostlyharmless-ai/watercooler-cloud")
          → "mostlyharmless_ai_watercooler_cloud"

        derive_project_group_id(code_repo_name="watercooler-cloud")
          → "watercooler_cloud"   # same as derive_group_id(...)

    Args:
        repo_slug: Preferred source — ``"<org>/<repo>"`` GitHub slug.
        code_repo_name: Repo-only fallback (e.g., ``"watercooler-cloud"``).
        code_path: Path-based fallback.
        threads_dir: Threads-dir-based fallback (strips threads suffix).

    Returns:
        Sanitised canonical project identifier suitable for a FalkorDB
        database name. Non-empty; falls back to ``"watercooler"`` if all
        inputs are absent.
    """
    org, repo_from_slug = _split_slug(repo_slug)

    if org and repo_from_slug:
        joined = f"{org}_{repo_from_slug}"
        return joined.replace("-", "_").lower() or "watercooler"

    # Fall through to repo-only form for back-compat.
    return derive_group_id(
        code_repo_name=code_repo_name,
        code_path=code_path,
        threads_dir=threads_dir,
    )


def derive_t1_database_name(
    repo_slug: Optional[str] = None,
    code_repo_name: Optional[str] = None,
    code_path: Optional[Path] = None,
    threads_dir: Optional[Path] = None,
) -> str:
    """Physical FalkorDB database name for T1 (baseline graph + embedding HNSW).

    Returns ``<project_group_id>_t1``. See :func:`derive_project_group_id`
    for the ``project_group_id`` rules.

    Example::

        derive_t1_database_name(repo_slug="mostlyharmless-ai/watercooler-cloud")
          → "mostlyharmless_ai_watercooler_cloud_t1"
    """
    project_group_id = derive_project_group_id(
        repo_slug=repo_slug,
        code_repo_name=code_repo_name,
        code_path=code_path,
        threads_dir=threads_dir,
    )
    return f"{project_group_id}_t1"


def derive_t2_database_name(
    repo_slug: Optional[str] = None,
    code_repo_name: Optional[str] = None,
    code_path: Optional[Path] = None,
    threads_dir: Optional[Path] = None,
) -> str:
    """Physical FalkorDB database name for T2 (Graphiti episodic + entity).

    Returns ``<project_group_id>_t2``. See :func:`derive_project_group_id`
    for the ``project_group_id`` rules.
    """
    project_group_id = derive_project_group_id(
        repo_slug=repo_slug,
        code_repo_name=code_repo_name,
        code_path=code_path,
        threads_dir=threads_dir,
    )
    return f"{project_group_id}_t2"


def _compose_threads_slug(code_repo: Optional[str], repo_root: Optional[Path]) -> Optional[str]:
    """Compose threads repository slug from code repository info.

    Appends configured threads suffix (default: "-threads") to repository name
    if not already present.

    Args:
        code_repo: Code repository path (e.g., "org/repo")
        repo_root: Code repository root directory

    Returns:
        Threads repository slug (e.g., "org/repo-threads")
    """
    suffix = get_threads_suffix()

    if code_repo:
        namespace, repo = _split_namespace_repo(code_repo)
        repo_name = derive_threads_repo_name(repo, suffix)
        if namespace:
            return f"{namespace}/{repo_name}"
        return repo_name

    if repo_root:
        return derive_threads_repo_name(repo_root.name, suffix)

    return None


def _compose_local_threads_path(base: Path, slug: str) -> Path:
    """Compose local path for threads directory from slug.

    Args:
        base: Base directory
        slug: Repository slug (e.g., "org/repo-threads")

    Returns:
        Resolved path combining base and slug parts
    """
    parts = [p for p in slug.split("/") if p]
    path = base
    for part in parts:
        path = path / part
    return _resolve_path(path)


def _discover_worktree(repo_name: str) -> Optional[Path]:
    """Check if an orphan branch worktree exists for the given repo.

    The MCP server creates worktrees at ~/.watercooler/worktrees/<repo>/
    for the orphan branch thread storage strategy. The CLI discovers
    (but never creates) these worktrees.

    Args:
        repo_name: Repository directory name (e.g., "watercooler-cloud")

    Returns:
        Worktree path if it exists and has a valid .git marker, else None
    """
    worktree_path = WORKTREE_BASE / repo_name
    if worktree_path.is_dir() and (worktree_path / ".git").exists():
        return worktree_path.resolve()
    return None


def resolve_threads_dir(
    cli_value: Optional[str] = None,
    code_root: Optional[Path] = None
) -> Path:
    """Resolve threads directory with precedence: CLI > env > worktree > sibling.

    Consolidates logic from watercooler/config.py and watercooler_mcp/config.py.

    Precedence:
    1. CLI argument (if provided)
    2. WATERCOOLER_DIR environment variable
    3. Orphan branch worktree (~/.watercooler/worktrees/<repo>/)
    4. Sibling directory (<repo-parent>/<repo-name>-threads)
    5. <base>/<org>/<repo>-threads (using remote URL)
    6. <base>/_local (fallback)

    Args:
        cli_value: Explicit directory from CLI argument
        code_root: Code repository root for context

    Returns:
        Resolved threads directory path
    """
    def _normalize(candidate: Path) -> Path:
        """Normalize path (expand, resolve)."""
        candidate = candidate.expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path.cwd() / candidate).resolve()

    # 1. CLI argument takes precedence
    if cli_value:
        return _normalize(Path(cli_value))

    # 2. Explicit environment variable
    explicit = os.getenv("WATERCOOLER_DIR")
    if explicit:
        return _normalize(_expand_path(explicit))

    # 3. Git-aware discovery
    if code_root is None:
        code_root = Path.cwd()

    git_info = discover_git_info(code_root)
    repo_root = git_info.root
    remote = git_info.remote

    # 3. Orphan branch worktree (created by MCP server)
    if repo_root is not None:
        worktree = _discover_worktree(repo_root.name)
        if worktree is not None:
            return worktree
        else:
            expected = WORKTREE_BASE / repo_root.name
            print(
                f"watercooler: no orphan branch worktree found at {expected}. "
                f"The MCP server creates this on first write. "
                f"Falling back to legacy or local directory.",
                file=sys.stderr,
            )

    base = _default_threads_base(repo_root)
    repo_slug = _extract_repo_path(remote)
    threads_slug = _compose_threads_slug(repo_slug, repo_root)

    # 4. Sibling <repo-parent>/<repo-name>-threads
    if repo_root is not None:
        threads_name = derive_threads_repo_name(repo_root.name)
        sibling = (repo_root.parent / threads_name).resolve()
        print(
            f"Warning: Using legacy threads directory {sibling}. "
            "Run watercooler via MCP server first to initialize the "
            "orphan branch worktree.",
            file=sys.stderr,
        )
        return sibling

    # Otherwise use base + slug
    if threads_slug:
        threads_dir = _compose_local_threads_path(base, threads_slug)

        # Never write threads inside the code repository
        try:
            if repo_root and threads_dir.is_relative_to(repo_root):
                return (base / "_local").resolve()
        except AttributeError:
            # Python <3.9: emulate is_relative_to using relative_to()
            if repo_root:
                try:
                    threads_dir.resolve().relative_to(repo_root.resolve())
                    # If relative_to() succeeds, threads_dir is inside repo_root
                    return (base / "_local").resolve()
                except ValueError:
                    # Not inside repo_root, continue
                    pass
        except ValueError:
            return (base / "_local").resolve()

        return threads_dir

    # Fallback
    fallback = (base / "_local").resolve()
    print(
        f"watercooler: could not resolve threads directory via git, worktree, "
        f"or sibling. Using fallback: {fallback}. "
        f"To fix: run from a git repo, set WATERCOOLER_DIR, or start the MCP server first.",
        file=sys.stderr,
    )
    return fallback


def resolve_templates_dir(cli_value: Optional[str] = None) -> Path:
    """Resolve templates directory with fallback chain.

    Precedence:
    1. CLI argument (--templates-dir)
    2. WATERCOOLER_TEMPLATES environment variable
    3. Project-local templates (./.watercooler/templates/ if exists)
    4. Package bundled templates (always available as fallback)

    Args:
        cli_value: Explicit directory from CLI argument

    Returns:
        Path to directory containing _TEMPLATE_*.md files
    """
    if cli_value:
        return Path(cli_value)

    env = os.getenv("WATERCOOLER_TEMPLATES")
    if env:
        return Path(env)

    # Check for project-local templates
    project_local = Path(".watercooler/templates")
    if project_local.exists() and project_local.is_dir():
        return project_local.resolve()

    # Fallback to package bundled templates
    # This returns src/watercooler/templates/ in development
    # or site-packages/watercooler/templates/ when installed
    return Path(__file__).parent / "templates"


def load_template(template_name: str, templates_dir: Path | None = None) -> str:
    """Load a template file with fallback to package bundled templates.

    Args:
        template_name: Name of template file (e.g., "_TEMPLATE_entry_block.md")
        templates_dir: Optional templates directory (uses resolve_templates_dir if None)

    Returns:
        Template content as string

    Raises:
        FileNotFoundError: If template not found in any location
    """
    if templates_dir is None:
        templates_dir = resolve_templates_dir()

    template_path = templates_dir / template_name

    # Try requested location first
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    # Fallback to package bundled templates
    bundled_path = Path(__file__).parent / "templates" / template_name
    if bundled_path.exists():
        return bundled_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"Template '{template_name}' not found in {templates_dir} or bundled templates"
    )
