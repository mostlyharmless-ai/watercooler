"""Event-sourced annotation store for thread entries and threads.

Annotations are stored per-thread in `annotations.jsonl` alongside `entries.jsonl`
and `meta.json`. The event log is append-only; a materialized state cache
(`annotation_state.json`) is rebuilt on demand.

Supported annotation kinds:
  reaction, tag, tag_remove, flag, flag_clear,
  xref, xref_remove, pin, unpin

Storage layout:
    graph/baseline/threads/<topic>/
        annotations.jsonl       # Append-only event log
        annotation_state.json   # Materialized state cache (rebuilt on demand)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import storage

logger = logging.getLogger(__name__)


# ============================================================================
# Data Structures
# ============================================================================


VALID_KINDS = frozenset(
    {
        "reaction",
        "reaction_remove",
        "tag",
        "tag_remove",
        "flag",
        "flag_clear",
        "xref",
        "xref_remove",
        "xref_supersedes",
        "xref_supersedes_remove",
        "pin",
        "unpin",
    }
)

VALID_TARGET_TYPES = frozenset({"entry", "thread", "uri"})


@dataclass
class AnnotationEvent:
    """A single annotation event (append-only log entry).

    Attributes:
        id: ULID for the event
        target_id: Entry ID, thread topic, or opaque URI (see ``target_type``)
        target_type: "entry", "thread", or "uri". ``"uri"`` allows callers
            to annotate content-addressed or namespace-scoped identifiers
            (e.g. ``codex://sha256:<hex>``) that are not tracked as
            entry/thread nodes in this graph. Downstream code treats
            ``target_id`` as an opaque string key regardless of
            ``target_type``; only validation gates branch on the value.
        kind: The annotation operation
        value: Emoji name, tag name, agent name, or target entry_id (for xrefs)
        actor: Who made the annotation
        timestamp: ISO 8601 UTC
    """

    id: str
    target_id: str
    target_type: str
    kind: str
    value: str
    actor: str
    timestamp: str


@dataclass
class AnnotationState:
    """Materialized annotation state for a single target (entry or thread).

    Attributes:
        reactions: Emoji name → list of actors who reacted
        tags: Unique tag names
        flags: List of flag records with agent, reason, timestamp
        xrefs: Entry IDs of referenced entries
        xref_supersedes: Entry IDs of successors that supersede this entry — the
            durable, append-only authored record of a ratified supersession
            (earned-edge RFC P3). Presence flips a supersession badge afforded→solid.
        pinned: Whether the target is pinned
        last_touched: ISO 8601 timestamp of last annotation activity.
            .. deprecated:: Use ``last_activity`` from meta.json instead.
            Retained for backward-compatibility with watercooler-site
            dashboard reads. Not populated on new annotation write paths.
        vote_score: Net upvotes (thumbsup - thumbsdown)
    """

    reactions: Dict[str, List[str]] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    flags: List[Dict[str, str]] = field(default_factory=list)
    xrefs: List[str] = field(default_factory=list)
    xref_supersedes: List[str] = field(default_factory=list)
    pinned: bool = False
    last_touched: Optional[str] = None  # deprecated — use last_activity in meta.json
    vote_score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AnnotationState:
        """Deserialize from dict.

        Coerces ``null`` container values to empty collections so callers can
        rely on the field types without defensive ``or []`` at every use site.
        """
        # vote_score is an int field where legitimately negative values
        # (net downvotes) must survive round-trip. ``x or 0`` would map
        # -3 → 0; use an explicit None-check to coerce only missing/null.
        raw_vote = data.get("vote_score")
        return cls(
            reactions=data.get("reactions") or {},
            tags=data.get("tags") or [],
            flags=data.get("flags") or [],
            xrefs=data.get("xrefs") or [],
            xref_supersedes=data.get("xref_supersedes") or [],
            pinned=bool(data.get("pinned", False)),
            last_touched=data.get("last_touched"),
            vote_score=0 if raw_vote is None else raw_vote,
        )


# ============================================================================
# Event Log I/O
# ============================================================================


def _annotations_file(thread_dir: Path) -> Path:
    """Get path to annotations.jsonl for a thread directory."""
    return thread_dir / "annotations.jsonl"


def _state_cache_file(thread_dir: Path) -> Path:
    """Get path to annotation_state.json cache."""
    return thread_dir / "annotation_state.json"


def append_annotation(thread_dir: Path, event: AnnotationEvent) -> None:
    """Append an annotation event to the thread's event log.

    Args:
        thread_dir: Per-thread graph directory (graph/baseline/threads/<topic>/)
        event: The annotation event to append
    """
    thread_dir.mkdir(parents=True, exist_ok=True)
    ann_file = _annotations_file(thread_dir)

    line = json.dumps(asdict(event), separators=(",", ":")) + "\n"
    with open(ann_file, "a", encoding="utf-8") as f:
        f.write(line)

    # Re-embed materialized annotation state into meta.json so the graph
    # node stays current (annotations flow through the same sync pipeline
    # as status, ball, and priority). This also rebuilds the state cache.
    _sync_annotations_to_meta(thread_dir)


def _sync_annotations_to_meta(thread_dir: Path) -> None:
    """Re-embed materialized annotation state into meta.json.

    Called after annotation writes so the graph node includes up-to-date
    annotation state, which flows through the sync pipeline to the dashboard
    (same as status, ball, and priority).

    Also rebuilds the state cache as a side effect (single materialization).
    """
    try:
        # Rebuild from events (single materialization pass)
        events = load_annotation_events(thread_dir)
        states = materialize_all_states(events)

        # Always write state cache
        _write_state_cache(thread_dir, states)

        # Embed in meta.json if it exists
        meta_file = thread_dir / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            # Deterministic key order: a no-op re-materialization then yields a
            # byte-identical meta.json, removing the spurious annotation-reorder
            # churn that bloated diffs (bug-sync-worktree-poisoning #14).
            meta["annotations"] = {
                tid: states[tid].to_dict() for tid in sorted(states)
            }
            storage.atomic_write_json(meta_file, meta)
    except Exception as e:
        logger.warning(f"Failed to sync annotations to meta.json: {e}")


def load_annotation_events(thread_dir: Path) -> List[AnnotationEvent]:
    """Read all annotation events for a thread.

    Args:
        thread_dir: Per-thread graph directory

    Returns:
        List of AnnotationEvent in chronological order
    """
    ann_file = _annotations_file(thread_dir)
    if not ann_file.exists():
        return []

    events: List[AnnotationEvent] = []
    with open(ann_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                events.append(AnnotationEvent(**data))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Skipping malformed annotation event: {e}")
    return events


# ============================================================================
# State Materialization
# ============================================================================


def materialize_state(
    events: List[AnnotationEvent],
    target_id: str,
) -> AnnotationState:
    """Fold annotation events into materialized state for a single target.

    Args:
        events: All events (will be filtered to target_id)
        target_id: The entry_id or thread topic to materialize

    Returns:
        AnnotationState for the target
    """
    state = AnnotationState()

    for ev in events:
        if ev.target_id != target_id:
            continue

        state.last_touched = ev.timestamp

        if ev.kind == "reaction":
            actors = state.reactions.setdefault(ev.value, [])
            if ev.actor not in actors:
                actors.append(ev.actor)
                # Only adjust vote_score on first reaction (dedup)
                if ev.value == "thumbsup":
                    state.vote_score += 1
                elif ev.value == "thumbsdown":
                    state.vote_score -= 1

        elif ev.kind == "reaction_remove":
            removed = False
            if ev.value in state.reactions:
                actors = state.reactions[ev.value]
                if ev.actor in actors:
                    actors.remove(ev.actor)
                    removed = True
                if not actors:
                    del state.reactions[ev.value]
            # Only adjust vote_score if actor actually had the reaction
            if removed:
                if ev.value == "thumbsup":
                    state.vote_score -= 1
                elif ev.value == "thumbsdown":
                    state.vote_score += 1

        elif ev.kind == "tag":
            if ev.value not in state.tags:
                state.tags.append(ev.value)

        elif ev.kind == "tag_remove":
            if ev.value in state.tags:
                state.tags.remove(ev.value)

        elif ev.kind == "flag":
            state.flags.append(
                {
                    "agent": ev.actor,
                    "reason": ev.value,
                    "timestamp": ev.timestamp,
                }
            )

        elif ev.kind == "flag_clear":
            state.flags = [
                f
                for f in state.flags
                if not (f.get("agent") == ev.actor and f.get("reason") == ev.value)
            ]

        elif ev.kind == "xref":
            if ev.value not in state.xrefs:
                state.xrefs.append(ev.value)

        elif ev.kind == "xref_remove":
            if ev.value in state.xrefs:
                state.xrefs.remove(ev.value)

        elif ev.kind == "xref_supersedes":
            if ev.value not in state.xref_supersedes:
                state.xref_supersedes.append(ev.value)

        elif ev.kind == "xref_supersedes_remove":
            if ev.value in state.xref_supersedes:
                state.xref_supersedes.remove(ev.value)

        elif ev.kind == "pin":
            state.pinned = True

        elif ev.kind == "unpin":
            state.pinned = False

    return state


def materialize_all_states(
    events: List[AnnotationEvent],
) -> Dict[str, AnnotationState]:
    """Fold all events into per-target states.

    Args:
        events: All annotation events for a thread

    Returns:
        Dict mapping target_id → AnnotationState
    """
    # Collect unique target IDs
    target_ids: set[str] = set()
    for ev in events:
        target_ids.add(ev.target_id)

    return {tid: materialize_state(events, tid) for tid in target_ids}


def load_or_rebuild_state(
    thread_dir: Path,
    read_only: bool = False,
) -> Dict[str, AnnotationState]:
    """Load annotation states from cache, rebuilding if stale.

    The cache is invalidated whenever a new event is appended.

    Args:
        thread_dir: Per-thread graph directory
        read_only: If True, return rebuilt state in-memory without writing
            the cache file. Use this on read paths to avoid dirtying
            the worktree with annotation_state.json writes.

    Returns:
        Dict mapping target_id → AnnotationState
    """
    cache = _state_cache_file(thread_dir)
    ann_file = _annotations_file(thread_dir)

    # If no annotations exist, check cache (may have last_touched from writes)
    if not ann_file.exists():
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                return {
                    tid: AnnotationState.from_dict(sdata)
                    for tid, sdata in data.items()
                    if tid != "_ann_size"
                }
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    # Try loading from cache if it matches the current event log size.
    # We use file size instead of mtime because git operations (pull, checkout)
    # can set mtimes that make the cache appear fresh when it's stale.
    if cache.exists():
        try:
            ann_size = ann_file.stat().st_size
            cache_data_raw = cache.read_text(encoding="utf-8")
            data = json.loads(cache_data_raw)
            if isinstance(data, dict) and data.get("_ann_size") == ann_size:
                return {
                    tid: AnnotationState.from_dict(sdata)
                    for tid, sdata in data.items()
                    if tid != "_ann_size"
                }
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Cache invalid, rebuilding: {e}")

    # Rebuild from events
    events = load_annotation_events(thread_dir)
    states = materialize_all_states(events)

    # Write cache (skip on read-only to avoid dirtying the worktree)
    if not read_only:
        _write_state_cache(thread_dir, states)

    return states


def rebuild_state_cache(thread_dir: Path) -> None:
    """Force a full rebuild of the annotation state cache.

    Args:
        thread_dir: Per-thread graph directory
    """
    events = load_annotation_events(thread_dir)
    states = materialize_all_states(events)
    _write_state_cache(thread_dir, states)


def _write_state_cache(
    thread_dir: Path,
    states: Dict[str, AnnotationState],
) -> None:
    """Write the materialized state cache atomically.

    Args:
        thread_dir: Per-thread graph directory
        states: Dict of target_id → AnnotationState
    """
    cache_data = {tid: s.to_dict() for tid, s in states.items()}
    # Store the annotations.jsonl file size so we can detect staleness
    # after git pull (mtime is unreliable across git operations)
    ann_file = _annotations_file(thread_dir)
    if ann_file.exists():
        try:
            cache_data["_ann_size"] = ann_file.stat().st_size
        except OSError:
            pass
    storage.atomic_write_json(_state_cache_file(thread_dir), cache_data)


def get_annotation_state(
    thread_dir: Path,
    target_id: str,
    read_only: bool = False,
) -> AnnotationState:
    """Get the annotation state for a specific target.

    Args:
        thread_dir: Per-thread graph directory
        target_id: Entry ID or thread topic
        read_only: If True, skip cache writes (safe for read paths)

    Returns:
        AnnotationState (empty if no annotations exist)
    """
    states = load_or_rebuild_state(thread_dir, read_only=read_only)
    return states.get(target_id, AnnotationState())


def update_last_touched(
    thread_dir: Path,
    target_id: str,
    timestamp: Optional[str] = None,
) -> None:
    """Update last_touched for a target without creating an annotation event.

    .. deprecated::
        Superseded by ``last_activity`` in meta.json (written by the synced
        write transaction).  Retained for backward-compatibility with
        watercooler-site dashboard reads.

    Used by say/ack/handoff to track temporal activity. Works even when no
    annotations.jsonl exists yet — writes directly to the state cache.

    Args:
        thread_dir: Per-thread graph directory
        target_id: Entry ID or thread topic
        timestamp: ISO 8601 timestamp (defaults to now)
    """
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    # Load existing state from cache if available, or from events
    cache = _state_cache_file(thread_dir)
    states: Dict[str, AnnotationState] = {}

    ann_file = _annotations_file(thread_dir)

    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            # Validate cache against annotations.jsonl size to detect
            # staleness after git pull (same check as load_or_rebuild_state)
            cached_size = data.get("_ann_size")
            actual_size = ann_file.stat().st_size if ann_file.exists() else None
            # Cache is valid when sizes match, OR when both are None
            # (no annotations.jsonl exists and cache was written without one)
            if cached_size == actual_size:
                states = {
                    tid: AnnotationState.from_dict(sdata)
                    for tid, sdata in data.items()
                    if tid != "_ann_size"
                }
        except (json.JSONDecodeError, OSError):
            pass

    if not states:
        # Rebuild from events (cache was stale or missing)
        if ann_file.exists():
            events = load_annotation_events(thread_dir)
            states = materialize_all_states(events)

    state = states.get(target_id, AnnotationState())
    state.last_touched = ts
    states[target_id] = state

    thread_dir.mkdir(parents=True, exist_ok=True)
    _write_state_cache(thread_dir, states)
