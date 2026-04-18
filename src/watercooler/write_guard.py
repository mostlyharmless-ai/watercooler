"""GitHub-backed write guard (stdlib-only, shared by CLI and MCP).

Watercooler threads are designed to be backed by a GitHub-hosted git
repository — the orphan `watercooler/threads` branch on the code repo's
origin. Silent fallback to a local directory that isn't pushed anywhere
contradicts the "threads are GitHub-backed" product contract and leaves
first-time users writing into `_local/` without realizing their
entries will never sync to a remote.

This module exposes a single check, ``assert_github_backed_threads``,
invoked at the two shared write wrappers in the codebase:

- ``_cli_write_with_sync()`` in ``src/watercooler/cli.py``
- ``run_with_sync()`` in ``src/watercooler_mcp/middleware.py``

Those wrappers cover every current and future CLI/MCP write command
(including the seven graph.py tools — ``annotate``, ``remove_annotation``,
``delete_entry``, ``delete_thread``, ``archive_thread``, ``unarchive`` —
that call ``run_with_sync`` directly, bypassing the ``thread_write.py``
reporting helper).

The guard is bypassed by setting ``WATERCOOLER_ALLOW_LOCAL_ONLY=1``,
which preserves the forward-looking non-code / offline-workflow use
case as an explicit opt-in rather than a silent default.

Stdlib-only: the core package must not grow a GitPython dependency for
this check. We detect git-repo state via the presence of a ``.git``
entry (directory or file) AT ``threads_dir`` — no ancestor walk, because
a ``<repo>/_local`` child would otherwise inherit the parent's origin
while writes still land in ``_local``. The origin URL comes from
``.git/config`` (via the gitdir pointer for worktrees and the
``commondir`` indirection when present); when the main config has no
``[remote "origin"]`` stanza we shell out to ``git config --get
remote.origin.url`` so includes (``[include]`` / ``[includeIf]``) and
any other git config semantics are honored via the canonical source.
This keeps ``core`` minimal-deps per the project's design principles.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


ENV_ALLOW_LOCAL_ONLY = "WATERCOOLER_ALLOW_LOCAL_ONLY"
ENV_EXTRA_GITHUB_HOSTS = "WATERCOOLER_GITHUB_HOSTS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_URL_SCHEMES = ("https://", "http://", "ssh://", "git://")


class WatercoolerWriteError(Exception):
    """Raised when a thread write is attempted against a target that
    doesn't meet watercooler's GitHub-backed contract.

    The exception message is the full, multi-line remediation string
    callers can surface directly to the user. Intended to be caught at
    the CLI/MCP boundary and printed to stderr / returned as an
    error payload, not re-wrapped.
    """


def _is_allow_local_only_enabled() -> bool:
    value = os.environ.get(ENV_ALLOW_LOCAL_ONLY, "").strip().lower()
    return value in _TRUTHY


def _resolve_real_gitdir(git_entry: Path) -> Optional[Path]:
    """Given a ``.git`` entry (dir or gitdir-pointer file), return the
    actual gitdir directory. Returns None if the entry is malformed
    or the target is missing."""
    if git_entry.is_dir():
        return git_entry
    # Worktrees / submodules: ``.git`` is a file containing
    # ``gitdir: /abs/or/rel/path``
    try:
        content = git_entry.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    target_str = content[len(prefix):].strip()
    target = Path(target_str)
    if not target.is_absolute():
        target = (git_entry.parent / target).resolve(strict=False)
    return target if target.is_dir() else None


def _read_origin_url_from_git(gitdir: Path) -> Optional[str]:
    """Ask git itself for the origin URL via ``git config --get``.

    Used as a fallback when the manual INI parser finds no origin
    stanza in the top-level config. Git's own reader honors
    ``[include] path = ...`` and ``[includeIf ... ] path = ...``
    stanzas (common in dotfile-shared configs), which our lightweight
    parser does not follow. Returns None when git reports no origin,
    the binary is unavailable, or any error occurs.
    """
    try:
        # ``--git-dir`` anchors the lookup to *this* repo's config
        # chain, not whatever cwd git happens to resolve. ``--local``
        # and ``--system`` / ``--global`` all compose into the final
        # answer when no scope flag is given, which is what we want —
        # the same value git would see for a push.
        result = subprocess.run(
            ["git", f"--git-dir={gitdir}", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _read_origin_url(gitdir: Path) -> Optional[str]:
    """Read the ``[remote "origin"] url`` value from ``config``.

    Handles worktree gitdirs by following the ``commondir`` indirection
    so worktrees see the main repo's origin URL, not a nonexistent
    worktree-local config. Falls back to ``git config --get
    remote.origin.url`` when the manual parser doesn't find an origin
    stanza — this catches configs that pull the remote in via
    ``[include]`` / ``[includeIf]``, which the lightweight parser
    doesn't follow. Returns None when origin is missing everywhere.
    """
    # If this is a worktree gitdir, the shared config is at
    # gitdir/commondir's target (typically the main repo's .git dir).
    commondir_file = gitdir / "commondir"
    if commondir_file.exists():
        try:
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            common = (gitdir / rel).resolve(strict=False)
            if common.is_dir():
                gitdir = common
        except OSError:
            pass

    config_path = gitdir / "config"
    if config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
        if text is not None:
            # Parse INI-style stanzas looking for [remote "origin"] url.
            in_origin = False
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    header = line[1:-1].strip()
                    # Accept both `remote "origin"` and `remote.origin`-style variants
                    in_origin = header.replace('"', '').lower() in (
                        "remote origin",
                        "remote.origin",
                    )
                    continue
                key, sep, value = line.partition("=")
                # Exact key match — ``startswith("url")`` would also
                # match hypothetical keys like ``urlpath`` or
                # ``url-tracking``. Standard git remote sections
                # don't have such keys today, but the strict form
                # keeps the parser correct if they ever appear.
                if in_origin and sep and key.strip().lower() == "url":
                    result = value.strip()
                    if result:
                        return result

    # Manual parse found nothing — defer to git itself, which honors
    # include / includeIf / any other config-resolution semantics we'd
    # otherwise have to reimplement.
    return _read_origin_url_from_git(gitdir)


def _extract_host(url: str) -> str:
    """Extract the host portion from a git remote URL.

    Handles https://, http://, ssh://, git:// schemes, as well as the
    SSH short form ``user@host:path`` (no scheme). Returns an empty
    string when the URL is malformed or unrecognized.
    """
    s = url.strip().lower()
    if not s:
        return ""
    for prefix in _URL_SCHEMES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # SSH short-form: user@host:path
    if "@" in s:
        before, _, after = s.partition("@")
        if "/" not in before:
            s = after
    # Now the host is the prefix up to the first ':' or '/'.
    for i, ch in enumerate(s):
        if ch in (":", "/"):
            return s[:i]
    return s


def _parse_extra_github_hosts() -> list[tuple[str, bool]]:
    """Parse ``WATERCOOLER_GITHUB_HOSTS`` into ``(pattern, is_suffix)`` pairs.

    Each comma-separated entry may be either ``host.example.com``
    (exact match) or ``*.example.com`` (suffix match — matches any
    subdomain of ``example.com`` but NOT ``example.com`` itself).
    Empty / whitespace-only entries are ignored. Case-insensitive.
    """
    raw = os.environ.get(ENV_EXTRA_GITHUB_HOSTS, "").strip()
    if not raw:
        return []
    entries: list[tuple[str, bool]] = []
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p.startswith("*."):
            suffix = p[2:]
            if suffix:
                entries.append((suffix, True))
        else:
            entries.append((p, False))
    return entries


def _looks_github_hosted(url: str) -> bool:
    """True if ``url`` points at a GitHub-family host.

    Accepts by default:
    - ``github.com``
    - subdomains of ``github.com`` (e.g. ``api.github.com``)

    The prior check (``host.startswith("github.")``) was too permissive
    — it accepted attacker-controlled hostnames like
    ``github.attacker.com``. GitHub Enterprise installations (which
    often use ``github.<company>.<tld>`` hostnames) are now supported
    via the ``WATERCOOLER_GITHUB_HOSTS`` env var, which takes a
    comma-separated allowlist of exact hostnames or ``*.suffix.com``
    patterns. This keeps the default guard tight while preserving an
    explicit escape hatch for GHE users.
    """
    host = _extract_host(url)
    if not host:
        return False
    # Built-in trust root: only real github.com.
    if host == "github.com" or host.endswith(".github.com"):
        return True
    # User-configured GitHub Enterprise hostnames.
    for entry, is_suffix in _parse_extra_github_hosts():
        if is_suffix:
            # ``*.example.com`` matches subdomains of ``example.com``
            # but NOT the bare ``example.com`` itself — same semantics
            # as the TLS SAN / cookie-domain convention.
            if host.endswith("." + entry):
                return True
        elif host == entry:
            return True
    return False


def _format_error(
    threads_dir: Path, reason: str, origin_url: Optional[str] = None
) -> str:
    lines = [
        "Cannot write threads — target is not a GitHub-backed git repository.",
        f"  Resolved threads dir: {threads_dir}",
        f"  Reason: {reason}",
    ]
    if origin_url:
        lines.append(f"  Current origin URL: {origin_url}")
    lines.extend(
        [
            "",
            "To proceed:",
            "  - cd into a git repository with a GitHub 'origin' remote, OR",
            f"  - set WATERCOOLER_DIR=<path> to point at an existing "
            f"GitHub-backed threads directory, OR",
            f"  - set {ENV_ALLOW_LOCAL_ONLY}=1 to explicitly enable "
            f"local-only mode",
            "    (threads will NOT be pushed to any remote).",
            "",
            "See docs/TROUBLESHOOTING.md#local-only-mode for details.",
        ]
    )
    return "\n".join(lines)


def assert_github_backed_threads(
    threads_dir: Path,
    code_root: Optional[Path] = None,
) -> None:
    """Ensure ``threads_dir`` is itself a GitHub-backed git worktree.

    Raises ``WatercoolerWriteError`` with an actionable remediation
    message when any of the following is true:

    - ``WATERCOOLER_ALLOW_LOCAL_ONLY=1`` is not set, AND
    - ``threads_dir`` is not itself a git worktree root (no ``.git``
      entry directly at the path — we do NOT walk up to ancestors,
      because a ``<repo>/_local`` child would falsely inherit the
      parent's origin while writes still land in ``_local``), OR
    - The repo has no ``origin`` remote, OR
    - The ``origin`` URL does not point at a GitHub-family host.

    ``code_root`` is currently unused by the check itself but is
    accepted for signature symmetry with ``_check_git_auth_health``
    (Bug #1) and reserved for a future fallback probe if we want to
    accept code_root as the repo reference when ``threads_dir`` is an
    orphan worktree that doesn't self-resolve.
    """
    if _is_allow_local_only_enabled():
        return

    # Require a .git entry AT threads_dir, not at any ancestor. Walking
    # up would accept <repo>/_local as "GitHub-backed" because the parent
    # repo has a github origin — but writes still land in _local and
    # never reach the remote. Orphan-branch worktrees pass this check
    # because their .git is a file (gitdir pointer) sitting at the
    # worktree root.
    git_entry = threads_dir / ".git"
    if not git_entry.exists():
        raise WatercoolerWriteError(
            _format_error(
                threads_dir,
                reason=(
                    "threads_dir is not itself a git worktree "
                    "(no .git entry at this path)"
                ),
            )
        )

    gitdir = _resolve_real_gitdir(git_entry)
    if gitdir is None:
        raise WatercoolerWriteError(
            _format_error(
                threads_dir,
                reason=f"gitdir pointer at {git_entry} is broken or target is missing",
            )
        )

    origin_url = _read_origin_url(gitdir)
    if not origin_url:
        raise WatercoolerWriteError(
            _format_error(threads_dir, reason="no 'origin' remote configured")
        )

    if not _looks_github_hosted(origin_url):
        raise WatercoolerWriteError(
            _format_error(
                threads_dir,
                reason="'origin' URL is not a GitHub-hosted remote",
                origin_url=origin_url,
            )
        )
