from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timezone
import shutil
import os
import re

logger = logging.getLogger(__name__)


_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_component(value: str, *, default: str = "") -> str:
    value = value.strip()
    if not value:
        return default
    sanitized = _SANITIZE_PATTERN.sub("-", value)
    sanitized = sanitized.strip("-._")
    return sanitized or (default or "untitled")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(p: Path) -> str:  # placeholder for L1
    return p.read_text(encoding="utf-8")


def write(p: Path, s: str) -> None:  # placeholder for L1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def ensure_exists(p: Path, hint: str) -> None:
    if not p.exists():
        raise FileNotFoundError(f"{hint}: missing {p}")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _backup_file(p: Path, keep: int = 3, topic: str | None = None) -> None:
    if not p.exists():
        return
    backups_dir = p.parent / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    tag = _sanitize_component(topic or p.stem, default=p.stem)
    ts = _now_ts()
    # ensure uniqueness even within same second
    dest = backups_dir / f"{tag}.{ts}{p.suffix}"
    i = 1
    while dest.exists():
        dest = backups_dir / f"{tag}.{ts}.{i}{p.suffix}"
        i += 1
    shutil.copy2(p, dest)
    # rotate old ones
    bks = sorted([x for x in backups_dir.glob(f"{tag}.*{p.suffix}") if x.is_file()])
    # keep the newest N
    bks = sorted(bks, key=lambda x: x.stat().st_mtime, reverse=True)
    for old in bks[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def thread_path(
    topic: str, threads_dir: Path, *, new_thread: bool = False
) -> Path:
    """Get the path for a thread file.

    In structured layouts (where threads/ subdir exists), new threads are
    routed to threads/<topic>.md. Existing threads are found wherever they
    live (root or any category subdir) via find_thread_path().

    In flat layouts (no threads/ subdir), falls back to root for backward compat.

    Args:
        topic: Thread topic slug
        threads_dir: Root threads directory
        new_thread: If True, skip the find_thread_path() search and route
            directly to the default location. Use this when the caller knows
            the thread does not yet exist (e.g., init_thread) to avoid
            unnecessary I/O.

    Note: Without new_thread=True, this performs bounded I/O (at most one
    stat per THREAD_CATEGORIES entry) to locate existing threads.
    """
    safe = _sanitize_component(topic, default="thread")
    filename = f"{safe}.md"

    if not new_thread:
        # Check if the thread already exists somewhere
        existing = find_thread_path(topic, threads_dir)
        if existing is not None:
            return existing

    # For new threads: route to threads/ subdir if structured layout is present
    threads_subdir = threads_dir / "threads"
    if threads_subdir.is_dir():
        return threads_subdir / filename

    # Flat layout fallback
    return threads_dir / filename


def lock_path_for_topic(topic: str, threads_dir: Path) -> Path:
    safe = _sanitize_component(topic, default="topic")
    return threads_dir / f".{safe}.lock"


def read_body(maybe_path: str | Path | None) -> str:
    if not maybe_path:
        return ""

    # Handle Path objects directly
    if isinstance(maybe_path, Path):
        if maybe_path.exists() and maybe_path.is_file():
            return read(maybe_path)
        return ""

    # Handle string paths
    text = maybe_path.strip()
    if not text:
        return ""

    # Support @filename convention while preserving legacy behaviour.
    if text.startswith("@") and len(text) > 1:
        candidate = Path(text[1:]).expanduser()
        if candidate.is_file():
            return read(candidate)

    p = Path(text).expanduser()
    if p.exists() and p.is_file():
        return read(p)
    return maybe_path


# =============================================================================
# Structured Directory Layout
# =============================================================================

# Category subdirectories that hold thread .md files.
# discover_thread_files() uses this as an allowlist when a structured layout
# is detected (i.e. threads/ subdir exists).
THREAD_CATEGORIES: tuple[str, ...] = (
    "threads",
    "reference",
    "debug",
    "closed",
    "decision-traces",
    "sessions",
    "enrichment",
)

# Full directory hierarchy to create (includes non-thread dirs).
_DIRECTORY_STRUCTURE: tuple[str, ...] = (
    *THREAD_CATEGORIES,
    "compound/reports",
    "compound/learnings",
    "compound/suggestions",
    "logs/agent",
    "logs/mcp",
    ".watercooler",  # Local config/state; NOT in .gitignore (tracked on orphan branch)
)


def has_structured_layout(threads_dir: Path) -> bool:
    """Check whether threads_dir uses the structured subdirectory layout."""
    return (threads_dir / "threads").is_dir()


def ensure_directory_structure(threads_dir: Path) -> list[Path]:
    """Create the full structured directory hierarchy under threads_dir.

    Idempotent — safe to call repeatedly. Existing directories are untouched.

    Returns:
        List of directories that were newly created.
    """
    created: list[Path] = []
    for rel in _DIRECTORY_STRUCTURE:
        d = threads_dir / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return created


def migrate_to_structured_layout(threads_dir: Path) -> list[tuple[Path, Path]]:
    """Move root-level .md thread files into the threads/ subdirectory.

    Idempotent and opt-in only. Does NOT move files that already live in
    a category subdirectory.

    Returns:
        List of (old_path, new_path) for files that were moved.
    """
    ensure_directory_structure(threads_dir)
    target = threads_dir / "threads"
    moved: list[tuple[Path, Path]] = []

    for p in sorted(threads_dir.glob("*.md")):
        if not p.is_file():
            continue
        if p.name.startswith((".", "_")):
            continue
        dest = target / p.name
        if dest.exists():
            logger.warning("migrate_to_structured_layout: skipping '%s' — collision with '%s'", p, dest)
            continue
        try:
            shutil.move(str(p), str(dest))
            moved.append((p, dest))
        except OSError as exc:
            logger.error("migrate_to_structured_layout: failed to move '%s' → '%s': %s", p, dest, exc)

    return moved


# =============================================================================
# Thread File Discovery
# =============================================================================

# Hidden directories and special prefixes to skip when scanning for threads
_SKIP_PREFIXES = (".", "_")
_SKIP_DIRS = {".backups", ".git", "graph", "__pycache__"}


def discover_thread_files(
    threads_dir: Path,
    category: Optional[str] = None,
) -> List[Path]:
    """Discover thread .md files in threads_dir, including subdirectories.

    .. deprecated::
        This function scans .md files on disk. In graph-first architecture, use
        ``watercooler.baseline_graph.storage.list_thread_topics()`` to enumerate
        threads from the graph. Retained only for .md projection writes and
        graph rebuild/reconciliation paths.

    Args:
        threads_dir: Root threads directory
        category: If provided, only scan that subdirectory

    Returns:
        Sorted list of .md file paths
    """
    import warnings
    warnings.warn(
        "discover_thread_files() is deprecated; use "
        "storage.list_thread_topics() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if not threads_dir.exists():
        return []

    results: List[Path] = []
    structured = has_structured_layout(threads_dir)

    if category:
        sub = threads_dir / category
        if sub.is_dir():
            results.extend(
                p for p in sub.glob("*.md")
                if p.is_file() and not p.name.startswith(tuple(_SKIP_PREFIXES))
            )
    elif structured:
        # Structured layout: allowlist scan — root + THREAD_CATEGORIES only
        results.extend(
            p for p in threads_dir.glob("*.md")
            if p.is_file() and not p.name.startswith(tuple(_SKIP_PREFIXES))
        )
        for cat in THREAD_CATEGORIES:
            sub = threads_dir / cat
            if sub.is_dir():
                results.extend(
                    p for p in sub.glob("*.md")
                    if p.is_file() and not p.name.startswith(tuple(_SKIP_PREFIXES))
                )
    else:
        # Flat layout: blocklist scan (backward compat)
        results.extend(
            p for p in threads_dir.glob("*.md")
            if p.is_file() and not p.name.startswith(tuple(_SKIP_PREFIXES))
        )
        try:
            for sub in threads_dir.iterdir():
                if (
                    sub.is_dir()
                    and sub.name not in _SKIP_DIRS
                    and not sub.name.startswith(tuple(_SKIP_PREFIXES))
                ):
                    results.extend(
                        p for p in sub.glob("*.md")
                        if p.is_file() and not p.name.startswith(tuple(_SKIP_PREFIXES))
                    )
        except OSError:
            pass

    results.sort(key=lambda p: p.name)
    return results


def find_thread_path(topic: str, threads_dir: Path) -> Optional[Path]:
    """Find a thread file by topic, searching root and subdirectories.

    Checks the flat root first (backward compat), then category subdirectories.
    In structured layout mode, only THREAD_CATEGORIES are searched.

    Args:
        topic: Thread topic identifier
        threads_dir: Root threads directory

    Returns:
        Path to the thread file, or None if not found
    """
    safe = _sanitize_component(topic, default="thread")
    filename = f"{safe}.md"

    # Check flat root first
    candidate = threads_dir / filename
    if candidate.exists():
        return candidate

    if not threads_dir.exists():
        return None

    structured = has_structured_layout(threads_dir)

    if structured:
        # Structured layout: only search THREAD_CATEGORIES
        for cat in THREAD_CATEGORIES:
            candidate = threads_dir / cat / filename
            if candidate.exists():
                return candidate
    else:
        # Flat layout: search all non-hidden subdirectories.
        # WARNING: This is O(subdirs) — unbounded scan.  Structured layouts
        # use a fixed allowlist (THREAD_CATEGORIES) and are preferred.
        try:
            for sub in threads_dir.iterdir():
                if (
                    sub.is_dir()
                    and sub.name not in _SKIP_DIRS
                    and not sub.name.startswith(tuple(_SKIP_PREFIXES))
                ):
                    candidate = sub / filename
                    if candidate.exists():
                        return candidate
        except OSError:
            pass

    return None


# =============================================================================
# Thread Status Utilities
# =============================================================================

CLOSED_STATES = {"done", "closed", "merged", "resolved", "abandoned", "obsolete"}


def is_closed(status: str) -> bool:
    """Check if a thread status indicates closure.

    Args:
        status: The status string to check

    Returns:
        True if the status is a closed state
    """
    return status.strip().lower() in CLOSED_STATES
