"""Orphan-branch worktree scanner for migration.

Reads entry metadata (role, entry_type, agent, timestamp, title, body)
from any ``entries.jsonl`` reachable under
``~/.watercooler/worktrees/<repo>/graph/baseline/threads/``.
This is the canonical source of truth for entries — embeddings may not
exist yet (e.g. on a fresh worktree), but the metadata always does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


def discover_threads_dir(code_path: Optional[str]) -> Path:
    """Resolve the orphan-branch worktree path for the given code repo."""
    from watercooler.path_resolver import resolve_threads_dir
    code_root = Path(code_path or ".").resolve()
    return resolve_threads_dir(cli_value=None, code_root=code_root)


def scan_orphan_entries(threads_dir: Path) -> Iterator[Dict[str, Any]]:
    """Yield every entry dict (with metadata) reachable from the orphan branch.

    Adds ``_topic`` to each yielded dict so the caller doesn't need to
    plumb the topic through separately. The topic is derived from the
    ``entries.jsonl`` location relative to ``threads_root`` so nested
    topics (e.g. ``fix/some-feature``) are handled — the prior
    ``iterdir()`` form skipped them silently and lost their entries.
    """
    from watercooler.baseline_graph.writer import get_entries_for_thread

    threads_root = threads_dir / "graph" / "baseline" / "threads"
    if not threads_root.is_dir():
        raise RuntimeError(f"Threads root not found: {threads_root}")

    seen_topics: set[str] = set()
    for entries_file in sorted(threads_root.rglob("entries.jsonl")):
        topic = entries_file.parent.relative_to(threads_root).as_posix()
        if topic in seen_topics:
            continue
        seen_topics.add(topic)
        try:
            entries = get_entries_for_thread(threads_dir, topic)
        except Exception as e:
            logger.warning("Could not read thread %s: %s", topic, e)
            continue
        for entry in entries:
            entry["_topic"] = topic
            yield entry
