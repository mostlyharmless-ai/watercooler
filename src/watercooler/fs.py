from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import shutil
import os
import re


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


def thread_path(topic: str, threads_dir: Path) -> Path:
    safe = _sanitize_component(topic, default="thread")
    return threads_dir / f"{safe}.md"


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
