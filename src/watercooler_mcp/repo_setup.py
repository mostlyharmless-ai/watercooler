"""Repository setup helpers for watercooler threads repositories.

This module provides functions to initialize threads repos with:
- .gitignore for memory/ and large files
- pre-commit hook to block files >100MB

These safeguards prevent common issues like:
- Accidentally committing FalkorDB/Redis dumps (100MB+)
- Push failures due to GitHub's 100MB file limit
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import List, Tuple

try:
    from importlib.resources import files, as_file
except ImportError:
    from importlib_resources import files, as_file

from .observability import log_debug, log_warning


# Required entries that should always be in .gitignore
REQUIRED_GITIGNORE_ENTRIES = [
    "# Watercooler threads repo - auto-managed entries",
    "# Do not remove these lines - they prevent push failures",
    "",
    "# Memory backend data (FalkorDB dumps, vector DBs)",
    "memory/",
    "*.rdb",
    "*.dump",
    "",
]

# File size limits (in bytes)
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100MB - GitHub's hard limit
WARN_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB - Warning threshold


def get_template_path(template_name: str) -> Path:
    """Get the path to a template file in the watercooler package.

    Args:
        template_name: Name of the template file (e.g., 'threads.gitignore')

    Returns:
        Path to the template file

    Note:
        Uses files("watercooler").joinpath("templates") because there's both
        a templates.py module and a templates/ directory in the watercooler
        package. Using "watercooler.templates" would conflict with the module.
    """
    watercooler_pkg = files("watercooler")
    return watercooler_pkg.joinpath("templates", template_name)  # type: ignore


def ensure_gitignore(repo_path: Path) -> Tuple[bool, List[str]]:
    """Ensure the threads repo has a proper .gitignore.

    Creates or updates .gitignore to include required entries for memory/
    and large file patterns. Existing user entries are preserved.

    Args:
        repo_path: Path to the threads repository

    Returns:
        Tuple of (modified: bool, entries_added: List[str])
    """
    gitignore_path = repo_path / ".gitignore"
    entries_added: List[str] = []

    # Read existing content if file exists
    existing_content = ""
    existing_lines: set[str] = set()
    if gitignore_path.exists():
        existing_content = gitignore_path.read_text()
        existing_lines = {line.strip() for line in existing_content.splitlines()}

    # Check which required entries are missing
    missing_entries: List[str] = []
    for entry in REQUIRED_GITIGNORE_ENTRIES:
        entry_stripped = entry.strip()
        # Skip comments and empty lines for duplicate check
        if entry_stripped.startswith("#") or not entry_stripped:
            if entry not in existing_content:
                missing_entries.append(entry)
        elif entry_stripped not in existing_lines:
            missing_entries.append(entry)
            entries_added.append(entry_stripped)

    if not missing_entries:
        log_debug("[REPO_SETUP] .gitignore already has required entries")
        return (False, [])

    # Append missing entries
    new_content = existing_content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    if new_content and not new_content.endswith("\n\n"):
        new_content += "\n"

    new_content += "\n".join(missing_entries)
    if not new_content.endswith("\n"):
        new_content += "\n"

    gitignore_path.write_text(new_content)
    log_debug(f"[REPO_SETUP] Added {len(entries_added)} entries to .gitignore")

    return (True, entries_added)


def install_hooks(repo_path: Path) -> Tuple[bool, List[str]]:
    """Install git hooks for the threads repository.

    Installs:
    - pre-commit: Blocks files >100MB

    Existing hooks are backed up with .bak suffix.

    Args:
        repo_path: Path to the threads repository

    Returns:
        Tuple of (modified: bool, hooks_installed: List[str])
    """
    hooks_dir = repo_path / ".git" / "hooks"
    if not hooks_dir.exists():
        log_debug("[REPO_SETUP] No .git/hooks directory, skipping hook installation")
        return (False, [])

    hooks_installed: List[str] = []

    for hook_name in ["pre-commit"]:
        try:
            template_ref = get_template_path(hook_name)
            with as_file(template_ref) as template_path:
                if not template_path.exists():
                    log_debug(f"[REPO_SETUP] Template {hook_name} not found, skipping")
                    continue

                hook_content = template_path.read_text()

            hook_path = hooks_dir / hook_name

            # Backup existing hook if different
            if hook_path.exists():
                existing_content = hook_path.read_text()
                if existing_content == hook_content:
                    log_debug(f"[REPO_SETUP] Hook {hook_name} already installed")
                    continue
                # Backup existing
                backup_path = hooks_dir / f"{hook_name}.bak"
                backup_path.write_text(existing_content)
                log_debug(f"[REPO_SETUP] Backed up existing {hook_name} to {hook_name}.bak")

            # Write new hook
            hook_path.write_text(hook_content)

            # Make executable
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            hooks_installed.append(hook_name)
            log_debug(f"[REPO_SETUP] Installed {hook_name} hook")

        except Exception as e:
            log_warning(f"[REPO_SETUP] Failed to install {hook_name} hook: {e}")

    return (len(hooks_installed) > 0, hooks_installed)


def check_large_files(repo_path: Path, staged_only: bool = False) -> List[Tuple[Path, int]]:
    """Check for files that exceed GitHub's size limits.

    Args:
        repo_path: Path to the repository
        staged_only: If True, only check staged files

    Returns:
        List of (file_path, size_bytes) for files exceeding limits
    """
    oversized: List[Tuple[Path, int]] = []

    if staged_only:
        # Get staged files from git
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return []
            files_to_check = [repo_path / f for f in result.stdout.strip().split("\n") if f]
        except Exception:
            return []
    else:
        # Check all files in repo (excluding .git)
        files_to_check = [
            f for f in repo_path.rglob("*")
            if f.is_file() and ".git" not in f.parts
        ]

    for file_path in files_to_check:
        if not file_path.exists():
            continue
        try:
            size = file_path.stat().st_size
            if size > MAX_FILE_SIZE_BYTES:
                oversized.append((file_path, size))
        except OSError:
            pass

    return oversized


def setup_threads_repo(repo_path: Path, install_git_hooks: bool = True) -> dict:
    """Full setup for a threads repository.

    Ensures .gitignore exists with required entries and optionally
    installs git hooks for pre-commit validation.

    Args:
        repo_path: Path to the threads repository
        install_git_hooks: Whether to install git hooks (default True)

    Returns:
        Dict with setup results:
        - gitignore_modified: bool
        - gitignore_entries: List[str]
        - hooks_installed: List[str]
        - large_files: List[Tuple[Path, int]]
    """
    result = {
        "gitignore_modified": False,
        "gitignore_entries": [],
        "hooks_installed": [],
        "large_files": [],
    }

    # Ensure .gitignore
    modified, entries = ensure_gitignore(repo_path)
    result["gitignore_modified"] = modified
    result["gitignore_entries"] = entries

    # Install hooks
    if install_git_hooks:
        _, hooks = install_hooks(repo_path)
        result["hooks_installed"] = hooks

    # Check for large files (warning only)
    large_files = check_large_files(repo_path)
    result["large_files"] = large_files

    if large_files:
        for file_path, size in large_files:
            size_mb = size / (1024 * 1024)
            log_warning(
                f"[REPO_SETUP] Large file detected: {file_path.relative_to(repo_path)} "
                f"({size_mb:.1f}MB) - may cause push failures"
            )

    return result
